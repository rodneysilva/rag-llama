"""
API FastAPI — composição da aplicação (Fase 1 do split, 28/08).

A rota mudou de lugar, não de comportamento: app.py era um monólito de
~7.000 linhas com 158 rotas; agora é a COMPOSIÇÃO (cria o app, registra
middleware/startup, inclui os routers NA ORDEM ORIGINAL de declaração —
a ordem de matching de rotas é parte do contrato de paridade).

Layout pós-split:
    api/app.py     → este arquivo (composição: app, CORS, /static, include)
    api/base.py    → infra compartilhada (helpers, templates, registries,
                     Pydantic models) — `from api.base import *` nos routers
    api/routers/*  → rotas por domínio (@router.get/post/…)
    core/jobs.py   → JobRegistry & cia (domínio de execução, sem FastAPI)

Contrato de compat: `from api.app import X` continua válido (app.py
re-exporta a API pública de api.base via `from api.base import *`).

Rodar a partir da raiz do projeto (rag-llama):
    python -m uvicorn api.app:app --port 8000
Depois abrir: http://localhost:8000
"""
from api.base import *  # noqa: F401,F403 — re-export de compatibilidade

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import routers
from api.base import auth

app = FastAPI(title="RAG local — API")

# CORS: LLM local consumida pelos apps do VPS (via túnel) e pela webui.
# Origem aberta: a proteção real fica no auth da API (users.json/token),
# não em CORS. allow_credentials=False pois não usamos cookies de sessão.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# UI server-rendered (HTMX + Jinja): estáticos na raiz do projeto
app.mount("/static", StaticFiles(directory=str(
    Path(__file__).resolve().parent.parent / "static")), name="static")

auth.bootstrap_admin()  # cria o usuário inicial do .env (uma vez)

# ---------- middleware de auth (ordem original preservada) ----------
app.middleware("http")(_auth_middleware)

# ---------- executor de jobs (async, in-process — substituiu o RabbitMQ) ----------
# Decisão do dono (27/08): "para esses tipos de chamadas não tem necessidade
# do rabbitmq; quero o controle de erros". Fila asyncio SERIAL (VRAM segue
# gargalo), fábricas rodam em to_thread (event loop livre), retry com
# backoff p/ transientes, falha de negócio registrada no próprio job.
# Restart da API: jobs somem com ERRO CLARO no polling (404 tratado).
app.on_event("startup")(_subir_executor)


# ---------- routers — ordem de include = ordem da 1ª rota no app.py original ----------
# ⚠️ ORDEM É CONTRATO: rotas ambíguas resolvem pela PRIMEIRA declarada
# (ex.: /hx/conversa/copy duplicada; /sandbox/app/{chave}/{path:path} antes
# da variante com slug — a {path:path} engole tudo, o fallback interno
# revalida o caso slug). Não reordenar sem ler docs/arquitetura.md.
from api.routers import (  # noqa: E402
    agentico, auth as auth_router, biblioteca, chat, jobs as jobs_router,
    midia, paginas, provedores, sandbox, sistema, telemetria, voz,
)

_ORDEM_INCLUDE = [jobs_router,  # status das famílias: paths literais únicos —
                  # ANTES do midia para /api/midia/status/{job} vencer o
                  # /api/midia/{pasta}/{nome} (ordem do arquivo original)
                  auth_router, chat, paginas, sandbox, biblioteca, sistema,
                  midia, telemetria, voz, provedores, agentico]

for _r in _ORDEM_INCLUDE:
    app.include_router(_r.router)
