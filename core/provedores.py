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

from . import config

# heurística de visão por nome (minúsculo) — lista REAL de cada provedor
# muda rápido demais para hardcode total; o padrão cobre o essencial
_RE_VISAO = re.compile(
    r"gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|gpt-5\.|o3|o4-|chatgpt|"
    r"claude-3|claude-4|claude-opus|claude-sonnet|claude-haiku|"
    r"glm-4v|glm-4\.5v|glm-4\.6v|glm-5v|"
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
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
}
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


def _modelos_do_endpoint(base: str, chave: str) -> list[str] | None:
    """GET {base}/models — lista REAL. Aceita OpenAI ({data:[{id}]}) E
    llama-server ({models:[{name}]}) — qualquer OpenAI-compatible serve."""
    try:
        cabe = {"Authorization": f"Bearer {chave}"} if chave else {}
        r = httpx.get(f"{base.rstrip('/')}/models", headers=cabe, timeout=12)
        if r.status_code != 200:
            return None
        corpo = r.json()
        dados = corpo.get("data") or corpo.get("models") or []
        nomes = sorted({str(m.get("id") or m.get("name") or "")
                        .removesuffix("-GGUF").strip()
                        for m in dados} - {""})
        return nomes or None
    except Exception:
        return None


def modelos(pid: str, force: bool = False) -> list[dict]:
    """[{nome, visao}] do provedor — /models → manual → sugestões."""
    with _LOCK:
        cacheado = _CACHE.get(pid)
        if (not force and cacheado and time.time() - cacheado[0] < _TTL):
            return cacheado[1]
    base, chave = _cfg(pid, "BASE_URL"), _cfg(pid, "API_KEY")
    nomes = (_modelos_do_endpoint(base, chave)
             or [m.strip() for m in _cfg(pid, "MODELOS").split(",") if m.strip()]
             or _SUGESTOES.get(pid, []))
    lista = [{"nome": n, "visao": e_multimodal(n)} for n in nomes]
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
