"""Agente da SANDBOX — execução de código para TESTES (container próprio).

Stdlib pura (sem pip): grava arquivos e executa comandos. TODA a
inteligência (qual comando por linguagem, scaffolds) vive no CLIENTE
(core/sandbox.py da API) — aqui é burro de propósito.

Rotas (rede interna do compose, sem porta publicada):
  GET  /saude    → {ok, linguagens: {python: "3.12 …", rustc: null, …}}
  POST /arquivos → {arquivos: {nome: conteudo}} escreve em /tmp/work
  POST /exec     → {cmd: ["python3", "x.py"], timeout: 240} → {code, saida, segundos}

Token: `Authorization: Bearer $SANDBOX_TOKEN` (quando definido no compose).
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORTA = 8020
WORK = Path("/tmp/work")
TOK = (os.getenv("SANDBOX_TOKEN") or "").strip()
LIMITE_SAIDA = 64 * 1024
TIMEOUT_MAX = 600

# PREVIEW só lê estes tipos (o agente serve ARQUIVOS DE TESTE — nunca
# binários arbitrários nem código do container)
_MIMES_PREVIEW = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css",
    ".js": "text/javascript", ".svg": "image/svg+xml",
    ".json": "application/json", ".txt": "text/plain",
    ".md": "text/plain", ".csv": "text/csv",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

_LINGUAGENS = {
    "python": ["python3", "--version"],
    "pip": ["pip", "--version"],
    "node": ["node", "--version"],
    "java": ["java", "--version"],
    "dotnet": ["dotnet", "--version"],
    "rustc": ["rustc", "--version"],
    "cargo": ["cargo", "--version"],
    "ruby": ["ruby", "--version"],
    "php": ["php", "--version"],
    "go": ["go", "version"],          # 'go --version' NÃO existe
    "dart": ["dart", "--version"],
}


def _linguagens() -> dict:
    vers = {}
    for nome, cmd in _LINGUAGENS.items():
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            linha = (r.stdout or r.stderr).strip().splitlines()
            vers[nome] = linha[0][:90] if linha else "?"
        except Exception:
            vers[nome] = None
    return vers


def _caminho(nome: str, subdir: str = "") -> Path | None:
    """Nome sanitizado (sem ../absoluto) dentro de /tmp/work (ou subpasta
    `subdir` — cada teste ganha a sua, isolando o lixo do anterior)."""
    p = Path(str(nome).replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.name:
        return None
    base = WORK / subdir if subdir else WORK
    return base / p


def _gravar(arquivos: dict, subdir: str = "") -> dict:
    escritos, erros = [], []
    for nome, conteudo in (arquivos or {}).items():
        alvo = _caminho(nome, subdir)
        if alvo is None:
            erros.append(f"nome inválido: {nome}")
            continue
        try:
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_text(str(conteudo), encoding="utf-8")
            escritos.append(str(alvo.relative_to(WORK)))
        except Exception as e:
            erros.append(f"{nome}: {e}")
    return {"ok": not erros, "escritos": escritos, "erros": erros}


def _rlimits(timeout_s: int) -> str:
    """Limite de recurso por execução como linha `ulimit` (ideia vinda do
    Piston/Isolate — SEM container privilegiado/cgroups). Aplicado: só
    CPU-time 4× o timeout. ⚠️ `-u` (processos) e `-f` (tamanho de arquivo)
    FORAM TESTADOS e REMOVIDOS: no user-namespace do Docker a contagem de
    threads do uid não bate com a realidade (dotnet morria nem em
    --version) e RLIMIT_FSIZE gera SIGBUS nos memory-maps do .NET. A
    proteção real contra árvores/fork-bomb é o KILL DO GRUPO no timeout
    (ver _exec), que cobre netos que o ulimit não alcançaria."""
    cpu = max(4 * int(timeout_s or 240), 240)
    return f"ulimit -t {cpu} 2>/dev/null; "


def _limitado(cmd: list, timeout_s: int) -> list:
    """Envolve o comando: bash aplica ulimit e `exec` assume o processo
    real (mesmo PID — o grupo criado por _exec cobre os filhos dele)."""
    return ["bash", "-c", _rlimits(timeout_s) + 'exec "$@"', "sandbox"] \
        + [str(c) for c in cmd]


def _exec(cmd: list, timeout: int, subdir: str = "") -> dict:
    """Executa com SESSÃO PRÓPRIA (start_new_session): no timeout o GRUPO
    inteiro morre (os.killpg) — filhos/netos (fork-bomb, servidores que
    abrem workers) não sobrevivem ao timeout, que era a proteção que o
    ulimit -u tentava dar e quebrava o dotnet."""
    import signal
    import subprocess as sp
    t0 = time.time()
    env = dict(os.environ)
    env.setdefault("HOME", "/tmp/home")
    env.setdefault("CARGO_HOME", "/tmp/home/.cargo")  # /opt/cargo é read-only
    cwd = str(WORK / subdir) if subdir else str(WORK)
    try:
        p = sp.Popen(_limitado(cmd, timeout), cwd=cwd, env=env,
                     stdout=sp.PIPE, stderr=sp.PIPE, text=True,
                     start_new_session=True)
        try:
            out, err = p.communicate(timeout=min(timeout or 240, TIMEOUT_MAX))
            saida = (out or "") + (("\n[stderr]\n" + err) if err else "")
            return {"code": p.returncode, "saida": saida[:LIMITE_SAIDA],
                    "segundos": round(time.time() - t0, 1)}
        except sp.TimeoutExpired:
            try:  # grupo INTEIRO: filhos e netos (SIGKILL não dá para ignorar)
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                p.kill()
            out, err = p.communicate(timeout=10)
            return {"code": -9, "saida": ((out or "") + (err or "")
                                          + f"\n⏱️ timeout de {timeout}s — "
                                            "processo e filhos encerrados")[:LIMITE_SAIDA],
                    "segundos": round(time.time() - t0, 1)}
    except Exception as e:
        return {"code": -1, "saida": f"erro ao executar: {e}",
                "segundos": round(time.time() - t0, 1)}


def _achar_preview(nome: str) -> Path | None:
    """Arquivo de preview por nome: direto em /tmp/work ou no subdir mais
    RECENTE que o tenha (cada teste roda em pasta própria agora)."""
    direto = _caminho(nome)
    if direto and direto.is_file():
        return direto
    cands = sorted(WORK.glob(f"**/{Path(nome).name}"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        if p.is_file() and WORK in p.parents:
            return p
    return None


class Handler(BaseHTTPRequestHandler):
    def _json(self, dados: dict, status: int = 200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _autorizado(self) -> bool:
        if not TOK:
            return True
        return self.headers.get("authorization") == f"Bearer {TOK}"

    def _corpo(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def _proxy_app(self, metodo: str):
        """Repassa a requisição ao app rodando em 127.0.0.1:{porta} (GET e
        POST — forms funcionam). Limites: corpo 4 MB, timeout 20 s por
        requisição (é um app de TESTE, não produção)."""
        import urllib.error
        import urllib.request
        m = re.match(r"^/app/(\d+)(/.*)?$", self.path.split("?")[0])
        if not m:
            return self._json({"detail": "caminho inválido"}, 400)
        porta = int(m.group(1))
        resto = m.group(2) or "/"
        url = f"http://127.0.0.1:{porta}{resto}"
        if "?" in self.path:
            url += "?" + self.path.split("?", 1)[1]
        data = None
        headers = {}
        if metodo == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(min(n, 4 * 1024 * 1024)) if n else b""
            if self.headers.get("Content-Type"):
                headers["Content-Type"] = self.headers["Content-Type"]
        req = urllib.request.Request(url, data=data, method=metodo,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                corpo = r.read(4 * 1024 * 1024)
                self.send_response(r.status)
                self.send_header("Content-Type",
                                 r.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
        except urllib.error.HTTPError as e:
            corpo = e.read(4 * 1024 * 1024)
            self.send_response(e.code)
            self.send_header("Content-Type",
                             e.headers.get("Content-Type", "text/plain"))
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        except Exception as e:
            self._json({"detail": f"app fora do ar na porta {porta} "
                                  f"({str(e)[:60]})"}, 502)

    def do_GET(self):
        if self.path == "/saude":
            return self._json({"ok": True, "linguagens": _linguagens()})
        # PROXY do APP VIVO (teste de site): /app/{porta}/{resto} →
        # 127.0.0.1:{porta}/{resto} — a API valida a chave HMAC pública e
        # repassa pra cá (rede interna; a porta é o destino interno)
        if self.path.startswith("/app/"):
            return self._proxy_app("GET")
        # PREVIEW de arquivos do teste (HTML e cia.) — usado pelo iframe do
        # modal de teste e pelo subdomínio sandbox.disroy.org (acha o
        # arquivo no SUBDIR do teste mais recente que o tenha)
        if self.path.startswith("/ver/"):
            alvo = _achar_preview(self.path[5:].split("?")[0])
            if alvo is None:
                return self._json({"detail": "arquivo não encontrado"}, 404)
            mime = _MIMES_PREVIEW.get(alvo.suffix.lower())
            if mime is None:
                return self._json({"detail": f"sem preview para '{alvo.suffix}'"}, 415)
            dados = alvo.read_bytes()[:2 * 1024 * 1024]  # 2 MB de teto
            self.send_response(200)
            self.send_header("Content-Type", mime + ("; charset=utf-8"
                              if mime.startswith("text/") or mime == "application/json" else ""))
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("Content-Security-Policy",
                             "default-src 'self' data:; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(dados)
            return
        self._json({"detail": "não encontrado"}, 404)

    def do_POST(self):
        # PROXY do app vivo: forms/POST do aplicativo (a chave pública já
        # foi validada pela API; aqui é rede interna)
        if self.path.startswith("/app/"):
            return self._proxy_app("POST")
        if not self._autorizado():
            return self._json({"detail": "token inválido"}, 401)
        dados = self._corpo()
        if self.path == "/arquivos":
            WORK.mkdir(parents=True, exist_ok=True)
            subdir = str(dados.get("dir") or "").strip()
            if subdir and (Path(subdir).is_absolute() or ".." in Path(subdir).parts):
                return self._json({"detail": "dir inválido"}, 400)
            return self._json(_gravar(dados.get("arquivos"), subdir))
        if self.path == "/limpar":
            # container CONTINUA de pé (pedido do dono): saem os ARQUIVOS
            # dos testes — TODOS, ou só as pastas EXCETO as indicadas (os
            # apps temporários vivos preservam a pasta deles)
            exceto = set(str(x) for x in (dados.get("exceto") or []))
            try:
                for item in WORK.iterdir():
                    if item.name in exceto:
                        continue  # app vivo: pasta preservada
                    (shutil.rmtree(item, ignore_errors=True)
                     if item.is_dir() else item.unlink(missing_ok=True))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "erro": str(e)}, 500)
        if self.path == "/exec":
            cmd = dados.get("cmd")
            # shell=False: cada item é ARGV puro — strings livres são seguras
            # (nenhum shell é invocado); a sandbox existe PARA executar código
            if (not isinstance(cmd, list) or not cmd or len(cmd) > 64
                    or not all(isinstance(c, str) and c for c in cmd)):
                return self._json({"detail": "cmd inválido (lista de argumentos)"},
                                  400)
            subdir = str(dados.get("dir") or "").strip()
            if subdir and (Path(subdir).is_absolute() or ".." in Path(subdir).parts):
                return self._json({"detail": "dir inválido"}, 400)
            return self._json(_exec(cmd, int(dados.get("timeout") or 240), subdir))
        self._json({"detail": "não encontrado"}, 404)

    def log_message(self, *_a):
        pass  # silencioso: o cliente loga o que importa


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    Path("/tmp/home").mkdir(parents=True, exist_ok=True)
    print(f"sandbox agent on :{PORTA} (token: {'sim' if TOK else 'não'})",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORTA), Handler).serve_forever()
