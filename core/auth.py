"""
Autenticação simples e eficiente: usuários em users.json (FORA do git) com
senha hasheada (scrypt + salt) e tokens stateless assinados (HMAC-SHA256 com
AUTH_SECRET do .env) — restart da API não derruba sessões logadas.

Bootstrap: se users.json não tem usuários e o .env define AUTH_ADMIN_USER +
AUTH_ADMIN_PASS, o primeiro usuário é criado no boot (a senha fica só no
.env, nunca no código/git). Novos perfis: POST /api/auth/register.

Isolamento: sessões de chat e do estúdio carregam `owner`; cada conta só vê
o seu.
"""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

USERS_FILE = Path(__file__).resolve().parent.parent / "users.json"
PERMITIDOS_FILE = Path(__file__).resolve().parent.parent / "usuarios_permitidos.txt"
TOKEN_DIAS = 30
_USER_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,40}$")
_io_lock = threading.Lock()  # users.json + geração do secret (corrida em registros simultâneos)
_SCRYPT_N = 2 ** 17          # OWASP atual (regs antigos com 2**14 sobem ao logar)
_SCRYPT_MAXMEM = 256 * 1024 * 1024  # hashlib limita ~32 MB por padrão; n=2^17 pede ~128 MB
_secret_cache = ""


def _garantir_permitidos() -> list[str]:
    """Cria o arquivo de permitidos com os nomes iniciais SE não existir
    (escrita só aqui — ler nunca mais reescreve o arquivo)."""
    try:
        linhas = PERMITIDOS_FILE.read_text(encoding="utf-8").splitlines()
        nomes = [l.strip().lower() for l in linhas if l.strip() and not l.startswith("#")]
        if nomes:
            return nomes
    except Exception:
        pass
    with _io_lock:
        if not PERMITIDOS_FILE.exists():
            PERMITIDOS_FILE.write_text(
                "# Nomes permitidos a criar login (um por linha — adicione os seus)\n"
                "ueslei\nwilliam\n", encoding="utf-8")
    return ["ueslei", "william"]


def nomes_permitidos() -> list[str]:
    """Nomes que PODEM se registrar (um por linha em usuarios_permitidos.txt).
    Só quem conhece um nome da lista consegue criar login — o arquivo é a
    chave."""
    try:
        linhas = PERMITIDOS_FILE.read_text(encoding="utf-8").splitlines()
        nomes = [l.strip().lower() for l in linhas if l.strip() and not l.startswith("#")]
        if nomes:
            return nomes
    except Exception:
        pass
    return _garantir_permitidos()


def registrar(user: str, senha: str) -> dict:
    """Cria um perfil NOVO — só para nomes da lista de permitidos
    (usuarios_permitidos.txt); quem não conhece um nome não cadastra."""
    user = user.strip()
    if user.lower() not in nomes_permitidos():
        raise ValueError("este nome não está liberado para cadastro — "
                         "fale com o administrador")
    if existe(user):
        raise ValueError("este nome já tem conta — use 'entrar'")
    return criar_usuario(user, senha)


def _secret() -> str:
    """AUTH_SECRET do .env (auto-gerado no primeiro boot e persistido lá).
    Sob lock: dois boots/registros simultâneos não podem gerar secrets
    diferentes (um venceria e derrubaria os tokens do outro). Em CACHE
    depois da 1ª leitura — token válido não reabre o .env a cada request."""
    global _secret_cache
    from . import config
    if _secret_cache:
        return _secret_cache
    with _io_lock:
        s = str(getattr(config, "AUTH_SECRET", "") or "").strip()
        if not s:
            s = secrets.token_hex(32)
            config.set_env("AUTH_SECRET", s)
        _secret_cache = s
        return s


def _carregar() -> dict:
    try:
        dados = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        if isinstance(dados.get("usuarios"), dict):
            return dados
    except Exception:
        pass
    return {"usuarios": {}}


def _gravar(dados: dict) -> None:
    """Escrita SEGURA sob lock. ⚠️ SEM os.replace: o compose faz bind-mount
    de ARQUIVO ÚNICO do users.json e rename devolve 'Device or resource
    busy' (mesma armadilha do set_env_inplace) — o REGISTRO DE USUÁRIO
    estava quebrado em produção. Truque: regravar o MESMO inode em duas
    etapas (trunca+escreve) dentro do lock global; falha de energia no
    meio é igualmente fatal para tmp+replace quando o rename não existe."""
    USERS_FILE.write_text(json.dumps(dados, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def _hash(senha: str, salt_hex: str, n: int = _SCRYPT_N) -> str:
    return hashlib.scrypt(senha.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                          n=n, r=8, p=1, dklen=32,
                          maxmem=_SCRYPT_MAXMEM).hex()


def criar_usuario(user: str, senha: str) -> dict:
    """Cria/atualiza o usuário; devolve o registro sem o hash."""
    user = user.strip()
    if not _USER_RE.match(user):
        raise ValueError("usuário: 2-40 caracteres (letras, números, _ . -)")
    if len(senha or "") < 8:
        raise ValueError("senha: mínimo de 8 caracteres")
    with _io_lock:  # ler-modificar-gravar atômico entre threads
        dados = _carregar()
        salt = secrets.token_hex(16)
        dados["usuarios"][user] = {"salt": salt, "hash": _hash(senha, salt),
                                   "n": _SCRYPT_N, "criado": int(time.time())}
        _gravar(dados)
    return {"user": user}


def verificar(user: str, senha: str) -> bool:
    reg = _carregar()["usuarios"].get((user or "").strip())
    if not reg:
        return False
    n = int(reg.get("n", 2 ** 14))  # regs antigos: custo menor da época
    if not hmac.compare_digest(reg["hash"], _hash(senha or "", reg["salt"], n)):
        return False
    if n < _SCRYPT_N:  # login válido com custo antigo → rehash transparente
        try:
            with _io_lock:
                dados = _carregar()
                dados["usuarios"][user.strip()] = {
                    "salt": reg["salt"], "hash": _hash(senha, reg["salt"]),
                    "n": _SCRYPT_N, "criado": reg.get("criado", int(time.time()))}
                _gravar(dados)
        except Exception:
            pass  # upgrade falhou: o login segue válido com o hash antigo
    return True


def existe(user: str) -> bool:
    return (user or "").strip() in _carregar()["usuarios"]


# ---------- tokens stateless (user|exp assinados com HMAC) ----------

def emitir_token(user: str) -> str:
    exp = int(time.time()) + TOKEN_DIAS * 86400
    corpo = f"{user}|{exp}"
    assinatura = hmac.new(_secret().encode(), corpo.encode(),
                          hashlib.sha256).hexdigest()
    return f"{corpo}|{assinatura}"


def usuario_do_token(token: str) -> str | None:
    """Valida assinatura e validade; devolve o usuário ou None."""
    try:
        user, exp, assinatura = (token or "").split("|")
        if int(exp) < time.time():
            return None
        esperada = hmac.new(_secret().encode(),
                            f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(assinatura, esperada):
            return None
        return user if existe(user) else None
    except Exception:
        return None


def bootstrap_admin() -> None:
    """Cria o usuário inicial a partir do .env (uma vez; depois pode trocar
    a senha no próprio .env de novo para resetar)."""
    from . import config
    user = str(getattr(config, "AUTH_ADMIN_USER", "") or "").strip()
    senha = str(getattr(config, "AUTH_ADMIN_PASS", "") or "")
    if user and senha and not existe(user):
        criar_usuario(user, senha)
        print(f"🔐 usuário inicial '{user}' criado (AUTH_ADMIN_* do .env)")
