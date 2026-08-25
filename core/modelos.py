r"""
Registro e troca dos modelos de D:\models, organizados por categoria.

Categorias (derivadas do nome do arquivo — novos GGUFs aparecem sozinhos):
- chat   → servido pelo llama-server :8090 (troca = liberar VRAM e subir outro)
- embed  → servido pelo llama-server :8081 (BGE-M3; a base inteira é 1024 dims)
- imagem → futuro: Flux/SD GGUF (texto→imagem)
- video  → futuro: Hunyuan/Wan/LTX GGUF (texto→vídeo, imagem→vídeo)

A troca do modelo de conversa (ativar) repete o ritual do servicos_llm.py,
mas cirúrgica: derruba SOMENTE o processo que escuta a :8090 (o embedding
:8081 e o Qdrant continuam de pé), espera a VRAM liberar, sobe o novo com
as mesmas flags e atualiza o LLM_MODEL do .env.
"""
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import httpx
from dotenv import set_key

from . import config

PASTA = Path(config.ENV_FILE).parent  # raiz do projeto (para o log)
LOGS = PASTA / "logs"

CHAT_PORTA = 8090
EMBED_PORTA = 8081
VL_PORTA = 8082  # visão (Qwen2.5-VL + mmproj) — sobe só quando pedida
# contexto: 32k tokens divididos em 2 slots (16k cada) — o máximo que cabe
# na VRAM de 8 GB com KV q8_0 junto do embedding; antes era 24k/4 slots (6k)
CHAT_FLAGS = ["-ngl", "99", "-c", "32768", "-np", "2", "-fa", "on",
              "-ctk", "q8_0", "-ctv", "q8_0", "--metrics",
              "--host", "127.0.0.1", "--port", str(CHAT_PORTA)]
EMBED_GGUF = r"D:\models\bge-m3-q8_0.gguf"
EMBED_FLAGS = ["--embeddings", "--pooling", "cls", "-ngl", "99", "-c", "8192",
               "-ub", "8192", "-b", "8192", "--alias", "bge-m3",
               "--host", "127.0.0.1", "--port", str(EMBED_PORTA)]
VL_GGUF = r"D:\models\visao\Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"

# Regras de compatibilidade com os 8 GB de VRAM (RTX 4070 de laptop):
# chat + embedding juntos precisam caber — 4,7 GB (7B Q4) + 0,7 GB (BGE-M3)
# ≈ 5,9 GB, o que bate com o uso observado. Acima de ~6 GB de arquivo não cabe.
GB_MAX_CHAT = 6.0
VRAM_BASE_MI = 2600  # só o embedding na VRAM — ponto de espera pós-kill

# Registro explícito: alias servido (bate com o menu do servicos_llm.py)
# → (arquivo, categoria). Arquivos desconhecidos entram com alias derivado.
REGISTRO = {
    "qwen2.5-coder-7b":   (r"D:\models\qwen2.5-coder\Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", "chat"),
    "qwen3-8b":           (r"D:\models\qwen3-8b\Qwen3-8B-Q4_K_M.gguf", "chat"),
    "qwen2.5-7b-instruct": (r"D:\models\qwen2.5-7b\Qwen2.5-7B-Instruct-Q4_K_M.gguf", "chat"),
    "llama3.1-8b-instruct": (r"D:\models\llama3.1\Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "chat"),
    "phi4-mini-instruct": (r"D:\models\phi4-mini\Phi-4-mini-instruct-Q5_K_M.gguf", "chat"),
    "mistral-nemo":       (r"D:\models\mistral-nemo\Mistral-Nemo-Instruct-2407-Q4_K_M.gguf", "chat"),
    "qwen2.5-vl-7b":      (r"D:\models\visao\Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf", "visao"),
    "bge-m3":             (r"D:\models\bge-m3-q8_0.gguf", "embed"),
    "wan2.1-t2v-1.3b":    (r"D:\models\video\Wan2.1-T2V-1.3B-Q8_0.gguf", "video"),
    "wan2.2-ti2v-5b":     (r"D:\models\video\Wan2.2-TI2V-5B-Q8_0.gguf", "video"),
}

# Padrões de nome para categorizar arquivos que chegarem sem registro
PADROES = [
    ("embed",  r"bge|embed|e5-|gte-|minilm|nomic-embed"),
    ("encoder", r"umt5|t5xxl|t5-encoder|clip|mmproj|text-encoder"),
    ("visao",  r"-vl\b|vision|moondream|llava|minicpm-v"),
    ("imagem", r"flux|stable-diffusion|sdxl|^sd|qwen-image|illustrious|pony|dev1"),
    ("video",  r"hunyuan|wan2|ltx|cogvideo|mochi|animatediff|svd|video"),
]
# ⚠️ encoders de TEXTO de difusão (umt5/t5xxl do Wan, clip do Flux, mmproj
# da visão) NÃO são modelos de conversa: sem esta categoria eles caíam no
# default "chat" e apareciam no menu do servicos_llm (bug real: umt5 de
# 6 GB listado como opção de conversa).

# Tag de uso para o seletor da webui (agrupa por finalidade)
USO = {"qwen2.5-coder-7b": "programacao"}

_troca = threading.Lock()  # uma troca por vez

# CACHE do servido(): a consulta cruza o TÚNEL até a estação em produção
# (llm.disroy.org) — 2 chamadas por carregamento de página deixavam o chat
# LENTO. 10 s de TTL: barato o suficiente para refletir trocas, aliviando
# o caminho crítico de render.
# ⚠️ LOCK PRÓPRIO — NUNCA o _troca: `ativar()` SEGURA o _troca durante a
# troca inteira e chama listar() → servido(); se servido() fizesse
# `with _troca` (Lock NÃO reentrante) era DEADLOCK eterno — o agente
# inteiro congelava (todo /saude e /ativar pendurados no lock morto;
# era o "agente zumbi" das trocas de modelo que viravam "(sem resposta)").
_servido_cache: dict = {"t": 0.0}
_SERVIDO_TTL = 10.0
_servido_lock = threading.Lock()


def _servido_invalidate() -> None:
    with _servido_lock:
        _servido_cache["t"] = 0.0


def _categorizar(nome: str) -> str:
    n = nome.lower()
    for categoria, padrao in PADROES:
        if re.search(padrao, n):
            return categoria
    return "chat"


def _alias(stem: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", stem.lower()).strip("-")


def listar(pasta: str | None = None) -> list[dict]:
    """GGUFs de D:\\models com categoria, provider e compatibilidade com a VRAM."""
    raiz = Path(pasta or os.getenv("MODELS_DIR", r"D:\models"))
    servidos = {CHAT_PORTA: servido(CHAT_PORTA), EMBED_PORTA: servido(EMBED_PORTA)}
    modelos, vistos = [], set()
    for p in sorted(raiz.rglob("*.gguf")) if raiz.is_dir() else []:
        gb = round(p.stat().st_size / 1e9, 1)
        # registro explícito tem prioridade; senão deriva do nome do arquivo
        alias = next((a for a, (f, _) in REGISTRO.items()
                      if Path(f).name.lower() == p.name.lower()), None)
        categoria = REGISTRO.get(alias, ("", _categorizar(p.stem)))[1]
        alias = alias or _alias(p.stem)
        if alias in vistos:
            continue
        vistos.add(alias)
        provider = ("llama-server" if categoria in ("chat", "embed", "visao")
                    else "stable-diffusion.cpp" if categoria == "imagem" else "pendente")
        compativel, motivo = True, ""
        if categoria == "chat" and gb > GB_MAX_CHAT:
            compativel = False
            motivo = (f"{gb} GB não cabem na VRAM de 8 GB junto com o embedding "
                      f"(limite ≈ {GB_MAX_CHAT} GB)")
        elif categoria == "visao":
            motivo = "multimodal (imagem→texto) sobe na :8082 quando pedida"
        elif categoria == "imagem":
            motivo = "geração pela aba 🎨 Mídia (pausa chat+embed durante)"
        elif categoria == "video":
            motivo = "vídeo/gif pelo chat (pausa as LLMs durante a geração)"
        modelos.append({
            "nome": alias, "arquivo": p.name, "caminho": str(p), "gb": gb,
            "categoria": categoria, "provider": provider,
            "uso": ("embed" if categoria == "embed"
                    else "visao" if categoria == "visao"
                    else "midia" if categoria in ("imagem", "video")
                    else USO.get(alias, "conversa")),
            "compativel": compativel, "motivo": motivo,
            "em_uso": alias == servidos[CHAT_PORTA] or alias == servidos[EMBED_PORTA],
        })
    if not modelos:
        # pasta de GGUFs ausente (ex.: container da VPS sem D:\models)
        # -> o REGISTRO conhecido alimenta o seletor (nome/categoria);
        # a troca real continua valida: o alias resolve no agente/host.
        for alias, (arq, cat) in REGISTRO.items():
            modelos.append({
                "nome": alias, "arquivo": arq, "gb": None,
                "categoria": cat, "provider": "llama-server",
                "uso": USO.get(alias, "conversa" if cat == "chat" else cat),
                "compativel": True, "motivo": "",
                "em_uso": alias == servidos[CHAT_PORTA] or alias == servidos[EMBED_PORTA],
            })
    return modelos


# ---------- embedding ON-DEMAND (ciclo de vida automático) ---------------

_embed_ok_ate = 0.0  # cache do health check (evita 1 HTTP por busca)


def _host_port_embed() -> tuple[str, int]:
    """host:porta do embedding a partir do EMBED_BASE_URL (no host é
    127.0.0.1:8081; em container é host.docker.internal:8081 — nunca
    hardcode, senão o container testa a si mesmo)."""
    from urllib.parse import urlparse
    u = urlparse(config.EMBED_BASE_URL)
    return (u.hostname or "127.0.0.1", u.port or 8081)


def derrubar_todos_motores(log=print) -> dict:
    """⏹ Parar tudo: desmonta TODOS os motores de GPU liberando a VRAM —
    inclusive ZOMBIES (processos llama-server órfãos de sessões antigas
    que não escutam porta nenhuma mas seguram VRAM): além das portas, mata
    TODOS os llama-server pelo NOME do processo."""
    derrubados = []
    for porta, nome in ((CHAT_PORTA, "chat (:8090)"),
                        (VL_PORTA, "visão (:8082)")):
        try:
            if servido(porta) or _pids_na_porta(porta):
                derrubar_porta(porta, f"parar-tudo {nome}")
                derrubados.append(nome)
        except Exception as e:
            log(f"   ⚠️ {nome}: {e}")
    try:
        if not embed_manual_off() and _pids_na_porta(EMBED_PORTA):
            derrubar_porta(EMBED_PORTA, "parar-tudo embedding (:8081)")
            derrubados.append("embedding (:8081)")
    except Exception as e:
        log(f"   ⚠️ embedding: {e}")
    # varredura por NOME: zombies sem porta também morrem (VRAM de volta)
    try:
        import subprocess
        r = subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                           capture_output=True, text=True, timeout=30)
        alvo = (r.stdout or "") + (r.stderr or "")
        n = alvo.count("finalizado") + alvo.count("terminated")
        if n:
            log(f"   🧟 {n} processo(s) llama-server morto(s) pelo nome (inclui zombies)")
            if "chat (:8090)" not in derrubados:
                derrubados.append("chat (:8090)")
    except Exception as e:
        log(f"   ⚠️ varredura por nome: {e}")
    log(f"⏹ motores desmontados: {', '.join(derrubados) or 'nenhum estava no ar'}")
    return {"derrubados": derrubados}


def embedding_no_ar() -> bool:
    """servidor de embedding respondendo? (cache 30 s).

    Em PRODUÇÃO (VPS) o embedding vem pelo TÚNEL zero-trust
    (EMBED_BASE_URL https://embed.disroy.org) — a checagem usa a URL REAL
    (esquema/porta certos), senão derivava host:porta de um :8081 local,
    falhava e tentava o AGENTE DO HOST, que só existe na estação com GPU."""
    global _embed_ok_ate
    import time as _t
    if _t.time() < _embed_ok_ate:
        return True
    cabecalhos = {}
    if getattr(config, "LLM_API_KEY", ""):
        cabecalhos["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    base = str(getattr(config, "EMBED_BASE_URL", "") or "").rstrip("/")
    try:
        if base.startswith("http"):
            # qualquer resposta HTTP = servidor vivo (401/404 também contam:
            # provam que o endpoint atendeu; a chave protege os dados, não /health)
            httpx.get(f"{base}/health", headers=cabecalhos, timeout=3)
        else:
            host, porta = _host_port_embed()
            httpx.get(f"http://{host}:{porta}/health", timeout=1.5)
        _embed_ok_ate = _t.time() + 30
        return True
    except Exception:
        _embed_ok_ate = 0.0
        return False


# ---------- desligamento MANUAL do embedding (persistido) ------------------
# O operador decide desligar pelo badge 🧬: NADA pode religar sozinho (nem a
# busca/ingestão, nem o boot do agente) até ele religar. Estado em marker
# dentro de saidas/ — sobrevive a restart de agente/API.
_EMBED_OFF_MARKER = PASTA / "saidas" / "embed_off.marker"
_LLM_OFF_MARKER = PASTA / "saidas" / "llm_off.marker"
_VL_OFF_MARKER = PASTA / "saidas" / "vl_off.marker"


def embed_manual_off() -> bool:
    return _EMBED_OFF_MARKER.exists()


def llm_manual_off() -> bool:
    """llama-server do chat DESLIGADO à mão (persistido) — nem o boot do
    agente nem o restore do estúdio o religam; só o ▶ no badge 🧠."""
    return _LLM_OFF_MARKER.exists()


def vl_manual_off() -> bool:
    """Visão (:8082) DESLIGADA à mão (persistido) — legendagem/análise de
    imagem falham com erro claro até religar no badge 👁."""
    return _VL_OFF_MARKER.exists()


def desligar_vl_manual(log=print, ja_derrubado: bool = False) -> dict:
    """Bloqueia a visão (marker) e derruba o servidor se estiver no ar."""
    _VL_OFF_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _VL_OFF_MARKER.write_text("off", encoding="utf-8")
    if not ja_derrubado:
        derrubar_porta(VL_PORTA, "llama-server (visão)")
    log("👁 visão DESLIGADA manualmente — análise de imagem falha com erro "
        "claro até ▶ ligar no badge 👁")
    return {"ok": True, "manual_off": True}


def ligar_vl_manual() -> dict:
    """Religa a visão (remove o marker) e a PRÉ-AQUECE (sobe agora, ~1 min)
    — a próxima análise já encontra o servidor de pé."""
    _VL_OFF_MARKER.unlink(missing_ok=True)
    ok = _subir_vl()
    return {"ok": ok, "manual_off": False}


def desligar_llm_manual(log=print, ja_derrubado: bool = False) -> dict:
    """Desliga o chat (:8090) e TRAVA a religação automática (marker).
    `ja_derrubado=True`: o container já pediu ao agente para derrubar —
    aqui só grava o marker (mesma pasta no volume compartilhado)."""
    _LLM_OFF_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _LLM_OFF_MARKER.write_text("off", encoding="utf-8")
    if not ja_derrubado:
        derrubar_porta(CHAT_PORTA, "llama-server (chat)")
    log("🧠 llama-server do chat DESLIGADO manualmente — fica off (nem o "
        "boot do agente sobe) até ▶ ligar no badge 🧠")
    return {"ok": True, "manual_off": True}


def ligar_llm_manual() -> dict:
    """Religa o chat e libera o ciclo automático (remove o marker)."""
    _LLM_OFF_MARKER.unlink(missing_ok=True)
    return ativar(config.LLM_MODEL)


def desligar_embedding_manual(log=print) -> dict:
    """Desliga o embedding E trava a religação automática (marker)."""
    _EMBED_OFF_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _EMBED_OFF_MARKER.write_text("off", encoding="utf-8")
    global _embed_ok_ate
    _embed_ok_ate = 0.0
    liberar_embedding(log)
    log("🧬 embedding DESLIGADO manualmente — buscas/ingestão ficam "
        "indisponíveis até religar (badge 🧬)")
    return {"ok": True, "manual_off": True}


def ligar_embedding_manual(log=print) -> dict:
    """Religa o embedding e libera a religação automática."""
    _EMBED_OFF_MARKER.unlink(missing_ok=True)
    ok = garantir_embedding(log=log)
    return {"ok": ok, "manual_off": False}


def garantir_embedding(log=print) -> bool:
    """Garante o servidor de embedding NO AR — sozinho, com PRIORIDADE:
    qualquer busca/ingestão que precise vetorializar o chama; se estiver
    fora, ele sobe na hora (é pequeno: ~0,6 GB — cabe junto do chat).
    DESLIGADO MANUALMENTE (marker) → ERRO claro: a decisão do operador
    vence o ciclo on-demand. Em CONTAINER: proxy ao agente do host."""
    if embed_manual_off():
        raise RuntimeError("embedding desligado manualmente — religue no "
                           "badge 🧬 da barra do topo (buscas e ingestão "
                           "precisam dele)")
    if embedding_no_ar():
        return True
    from . import config as _cfg
    if _cfg.EM_CONTAINER:
        try:
            return bool(_chamar_agente("/embed/garantir", timeout=180).get("ok"))
        except RuntimeError as e:
            log(f"⚠️ {e}")
            raise
    log("🧬 embedding :8081 fora do ar — subindo com prioridade…")
    ok = _subir_embed()
    if ok:
        log("🧬 embedding no ar (bge-m3) — vetorialização liberada")
    else:
        log("⚠️ embedding não subiu — rode servicos_llm.py")
    return ok


def liberar_embedding(log=print) -> None:
    """Libera a VRAM do embedding (difusão pesada / fim de trabalho vetorial).
    A próxima chamada de busca/ingestão o religa sozinho (garantir_embedding)."""
    global _embed_ok_ate
    _embed_ok_ate = 0.0
    pids = derrubar_porta(EMBED_PORTA, "llama-server (embedding)")
    if pids:
        log("🧬 embedding liberado (VRAM livre; religa sozinho quando precisar)")


def _host_de(porta: int) -> str:
    """Host real do serviço da porta, derivado das URLs do .env — em modo
    container 127.0.0.1 testaria o próprio container, não o host (o mesmo
    princípio do EMBED_BASE_URL em embedding_no_ar)."""
    urls = ([config.EMBED_BASE_URL] if porta == EMBED_PORTA else []) + [config.LLM_BASE_URL]
    for url in urls:
        m = re.match(r"https?://([^/:]+)", url or "")
        if m:
            return m.group(1)
    return "127.0.0.1"


def _auth_headers() -> dict:
    """Authorization da LLM quando LLM_API_KEY existe no .env (llama-server
    com --api-key exige; sem a flag no server, o header é ignorado)."""
    chave = os.getenv("LLM_API_KEY", "").strip()
    return {"Authorization": f"Bearer {chave}"} if chave else {}


def servido(porta: int = CHAT_PORTA) -> str | None:
    """Alias do modelo que a porta está servindo agora (None se fora do ar).

    Em produção a LLM de CONVERSA vem pelo TÚNEL (LLM_BASE_URL https://…) —
    para a porta do chat, consulta a URL real em vez de derivar host:porta
    (a derivação falha no container da VPS e o guard de modelo 409 mentiria).

    ⚠️ O BASE_URL do .env TERMINA em '/v1' (padrão OpenAI). Concatenar
    '/v1/models' direto gerava '…/v1/v1/models' → 404 → None → o chamador
    achava que NENHUM modelo estava no ar e RECARREGAVA o mesmo modelo a
    cada mensagem. O sufixo '/v1' é removido antes de montar a consulta.
    Cache de 10 s: a consulta cruza a internet em produção (2× por página
    carregada = chat lento)."""
    chave = "chat" if porta == CHAT_PORTA else "embed" if porta == EMBED_PORTA else str(porta)
    agora = time.time()
    with _servido_lock:
        if chave in _servido_cache and agora - _servido_cache.get("t", 0) < _SERVIDO_TTL:
            return _servido_cache[chave]
    base = ""
    if porta == CHAT_PORTA and str(getattr(config, "LLM_BASE_URL", "")).startswith("http"):
        base = str(config.LLM_BASE_URL).rstrip("/")
        if base.lower().endswith("/v1"):
            base = base[:-3].rstrip("/")
    try:
        if base:
            r = httpx.get(f"{base}/v1/models", headers=_auth_headers(), timeout=3)
        else:
            r = httpx.get(f"http://{_host_de(porta)}:{porta}/v1/models",
                          headers=_auth_headers(), timeout=1.5)
        dados = r.json().get("models") or r.json().get("data") or []
        alias = (dados[0].get("model") or dados[0].get("id")) if dados else None
    except Exception:
        alias = None
    with _servido_lock:
        _servido_cache.update({"t": agora, chave: alias})
    return alias


def normalizar(alias: str | None) -> str:
    """Nome CANÔNICO para comparações ('é o mesmo modelo?'): minúsculo e
    resolvido stem↔alias pelo REGISTRO — o servidor pode reportar o nome
    do ARQUIVO enquanto a UI manda o ALIAS (e vice-versa); sem normalizar,
    'igual' virava 'diferente' e o modelo recarregava à toa."""
    if not alias:
        return ""
    a = alias.strip().lower()
    for reg, (arq, _) in REGISTRO.items():
        if a in (reg.lower(), Path(arq).stem.lower(), arq.lower()):
            return reg.lower()
    return a


def _pids_na_porta(porta: int) -> list[int]:
    """PIDs dos processos escutando a porta (netstat; IPv4 e IPv6)."""
    try:
        saida = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                               capture_output=True, timeout=10,
                               # netstat fala cp1252/cp850 no Windows PT-BR:
                               # sem o replace, byte acentuado derruba o reader
                               encoding="utf-8", errors="replace").stdout
    except Exception:
        return []
    pids = set()
    for linha in saida.splitlines():
        if "LISTENING" in linha.upper() and f":{porta} " in linha:
            campos = linha.split()
            if campos and campos[-1].isdigit():
                pids.add(int(campos[-1]))
    return sorted(pids)


def _vram_uso_mi() -> int | None:
    try:
        s = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, timeout=10,
                           encoding="utf-8", errors="replace").stdout
        return int(s.strip().splitlines()[0])
    except Exception:
        return None


def _esperar_saude(porta: int, timeout: int = 240) -> bool:
    """Aguarda o /health da porta responder 200 (carga de 7B Q4 ≈ 40 s)."""
    for _ in range(timeout):
        try:
            if httpx.get(f"http://127.0.0.1:{porta}/health",
                         headers=_auth_headers(), timeout=1.5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _subir_chat(alias: str, caminho: str, esperar: bool = True) -> bool:
    """Sobe o llama-server de conversa na :8090 (flags do servicos_llm.py)."""
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"llama-chat-{alias}.log", "ab")
    subprocess.Popen(
        [config.LLAMA_BIN, "-m", caminho, *CHAT_FLAGS, "--alias", alias],
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    ok = _esperar_saude(CHAT_PORTA) if esperar else True
    if ok:
        set_key(str(config.ENV_FILE), "LLM_MODEL", alias)
        config.reload()
    return ok


def _subir_embed(esperar: bool = True) -> bool:
    """Sobe o llama-server de embedding (BGE-M3) na :8081."""
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / "llama-embed.log", "ab")
    subprocess.Popen(
        [config.LLAMA_BIN, "-m", EMBED_GGUF, *EMBED_FLAGS],
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return _esperar_saude(EMBED_PORTA) if esperar else True


def _mmproj() -> str | None:
    """Arquivo mmproj do Qwen2.5-VL em D:\\models\\visao (procura pelo nome)."""
    pasta = Path(VL_GGUF).parent
    if not pasta.is_dir():
        return None
    cand = sorted(pasta.glob("*mmproj*.gguf"))
    return str(cand[0]) if cand else None


def _subir_vl(esperar: bool = True) -> bool:
    """Sobe a visão (Qwen2.5-VL + mmproj) na :8082 para legendar/ler mídia.

    DESLIGADA MANUALMENTE (marker) → erro claro: a decisão do operador vence
    o ciclo on-demand. Se a porta estiver servindo um modelo que NÃO é de
    visão (llama-server sem mmproj — sobrou de outra sessão), ele é
    DERRUBADO e o VL certo sobe no lugar: sem isto, a requisição com imagem
    ia para um modelo texto puro e o servidor respondia 'this model does not
    support image input'."""
    if vl_manual_off():
        raise RuntimeError("multimodal desligado manualmente — religue em "
                           "Sistema → '🖼️ subir multimodal' (análises de "
                           "imagem precisam dele)")
    mmproj = _mmproj()
    if not (Path(VL_GGUF).exists() and mmproj):
        raise RuntimeError("modelo de visão ausente: baixe Qwen2.5-VL-7B + mmproj "
                           "(tests_manual/baixar_multimodal.py)")
    alias = servido(VL_PORTA)
    if alias:
        if "vl" in alias.lower():
            return True  # visão de verdade já no ar
        # porta ocupada por modelo SEM visão: fora daqui
        derrubar_porta(VL_PORTA, f"llama-server sem visão ({alias})")
        time.sleep(2)
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / "llama-vl.log", "ab")
    subprocess.Popen(
        [config.LLAMA_BIN, "-m", VL_GGUF, "--mmproj", mmproj, "-ngl", "99", "-c", "8192",
         "--alias", "qwen2.5-vl-7b", "--host", "127.0.0.1",
         "--port", str(VL_PORTA)],
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return _esperar_saude(VL_PORTA, timeout=180) if esperar else True


def derrubar_porta(porta: int, nome: str = "") -> list[int]:
    """Mata os processos que escutam a porta e devolve os PIDs (usado pelo
    swap de chat e pelo modo mídia, que precisa da VRAM inteira)."""
    pids = _pids_na_porta(porta)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15)
        print(f"   🛑 {nome or 'servico'} {pid} finalizado (porta {porta})")
    for _ in range(30):  # espera a porta liberar de verdade
        if not _pids_na_porta(porta):
            break
        time.sleep(0.5)
    return pids


def _agente_host():
    """URL do agente do host (modo container) — None no modo host direto.
    Padrão de gateway no registro único (core/gateways.py)."""
    if not config.EM_CONTAINER:
        return None
    from . import gateways
    return gateways.base("agente")


def _agente_headers() -> dict:
    """Authorization do agente quando AGENTE_TOKEN existe (obrigatório
    quando o agente é exposto pelo túnel público). Padrão: gateways.headers."""
    from . import gateways
    return gateways.headers("agente")


def _chamar_agente(caminho: str, corpo: dict | None = None, timeout: float = 240) -> dict:
    """POST no agente do host (operações de GPU que o container não faz).
    Erro de conexão vira RuntimeError com instrução clara — e o CORPO da
    resposta de erro NUNCA vira mensagem (cloudflared devolve HTML de 404/
    502, ou corpo VAZIO: o HTML aparecia como resposta do chat e o vazio
    virava '(sem resposta)' no lugar do erro)."""
    base = _agente_host()
    try:
        r = httpx.post(f"{base}{caminho}", json=corpo or {}, timeout=timeout,
                       headers=_agente_headers())
        if r.status_code >= 400:
            try:
                detalhe = r.json().get("detail", r.text)
            except Exception:
                detalhe = r.text
            detalhe = str(detalhe).strip()
            # corpo HTML/empty/longo = página de erro da BORDA, não do
            # agente — mensagem limpa e acionável
            if (not detalhe or detalhe.lstrip().startswith("<")
                    or len(detalhe) > 200):
                detalhe = (f"estação com a GPU não respondeu (HTTP "
                           f"{r.status_code} na borda do túnel) — agente "
                           "offline ou reiniciando")
            raise RuntimeError(detalhe[:200])
        return r.json()
    except httpx.HTTPError as e:
        tipo = type(e).__name__
        raise RuntimeError(f"agente do host fora do ar ({base}, {tipo}) — "
                           "suba com 'python -X utf8 -m api.agente_host' "
                           "na máquina com a GPU (ele ergue o chat e o "
                           "embedding no boot)")


def ativar(alias: str, log=None) -> dict:
    """Troca o modelo de conversa da :8090 (o embedding não é tocado).

    Mata apenas o processo da porta, espera a VRAM baixar, sobe o novo
    GGUF (subprocess do llama-server NO HOST). Em container, a chamada é
    PROXYADA ao agente do host (:8010) — quem tem os processos/GPU — em
    DOIS passos (a carga passa dos ~100 s de timeout da borda do
    Cloudflare): POST /ativar (responde na hora, troca em background lá)
    + POLLING /saude até o modelo pedido servir.
    llama-server com as flags do servicos_llm.py, atualiza o .env e
    espera o /health responder.
    """
    from . import config as _cfg
    _log = log or (lambda m, *a: None)
    if _cfg.EM_CONTAINER:
        _chamar_agente("/ativar", {"modelo": alias}, timeout=30)
        _log(f"⏳ {alias} carregando na estação (GPU)…", "modelo")
        t0 = time.time()
        while time.time() - t0 < 300:
            time.sleep(5)
            try:
                s = _chamar_agente("/saude", timeout=20)
            except Exception:
                continue  # agente ocupado/bootando: tenta de novo
            chat = s.get("chat")
            if normalizar(chat) == normalizar(alias):
                return {"trocou": True, "modelo": alias,
                        "provider": "llama-server",
                        "segundos": round(time.time() - t0),
                        "vram_mi": s.get("vram_mi")}
            _log(f"⏳ aguardando {alias} subir ({int(time.time() - t0)}s"
                 + (f" — servindo: {chat}" if chat else "") + ")…", "modelo")
        raise RuntimeError(f"{alias} não subiu na estação em 300 s — "
                           "confira a VRAM/GGUF na máquina com a GPU")
    if not _troca.acquire(blocking=False):
        raise RuntimeError("uma troca de modelo já está em andamento — aguarde")
    try:
        m = next((x for x in listar() if x["nome"] == alias), None)
        if not m:
            raise ValueError(f"modelo desconhecido: {alias}")
        if m["categoria"] != "chat":
            raise ValueError(f"'{alias}' é {m['categoria']} — só modelo de chat "
                             "pode ser servido na :8090")
        if not m["compativel"]:
            raise ValueError(f"'{alias}' não cabe na VRAM: {m['motivo']}")
        if servido(CHAT_PORTA) == alias:
            return {"trocou": False, "modelo": alias, "provider": "llama-server",
                    "vram_mi": _vram_uso_mi(), "detalhe": "já está no ar"}

        t0 = time.time()
        print(f"🔁 Trocando modelo de conversa → {alias} ({m['gb']} GB)")
        # 1. derruba o chat da :8090 E a visão da :8082 se estiver no ar
        #    (embedding :8081 fica de pé)
        derrubar_porta(CHAT_PORTA, "llama-server (chat)")
        derrubar_porta(VL_PORTA, "llama-server (visão)")
        # 2. espera fixa por parâmetro (ESTUDIO_VRAM_ASSENTAMENTO_S): a
        #    liberação de VRAM é do servidor/OS — o app não mede nem gerencia
        assentar = config.ESTUDIO_VRAM_ASSENTAMENTO_S
        print(f"   🧹 aguardando {assentar}s o servidor liberar a memória "
              "(parâmetro ESTUDIO_VRAM_ASSENTAMENTO_S)")
        time.sleep(assentar)
        # 3. sobe o novo e espera o /health (já atualiza o .env)
        if not _subir_chat(alias, m["caminho"]):
            raise RuntimeError(f"{alias} não subiu em 240 s — veja "
                               f"logs/llama-chat-{alias}.log")
        segundos = round(time.time() - t0)
        vram = _vram_uso_mi()
        _servido_invalidate()
        print(f"   ✅ {alias} no ar na :{CHAT_PORTA} em {segundos} s — "
              f"VRAM {vram} MiB")
        return {"trocou": True, "modelo": alias, "provider": "llama-server",
                "vram_mi": vram, "segundos": segundos}
    finally:
        _troca.release()
