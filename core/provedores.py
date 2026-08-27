# -*- coding: utf-8 -*-
"""Provedores de LLM EXTERNOS (glm · deepseek · openai · anthropic ·
qualquer endpoint OpenAI-compatible) ao lado do local (llama-server).

Configuração 100% no .env (nada hardcoded — a UI só LÊ):

    LLM_PROVIDERS=glm,deepseek,openai,anthropic     # ids, vírgula
    PROV_GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
    PROV_GLM_API_KEY=sk-...
    PROV_GLM_NOME=GLM                               # rótulo da UI
    PROV_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
    PROV_DEEPSEEK_API_KEY=sk-...
    PROV_OPENAI_BASE_URL=https://api.openai.com/v1
    PROV_OPENAI_API_KEY=sk-...
    PROV_ANTHROPIC_BASE_URL=https://api.anthropic.com/v1
    PROV_ANTHROPIC_API_KEY=sk-ant-...

MODELOS: tentamos `GET {base}/models` (lista REAL do provedor — nada de
nome predefinido); falhou (chave ausente/offline/endpoint sem lista) →
`PROV_{ID}_MODELOS` (lista manual) → heurística mínima por provedor.
MULTIMODAL: heurística por NOME (gpt-4o/4.1/5, o3/o4, claude-3+/4+,
glm-4v/4.5v+, qwen*-vl, gemini, llava, pixtral, grok-4…) — o chat usa para
habilitar i2t (imagem→texto) com modelo externo.
"""
from __future__ import annotations

import re
import threading
import time

import httpx

# heurística de visão por nome (minúsculo) — lista REAL de cada provedor
# muda rápido demais para hardcode total; o padrão cobre o essencial
_RE_VISAO = re.compile(
    r"gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|gpt-5\.|o3|o4-|chatgpt|"
    r"claude-3|claude-4|claude-opus|claude-sonnet|claude-haiku|"
    r"glm-4v|glm-4\.5v|glm-4\.6v|glm-5v|glm-v\d|"
    r"qwen[\w.-]*vl|qvq|llava|pixtral|internvl|moondream|"
    r"gemini|grok-4|doubao[\w.-]*vision|vision|vl-|vl\b|-vl", re.I)
# falsos positivos conhecidos (texto puro com nome parecido)
_RE_SO_TEXTO = re.compile(r"embed|rerank|whisper|tts|audio|deepseek-chat|"
                          r"deepseek-reasoner|deepseek-coder|glm-4-flash|"
                          r"o3-mini-text|davinci|babbage", re.I)
# fallback humano se nem /models nem PROV_MODELOS existirem
_SUGESTOES = {
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
    "glm": ["glm-4.6", "glm-4.5v", "glm-4-flash"],
    "zai": ["glm-4.6", "glm-4.5-air", "glm-4.5v"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}

# ☁️ PROVEDORES PRINCIPAIS (catálogo p/ o cadastro em 1 clique — falta SÓ a
# chave; "zai coding plan" é o plano de API da Z.AI, mesmo endpoint GLM).
# `visao` = multimodais REAIS da casa (sondado 27/08 contra a API: glm-v5-
# turbo NÃO existe — "modelCode: does not exist"; glm-4.5v/4.6v respondem
# com image_url base64). `gera` = GERADORES de imagem (endpoint
# /images/generations — na Z.AI existem mas FORA do coding plan: 429
# "insufficient balance" sem saldo próprio).
CONHECIDOS = {
    "zai": {"nome": "Z.AI Coding Plan (GLM)",
            "base_url": "https://api.z.ai/api/paas/v4",
            "site": "z.ai", "dica": "glm-4.6 · glm-4.5 · glm-4.5v (plano coding)",
            "visao": ["glm-4.5v", "glm-4.6v"],
            "gera": ["glm-image", "cogview-4-250304"]},
    "openai": {"nome": "ChatGPT / OpenAI",
               "base_url": "https://api.openai.com/v1",
               "site": "platform.openai.com", "dica": "gpt-5 · gpt-5-mini · o3",
               "visao": ["gpt-5", "gpt-4o", "o3"],
               "gera": ["gpt-image-1"]},
    "anthropic": {"nome": "Claude / Anthropic",
                  "base_url": "https://api.anthropic.com/v1",
                  "site": "console.anthropic.com",
                  "dica": "claude-sonnet-4-5 · claude-opus-4-1",
                  "visao": ["claude-sonnet-4-5", "claude-opus-4-1"]},
    "deepseek": {"nome": "DeepSeek",
                 "base_url": "https://api.deepseek.com/v1",
                 "site": "platform.deepseek.com", "dica": "deepseek-chat · reasoner"},
    "openrouter": {"nome": "OpenRouter (vários)",
                   "base_url": "https://openrouter.ai/api/v1",
                   "site": "openrouter.ai",
                   "dica": "1 chave, modelos de TODAS as casas (com preço)",
                   "visao": ["anthropic/claude-sonnet-4.5",
                             "google/gemini-2.5-pro"],
                   "gera": ["google/gemini-2.5-flash-image-preview"]},
    "gemini": {"nome": "Google Gemini",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "site": "aistudio.google.com", "dica": "gemini-2.5-pro · flash",
               "visao": ["gemini-2.5-pro", "gemini-2.5-flash"]},
    "grok": {"nome": "xAI Grok",
             "base_url": "https://api.x.ai/v1",
             "site": "console.x.ai", "dica": "grok-4 · grok-code-fast",
             "visao": ["grok-4"]},
    "groq": {"nome": "Groq (rápidos)",
             "base_url": "https://api.groq.com/openai/v1",
             "site": "console.groq.com", "dica": "llama/openai na GPU deles"},
    "mistral": {"nome": "Mistral",
                "base_url": "https://api.mistral.ai/v1",
                "site": "console.mistral.ai", "dica": "mistral-large · codestral"},
}

# 📊 CONTEXTO (janela, em tokens) por PADRÃO de nome — quando a API não
# entrega (OpenAI/Anthropic/Z.AI listam só id), a heurística local cobre;
# desconhecido → None (NUNCA inventar número)
_RE_CTX = [
    (r"gpt-4\.1", 1000_000), (r"gpt-5", 400_000), (r"o3|o4-", 200_000),
    (r"gpt-4o|chatgpt-4o", 128_000),
    (r"claude-(?:sonnet|opus|haiku)-[34]", 200_000),
    (r"glm-4\.[56]", 200_000), (r"glm-4-flash", 128_000), (r"glm-4v", 128_000),
    (r"deepseek", 128_000),
    (r"gemini-2\.5", 1000_000), (r"gemini-2\.0", 1000_000),
    (r"grok-4", 256_000), (r"grok-3", 131_000), (r"grok-code", 256_000),
    (r"qwen3", 256_000), (r"qwen2\.5", 32_768), (r"qwen2", 32_768),
    (r"llama-3", 128_000), (r"mistral-large|codestral", 128_000),
    (r"kimi-k2", 128_000),
]


def _ctx_do_nome(nome: str) -> int | None:
    n = (nome or "").lower()
    for pad, ctx in _RE_CTX:
        if re.search(pad, n):
            return ctx
    return None


# 🏷️ CATEGORIA + "PARA QUE SERVE" cada modelo (pedido do dono) — a
# heurística cobre as famílias conhecidas; descrição rica quando a API
# entrega (info). EXCLUSÕES primeiro: gerador de imagem/áudio/embedding
# NÃO servem o chat de texto (aparecem no Sistema com o uso explicado).
_CATS_EXCLUIR = [
    ("imagem", r"image|cogview|dall[-_]?e|flux|sora|seedream|ideogram|"
               r"stable[-_]?diffusion|sd3|imagen",
     "🎨 GERA imagens a partir de texto (editoria visual) — não é chat"),
    ("audio", r"tts|whisper|voice|audio|speech|realtime",
     "🔊 voz/transcrição de áudio — não é chat"),
    ("embed", r"embed|rerank|bge-|gte-|e5-",
     "🧲 embedding (busca semântica) — não é chat"),
]
_CATS_CHAT = [
    ("programacao", r"coder|code-|coding|codestral|devstral|starcoder",
     "💻 otimizado para PROGRAMAÇÃO (gera/explica código)"),
    ("raciocinio", r"reasoner|reasoning|thinking|-r1\b|o1\b|o3-mini|qwq",
     "🧠 raciocínio profundo — pensa em cadeia antes de responder"),
]

# emoji/rotulo por categoria (ordem da UI)
CAT_ROTULOS = {
    "visao": "👁 visão (multimodal)",
    "programacao": "💻 programação",
    "raciocinio": "🧠 raciocínio",
    "conversa": "💬 conversa",
    "imagem": "🎨 gera imagem",
    "audio": "🔊 áudio",
    "embed": "🧲 embedding",
}


def categoria_do_modelo(nome: str) -> tuple[str, str]:
    """(categoria, para-que-serve) — visão usa a heurística multimodal já
    existente (respeita falsos positivos como deepseek-chat)."""
    n = (nome or "").lower()
    for cat, pad, uso in _CATS_EXCLUIR:
        if re.search(pad, n):
            return cat, uso
    if e_multimodal(nome):
        return "visao", ("👁 MULTIMODAL: entende IMAGENS (análise, descrição, "
                         "leitura de tela/foto) além de texto")
    for cat, pad, uso in _CATS_CHAT:
        if re.search(pad, n):
            if cat == "raciocinio" and re.search(r"o1\b|o3-mini", n):
                pass  # o1/o3-mini: raciocínio SEM imagem (texto puro)
            return cat, uso
    return "conversa", "💬 chat geral: pergunta, escreve, resume, traduz"
_CACHE: dict[str, tuple[float, list]] = {}
_LOCK = threading.Lock()
_TTL = 300  # 5 min


import os


def ids() -> list[str]:
    """Ids configurados: LLM_PROVIDERS explícito ∪ qualquer PROV_*_BASE_URL
    descoberto no ambiente (achou a chave do glm no Sistema? o provedor JÁ
    aparece — sem precisar editar a lista à mão)."""
    achados = {b.strip().lower() for b in
               (os.getenv("LLM_PROVIDERS") or "").split(",")
               if b.strip() and b.strip() != "local"}
    for chave in os.environ:
        m = re.match(r"^PROV_([A-Z0-9]+)_BASE_URL$", chave)
        if m and os.environ.get(chave, "").strip():
            achados.add(m.group(1).lower())
    return sorted(achados)


def _cfg(pid: str, chave: str, default: str = "") -> str:
    return (os.getenv(f"PROV_{pid.upper()}_{chave}") or default).strip()


def e_multimodal(nome: str) -> bool:
    """Heurística: o modelo aceita IMAGEM na entrada?"""
    n = (nome or "").lower()
    if not n or _RE_SO_TEXTO.search(n):
        return False
    return bool(_RE_VISAO.search(n))


def _modelos_do_endpoint(base: str, chave: str):
    """GET {base}/models — lista REAL + METADADOS quando a API entrega
    (OpenRouter: context_length/description/pricing; aceita OpenAI
    {data:[{id}]} E llama-server {models:[{name}]}) — qualquer
    OpenAI-compatible serve. Devolve (nomes, meta) ou (None, {})."""
    try:
        cabe = {"Authorization": f"Bearer {chave}"} if chave else {}
        # ⚠️ UA PRÓPRIO: o WAF da borda (Cloudflare) BLOQUEIA os
        # User-Agents padrão de Python ("python-httpx", "Python-urllib")
        # com 403 sem body — bug real: o container listava 0 modelos do
        # túnel da própria estação por causa disto
        cabe["User-Agent"] = "ragaroy/1.0"
        r = httpx.get(f"{base.rstrip('/')}/models", headers=cabe, timeout=12)
        if r.status_code != 200:
            return None, {}
        corpo = r.json()
        dados = corpo.get("data") or corpo.get("models") or []
        nomes, meta = set(), {}
        for m in dados:
            nome = str(m.get("id") or m.get("name") or "") \
                .removesuffix("-GGUF").strip()
            if not nome:
                continue
            nomes.add(nome)
            info = {}
            if m.get("context_length"):
                info["ctx"] = int(m["context_length"])
            desc = str(m.get("description") or "").strip()
            if desc:
                info["info"] = desc.split(". ")[0][:140]
            preco = (m.get("pricing") or {}).get("prompt")
            try:
                if preco is not None and float(preco) > 0:
                    info["info"] = (info.get("info", "") + f" · US$ "
                                    f"{float(preco) * 1_000_000:.2f}/M in").strip(" ·")
            except (TypeError, ValueError):
                pass
            mods = (m.get("architecture") or {}).get("input_modalities") or []
            if mods and "image" in [str(x).lower() for x in mods]:
                info["visao_api"] = True
            if info:
                meta[nome] = info
        return sorted(nomes) or None, meta
    except Exception:
        return None, {}


def gerar_imagem(pid: str, modelo: str, prompt: str,
                 tamanho: str = "1024x1024", log=print) -> dict:
    """Geração de IMAGEM por provedor cloud (POST {base}/images/
    generations — padrão OpenAI Images: b64_json OU url). Devolve o dict
    no formato do t2i local ({arquivo, pasta, tipo, modelo, segundos}).
    Sem GPU local envolvida — a z.ai responde 429 claro se o plano não
    cobre geração (coding plan é texto/visão)."""
    import time as _t
    from core import midia as _midia
    t0 = _t.time()
    base, chave = _cfg(pid, "BASE_URL"), _cfg(pid, "API_KEY")
    if not base:
        raise RuntimeError(f"provedor {pid.upper()} sem PROV_{pid.upper()}_"
                           "BASE_URL — cadastre a chave no Sistema (☁️)")
    log(f"🎨 geração EXTERNA [{pid}] {modelo} — GPU local intocada…", "gerar")
    r = httpx.post(f"{base.rstrip('/')}/images/generations",
                   headers={"Authorization": f"Bearer {chave}",
                            "Content-Type": "application/json",
                            "User-Agent": "ragaroy/1.0"},
                   json={"model": modelo, "prompt": prompt, "size": tamanho},
                   timeout=240)
    if r.status_code != 200:
        detalhe = ""
        try:
            detalhe = str((r.json().get("error") or {}).get("message")
                          or r.text)[:200]
        except Exception:
            detalhe = r.text[:200]
        raise RuntimeError(f"geração externa {modelo} → HTTP {r.status_code}"
                           + (f": {detalhe}" if detalhe else ""))
    item = ((r.json().get("data") or [{}])[0])
    _midia.SAIDAS["imagem"].mkdir(parents=True, exist_ok=True)
    alvo = _midia.SAIDAS["imagem"] / f"{pid}_{modelo.replace('/', '_')}_{int(t0)}.png"
    if item.get("b64_json"):
        import base64 as _b64
        alvo.write_bytes(_b64.b64decode(item["b64_json"]))
    elif item.get("url"):
        img = httpx.get(item["url"], timeout=120,
                        headers={"User-Agent": "ragaroy/1.0"})
        img.raise_for_status()
        alvo.write_bytes(img.content)
    else:
        raise RuntimeError("provedor respondeu 200 sem b64_json nem url")
    kb = round(alvo.stat().st_size / 1024)
    log(f"✅ imagem externa salva ({kb} KB)", "salvar")
    return {"arquivo": alvo.name, "pasta": str(_midia.SAIDAS["imagem"]),
            "prompt": prompt, "modelo": f"{pid}:{modelo}", "tipo": "imagem",
            "kb": kb, "segundos": round(_t.time() - t0),
            "vram_mi": None}


def modelos(pid: str, force: bool = False) -> list[dict]:
    """[{nome, visao, ctx, info}] do provedor — /models → manual →
    sugestões. `ctx` = janela de contexto (metadado da API quando existe
    — OpenRouter — senão heurística local por nome); `info` = descrição
    curta/preço quando a API entrega."""
    with _LOCK:
        cacheado = _CACHE.get(pid)
        if (not force and cacheado and time.time() - cacheado[0] < _TTL):
            return cacheado[1]
    base, chave = _cfg(pid, "BASE_URL"), _cfg(pid, "API_KEY")
    nomes, meta = _modelos_do_endpoint(base, chave)
    nomes = (nomes
             or [m.strip() for m in _cfg(pid, "MODELOS").split(",") if m.strip()]
             or _SUGESTOES.get(pid, []))
    lista = []
    for n in nomes:
        m = meta.get(n) or {}
        cat, uso = categoria_do_modelo(n)
        lista.append({"nome": n,
                      "visao": bool(m.get("visao_api")) or cat == "visao",
                      "ctx": m.get("ctx") or _ctx_do_nome(n),
                      "info": m.get("info", "") or uso,
                      "cat": cat, "uso": uso})
    # 👁 FALLBACK DA CASA: endpoints de CODING PLAN (ex.: api.z.ai/api/
    # coding/paas/v4) listam SÓ os de conversa — os multimodais EXISTEM na
    # API mas não vêm no /models. Sem isto o módulo Multimídia ficava sem
    # 👁 nenhum do provedor cadastrado (bug real do dono). Apensamos os
    # TÍPICOS do CONHECIDOS que não vieram na listagem (se um deles não
    # existir de verdade, o erro da API é claro na hora do uso).
    if not any(x["cat"] == "visao" for x in lista):
        for nome in CONHECIDOS.get(pid, {}).get("visao", []):
            if any(x["nome"] == nome for x in lista):
                continue
            cat, uso = categoria_do_modelo(nome)
            if cat != "visao":
                continue
            lista.append({"nome": nome, "visao": True,
                          "ctx": _ctx_do_nome(nome),
                          "info": uso + " · modelo da casa (não veio na "
                                   "listagem da API — se indisponível, o "
                                   "erro aparece ao usar)",
                          "cat": "visao", "uso": uso})
    # 🎨 idem para GERADORES de imagem (a listagem do coding plan não traz
    # glm-image/cogview — existem no endpoint /images/generations)
    if not any(x["cat"] == "imagem" for x in lista):
        for nome in CONHECIDOS.get(pid, {}).get("gera", []):
            if any(x["nome"] == nome for x in lista):
                continue
            lista.append({"nome": nome, "visao": False, "ctx": None,
                          "info": "🎨 GERA imagens via API do provedor — "
                                  "fora do coding plan da Z.AI (saldo "
                                  "próprio); o erro 429 avisa se não tiver",
                          "cat": "imagem",
                          "uso": "🎨 gera imagens (API do provedor)"})
    with _LOCK:
        _CACHE[pid] = (time.time(), lista)
    return lista


def resolver(pid: str, modelo: str) -> dict | None:
    """Credenciais/base do provedor p/ o modelo — ou None se não configurado
    (o chamador cai para o llama-server local SEM falhar)."""
    if not pid or pid == "local":
        return None
    base = _cfg(pid, "BASE_URL")
    if not base:
        return None
    return {"base_url": base.rstrip("/"),
            "api_key": _cfg(pid, "API_KEY") or "nada",
            "model": modelo or "",
            "provedor": pid,
            "nome": _cfg(pid, "NOME") or pid.upper()}


def listar(force: bool = False) -> list[dict]:
    """Catálogo p/ a UI (chaves NUNCA saem): local + externos CONFIGURADOS
    (sem BASE_URL o provedor não aparece — evita lista fantasma)."""
    out = [{"id": "local", "nome": "local (llama-server)",
            "externo": False,
            "modelos": []}]  # local é servido pelo seletor de GGUFs
    for pid in ids():
        if not _cfg(pid, "BASE_URL"):
            continue
        out.append({"id": pid, "nome": _cfg(pid, "NOME") or pid.upper(),
                    "externo": True, "modelos": modelos(pid, force)})
    return out
