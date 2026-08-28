"""Rotas de provedores — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.get("/api/provedores")
def api_provedores(force: bool = False):
    """Catálogo de LLMs: local (llama-server) + provedores EXTERNOS
    configurados no .env (glm/deepseek/openai/anthropic/…) com a lista REAL
    de modelos (GET /models do provedor; manual PROV_MODELOS como reserva),
    a marcação 👁 multimodal (i2t com visão externa) E os METADADOS
    automáticos (ctx = janela de contexto, info = descrição/preço quando a
    API entrega). Chaves NUNCA saem."""
    from core import provedores
    return {"provedores": provedores.listar(force=force)}


@router.get("/api/provedores/conhecidos")
def api_provedores_conhecidos():
    """☁️ Provedores PRINCIPAIS (Z.AI Coding Plan, ChatGPT, Claude, DeepSeek,
    OpenRouter, Gemini, Grok, Groq, Mistral) para o cadastro em 1 clique —
    o form do Sistema preenche id/nome/URL; falta só a chave."""
    from core import provedores
    return {"conhecidos": [{"id": k, **v} for k, v in
                           provedores.CONHECIDOS.items()]}


@router.post("/api/provedores/cadastrar")
def api_provedores_cadastrar(body: ProvedorIn, request: Request):
    """Cadastra (ou SOBRESCREVE) um provedor OpenAI-compatible: grava
    PROV_<ID>_BASE_URL/_API_KEY/_NOME(/_MODELOS) no .env, recarrega a
    config e devolve o catálogo JÁ com os modelos reais da API (GET
    /models com a chave — o grupo 🌐 aparece no seletor do chat)."""
    _exigir_admin(request)
    pid = re.sub(r"[^A-Z0-9]", "", (body.id or "").upper().strip())
    base = (body.base_url or "").strip().rstrip("/")
    if not (2 <= len(pid) <= 12):
        raise HTTPException(422, "id: 2 a 12 letras/números (ex.: zai, glm, deepseek)")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(422, "base_url deve começar com http(s):// "
                            "(ex.: https://api.z.ai/api/paas/v4)")
    from core import provedores
    config.set_env_inplace(f"PROV_{pid}_BASE_URL", base)
    if (body.api_key or "").strip():
        config.set_env_inplace(f"PROV_{pid}_API_KEY",
                               body.api_key.strip())
    if (body.nome or "").strip():
        config.set_env_inplace(f"PROV_{pid}_NOME", body.nome.strip())
    if (body.modelos or "").strip():
        config.set_env_inplace(f"PROV_{pid}_MODELOS", body.modelos.strip())
    config.reload()
    cat = provedores.listar(force=True)
    # ⚠️ o catálogo usa id MINÚSCULO (provedores.ids() loweriza) — comparar
    # com pid.lower() senão o "meu" nunca acha e devolve lista vazia
    meu = next((p for p in cat if p["id"] == pid.lower()), None)
    return {"ok": True, "id": pid, "modelos": (meu or {}).get("modelos", []),
            "dica": ("modelos carregados da API do provedor" if (meu or {})
                     .get("modelos") else
                     "nenhum modelo veio da API — confira a chave; ou preencha "
                     "'modelos' com a lista manual separada por vírgula")}


