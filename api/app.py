"""
API FastAPI: status dos serviços, configurações (.env), ingestão e consulta.
Também serve a webui (build React em webui/dist/; sem build, cai na antiga
webui/legacy.html, acessível também em /legacy).

Rodar a partir da raiz do projeto (rag-llama):
    python -m uvicorn api.app:app --port 8000
Depois abrir: http://localhost:8000
"""
import asyncio
import json
import os
import re
import uuid
from itertools import count
from pathlib import Path
from typing import Literal
import threading
import time

import httpx
from dotenv import set_key
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from core import agent, auth, bussola, cache, catalog, config, contadores, fila, grafo, hf, limpeza, midia, modalidades, modelos, mcp_registry, rag, rerank, sessoes, sessions, tarefas, voz
from core import historico, resolucoes, telemetria
from core.linguagens import LINGUAGENS
from core.auto import responde_auto, _web_aprofundado
from core.analyze import analyze_all
from core.enrich import enrich_collection
from core.higieniza import higienizar_colecao
from core.ingest import ingest_docs, ingest_folder
from core.seed import seed_collection
from core.varredura import varredura_colecao

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

RAIZ_PROJETO = Path(__file__).resolve().parent.parent  # (webui React removida — UI é HTMX+Jinja)

# UI server-rendered (HTMX + Jinja): estáticos e templates na raiz
app.mount("/static", StaticFiles(directory=str(
    Path(__file__).resolve().parent.parent / "static")), name="static")

auth.bootstrap_admin()  # cria o usuário inicial do .env (uma vez)

# ---------- fila de jobs (RabbitMQ + DLX/DLQ) ----------

# Jobs RE-EXECUTÁVEIS: a mensagem da fila carrega kind+payload; a FÁBRICA de
# cada kind (registrada no import) reconstrói o executor a partir do payload.
# Antes a mensagem trazia só o id e o executor era uma closure em memória —
# restart da API descartava a mensagem silenciosamente (job perdido).
# `_despachar(fabricar, kind, payload)` chama `fabricar(payload)` na hora E
# registra a fábrica: o worker reconstrói executores perdidos no consumo.
_FABRICAS: dict[str, object] = {}
# registry de status por kind — para o worker detectar REENTREGA (mensagem
# re-entregada após queda de conexão) e NÃO re-executar o que já rodou
_REGISTROS: dict = {}


def _despachar(fabricar, kind: str, payload: dict,
               reg: "JobRegistry | None" = None) -> None:
    """Executa um job VIA FILA (RabbitMQ — sobrevive a restart, DLX/DLQ);
    sem broker no ar, cai para thread direta (o sistema nunca para).

    `fabricar(payload)` devolve o runner (closure) — é chamado aqui para o
    caminho imediato E de novo no worker se o executor se perder. Com
    `reg` informado, a entrada de status já existe ANTES do return (o
    worker pode demorar a pegar a mensagem — status não pode dar 404)."""
    jid = payload.get("job") or uuid.uuid4().hex
    payload = {**payload, "job": jid}
    if reg is not None:
        _REGISTROS[kind] = reg
        reg.iniciar(jid)

    # HISTÓRICO: embrulha a FÁBRICA (não o runner) — cobre o caminho direto
    # E o replay do worker, que chama fabricar(payload) de novo
    fabricar_puro = fabricar

    def fabricar_com_historico(p: dict):
        rodar_puro = fabricar_puro(p)

        def rodar():
            t0 = time.time()
            try:
                return rodar_puro()
            finally:
                try:
                    est = (reg.jobs.get(p.get("job")) if reg else None) or {}
                    resumo = est.get("result")
                    # caminho do log COMPLETO em disco (logs/jobs/{job}.jsonl)
                    caminho_log = (f"logs/jobs/{p.get('job')}.jsonl"
                                   if reg and (PASTA_LOGS_JOBS
                                               / f"{p.get('job')}.jsonl").exists()
                                   else None)
                    historico.registrar(
                        kind, f"{kind} {p.get('job')}", time.time() - t0,
                        job=p.get("job"), ok=not est.get("error"),
                        erro=(est.get("error") or None),
                        linhas=len(est.get("lines") or []),
                        log=caminho_log,
                        resumo=(json.dumps(resumo, ensure_ascii=False)[:300]
                                if resumo else None))
                except Exception:
                    pass
        return rodar

    fabricar = fabricar_com_historico
    _FABRICAS[kind] = fabricar
    rodar = fabricar(payload)
    if not callable(rodar):
        # 🚨 fábrica FORA DO CONTRATO (executa no corpo em vez de devolver
        # rodar): o corpo JÁ RODOU nesta thread — publicar no worker
        # executaria TUDO DE NOVO (bug real: teste de sandbox rodava 2x e
        # o POST travava o build inteiro na request). Não publica; avisa
        # alto para a fábrica ser corrigida.
        print(f"🚨 fábrica de '{kind}' executou no corpo (devolve "
              f"{type(rodar).__name__} em vez de runner) — corrigir para "
              "def fabricar(p): …; return rodar. Job NÃO publicado "
              "(evitaria execução dupla)")
        return
    if not fila.publicar(kind, jid, payload):
        # fallback thread: É esta thread que executa — marca picked
        if reg is not None:
            reg.jobs[jid]["picked"] = True
        threading.Thread(target=rodar, daemon=True).start()


def _no_worker(msg: dict) -> None:
    """Callback do worker: roda o executor registrado e acks; crash → DLQ.
    Executor perdido (API reiniciou com mensagem na fila): a FÁBRICA do kind
    reconstrói — o status do job volta a existir e o polling recupera.

    IDEMPOTENTE de verdade: a entrada de status é pré-criada pelo _despachar
    com `picked=False` (aguardando pickup). Aqui:
      - picked=True + running  → execução em curso noutra thread → IGNORA
        (é REENTREGA: o broker reenviou após queda de conexão — sem isto o
        job executava 2x e a 2ª rodada zerava o status da 1ª);
      - picked=True + concluído (result/error) → já processado → IGNORA;
      - picked=False → PRIMEIRA entrega → marca picked e EXECUTA (a entrada
        pré-criada NÃO é "em curso" — é o placeholder do status)."""
    jid = msg.get("job", "")
    kind = msg.get("kind")
    reg = _REGISTROS.get(kind or "")
    if reg is not None:
        if jid in reg.jobs:
            if reg.jobs[jid].get("picked"):
                print(f"🔁 job {jid} ({kind}) já executado/em curso — reentrega ignorada")
                telemetria.evento("rabbit", f"🔁 reentrega de {jid} ({kind}) IGNORADA "
                                            "(idempotente)", job=jid, kind=kind)
                return
            reg.jobs[jid]["picked"] = True
        else:
            # replay pós-restart SEM dispatch novo nesta instância: reconstrói
            # o placeholder JÁ marcado — reentregas seguintes são ignoradas
            reg.iniciar(jid)
            reg.jobs[jid]["picked"] = True
    telemetria.evento("rabbit", f"📥 job {jid} ({kind}) pego pelo worker",
                      job=jid, kind=kind)
    fabricar = _FABRICAS.get(kind)
    if fabricar:
        fabricar(msg.get("payload") or {})()
    else:
        print(f"⚠️ job {jid} ({kind}) sem fábrica — descartado "
              "(kind desconhecido nesta versão da API)")

fila.iniciar_worker(_no_worker, log=lambda m: print(f"🐇 {m}"))


# ⏱️ PRÉ-AQUECIMENTO do reranker (pedido do dono — "por que demorou tanto?"):
# o bge-reranker (~1,1 GB) é LAZY e a 1ª pergunta pagava ~36 s de
# download+ carga na CPU. Uma thread daemon aquece no BOOT (5 s de folga
# para a API subir primeiro); falha em silêncio — o comportamento lazy
# original segue como fallback.
def _preaquecer_reranker() -> None:
    def _aq():
        time.sleep(5)
        try:
            if rerank.disponivel() and getattr(config, "RERANKER", True):
                rerank.notas_de("aquecimento", ["texto de aquecimento"],
                                log=lambda m, g="": print(f"🎛️ {m}"))
                print("🎛️ reranker pré-aquecido no boot — a 1ª pergunta "
                      "não paga o carregamento")
        except Exception as e:
            print(f"🎛️ pré-aquecimento do reranker pulado: {str(e)[:80]}")
    threading.Thread(target=_aq, daemon=True,
                     name="preaquecer-reranker").start()


_preaquecer_reranker()

# ---------- autenticação (login simples, isolamento por conta) ----------

# rotas que não exigem login: auth em si, status (o lock da LLM funciona
# pré-login) e os arquivos estáticos/mídia (aceitam ?token= também)
_ROTAS_PUBLICAS = {"/api/auth/login", "/api/auth/register", "/api/status"}

# cookie da sessão (httpOnly): <img>/<video> não mandam header Authorization
# — com o cookie a mídia autentica sozinha e o token sai da query string
COOKIE_TOKEN = "ragaroy_token"

# rate limit do login: {chave ip|usuário: [timestamps das tentativas]}
_LOGIN_TENTATIVAS: dict[str, list[float]] = {}
_LOGIN_JANELA_S = 300   # 5 min
_LOGIN_MAX = 8          # tentativas por janela


def _usuario(request: Request) -> str:
    """Usuário autenticado da requisição (header Bearer, cookie ou ?token=)."""
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() or \
        request.cookies.get(COOKIE_TOKEN, "") or \
        request.query_params.get("token", "")
    user = auth.usuario_do_token(token) if token else None
    if not user:
        raise HTTPException(status_code=401, detail="faça login para usar o sistema")
    return user


def _usuario_ok(request: Request) -> bool:
    """Mesma checagem SEM levantar exceção (rotas com auth alternativo,
    como o preview da sandbox via token HMAC do subdomínio)."""
    try:
        return bool(_usuario(request))
    except HTTPException:
        return False


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    caminho = request.url.path
    if caminho.startswith("/api/") and caminho not in _ROTAS_PUBLICAS \
            and not caminho.startswith("/api/auth/"):
        try:
            _usuario(request)
        except HTTPException:
            return JSONResponse(status_code=401,
                                content={"detail": "faça login para usar o sistema"})
    if not caminho.startswith(("/api/", "/static", "/sandbox/")) and caminho != "/entrar":
        # páginas e /hx/*: sem login → volta para o /entrar (HTMX segue o
        # redirect). /sandbox/* TEM AUTH PRÓPRIO (token HMAC curto do
        # subdomínio sandbox.disroy.org — cookie não atravessa subdomínios)
        try:
            _usuario(request)
        except HTTPException:
            return RedirectResponse("/entrar", status_code=303)
    resposta = await call_next(request)
    # o frontend detecta 401 e volta para o login
    # ── RESGATE de URLs ABSOLUTAS do app temporário: o Flask/FastAPI gera
    #    /static/x.css e Location: /login SEM a chave da URL pública — o
    #    pedido cai em 404 na API. Se o REFERER aponta para um link de app
    #    válido, o pedido vai PARA O APP (o navegador está "dentro" dele).
    if resposta.status_code == 404:
        ref = request.headers.get("referer", "")
        m = re.search(r"/sandbox/app/([\w.\-]+)", ref)
        if m:
            from core import sandbox as _sb
            porta = _sb.chave_app_ok(m.group(1))
            if porta and request.method in ("GET", "POST", "HEAD"):
                try:
                    return await _proxy_app_api(porta, request,
                                                request.url.path,
                                                chave=m.group(1))
                except HTTPException:
                    pass   # app caiu no meio: segue o 404 original
    return resposta


class LoginIn(BaseModel):
    user: str
    senha: str


def _limpar_tentativas(agora: float) -> None:
    """Descarta janelas velhas (o dicionário não cresce para sempre)."""
    for chave, ts in list(_LOGIN_TENTATIVAS.items()):
        vivos = [t for t in ts if agora - t < _LOGIN_JANELA_S]
        if vivos:
            _LOGIN_TENTATIVAS[chave] = vivos
        else:
            _LOGIN_TENTATIVAS.pop(chave, None)


def _cookie(dados: dict, token: str) -> JSONResponse:
    """Resposta JSON com a sessão também em cookie httpOnly (mesma validade
    do token — <img>/<video> autenticam sozinhos, sem token na query)."""
    resp = JSONResponse(dados)
    resp.set_cookie(COOKIE_TOKEN, token, max_age=auth.TOKEN_DIAS * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    """Valida usuário+senha e emite o token (30 dias). `admin` marca o
    operador dono das configurações (AUTH_ADMIN_USER do .env).
    Rate limit: 8 tentativas erradas por 5 min (brute force online)."""
    agora = time.time()
    ip = (request.client.host if request.client else "?")
    chave = f"{ip}|{body.user.strip().lower()}"
    _limpar_tentativas(agora)
    if len(_LOGIN_TENTATIVAS.get(chave, [])) >= _LOGIN_MAX:
        raise HTTPException(status_code=429,
                            detail="muitas tentativas — aguarde alguns minutos "
                                   "antes de tentar de novo")
    if not auth.verificar(body.user, body.senha):
        _LOGIN_TENTATIVAS.setdefault(chave, []).append(agora)
        raise HTTPException(status_code=401, detail="usuário ou senha incorretos")
    _LOGIN_TENTATIVAS.pop(chave, None)  # sucesso zera a contagem
    user = body.user.strip()
    token = auth.emitir_token(user)
    return _cookie({"user": user, "token": token,
                    "admin": user == config.AUTH_ADMIN_USER}, token)


@app.post("/api/auth/register")
def register(body: LoginIn, request: Request):
    """Cria um perfil novo — SÓ para nomes da lista de permitidos
    (usuarios_permitidos.txt); cada conta vê só as suas sessões e mídias."""
    try:
        auth.registrar(body.user, body.senha)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = body.user.strip()
    token = auth.emitir_token(user)
    return _cookie({"user": user, "token": token,
                    "admin": user == config.AUTH_ADMIN_USER}, token)


@app.post("/api/auth/logout")
def logout(request: Request):
    """Derruba o cookie da sessão (o token Bearer é stateless — expira sozinho)."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_TOKEN)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    """Quem está logado (valida o token do frontend) + se é admin."""
    user = _usuario(request)
    return {"user": user, "admin": user == config.AUTH_ADMIN_USER}


def _exigir_admin(request: Request) -> str:
    """Configurações e troca de modelo são exclusivas do operador dono
    (AUTH_ADMIN_USER) — os demais perfis usam o sistema, não o administram."""
    user = _usuario(request)
    if user != config.AUTH_ADMIN_USER:
        raise HTTPException(status_code=403,
                            detail="apenas o administrador altera configurações "
                                   "e modelos")
    return user


def _exigir_host(recurso: str) -> None:
    """Estúdio/visão/troca de modelo gerenciam PROCESSOS e GPU do HOST
    (llama-server, sd-cli, whisper). Com a API em container isso não é
    possível — o erro diz exatamente o que fazer."""
    if config.EM_CONTAINER:
        raise HTTPException(status_code=400,
                            detail=f"'{recurso}' precisa dos binários de GPU do "
                                   "host e a API está em container. Rode o modo "
                                   "host (python -m uvicorn api.app:app) para o "
                                   "Estúdio, ou suba os modelos no host com "
                                   "servicos_llm.py — chat/ingestão/coleções "
                                   "funcionam normalmente no container.")


class IngestIn(BaseModel):
    folder: str
    collection: str | None = None  # categoria/coleção (padrão: a do .env)
    rapido: bool = True  # modo lote (DEFAULT — pedido do dono: ingestão é
    # SÓ RAG, sem LLM por arquivo; a categoria é a própria coleção). Quem
    # quiser categorização com LLM manda rapido=false explicitamente.


class HigienizarIn(BaseModel):
    collection: str  # coleção a limpar (ruído de página, duplicados, re-embed)


class VarreduraIn(BaseModel):
    collection: str  # coleção a varrer com a LLM (lixo claro é apagado)


class SeedIn(BaseModel):
    assunto: str  # qualquer tema: "psicanálise", "medicina", um framework…
    colecao: str | None = None  # nome da coleção (padrão: slug do assunto)
    fontes: int = 8  # máximo de fontes aprovadas na curadoria


class QueryIn(BaseModel):
    question: str
    collection: str | None = None  # uma coleção (compatibilidade)
    collections: list[str] | None = None  # várias coleções no mesmo contexto
    mode: Literal["rag", "hibrido", "livre", "auto"] = "rag"  # 422 se inventar modo
    mcps: list[str] | None = None  # servidores MCP que participam do híbrido
    history: list[dict] | None = None  # mensagens anteriores do chat
    sessao: str | None = None  # id da sessão do chat (trava enquanto processa)
    estado_agente: dict | None = None  # retomada: estado devolvido num "pendente"
    aprovacao: dict | None = None  # {ferramenta, argumento, decisao: uma_vez|sessao|negar}
    aprovacoes_sessao: dict | None = None  # {ferramenta: "sessao"} — não perguntar mais
    model: str | None = None  # modelo de conversa solicitado (troca se diferente)
    provider: str | None = None  # sempre enviado junto (hoje: llama-server)
    anexo_imagem: str | None = None  # mídia do painel incluída no contexto (visão descreve)
    job: bool = False  # True → roda em 2º plano e devolve {job} (webui: logs em tempo real, imune ao timeout do proxy — mata o 524)


# 🔎 "MCP" NATIVO de busca web: aparece no seletor do chat junto dos
# servidores registrados; marcado = o motor de busca do RagAroy entra no
# contexto da resposta (páginas INTEIRAS, não snippets — pedido do dono:
# "selecionar o mcp de motor de busca para contextualizar a sessão").
MCP_WEB = "pesquisa-web"


class ModeloAtivarIn(BaseModel):
    modelo: str  # alias do registro (core/modelos.py)
    provider: str | None = None  # informativo (hoje só llama-server)


class SessionIn(BaseModel):
    id: str | None = None  # null = nova sessão
    titulo: str = ""  # vazio = usa a primeira pergunta
    modo: str = ""
    colecoes: list[str] | None = None
    aprovacoes: dict | None = None  # {ferramenta: "sessao"} liberadas pelo operador
    messages: list[dict]  # [{role, content}]
    raw: list[dict] | None = None  # mensagens COMPLETAS (docs/fontes, tokens, mídia)


class McpIn(BaseModel):
    nome: str
    transport: str  # "stdio" | "http" | "sse"
    command: str = ""  # stdio: executável (ex.: python, npx)
    args: list[str] = []  # stdio: argumentos do comando
    url: str = ""  # http/sse: endereço do servidor


class DocEditIn(BaseModel):
    collection: str
    id: str
    page_content: str | None = None  # novo texto (re-embeda o vetor)
    metadata: dict | None = None  # campos de metadata a atualizar/merjar


class DocDeleteIn(BaseModel):
    collection: str
    ids: list[str]


class SettingsIn(BaseModel):
    values: dict[str, str]


# chaves do .env que NUNCA podem vir de um instalador de MCP (permitem
# sequestrar a auth/infra: AUTH_SECRET forja token de admin, LLM_* redireciona
# o chat etc.) — além destas, só maiúsculas/underline simples
_ENV_PROIBIDAS = {"AUTH_SECRET", "AUTH_ADMIN_USER", "AUTH_ADMIN_PASS",
                  "LLM_BASE_URL", "LLM_MODEL", "EMBED_BASE_URL", "EMBED_MODEL",
                  "QDRANT_URL", "RAGAROY_CONTAINER", "MODELS_DIR",
                  "LLAMA_BIN", "SD_CLI", "WHISPER_CLI", "SERPER_API_KEY"}
_RE_ENV_OK = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")


class MidiaPromptsIn(BaseModel):
    ideia: str  # a ideia em português; a LLM gera e critica as variações
    tipo: str = "imagem"  # "imagem" | "video" (dicas de movimento no vídeo)
    modelo: str | None = None  # só informativo na fase de prompts


class TarefaIn(BaseModel):
    """Dispara UMA tarefa do estúdio (modalidade) em segundo plano."""
    modalidade: str  # t2i|t2v|i2v|i2t|v2t|a2t|a2v (chat/dev seguem no /api/query)
    sessao: str | None = None  # sessão do chat que disparou (fica ocupada)
    arquivo: str | None = None  # referência (i2v/a2t/v2t…): caminho ou nome
    arquivo_b64: str | None = None  # em container: CONTEÚDO da referência —
    # o host não vê o disco da VPS; o agente grava em saidas/entrada/ e usa
    modelo: str | None = None  # modelo de conversa esperado (409 se divergente)
    texto: str = ""  # prompt/pergunta/texto de entrada
    params: dict = {}  # largura/altura/seed/frames/pergunta… por modalidade


class ContextoIn(BaseModel):
    """Inclui UMA mídia no contexto do RAG (descreve → embeda → indexa)."""
    arquivo: str
    tipo: str  # "imagem" | "video" | "audio"
    prompt: str = ""  # pergunta opcional p/ guiar a descrição (i2t)
    sessao: str | None = None


class SessaoEstudioIn(BaseModel):
    """Cria/renomeia UMA sessão do estúdio (agrupa as mídias geradas)."""
    nome: str


# Coleções de SISTEMA: funcionam por dentro mas não aparecem na webui
# (catálogo, contexto de mídia, prompts de mídia, sessões de chat indexadas
# e a base unificada — que entra AUTOMÁTICA nas buscas por linguagem).
COLECOES_SISTEMA = {"meta_colecoes", "midia_gerada", "prompts_midia",
                    "arquitetura_unificada", "sessoes_chat"}
BASE_UNIFICADA = "arquitetura_unificada"
# coleções nomeadas por LINGUAGEM de programação — base unificada entra
# junto automaticamente quando a busca toca qualquer uma delas
# LINGUAGENS vem do rodeiro único (core/linguagens.py) — a regra da base
# unificada bate igual no /api/query e no modo Auto
# ^ "csharp" mantido por compatibilidade (chunks da base unificada trazem a
# origem no metadata); a coleção visível hoje é "dotnet"


def _check(name: str, url: str) -> dict:
    """Tenta acessar uma URL de saúde e retorna online/offline (exige HTTP 200)."""
    try:
        r = httpx.get(url, timeout=1.5)
        return {"name": name, "ok": r.status_code == 200, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"name": name, "ok": False, "detail": str(e)[:120]}


# ---------- UI: HTMX + Jinja (server-rendered, sem build) ----------
# A interface é SIMPLES por decisão do dono: templates em templates/,
# CSS único em static/app.css, HTMX pelo CDN. Logs de job aparecem INLINE
# (linha por linha, com hora) — nada de popup flutuante.

from fastapi.templating import Jinja2Templates  # noqa: E402

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _jinja_strftime(ts, fmt="%d/%m %H:%M"):
    """Epoch → data legível (o conteúdo do cache mostra QUANDO foi guardado)."""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(int(ts)).strftime(fmt)
    except Exception:
        return ""


TEMPLATES.env.filters["strftime"] = _jinja_strftime
TEMPLATES.env.filters["basename"] = lambda s: str(s).split("\\")[-1].split("/")[-1]
SESSAO_COOKIE = "rag_sessao"

# ═══ CACHE-BUSTING dos estáticos ═════════════════════════════════════
# O Cloudflare cacheia /static/* agressivamente (HIT mesmo após deploy).
# Versão = hash curto do CONTEÚDO: qualquer mudança no CSS muda a URL
# (?v=abc123) e o CDN busca do container na hora — sem purgar nada.
import hashlib as _hl
def _versao_static(nome: str) -> str:
    try:
        p = Path(__file__).resolve().parent.parent / "static" / nome
        return _hl.md5(p.read_bytes()).hexdigest()[:8]
    except Exception:
        return "0"
TEMPLATES.env.globals["v_css"] = _versao_static("app.css")
TEMPLATES.env.globals["v_fav"] = _versao_static("favicon.svg")
TEMPLATES.env.globals["v_js"] = _versao_static("htmx.min.js")
TEMPLATES.env.globals["v_tw"] = _versao_static("vendor/tailwind.js")


def _card_codigo(corpo: str, lingua: str) -> str:
    """Bloco de código como CARD (o "quote" pedido): rótulo da linguagem +
    botão COPIAR (clique delegado em base.html — funciona em qualquer swap
    HTMX) + realce de sintaxe Pygments INLINE (sem CSS externo)."""
    import html as _h
    rotulo = lingua or "código"
    try:
        from pygments import highlight as _realce
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import TextLexer, get_lexer_by_name
        from pygments.util import ClassNotFound
        try:
            lexer = get_lexer_by_name(lingua or "text", stripall=False)
        except ClassNotFound:
            lexer = TextLexer()
        dentro = _realce(corpo, lexer,
                         HtmlFormatter(noclasses=True, nowrap=True))
    except Exception:
        dentro = _h.escape(corpo)
    cls = f"language-{lingua}" if lingua else ""
    return ('<figure class="cod"><figcaption><span class="cod-ling">'
            + _h.escape(rotulo)
            + '</span><span class="cod-acoes">'
            + '<button type="button" class="cod-btn" data-testar '
            + 'title="executar este código na sandbox (leva TODOS os códigos '
            + 'da conversa como contexto — python, rust, c#, java, node, go, '
            + 'ruby, php, dart)">&#9654; testar</button>'
            + '<button type="button" class="cod-btn" data-copiar>'
            + "&#128203; copiar</button></span></figcaption><pre><code"
            + (f' class="{cls}"' if cls else "") + ">" + dentro
            + "</code></pre></figure>")


def _md_basico(texto: str) -> str:
    """Markdown COMPLETO no servidor (lib `markdown`) SEM escape duplo e
    SEM buraco de XSS. Blocos de código viram CARD (figure) com copiar.

    v4 — fences que FUGIAM da regex e renderizavam como texto corrido:
    CRLF (stream/whisper), linguagem seguida de espaço (```python ␊) e
    abertura com 4+ backticks. Tudo normalizado antes de extrair."""
    import html as _h
    import re as _re
    if not texto:
        return ""

    # NORMALIZAÇÃO de fences (modelos 7B): CRLF -> LF; 4+ backticks -> 3;
    # indentado -> coluna 0; colado no texto ("Estrutura:```html") -> quebra
    # antes; último aberto -> fecha.
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = _re.sub(r"`{4,}", "```", texto)
    texto = _re.sub(r"(?m)^[ \t]{1,8}(```)", r"\1", texto)
    texto = _re.sub(r"(?m)^(?!\s*```)([^\n`]{1,200}?)(```[\w+\-#.]*)[ \t]*$",
                    r"\1\n\2", texto)
    if texto.count("```") % 2:
        texto += "\n```"

    # 1) extrai os blocos de código (conteúdo INTACTO) -> CARD de cada um
    blocos: list[str] = []

    def _guarda(m):
        blocos.append(_card_codigo(m.group(2), (m.group(1) or "").strip().lower()))
        return f"\x00BLOCO{len(blocos) - 1}\x00"

    texto = _re.sub(r"```([\w+\-#.]*)[ \t]*\n(.*?)```", _guarda,
                    texto, flags=re.S)

    # 2) o RESTO é texto seguro: escapa e deixa a lib montar markdown
    try:
        import markdown as _mk
        saida = _mk.markdown(_h.escape(texto, quote=False), extensions=[
            "tables", "sane_lists", "nl2br"])
    except ImportError:
        saida = _md_fallback(texto)

    # 3) devolve os cards (nos DOIS caminhos — o fallback recebe o texto
    #    com placeholders e NÃO os perdeu mais)
    for n, bloco in enumerate(blocos):
        saida = saida.replace(f"\x00BLOCO{n}\x00", bloco)
    return saida


def _md_fallback(texto: str) -> str:
    """Parser mínimo (usado só sem a lib markdown): recebe o texto JÁ com os
    blocos extraídos em placeholders \x00BLOCOn\x00 (substituídos pelo
    chamador) — inline, negrito, títulos e listas.

    ⚠️ SEGURANÇA: ESCAPA o HTML de entrada ANTES de tudo (os testes de XSS
    pegaram o fallback deixando <script> passar cru — qualquer ambiente sem
    a lib `markdown` virava buraco de XSS; os placeholders \x00 não são
    afetados pelo escape)."""
    import html as _h
    import re as _re
    p = _h.escape(texto, quote=False)
    p = _re.sub(r"`([^`\n]+)`", r"<code>\1</code>", p)
    p = _re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", p)
    p = _re.sub(r"(?m)^#{1,3}\s+(.+)$", r"<h3>\1</h3>", p)
    p = _re.sub(r"(?m)^\s*[-*]\s+(.+)$", r"<li>\1</li>", p)
    p = _re.sub(r"(?m)^\s*\d+[.)]\s+(.+)$", r"<li>\1</li>", p)
    p = _re.sub(r"(https?://[^\s<)]+)",
                r'<a href="\1" target="_blank" rel="noreferrer">\1</a>', p)
    return p.replace("\n", "<br>")


def _paginas_ctx(request: Request, aba: str) -> dict:
    user = _usuario(request)
    return {"request": request, "aba": aba, "usuario": user,
            "admin": user == config.AUTH_ADMIN_USER}


def _sessao_id(request: Request, resposta=None, criar: bool = False) -> str | None:
    """Sessão do chat via cookie (a última conversa volta ao recarregar)."""
    sid = request.cookies.get(SESSAO_COOKIE)
    # criar = cria SE NAO EXISTIR cookie (nao substitui o valido!). O bug:
    # hx_chat passava criar=True e CADA mensagem virava sessao nova —
    # o historico anterior ficava orfao noutra sessao (o chat 'perdia'
    # a conversa ao enviar a mensagem seguinte).
    if not sid and resposta is not None:
        sid = str(uuid.uuid4())
        resposta.set_cookie(SESSAO_COOKIE, sid, max_age=30 * 86400,
                            httponly=True, samesite="lax")
    return sid


def _msgs_da_sessao(sid: str | None, owner: str) -> list[dict]:
    """Mensagens completas (com tokens/pensamentos) da sessão, para render."""
    if not sid:
        return []
    try:
        dados = sessions.get_session(sid) or {}
        bruto = dados.get("raw") or []
        saida = []
        for m in bruto:
            item = {"role": m.get("role", "user"),
                    "content": m.get("content", "")}
            for campo in ("tokens", "modelo", "docs", "pensamentos",
                          "pensamentos_sintetizados", "cache",
                          "midia", "segundos"):
                if m.get(campo):
                    item[campo] = m[campo]
            if m.get("role") != "user":
                item["html"] = m.get("html") or _md_basico(m.get("content", ""))
            saida.append(item)
        return saida
    except Exception:
        return []


@app.get("/hx/contagem")
def hx_contagem(request: Request):
    """CONTADORES EM TEMPO REAL (partial do topbar): LLM CARREGADA (sutil,
    pedido do dono) + tokens enviados/gerados + cache semantico."""
    _usuario(request)
    tot = contadores.totais() or {}
    t = tot.get("total") or {}
    try:
        ci = cache.info() or {}
    except Exception:
        ci = {"online": False, "entradas": 0}
    modelo = modelos.servido(modelos.CHAT_PORTA)  # cache 10s: barato
    vl = False
    try:
        vl = bool(modelos.servido(modelos.VL_PORTA))
    except Exception:
        pass
    return TEMPLATES.TemplateResponse(request, "_contagem.html",
                                      {"request": request,
                                       "modelo": modelo, "vl": vl,
                                       "entrada": t.get("entrada", 0),
                                       "saida": t.get("saida", 0),
                                       "chamadas": t.get("chamadas", 0),
                                       "cache_n": ci.get("entradas", 0),
                                       "cache_on": bool(ci.get("online"))})


@app.get("/hx/jobsbar")
def hx_jobsbar(request: Request):
    """JOBS EM SEGUNDO PLANO (partial global): qualquer pesquisa/ingestao/
    revisao/seed/manutencao em andamento aparece no TOPO de TODAS as
    paginas — sair e voltar nunca mais perde a tarefa de vista."""
    _usuario(request)
    ativos = []
    for reg, kind, rotulo in ((_pesquisa, "pesquisa", "pesquisa"),
                              (_preview, "preview", "revisao"),
                              (_ingest, "ingest", "ingestao"),
                              (_seed, "seed", "colecao"),
                              (_manutencao, "manutencao", "manutencao")):
        try:
            with reg.lock:
                for jid, st in (reg.jobs or {}).items():
                    if isinstance(st, dict) and st.get("running"):
                        nl = len(st.get("lines") or [])
                        ativos.append({"kind": kind, "job": jid,
                                       "rotulo": f"{rotulo} {nl} linha(s)"})
        except Exception:
            pass
    return TEMPLATES.TemplateResponse(request, "_jobsbar.html",
                                      {"request": request, "jobs": ativos})


@app.get("/hx/conversa/copy")
def hx_conversa_copy(request: Request):
    """Conversa INTEIRA em markdown (pergunta, resposta, tokens, raciocínio
    de cada mensagem) — o botão COPIAR CONVERSA do chat cola isto na
    área de transferência, com o contexto completo."""
    _usuario(request)
    bruto = (sessions.get_session(request.cookies.get(SESSAO_COOKIE))
             or {}).get("raw") or []
    linhas = []
    for m in bruto:
        if m.get("role") == "user":
            linhas.append("## você")
        else:
            mod = f" · {m['modelo']}" if m.get("modelo") else ""
            linhas.append(f"## assistente{mod}")
        linhas.append(m.get("content") or "")
        tk = m.get("tokens") or {}
        if tk:
            linhas.append(f"_🪙 🔻{tk.get('entrada', 0)} · 🔺{tk.get('saida', 0)}"
                          f" · {tk.get('chamadas', 0)} chamada(s)_")
        for l in (m.get("pensamentos") or []):
            linhas.append(f"> {l.get('msg') or ''}")
        linhas.append("")
    return PlainTextResponse("\n".join(linhas))


@app.get("/")
def pagina_chat(request: Request):
    """Home = conversa (mensagens da sessão do cookie + composer)."""
    ctx = _paginas_ctx(request, "chat")
    ctx["mensagens"] = _msgs_da_sessao(request.cookies.get(SESSAO_COOKIE), ctx["usuario"])
    # job EM CURSO da sessão: o polling volta renderizado (refresh não perde)
    ctx.update(_job_ativo_ctx(request.cookies.get(SESSAO_COOKIE)))
    try:
        ctx["colecoes"] = collections() or []
    except Exception:
        ctx["colecoes"] = []
    # ⚡ cache semântico no header do chat (estado real do core/cache)
    try:
        ctx["cache"] = cache.info() or {"online": False, "entradas": 0}
    except Exception:
        ctx["cache"] = {"online": False, "entradas": 0}
    # modelos de CONVERSA p/ o seletor (o ativo marcado), CATEGORIZADOS:
    # programação (coder) x conversa geral — optgroups no combobox
    try:
        _ativo = modelos.servido(modelos.CHAT_PORTA)
        _stem_alias = {}
        for alias, (arq, _c) in modelos.REGISTRO.items():
            _stem_alias.setdefault(modelos.Path(arq).stem, alias)
        _grupos = {"programacao": [], "conversa": []}
        for m in modelos.listar():
            if m.get("categoria") != "chat":
                continue
            nome = _stem_alias.get(m["nome"], m["nome"])
            _grupos["programacao" if "coder" in nome.lower() else "conversa"].append(
                {"nome": nome, "gb": m.get("gb"),
                 "ativo": nome == _ativo})
        ctx["modelos_chat"] = (_grupos["programacao"] + _grupos["conversa"])
        ctx["modelos_chat_grupos"] = [
            {"rotulo": "programação", "modelos": _grupos["programacao"]},
            {"rotulo": "conversa", "modelos": _grupos["conversa"]}]
        # 👁 VISÃO LOCAL: GGUFs categoria visao da estação — sem este grupo
        # o i2t do chat ficava SEM modelo algum quando não há provedor 👁
        # cloud cadastrado (o multimodal local morava no optgrp de geração
        # que saiu do composer — bug real do dono: "seleciono imagem→texto
        # e não aparece nenhum modelo")
        _visao = [{"nome": m["nome"], "gb": m.get("gb"), "ativo": False,
                   "visao": True, "ctx": None,
                   "info": "multimodal local (GPU da estação)"}
                  for m in modelos.listar() if m.get("categoria") == "visao"]
        if _visao:
            ctx["modelos_chat_grupos"].append(
                {"rotulo": "👁 visão local", "modelos": _visao})
    except Exception:
        ctx["modelos_chat"] = []
        ctx["modelos_chat_grupos"] = []
    # 🌐 PROVEDORES EXTERNOS (glm/deepseek/openai/anthropic…) no mesmo
    # seletor: um optgroup por provedor; multimodais (👁) também servem o
    # i2t (análise de imagem pela API externa — GPU local intocada).
    # O valor é "prov:modelo" (parseado no _processar_query). Sem custo
    # quando LLM_PROVIDERS está vazio (nada configurado no .env).
    try:
        from core import provedores as _prov
        for _p in _prov.listar():
            if not _p["externo"] or not _p["modelos"]:
                continue
            # 🏷️ por CATEGORIA (pedido do dono): conversa/programação/
            # raciocínio/visão — cada uma com seu optgroup; modelo de
            # GERAÇÃO de imagem/áudio/embedding NÃO serve o chat (fica no
            # Sistema com o uso explicado)
            _ordem = {"visao": 0, "programacao": 1, "raciocinio": 2,
                      "conversa": 3}
            _por_cat = {}
            for m in _p["modelos"]:
                if m.get("cat") in ("imagem", "audio", "embed"):
                    continue
                _por_cat.setdefault(m.get("cat", "conversa"), []).append(m)
            for _cat in sorted(_por_cat, key=lambda c: _ordem.get(c, 9)):
                ctx["modelos_chat_grupos"].append({
                    "rotulo": f"🌐 {_p['nome']} · "
                              f"{_prov.CAT_ROTULOS.get(_cat, _cat)}",
                    "externo": _p["id"],
                    "modelos": [{"nome": f"{_p['id']}:{m['nome']}",
                                 "rotulo": m["nome"], "gb": None,
                                 "ativo": False, "visao": m["visao"],
                                 "ctx": m.get("ctx"), "info": m.get("info", "")}
                                for m in _por_cat[_cat]]})
    except Exception:
        pass
    # modelos de GERAÇÃO (combobox inteligente: aparecem SÓ quando a mídia
    # do composer é imagem [Flux variants] ou vídeo/gif [Wan2.2]).
    # FALLBACK fixo: na VPS não há /models montado — sem isto o combobox
    # de imagem ficava VAZIO (nada para selecionar).
    _FLUX_FIXO = [{"nome": "flux1-schnell", "gb": 6.8},
                  {"nome": "flux1-dev", "gb": 6.8}]
    try:
        _ger = {"imagem": [], "video": []}
        for m in modelos.listar():
            if m.get("categoria") in ("imagem", "video") and m.get("compativel", True):
                _ger[m["categoria"]].append({"nome": m["nome"], "gb": m.get("gb")})
        if not _ger["imagem"]:
            _ger["imagem"] = _FLUX_FIXO
        if not _ger["video"]:   # sem /models montado (VPS): as gerações de Wan
            # conhecidas pela estação (o alias resolve no agente por substring)
            _ger["video"] = [{"nome": "wan2.1-t2v-1.3b", "gb": 1.4},
                             {"nome": "wan2.2-ti2v-5b", "gb": 5.0}]
        ctx["modelos_geracao"] = _ger
    except Exception:
        ctx["modelos_geracao"] = {"imagem": _FLUX_FIXO,
                                  "video": [{"nome": "wan2.1-t2v-1.3b", "gb": 1.4},
                                            {"nome": "wan2.2-ti2v-5b", "gb": 5.0}]}
    try:
        ctx["mcps"] = [s.get("nome") or s for s in mcp_registry.list_servers()]
    except Exception:
        ctx["mcps"] = []
    return TEMPLATES.TemplateResponse(request, "chat.html", ctx)


@app.post("/hx/chat")
def hx_chat(request: Request, question: str = Form(""), mode: str = Form("hibrido"),
            model: str = Form(""),
            mcps: list[str] = Form(default=[]),
            colecoes: list[str] = Form(default=[]),
            midia: str = Form(default=""), audio: UploadFile | None = File(None),
            referencia: str = Form(default=""),
            duracao: str = Form(default="")):
    """Inicia o job do chat e devolve o partial INLINE (bolha do usuário +
    tail de polling com o log ao vivo)."""
    if audio is not None and audio.filename:
        dados = audio.file.read()
        texto = voz.transcrever_bytes(dados)
        if texto.strip():
            question = texto.strip()
    question = (question or "").strip()
    if not question:
        return TEMPLATES.TemplateResponse(request, "_chatjob.html",
                                          {"request": request, "job": "-",
                                           "linhas": [], "running": False,
                                           "rodape": "pergunta vazia"}, status_code=400)
    # so troca quando o pedido e um ALIAS conhecido (REGISTRO); arquivos
    # crus (stem) da estacao travam a troca na VPS — ignora silenciosamente.
    # EXTERNO ("glm:glm-4.6") NÃO é alias: passa INTEIRO (o override da
    # execução cuida do resto; aqui só não podemos descartar)
    _model_raw = (model or "").strip()   # valor ORIGINAL p/ geração de mídia
    _alias_ok = _model_raw in modelos.REGISTRO if _model_raw else False
    model = _model_raw if (_alias_ok or ":" in _model_raw) else None
    # HISTÓRICO da sessão salva (fonte da verdade no servidor): a webui
    # HTMX não envia history — sem isto a LLM respondia SEM contexto
    # ("o que se perdeu": o React antigo mandava). 📦 ENXUTO (pedido:
    # "máxima performance, sem ruídos"): ÚLTIMAS 4 mensagens e cada
    # resposta anterior TRUNCADA — o histórico ia INTEIRO (12 msgs com
    # respostas completas ~2× o prompt) e o pré-processamento comia o
    # tempo de geração; o follow-up só precisa do FIO da conversa.
    _hist = []
    try:
        _dados = sessions.get_session(request.cookies.get(SESSAO_COOKIE)) or {}
        for m in (_dados.get("raw") or [])[-4:]:
            if not m.get("content"):
                continue
            if m.get("role") == "assistant":
                _hist.append({"role": "assistant",
                              "content": m["content"][:220].rstrip() + ("…" if len(m["content"]) > 220 else "")})
            else:
                _hist.append({"role": "user", "content": m["content"][:400]})
    except Exception:
        pass
    # COLEÇÃO SELECIONADA = CONTEXTO RAG (pedido do dono): no modo LIVRE a
    # busca nunca roda — o usuário marca coleções e elas não entravam no
    # contexto (a armadilha). Promovido para HÍBRIDO: a base entra como
    # referência primária e o conhecimento do modelo complementa.
    if colecoes and mode == "livre":
        mode = "hibrido"
    # ⚠️ SID CRIADO ANTES DO CORPO: na 1ª mensagem o cookie ainda NÃO
    # existia no request → o job rodava com sessao=None → o CACHE gravava
    # a resposta com owner VAZIO e a 2ª pergunta (com cookie, owner certo)
    # NUNCA batia o escopo (bug real do "cache não funciona no chat").
    _stub_sid = JSONResponse({})
    sid = _sessao_id(request, _stub_sid, criar=True)
    corpo = QueryIn(question=question, mode=mode, model=model, mcps=mcps or [],
                    collections=colecoes or [], history=_hist or None,
                    sessao=sid, job=True,
                    anexo_imagem=(referencia.strip() or None))
    # 🎨🚫 GERAÇÃO SAIU DO CHAT (pedido do dono 27/08: "geração de imagem e
    # vídeo fica só no Multimídia, o retorno da tela do chat é texto") — o
    # composer não oferece mais; páginas ANTIGAS abertas que ainda mandem
    # caem neste aviso (a análise i2t segue: retorno é TEXTO)
    if midia in ("imagem", "video", "gif", "i2v", "i2g"):
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "erro", "job": "erro",
             "rotulo": f"gerar {midia}", "linhas": [], "running": False,
             "erro": "geração de imagem/vídeo agora mora no módulo 👁 "
                     "Multimídia — abra no menu (ou /midia): análise E "
                     "geração (🖼 Flux · 🎬 Wan · 🎞 gif) com log ao vivo"})
    if midia in ("i2t",):
        # 👁 i2t é RESPOSTA DE CHAT (layout de mensagem + raciocínio — era
        # card de TAREFA "✓ concluído · análise: …" cru; pedido do dono
        # "por que o chat perdeu o layout?"): job no registry do CHAT com
        # a análise como answer
        if not referencia.strip():
            return TEMPLATES.TemplateResponse(
                request, "_job.html",
                {"request": request, "kind": "erro", "job": "erro",
                 "rotulo": "analisar imagem", "linhas": [], "running": False,
                 "erro": "a análise precisa de uma imagem — clique em "
                         "📎 subir imagem e tente de novo"})
        job = _query.novo_id()

        def _fab_i2t(payload: dict):
            jid = payload["job"]

            def rodar():
                # ⚠️ o PARÂMETRO `midia` (str do form) SOMBREIA o módulo no
                # closure — import local com alias resolve
                from core import midia as _midia
                _query.log(jid, f"👁 análise multimodal de "
                               f"{Path(payload['referencia']).name}"
                               + (f" com {payload['modelo']}"
                                  if payload["modelo"] else " (local)"),
                           etapa="análise")
                try:
                    alvo = _midia.ENTRADA / Path(payload["referencia"]).name
                    if not alvo.exists():
                        alvo = Path(payload["referencia"])
                    modelo = payload["modelo"]
                    # LOCAL (sem ":") em CONTAINER → AGENTE do host (a GPU e
                      # o GGUF vivem na estação; direto aqui procura D:\models
                      # no Linux e morre). EXTERNO "prov:nome" roda NA API.
                    if config.EM_CONTAINER and ":" not in modelo:
                        import base64 as _b64
                        with open(alvo, "rb") as f:
                            img_b64 = _b64.b64encode(f.read()).decode()
                        r = modelos._chamar_agente(
                            "/visao", {"b64": img_b64,
                                       "nome": Path(alvo).name,
                                       "pergunta": payload["pergunta"]},
                            timeout=420)
                        analise = r.get("descricao", "")
                    else:
                        analise = _midia.legendar_imagem(
                            str(alvo), payload["pergunta"] or None,
                            modelo=modelo,
                            log=lambda m, g="": _query.log(
                                jid, m, **({"etapa": g} if g else {})))
                    _query.concluir(jid, result={
                        "question": payload["pergunta"],
                        "answer": analise or "(a análise não retornou texto)",
                        "mode": "i2t", "docs": [], "cache": None,
                        "model": None, "pensamentos": None,
                        "tokens": {"entrada": 0, "saida": 0, "chamadas": 0}})
                except Exception as e:
                    _query.concluir(jid, error=str(e)[:400])
            return rodar

        _despachar(_fab_i2t, "i2t",
                   {"referencia": referencia.strip(), "pergunta": question,
                    "modelo": _model_raw.strip(), "job": job}, _query)
        try:
            anterior = sessions.get_session(sid) or {}
            bruto = anterior.get("raw") or []
            bruto.append({"role": "user", "content": question})
            sessions.save_session(bruto, sid=sid,
                                  owner=anterior.get("owner", ""),
                                  titulo="", modo=mode, colecoes=colecoes,
                                  aprovacoes=anterior.get("aprovacoes", {}),
                                  raw=bruto,
                                  job_ativo={"kind": "chat", "job": job})
        except Exception:
            pass
        linhas = _query.status(job, 0, "")["lines"]
        parcial = TEMPLATES.TemplateResponse(
            request, "_chat_inicio.html",
            {"request": request, "job": job, "linhas": linhas,
             "running": True, "pergunta": question,
             "otimista": request.headers.get("x-otimista") == "1"})
        # cookie do sid: o _stub_sid foi criado ANTES (linha do corpo) —
        # ler DELE (resp_stub só nasce no fluxo de texto adiante)
        _sc = _stub_sid.headers.get("set-cookie", "")
        if _sc.startswith(SESSAO_COOKIE + "="):
            _sid = _sc.split("=", 1)[1].split(";", 1)[0]
            parcial.set_cookie(SESSAO_COOKIE, _sid, max_age=30 * 86400,
                               httponly=True, samesite="lax")
        return parcial
    try:
        r = query(corpo)
    except HTTPException as e:
        # ERRO como partial 200: com status de erro o HTMX NÃO faz swap e o
        # form parece MORTO (era o "não consigo mais mandar mensagens") —
        # a falha entra na conversa como card, o chat segue utilizável.
        detalhe = e.detail if isinstance(e.detail, str) else str(e.detail)
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "erro", "job": "erro",
             "rotulo": "mensagem", "linhas": [], "running": False,
             "erro": detalhe})
    except Exception as e:  # fila fora/servidor sumiu: idem, sem travar
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "erro", "job": "erro",
             "rotulo": "mensagem", "linhas": [], "running": False,
             "erro": f"o serviço do chat não respondeu ({str(e)[:160]}) — "
                     "aguarde alguns segundos e tente de novo"})
    # grava a pergunta na sessão (a resposta entra quando o job conclui) —
    # sid/resp_stub já criados ANTES do corpo (owner correto no cache)
    resp_stub = _stub_sid
    try:
        anterior = sessions.get_session(sid) or {}
        bruto = anterior.get("raw") or []
        # 1ª mensagem → o título NASCE "(sem título)" e o TÍTULO SEMÂNTICO
        # (embedding) é calculado no POLL de conclusão — o POST NUNCA
        # espera pelo embed (era o delay do envio: até 3,5 s pendurados
        # antes da caixa limpar)
        bruto.append({"role": "user", "content": question})
        sessions.save_session(bruto, sid=sid, owner=anterior.get("owner", ""),
                              titulo="", modo=mode, colecoes=colecoes,
                              aprovacoes=anterior.get("aprovacoes", {}),
                              raw=bruto,
                              job_ativo={"kind": "chat", "job": r["job"]})
    except Exception:
        pass
    linhas = _query.status(r["job"], 0, "")["lines"]
    ctx = {"request": request, "job": r["job"], "linhas": linhas,
           "running": True, "pergunta": question,
           # 🫧 MODO OTIMISTA: o browser já inseriu a bolha do usuário NA
           # HORA (antes do POST voltar) — o partial traz SÓ o card do job
           # (sem bolha = sem duplicata)
           "otimista": request.headers.get("x-otimista") == "1"}
    parcial = TEMPLATES.TemplateResponse(request, "_chat_inicio.html", ctx)
    # cookie de sessao criado no resp_stub (que NAO vai ao cliente) —
    # propaga para a resposta REAL; sem isto o navegador nunca recebe o sid
    # e a conversa nao existe ao recarregar (o bug das sessions)
    # JSONResponse nao expoe .cookies: recupera o sid do Set-Cookie cru
    _sc = resp_stub.headers.get("set-cookie", "")
    if _sc.startswith(SESSAO_COOKIE + "="):
        _sid = _sc.split("=", 1)[1].split(";", 1)[0]
        parcial.set_cookie(SESSAO_COOKIE, _sid, max_age=30 * 86400,
                           httponly=True, samesite="lax")
    return parcial


@app.get("/hx/chat/{job}")
def hx_chat_poll(job: str, request: Request):
    """Polling do chat: linhas novas enquanto roda; ao concluir, salva a
    resposta na sessão e devolve a MENSAGEM completa (substitui o tail)."""
    try:
        s = _query.status(job, 0, "")
    except HTTPException:
        s = {"running": False, "lines": [], "result": None, "error": "job não encontrado"}
    if s["running"]:
        return TEMPLATES.TemplateResponse(request, "_chatjob.html",
                                          {"request": request, "job": job,
                                           "linhas": s["lines"], "running": True,
                                           "parcial": s.get("parcial") or "",
                                           "parcial_md": _md_basico(s.get("parcial") or "")})
    res = s.get("result") or {}
    sid = request.cookies.get(SESSAO_COOKIE)
    resposta = str(res.get("answer")
                   or (s.get("error") or "").strip()
                   or "o serviço não devolveu resposta — tente enviar de novo")
    # salva a resposta na sessão (idempotente: não duplica a última igual)
    try:
        anterior = sessions.get_session(sid) or {}
        bruto = anterior.get("raw") or []
        ultima = bruto[-1] if bruto else {}
        if not (ultima.get("role") == "assistant" and ultima.get("content") == resposta):
            # 🧠 SÍNTESE DO RACIOCÍNIO (pedido do dono: "sintetizado, usa o
            # embedding"): as linhas cruas viram PASSOS semanticamente
            # coerentes (embedding bge-m3, agrupa consecutivas parecidas) —
            # nada se perde, as linhas completas ficam a um clique
            try:
                from core import sintese
                passos = sintese.sintetizar([l for l in s["lines"]])
            except Exception:
                passos = None
            bruto.append({"role": "assistant", "content": resposta,
                          "tokens": res.get("tokens"),
                          "tok_s": res.get("tok_s"),
                          "duracao_s": res.get("duracao_s"),
                          "modelo": res.get("model"),
                          "docs": res.get("docs") or [],
                          "cache": res.get("cache") or None,
                          "pensamentos": passos or [l for l in s["lines"]],
                          "pensamentos_sintetizados": bool(passos)})
            # 🏷️ TÍTULO SEMÂNTICO ADIADO: nasceu "(sem título)" no envio
            # (o POST nunca espera embed) — aqui, na CONCLUSÃO da 1ª
            # resposta, o embedding roda com calma (o usuário já lê a
            # resposta; embed quente do job, ~centenas de ms)
            titulo_calc = None
            if (anterior.get("titulo") or "") in ("", "(sem título)"):
                try:
                    primeira = next((m.get("content", "") for m in bruto
                                      if m.get("role") == "user"), "")
                    if primeira:
                        titulo_calc = sessions.titulo_semantico(
                            primeira, anterior.get("colecoes"))
                except Exception:
                    titulo_calc = None
            sessions.save_session(bruto, sid=sid, owner=anterior.get("owner", ""),
                                  titulo=titulo_calc, modo=res.get("mode", "hibrido"),
                                  aprovacoes=anterior.get("aprovacoes", {}),
                                  raw=bruto, job_ativo=None)  # concluiu: limpa
            if passos:
                s["lines_sintese"] = passos   # o partial final renderiza os passos
    except Exception:
        pass
    ctx = {"request": request, "job": job, "linhas": s["lines"],
           "passos": s.get("lines_sintese"),
           "running": False, "resposta": resposta,
           "html": _md_basico(resposta),
           "tokens": res.get("tokens"), "modelo": res.get("model"),
           "tok_s": res.get("tok_s"), "duracao_s": res.get("duracao_s"),
           "cache": res.get("cache") or None,
           "busca": res.get("busca"),
           "docs": res.get("docs") or []}
    return TEMPLATES.TemplateResponse(request, "_chat_fim.html", ctx)


def _iniciar_midia(request: Request, prompt: str, tipo: str, colecoes: list[str],
                   referencia: str = "", modelo: str = "", duracao: str = ""):
    """Geração de mídia pelo chat: cria a tarefa via a rota oficial (conjunto
    de modelos sobe por trás) e devolve o partial do job.

    Tipos: imagem (t2i) · video (t2v) · gif (t2v+gif) e as modalidades COM
    REFERÊNCIA (📎 no painel): i2t (multimodal descreve/analisa), i2v (a
    imagem anexa vira o 1º quadro do vídeo) e i2g (idem, em GIF). O modelo
    do combobox vale para imagem (Flux) e vídeo (Wan2.1/2.2).

    DURAÇÃO (`duracao` em segundos, seletor do composer — pedido do dono):
    o Wan gera a ~16 fps → frames = s×16+1 (2s=33 · 3s=49 · 5s=81 ·
    8s=129; spec core/specs/midia_duracao.md). GIF segue 17 frames (loop).

    HISTÓRIA da sessão (pedido: "usar o contexto para imagens/vídeos —
    contar a história de cada sessão"): as últimas trocas da conversa
    viajam como continuidade narrativa no prompt da difusão.
    """
    _COM_REF = {"i2t", "i2v", "i2g"}
    if tipo in _COM_REF and not referencia:
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "erro", "job": "erro",
             "rotulo": f"gerar {tipo}", "linhas": [], "running": False,
             "erro": f"'{tipo}' precisa de uma imagem de referência — "
                     "clique em 📎 incluir no contexto numa imagem do "
                     "painel e tente de novo"})
    # 🚫 NEGATIVE PROMPT no próprio pedido (pedido do dono): "negativo: X"
    # no texto vira o -n da difusão (condiz com a solicitação); sem a
    # cláusula vale o padrão do modelo. Spec core/specs/midia_duracao.md.
    negativo = ""
    m_neg = re.search(r"negativo\s*[:=]\s*(.+?)(?:\.$|$)", prompt,
                      re.IGNORECASE | re.DOTALL)
    if m_neg and tipo in ("imagem", "video", "gif", "i2v", "i2g"):
        negativo = m_neg.group(1).strip()[:300]
        prompt = re.sub(r"[,.;]?\s*negativo\s*[:=]\s*.+$", "", prompt,
                        flags=re.IGNORECASE | re.DOTALL).strip(" ,.")
    try:
        ref_arquivo = _resolver_arquivo(Path(referencia).name) if referencia else None
        mod = {"imagem": "t2i", "video": "t2v", "gif": "t2v",
               "i2t": "i2t", "i2v": "i2v", "i2g": "i2v"}.get(tipo, "t2i")
        params: dict = {}
        if tipo in ("gif", "i2g"):
            # ⏱️ duração do GIF (pedido do dono): o seletor manda os FRAMES
            # (17/33/49 → ~1,5/3/4 s a 12 fps no loop — spec midia_duracao)
            try:
                frames_gif = int(duracao) if duracao.isdigit() else 17
            except ValueError:
                frames_gif = 17
            params = {"gif": True, "frames": max(9, min(81, frames_gif)),
                      "duracao_s": round(frames_gif / 12, 1)}
        elif tipo in ("video", "i2v"):
            # DURAÇÃO escolhida no composer → frames (16 fps; teto 8 s:
            # tempo/VRAM de difusão cresce ~linear e o Wan mantém coerência
            # em cena ÚNICA — acima disso degrade rápido)
            try:
                seg = max(2, min(8, int(float(duracao or "2"))))
            except ValueError:
                seg = 2
            params["frames"] = seg * 16 + 1
            params["duracao_s"] = seg
        if tipo in ("imagem", "video", "i2v") and modelo:
            params["modelo"] = modelo
        if negativo:
            params["negativo"] = negativo
        if tipo == "i2t" and prompt:
            params["pergunta"] = prompt   # pergunta ESPECÍFICA sobre a imagem
        # 📖 HISTÓRIA da sessão → continuidade narrativa (pedido do dono):
        # as últimas trocas compactadas viajam com o prompt da difusão para
        # a cena CONTINUAR a conversa (mesma personagem/ambiente/enredo)
        try:
            _s = sessions.get_session(request.cookies.get(SESSAO_COOKIE)) or {}
            _trocas = []
            for m in (_s.get("raw") or [])[-6:]:
                if not m.get("content"):
                    continue
                quem = "usuário" if m.get("role") == "user" else "assistente"
                _trocas.append(f"{quem}: {str(m['content'])[:140].rstrip()}")
            if _trocas:
                params["historia"] = "\n".join(_trocas)[:600]
        except Exception:
            pass
        # em CONTAINER a referência resolvida aqui (disco da VPS) não existe
        # no host — o CONTEÚDO viaja em base64 e o agente grava em entrada/
        ref_b64 = None
        if ref_arquivo and config.EM_CONTAINER:
            try:
                import base64 as _b64
                ref_b64 = _b64.b64encode(
                    Path(ref_arquivo).read_bytes()).decode("ascii")
            except Exception:
                ref_b64 = None
        rotulos = {"i2t": "analisar imagem (multimodal)",
                   "i2v": "gerar vídeo (a partir da imagem anexa)",
                   "i2g": "gerar gif (a partir da imagem anexa)"}
        r = criar_tarefa(TarefaIn(modalidade=mod, texto=prompt, params=params,
                                  arquivo=(Path(ref_arquivo).name
                                           if ref_arquivo else (referencia or None)),
                                  arquivo_b64=ref_b64),
                         request)
    except HTTPException as e:
        detalhe = e.detail if isinstance(e.detail, str) else str(e.detail)
        if "agente do host" in detalhe:
            detalhe += ("\n\nComo resolver (a GPU está na SUA estação):\n"
                        "1. Na estação: python -X utf8 -m api.agente_host (deixe rodando)\n"
                        "2. Tunel Cloudflare: agente.<seu-dominio> → http://localhost:8010\n"
                        "   (ingress do túnel local; reinicie o cloudflared)\n"
                        "3. No .env da VPS: AGENTE_HOST_URL=https://agente.<seu-dominio>\n"
                        "4. Na VPS: docker compose up -d --force-recreate api")
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "tarefa", "job": "erro",
             "rotulo": "geração de mídia", "linhas": [], "running": False,
             "erro": detalhe})
    # 💾 SESSÃO: a solicitação de mídia FAZ PARTE da conversa (pedido do
    # dono — "sessões multimodais não estão sendo salvas"). A pergunta é
    # gravada AGORA; o RESULTADO entra via hx_job quando a tarefa conclui
    # (_TAREFA_CHAT casa job↔sessão; memória da API).
    stub = JSONResponse({})
    sid = _sessao_id(request, stub, criar=True)
    try:
        anterior = sessions.get_session(sid) or {}
        bruto = anterior.get("raw") or []
        bruto.append({"role": "user", "content": prompt})
        sessions.save_session(bruto, sid=sid, owner=anterior.get("owner", ""),
                              titulo=None, modo="midia", colecoes=colecoes,
                              aprovacoes=anterior.get("aprovacoes", {}), raw=bruto,
                              job_ativo={"kind": "tarefa", "job": r["tarefa"],
                                         "rotulo": rotulos.get(tipo, f"gerar {tipo}")})
        _TAREFA_CHAT[r["tarefa"]] = {"sid": sid, "pergunta": prompt, "tipo": tipo}
        # registra o resultado MESMO SEM polling da página (navegador
        # fechou no meio da geração — a sessão não pode perder a mídia)
        threading.Thread(target=_finalizar_midia_fundo,
                         args=(r["tarefa"],), daemon=True).start()
    except Exception as e:
        print(f"⚠️ sessão multimodal (pergunta): {e}")
    # BOLHA do usuário + card do job (partials ÚNICOS — sem a bolha a
    # pergunta SUMIA ao pedir mídia; pedido do dono). Resposta única com
    # o cookie da sessão criada no stub propagado. MODO OTIMISTA: o
    # browser já mostrou a bolha (header X-Otimista) — vem SÓ o card.
    _bolha = "" if request.headers.get("x-otimista") == "1" else \
        TEMPLATES.get_template("_bolha_usuario.html").render(pergunta=prompt)
    corpo = (_bolha
             + TEMPLATES.get_template("_job.html").render(
                 request=request, kind="tarefa", job=r["tarefa"],
                 rotulo=rotulos.get(tipo, f"gerar {tipo}"),
                 linhas=[], running=True))
    resposta = HTMLResponse(corpo)
    _sc = stub.headers.get("set-cookie", "")
    if _sc.startswith(SESSAO_COOKIE + "="):
        _sid = _sc.split("=", 1)[1].split(";", 1)[0]
        resposta.set_cookie(SESSAO_COOKIE, _sid, max_age=30 * 86400,
                            httponly=True, samesite="lax")
    return resposta


@app.post("/hx/nova")
def hx_nova():
    resp = JSONResponse({})
    resp.delete_cookie(SESSAO_COOKIE)
    resp.headers["HX-Redirect"] = "/"
    return resp


class SandboxIn(BaseModel):
    """Teste de código na sandbox: TODO o contexto (todos os arquivos da
    conversa/da RESPOSTA) para cross-file compilar. `principal` vazio =
    o SERVIDOR escolhe o entry point (Program.cs > Main > top-level >
    site > primeiro executável) — pedido do dono: "testar a resposta
    completa"."""
    principal: str = ""
    arquivos: list[dict] = []
    timeout: int = 300


@app.post("/api/sandbox/testar")
def sandbox_testar(body: SandboxIn, request: Request):
    """Roda o código do painel na sandbox — como JOB (pedido do dono): o
    teste com `pip install` passa dos 100 s da borda do Cloudflare e o
    fetch síncrono morria em HTML de 502 ("Unexpected token '<'"). O modal
    faz polling em /api/sandbox/status/{job} com as linhas ao vivo."""
    _usuario(request)
    from core import sandbox
    principal = (body.principal or "").strip() or \
        sandbox.escolher_principal(body.arquivos)
    body.principal = principal

    def _fabricar(payload: dict):
        jid = payload["job"]

        def rodar():
            _sbx.log(jid, f"⚙️ teste de {payload['principal']} "
                          f"(+{max(0, len(payload.get('arquivos', [])) - 1)} arquivo(s) "
                          "de contexto)")
            try:
                r = sandbox.testar(payload.get("arquivos", []),
                                   payload["principal"],
                                   payload.get("timeout", 300),
                                   log=lambda m, g="": _sbx.log(jid, m))
                _sbx.concluir(jid, result=r)
            except Exception as e:
                _sbx.concluir(jid, error=str(e)[:400])
        return rodar

    job = _sbx.novo_id()
    _despachar(_fabricar, "sandbox",
               {"principal": principal, "arquivos": body.arquivos,
                "timeout": body.timeout, "job": job}, _sbx)
    return {"job": job}


@app.get("/api/sandbox/linguagens")
def sandbox_linguagens(request: Request):
    """Versões instaladas na sandbox (para a UI avisar o que dá pra testar)."""
    _usuario(request)
    from core import sandbox
    try:
        return {"ok": True, "linguagens": sandbox.linguagens()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/sandbox/ver/{arquivo:path}")
def sandbox_ver(arquivo: str, request: Request, k: str = ""):
    """PREVIEW do arquivo do teste (HTML e afins): iframe do modal de teste
    e o subdomínio sandbox.disroy.org. Auth: cookie da sessão OU token
    curto (?k= — cookie não atravessa subdomínios; HMAC do AUTH_SECRET,
    15 min). Faz STREAM do agente da sandbox (mesma rede do compose)."""
    from core import sandbox as _sb
    if not (_usuario_ok(request) or (k and _sb.token_preview_ok(arquivo, k))):
        raise HTTPException(status_code=401, detail="faça login ou use o link do teste")
    try:
        with httpx.stream("GET", f"{_sb._base()}/ver/{arquivo}",
                          headers=_sb._headers(), timeout=30) as r:
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code,
                                    detail="arquivo não encontrado no teste")
            return Response(
                content=b"".join(r.iter_bytes()),
                media_type=r.headers.get("Content-Type", "text/html"),
                headers={"Content-Security-Policy":
                         "default-src 'self' data:; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"sandbox fora do ar ({str(e)[:60]})")


async def _proxy_app_api(porta: int, request: Request,
                         resto: str = "/", chave: str = "") -> Response:
    """Repassa o pedido ao app VIVO na sandbox (mesma rede do compose).

    Segue redirect? NÃO: o Location volta ao BROWSER (mesma chave na URL
    base — /sandbox/app/{chave}/login continua válido); redirects absolutos
    (/login, /static/x.css) que caem fora da chave são RESGATADOS pelo
    middleware 404-por-Referer."""
    from core import sandbox as _sb
    url = f"{_sb._base()}/app/{porta}{resto}"
    if request.url.query:
        url += "?" + request.url.query
    corpo = await request.body()
    try:
        r = httpx.request(request.method, url, content=corpo or None,
                          headers=({"Content-Type": request.headers["content-type"]}
                                   if corpo and request.headers.get("content-type")
                                   else {}),
                          timeout=30, follow_redirects=False)
        # no-store: o Cloudflare faz NEGATIVE caching (404 do app fica ~3 min
        # no edge — o app é TEMPORÁRIO, nada dele pode ser cacheado)
        # 🧭 404 do APP (navegação HTML): página amigável em vez do 404 cru
        # do Flask/Werkzeug (bug real do dono: rota inexistente no app
        # GERADO — aba/link morto no código do modelo — vazava o "Not
        # Found… check your spelling" sem contexto). JSON 404 passa reto
        # (apps de API seguem com a semântica deles).
        # ⚡ 404 na RAIZ: app gerado às vezes nasce SEM rota "/" (só
        # /api/...) — antes de mostrar a página morta, tenta o /docs
        # (Swagger automático do FastAPI): respondendo, o browser é
        # REDIRECIONADO para a documentação interativa do app.
        if r.status_code == 404 and resto in ("/", "") and chave:
            try:
                d = httpx.get(f"{_sb._base()}/app/{porta}/docs", timeout=5)
                if d.status_code == 200:
                    print(f"⚡ app :{porta} sem rota / — redirecionando ao "
                          "/docs (Swagger) do app")
                    return RedirectResponse(f"/sandbox/app/{chave}/docs",
                                            status_code=302)
            except Exception:
                pass
        if (r.status_code == 404
                and "text/html" in (r.headers.get("Content-Type") or "")):
            home = f"/sandbox/app/{chave}/" if chave else ".."
            corpo = ("<!doctype html><html lang=pt-BR><head><meta charset=utf-8>"
                     "<title>rota não existe neste app</title><style>"
                     "body{font-family:Segoe UI,sans-serif;background:#f6f7f9;"
                     "color:#1a1d21;display:grid;place-items:center;min-height:100vh;"
                     "margin:0}.c{max-width:32rem;margin:1rem;padding:2rem;background:#fff;"
                     "border:1px solid #e2e8f0;border-radius:14px;text-align:center}"
                     "a{display:inline-block;margin-top:1rem;padding:.55rem 1.2rem;"
                     "border-radius:10px;background:#2563eb;color:#fff;"
                     "text-decoration:none;font-weight:600}</style></head><body>"
                     "<div class=c><div style=font-size:2.4rem>🧭</div>"
                     "<h2>esta rota não existe no aplicativo</h2>"
                     "<p>O app de teste não tem a página que você abriu — "
                     "geralmente um <b>link/aba morto</b> no código gerado. "
                     "Volte à home do app ou peça no chat para corrigir a "
                     "rota.</p>"
                     f"<a href='{home}'>← home do aplicativo</a></div></body></html>")
            print(f"🧭 app :{porta} 404 em {resto[:80]} (rota ausente no app "
                  "gerado — página amigável devolvida)")
            return Response(content=corpo, status_code=404,
                            media_type="text/html",
                            headers={"Cache-Control": "no-store"})
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("Content-Type", "text/html"),
                        headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        return _pag_fora(request, "app_caiu")


def _pag_fora(request: Request, caso: str) -> HTMLResponse:
    """Página AMIGÁVEL em vez de JSON/404 cru (bug real do dono: "deu erro
    404 em outra aba" sem explicação). Dois casos: link expirado/substituído
    e app fora do ar — ambos com o que fazer."""
    casos = {
        "expirou": ("⏳", "este link expirou",
                    "Os aplicativos de teste ficam hospedados por <b>~30 "
                    "minutos</b>. O tempo deste acabou — ou um teste novo do "
                    "mesmo projeto o substituiu.", 410),
        "app_caiu": ("🔌", "este app saiu do ar",
                     "O servidor do aplicativo parou de responder dentro da "
                     "hospedagem temporária — pode ter caído sozinho ou o "
                     "tempo acabou.", 502),
        "raiz": ("📦", "sandbox do RagAroy",
                 "Aqui aparecem os <b>aplicativos temporários</b> gerados "
                 "pelos testes de código — cada um tem um link próprio, "
                 "válido por ~30 minutos.", 200),
    }
    emo, titulo, detalhe, status = casos.get(caso, casos["app_caiu"])
    return TEMPLATES.TemplateResponse(
        request, "_sandbox_fora.html",
        {"titulo": titulo, "emo": emo, "detalhe": detalhe,
         "extras": "Abra a conversa no RagAroy e rode o <b>▶ testar "
                   "resposta</b> de novo para gerar um link novo."
         if caso != "raiz" else ""},
        status_code=status,
        headers={"Cache-Control": "no-store"})


@app.get("/sandbox", include_in_schema=False)
@app.get("/sandbox/", include_in_schema=False)
def sandbox_raiz(request: Request):
    """Raiz do subdomínio sandbox.*: explica o que vive ali (antes: 404 cru
    do Cloudflare — bug real do dono)."""
    return _pag_fora(request, "raiz")


@app.api_route("/sandbox/app/{chave}/{path:path}",
               methods=["GET", "POST", "HEAD"])
async def sandbox_app(chave: str, path: str, request: Request):
    """APP VIVO do teste de site — HOSPEDADO TEMPORARIAMENTE (pedido do
    dono): o link público {chave} = porta+expiração assinadas (HMAC do
    AUTH_SECRET, ~30 min). Sem login: quem TEM o link acessa."""
    from core import sandbox as _sb
    porta = _sb.chave_app_ok(chave)
    if not porta:
        return _pag_fora(request, "expirou")
    return await _proxy_app_api(porta, request, "/" + (path or ""), chave=chave)


@app.post("/api/sandbox/limpar")
def sandbox_limpar(request: Request):
    """Limpa os ARQUIVOS do último teste (o container segue de pé — rápido).
    Chamado ao FECHAR o modal de teste (pedido do dono)."""
    _usuario(request)
    from core import sandbox
    return {"ok": sandbox.limpar()}


@app.post("/hx/prompt-melhorar")
@app.post("/hx/prompt-midia")   # compat: páginas abertas no deploy ainda chamam
def hx_prompt_melhorar(request: Request, ideia: str = Form(""),
                        tipo: str = Form(""), referencia: str = Form("")):
    """✨ do composer: a LLM reescreve o RASCUNHO na melhor forma (spec
    prompt_melhoria.md — universal: pergunta, código, instrução ou mídia).
    `tipo` é DICA opcional (vem do seletor de mídia quando ativo).
    CONTEXTO = TODAS as mensagens enviadas pelo USUÁRIO na conversa
    (respostas do assistente NÃO entram) + a referência selecionada."""
    _usuario(request)
    from core import prompt as _prompt
    tipo_dica = (tipo or "").strip()
    # CONTEXTO para TUDO (pedido do dono: "o prompt não está respeitando o
    # contexto nem o que está escrito na caixa"): as mensagens do usuário
    # da SESSÃO são o FIO da melhoria — inclusive de mídia (a cena continua
    # a conversa); a spec manda PRESERVAR todo o conteúdo factual do
    # rascunho (melhorar ≠ substituir).
    contexto = ""
    try:
        dados = sessions.get_session(request.cookies.get(SESSAO_COOKIE)) or {}
        # FIO COMPLETO: mensagens do usuário + as 2 últimas respostas do
        # assistente (truncadas) — sem elas o ✨ perdia o que a conversa
        # JÁ estabeleceu (personagens, formato, código em andamento)
        trocas = []
        for m in (dados.get("raw") or [])[-10:]:
            if not m.get("content"):
                continue
            if m.get("role") == "user":
                trocas.append(f"usuário: {str(m['content'])[:400]}")
            else:
                trocas.append(f"assistente: {str(m['content'])[:200]}"
                              + ("…" if len(str(m['content'])) > 200 else ""))
        contexto = "\n".join(trocas)
    except Exception:
        pass
    ref = (referencia or "").strip()
    if ref:
        contexto += f"\nREFERÊNCIA SELECIONADA no painel: {ref}"
    return PlainTextResponse(_prompt.melhorar(ideia, tipo, contexto))


# ═══════════════ CONVERSAS (múltiplas sessões do chat) ═══════════════

@app.get("/hx/conversa/copy")
def hx_conversa_copy(request: Request):
    """Conversa INTEIRA em markdown (pergunta, resposta, tokens, raciocínio
    de cada mensagem) — o botão COPIAR CONVERSA do chat cola isto na
    área de transferência, com o contexto completo."""
    _usuario(request)
    bruto = (sessions.get_session(request.cookies.get(SESSAO_COOKIE))
             or {}).get("raw") or []
    linhas = []
    for m in bruto:
        if m.get("role") == "user":
            linhas.append("## você")
        else:
            mod = f" · {m['modelo']}" if m.get("modelo") else ""
            linhas.append(f"## assistente{mod}")
        linhas.append(m.get("content") or "")
        tk = m.get("tokens") or {}
        if tk:
            linhas.append(f"_🪙 🔻{tk.get('entrada', 0)} · 🔺{tk.get('saida', 0)}"
                          f" · {tk.get('chamadas', 0)} chamada(s)_")
        for l in (m.get("pensamentos") or []):
            linhas.append(f"> {l.get('msg') or ''}")
        linhas.append("")
    return PlainTextResponse("\n".join(linhas))


@app.get("/hx/conversas")
def hx_conversas(request: Request):
    """Lista de conversas (partial HTMX para o drawer do chat)."""
    usuario = _usuario(request)
    try:
        convs = sessions.list_sessions(owner=usuario)
    except Exception:
        convs = []
    return TEMPLATES.TemplateResponse(request, "_conversas.html",
                                      {"request": request, "conversas": convs,
                                       "apagando": _apagando_do_usuario(usuario),
                                       "atual": request.cookies.get(SESSAO_COOKIE)})


def _job_ativo_ctx(sid: str) -> dict:
    """Contexto do JOB EM CURSO da sessão (para o _palco re-renderizar o
    polling após um refresh — pedido do dono: nada se perde). Job morto
    (concluído sem poll/registro evaporado) é LIMPO da sessão na hora."""
    try:
        ja = (sessions.get_session(sid) or {}).get("job_ativo")
    except Exception:
        return {}
    if not ja or not ja.get("job"):
        return {}
    try:
        if ja.get("kind") == "chat":
            s = _query.status(ja["job"], 0, "")
        else:
            s = tarefas.status(ja["job"], 0)
            if s is None and config.EM_CONTAINER:
                import httpx as _hx
                rr = _hx.get(f"{modelos._agente_host()}/tarefas/status/{ja['job']}",
                             params={"cursor": 0}, timeout=6,
                             headers=modelos._agente_headers())
                if rr.status_code == 200:
                    s = rr.json()
    except Exception:
        s = None
    if not s or not s.get("running"):
        # terminou sem ninguém pollar (ou registro foi embora): limpa e segue
        try:
            an = sessions.get_session(sid) or {}
            sessions.save_session(an.get("raw") or [], sid=sid,
                                  owner=an.get("owner", ""),
                                  aprovacoes=an.get("aprovacoes", {}),
                                  raw=an.get("raw"), job_ativo=None)
        except Exception:
            pass
        return {}
    linhas = _linhas_visual(s.get("lines") or [])
    if ja.get("kind") == "chat":
        return {"job_ativo": {"kind": "chat", "job": ja["job"]},
                "job": ja["job"], "linhas": linhas, "running": True,
                "parcial": s.get("parcial") or "",
                "parcial_md": _md_basico(s.get("parcial") or "")}
    return {"job_ativo": {"kind": "tarefa", "job": ja["job"]},
            "kind": "tarefa", "job": ja["job"],
            "rotulo": ja.get("rotulo") or "geração",
            "linhas": linhas, "running": True,
            "progresso": (round((s.get("progresso") or 0) * 100)
                          if isinstance(s.get("progresso"), (int, float)) else None),
            "etapa_atual": s.get("etapa"), "eta_s": s.get("eta_s"),
            "erro": None, "resumo_texto": "", "segundos": None,
            "preview_pid": None, "resultado_midia": None}


def _palco_response(request: Request, sid: str | None, usuario: str):
    """Partial da conversa (swap no #palco, SEM reload) com o cookie já
    setado na resposta."""
    msgs = _msgs_da_sessao(sid, usuario)
    ctx = {"request": request, "mensagens": msgs, **_job_ativo_ctx(sid)}
    resp = TEMPLATES.TemplateResponse(request, "_palco.html", ctx)
    if sid:
        resp.set_cookie(SESSAO_COOKIE, sid, max_age=90 * 24 * 3600,
                        httponly=True, samesite="lax")
    return resp


@app.post("/hx/conversa/nova")
def hx_conversa_nova(request: Request):
    """Nova conversa SEM reload: troca só o palco (cookie sai, palco vazio)."""
    usuario = _usuario(request)
    resp = _palco_response(request, None, usuario)
    resp.delete_cookie(SESSAO_COOKIE)
    return resp


@app.post("/hx/conversa/{sid}/abrir")
def hx_conversa_abrir(sid: str, request: Request):
    """Troca para a conversa SEM reload: devolve o palco dela (HTMX faz o
    swap) e seta o cookie. Owner conferido."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", sid):
        return HTMLResponse("id inválido", status_code=400)
    usuario = _usuario(request)
    dados = sessions.get_session(sid) or {}
    if dados.get("owner") and dados["owner"] != usuario:
        return HTMLResponse("conversa de outro usuário", status_code=403)
    return _palco_response(request, sid, usuario)


@app.delete("/hx/conversa/{sid}")
def hx_conversa_apagar(sid: str, request: Request):
    """Apaga a conversa (owner conferido) COMO JOB NA FILA (RabbitMQ):
    a resposta é IMEDIATA — o item entra em estado "⏳ apagando…" e a lista
    segue polando até o job concluir (mídias em disco + cache + sessão).
    Antes a exclusão era SÍNCRONA no request: a lista travava entre uma
    exclusão e outra (e um engasgo da API virava 502 no DELETE).
    Quando a apagada era a ATIVA, o palco troca por vazio via swap OOB na
    mesma resposta (o usuário não fica olhando uma conversa condenada)."""
    usuario = _usuario(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", sid):
        return HTMLResponse("id inválido", status_code=400)
    dados = sessions.get_session(sid) or {}
    if dados.get("owner") and dados["owner"] != usuario:
        return HTMLResponse("<p class='erro-texto'>conversa de outro usuário</p>",
                            status_code=403)
    if sid not in _APAGANDO:  # idempotente: 2º clique não duplica job
        job = _conv.novo_id()
        _APAGANDO[sid] = {"job": job, "owner": usuario}

        def fabricar(p: dict):
            jid, alvo = p["job"], p["sid"]

            def rodar():
                try:
                    _conv.log(jid, f"🗑️ apagando a conversa "
                                   f"“{(p.get('titulo') or alvo[:8])[:60]}”…")
                    dados2 = sessions.get_session(alvo) or {}
                    apagados = []
                    for m in (dados2.get("raw") or []):
                        mid = m.get("midia") or {}
                        if mid.get("arquivo") and mid.get("pasta"):
                            alvo_arq = Path(mid["pasta"]) / Path(mid["arquivo"]).name
                            if alvo_arq.is_file():
                                alvo_arq.unlink()
                                apagados.append(alvo_arq.name)
                    if apagados:
                        _conv.log(jid, f"🧹 {len(apagados)} mídia(s) apagada(s) do disco")
                    try:
                        n = cache.limpar_sid(alvo)
                        if n:
                            _conv.log(jid, f"🧹 {n} entrada(s) de cache desta sessão")
                    except Exception as e:
                        _conv.log(jid, f"⚠️ cache: {str(e)[:100]}")
                    sessions.delete_session(alvo)
                    _conv.log(jid, "✓ conversa apagada")
                    _conv.concluir(jid, result={"sid": alvo,
                                                "midias": len(apagados)})
                except Exception as e:
                    _conv.log(jid, f"✕ falhou: {str(e)[:160]}")
                    _conv.concluir(jid, error=str(e))
                finally:
                    _APAGANDO.pop(alvo, None)
            return rodar

        _despachar(fabricar, "conversa_apagar",
                   {"job": job, "sid": sid,
                    "titulo": (dados.get("titulo") or "")[:60]}, _conv)
    ativa = request.cookies.get(SESSAO_COOKIE) == sid
    convs = sessions.list_sessions(owner=usuario)
    corpo = TEMPLATES.get_template("_conversas.html").render(
        conversas=convs, apagando=_apagando_do_usuario(usuario),
        atual=None if ativa else sid)
    if ativa:
        # palco vazio no MESMO swap (OOB) + cookie limpo — na HORA do clique
        palco = TEMPLATES.get_template("_palco.html").render(mensagens=[])
        corpo += ('\n<div hx-swap-oob="innerHTML:#palco">' + palco + "</div>")
    resp = HTMLResponse(corpo)
    if ativa:
        resp.delete_cookie(SESSAO_COOKIE)
    return resp


@app.post("/hx/voz")
def hx_voz(request: Request, audio: UploadFile = File(...)):
    """Áudio (arquivo) → texto no campo (whisper local)."""
    _usuario(request)
    try:
        dados = audio.file.read()
        if not dados:
            raise ValueError("áudio vazio")
        texto = voz.transcrever_bytes(dados)
        if not (texto or "").strip():
            raise ValueError("nada transcrito — só silêncio?")
        return HTMLResponse(
            f'<textarea id="pergunta" name="question" required>'
            f'{texto.strip()}</textarea>')
    except Exception as e:
        return HTMLResponse(f'<p class="erro-texto">falha na transcrição: {e}</p>',
                            status_code=200)


@app.get("/hx/tts")
def hx_tts(texto: str, request: Request):
    _usuario(request)
    wav = voz.falar_bytes(texto[:2000])
    return Response(wav, media_type="audio/wav")


@app.get("/biblioteca")
def pagina_biblioteca(request: Request):
    ctx = _paginas_ctx(request, "biblioteca")
    try:
        ctx["colecoes"] = collections() or []
    except Exception:
        ctx["colecoes"] = []
    # ═══ JOBS ATIVOS voltam com a página (a pesquisa não "some" ao navegar):
    # qualquer pesquisa/preview em andamento é re-injetada no topo com o
    # partial de polling — o estado vive no registry, não no DOM. ═══
    ativos = []
    for reg, kind, rotulo in ((_pesquisa, "pesquisa", "pesquisa na web"),
                              (_preview, "preview", "revisão de aquisição"),
                              (_ingest, "ingest", "ingestão"),
                              (_seed, "seed", "coleção por assunto")):
        try:
            with reg.lock:
                for jid, st in (reg.jobs or {}).items():
                    if isinstance(st, dict) and st.get("running"):
                        ativos.append({"kind": kind, "job": jid,
                                       "rotulo": f"{rotulo} · em andamento"})
        except Exception:
            pass
    ctx["jobs_ativos"] = ativos
    return TEMPLATES.TemplateResponse(request, "biblioteca.html", ctx)


@app.get("/dashboard")
def pagina_dashboard(request: Request):
    ctx = _paginas_ctx(request, "dashboard")
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=10,
                              check_compatibility=False)
        scan = _scan_collections(client) or {}
    except Exception:
        scan = {}
    uso = contadores.totais() or {}
    total = uso.get("total") or {}
    por = uso.get("por_servico") or {}
    try:
        cache_info = cache.info()
    except Exception:
        cache_info = {"online": False, "entradas": 0}
    try:
        fila_info = fila.estado()
    except Exception:
        fila_info = {"online": False, "pendentes": 0, "mortas": 0}
    ctx["kpis"] = {
        "colecoes": len(scan), "grupos": 0,
        "pontos": sum((v or {}).get("points") or 0 for v in scan.values()),
        "tokens_in": total.get("entrada", 0), "tokens_out": total.get("saida", 0),
        "tokens_total": total.get("entrada", 0) + total.get("saida", 0),
        "chamadas": total.get("chamadas", 0), "por_servico": por,
        "cache": f"{cache_info.get('entradas', 0)}" if cache_info.get("online") else "offline",
        "fila": (f"{fila_info.get('pendentes', 0)}/{fila_info.get('mortas', 0)}"
                 if fila_info.get("online") else "threads"),
    }
    ctx["execucoes"] = historico.ultimos(None, 40)
    # ── INFRA detalhada (pedido do dono: "quero saber como está o qdrant,
    # redis, rabbitmq"): coleções do Qdrant com pontos/dimensão e o estado
    # do cache Redis — o RabbitMQ tem seção própria AO VIVO abaixo ──
    ctx["colecoes_detalhe"] = sorted(
        ((nome, v) for nome, v in (scan or {}).items()),
        key=lambda kv: -(kv[1].get("points") or 0))
    ctx["cache_info"] = cache_info
    # ── modelos de linguagem: uso REAL por modelo (telemetria) + GB do
    # GGUF + quem está servindo agora + VRAM corrente ──
    from core import estatisticas
    try:
        uso_modelo = estatisticas.por_modelo() or {}
    except Exception:
        uso_modelo = {}
    gb_por_alias = {}
    try:
        for m in modelos.listar():
            gb_por_alias[modelos.normalizar(m["nome"])] = m.get("gb")
    except Exception:
        pass
    servindo = modelos.servido(modelos.CHAT_PORTA)
    vram = None
    if config.EM_CONTAINER:
        try:
            vram = modelos._chamar_agente("/saude", timeout=4).get("vram_mi")
        except Exception:
            pass
    else:
        vram = modelos._vram_uso_mi()
    modelos_llm = []
    for nome, u in sorted(uso_modelo.items(),
                          key=lambda kv: -kv[1]["chamadas"]):
        modelos_llm.append({
            "nome": nome, **u,
            "gb": gb_por_alias.get(modelos.normalizar(nome)),
            "ativo": modelos.normalizar(nome) == modelos.normalizar(servindo),
        })
    # 🧩 UNIÃO (pedido do dono: "cadê os outros modelos?"): TODO modelo
    # conhecido aparece — com uso quando a telemetria registrou, "sem uso"
    # quando ainda não. Agregação por nome_curto (sem sufixo de quant).
    from core.estatisticas import nome_curto as _nc
    _zero = {"chamadas": 0, "entrada": 0, "saida": 0, "segundos": 0.0,
             "tok_s": None, "ultima": ""}
    _ja = {_nc(m["nome"]) for m in modelos_llm}   # telemetria + união (sem dup)
    for alias in list(modelos.REGISTRO) + ["wan2.1-t2v-1.3b", "wan2.2-ti2v-5b",
                                           "flux1-schnell", "flux1-dev"]:
        cat = modelos.REGISTRO.get(alias, ("", "midia"))[1]
        if cat not in ("chat", "video", "imagem") or _nc(alias) in _ja:
            continue
        _ja.add(_nc(alias))
        modelos_llm.append({
            "nome": alias, **_zero,
            "gb": gb_por_alias.get(modelos.normalizar(alias)),
            "ativo": modelos.normalizar(alias) == modelos.normalizar(servindo),
            "sem_uso": True,
        })
    ctx["modelos_llm"] = modelos_llm
    ctx["vram_mi"] = vram
    ctx["servindo"] = servindo
    try:
        ctx["embed_resumo"] = estatisticas.embedding_resumo()
        ctx["cache_stats"] = estatisticas.cache_resumo()
    except Exception:
        ctx["embed_resumo"] = {"chamadas": 0, "documentos": 0, "segundos": 0}
        ctx["cache_stats"] = {"hits": 0, "stores": 0, "misses": 0}
    # fila Rabbit (explorada em tempo real no dashboard)
    try:
        ctx["fila_info"] = fila.estado()
    except Exception:
        ctx["fila_info"] = {"online": False}
    return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/hx/fila")
def hx_fila(request: Request):
    """Fila RabbitMQ em TEMPO REAL (partial com polling): profundidade,
    DLQ, worker, jobs por tipo — e o estado COMPLETO do broker (Management
    API): por fila consumidores/taxas, PEEK da DLQ (mensagens mortas com
    o erro) e histórico agregado da telemetria."""
    _usuario(request)
    try:
        f = fila.detalhe()
    except Exception:
        f = {"online": False, "filas": [], "dlq_msgs": [], "telemetria": {}}
    por_tipo = {}
    try:
        for e in historico.ultimos(None, 60):
            t = e.get("tipo") or "outro"
            d = por_tipo.setdefault(t, {"total": 0, "ok": 0, "erro": 0})
            d["total"] += 1
            d["ok" if e.get("ok") else "erro"] += 1
    except Exception:
        pass
    return TEMPLATES.TemplateResponse(
        request, "_fila.html",
        {"request": request, "fila": f, "por_tipo": por_tipo})


def _campos_config() -> dict:
    """FIELDS + campos DINÂMICOS dos provedores externos (mesma lista na
    tela Sistema e no /hx/settings — sem isto o form enviava campos que o
    save ignorava)."""
    campos = dict(config.FIELDS)
    try:
        from core import provedores as _prov
        for _pid in _prov.ids():
            for _suf, _rot, _tp in (
                    ("BASE_URL", "URL da API (OpenAI-compatible)", "str"),
                    ("API_KEY", "Chave da API", "secret"),
                    ("MODELOS", "Modelos (lista manual, vírgula — reserva)",
                     "str")):
                campos.setdefault(
                    f"PROV_{_pid.upper()}_{_suf}",
                    (f"Provedor {_pid.upper()}", _rot, _tp))
    except Exception:
        pass
    return campos


@app.get("/sistema")
def pagina_sistema(request: Request):
    ctx = _paginas_ctx(request, "sistema")
    if not ctx["admin"]:
        return RedirectResponse("/", status_code=303)
    st = status()
    ctx["servicos"] = st["services"]
    ctx["modelos"] = {"llm": st.get("modelo"), "embed": st.get("embedding")}
    ctx["nomes"] = {"qdrant": "Qdrant", "llm": "LLM (chat)", "embed": "Embedding",
                    "visao": "Multimodal (imagem→texto)", "rabbit": "RabbitMQ",
                    "redis": "Redis"}
    # 🧠 ATIVOS pela FONTE ÚNICA (`modelos_ativos`): o cabeçalho mostra o
    # que está SERVINDO agora (chat/visão/difusores) — nunca o .env velho.
    ctx["ativos"] = modelos_ativos()
    # 🌐 provedores CADASTRADOS (retrato no cartão ☁️ do Sistema — feedback
    # de que a chave gravou: nome + nº de modelos e de 👁 multimodais)
    try:
        from core import provedores as _prov
        ctx["provedores_externos"] = [
            {"id": p["id"], "nome": p["nome"],
             "n_modelos": len(p["modelos"]),
             "n_visao": sum(1 for m in p["modelos"] if m.get("cat") == "visao")}
            for p in _prov.listar() if p["externo"]]
        ctx["prov_conhecidos"] = [{"id": k, **v} for k, v in
                                  _prov.CONHECIDOS.items()]
    except Exception:
        ctx["provedores_externos"] = []
        ctx["prov_conhecidos"] = []
    # ── painel do MOTOR (modelos ativos + VRAM) ──────────────────────
    # em container: a verdade está no agente do host (quem tem a GPU).
    # AGENTE FORA? O status NÃO mente "desligado": a LLM continua
    # servindo PELO TÚNEL (llm.disroy.org) — lê pelo túnel e avisa.
    motor = {"chat": None, "embed": None, "visao": None, "vram_mi": None,
             "agente": None, "rodando": []}
    try:
        if config.EM_CONTAINER:
            try:
                saude = modelos._chamar_agente("/saude", timeout=5)
                motor = {"chat": saude.get("chat"),
                         "embed": bool(saude.get("embed")),
                         "visao": None, "vram_mi": saude.get("vram_mi"),
                         "agente": True,
                         "rodando": saude.get("rodando") or []}
            except Exception as e:
                # agente offline: a LLM pode estar no ar mesmo assim (túnel)
                motor = {"chat": modelos.servido(modelos.CHAT_PORTA),
                         "embed": modelos.embedding_no_ar(),
                         "visao": None, "vram_mi": None,
                         "agente": f"offline ({str(e)[:60]}) — status lido "
                                   "pelos túneis; geração de mídia exige o "
                                   "agente na estação",
                         "rodando": []}
        else:
            motor = {"chat": modelos.servido(modelos.CHAT_PORTA),
                     "embed": modelos.embedding_no_ar(),
                     "visao": modelos.servido(modelos.VL_PORTA),
                     "vram_mi": modelos._vram_uso_mi(), "agente": None,
                     "rodando": (tarefas.ativos()
                                 if not config.EM_CONTAINER else [])}
    except Exception as e:
        motor["agente"] = f"fora do ar ({str(e)[:80]})"
    ctx["motor"] = motor
    # ⚙️ CONFIGURAÇÕES: TODAS as chaves editáveis do registro FIELDS, com o
    # VALOR ATUAL do .env — agrupadas por categoria, segredos MASCARADOS.
    # (bug antigo: a lógica da máscara era invertida — chave DEFINIDA sumia
    # do form em vez de aparecer como ••••; parecia "perdida".)
    cfg_atual = config.as_dict()
    ctx["config_atual"] = cfg_atual   # usados avulsos no cabeçalho (KPIs)
    grupos_cfg: dict = {}
    for chave, (grupo, rotulo, tipo) in _campos_config().items():
        bruto = cfg_atual.get(chave, os.getenv(chave, ""))
        grupos_cfg.setdefault(grupo, []).append({
            "chave": chave, "rotulo": rotulo, "tipo": tipo,
            "valor": "" if tipo == "secret" else str(bruto or ""),
            "definido": bool(bruto),   # secret definido mostra placeholder ••••
        })
    ctx["grupos_cfg"] = grupos_cfg
    return TEMPLATES.TemplateResponse(request, "sistema.html", ctx)


@app.get("/entrar")
def pagina_entrar(request: Request):
    return TEMPLATES.TemplateResponse(request, "entrar.html",
                                      {"request": request, "erro": None})


@app.post("/entrar")
def entrar(request: Request, user: str = Form(...), senha: str = Form(...)):
    if not auth.verificar(user, senha):
        return TEMPLATES.TemplateResponse(request, "entrar.html",
                                          {"request": request,
                                           "erro": "usuário ou senha incorretos"},
                                          status_code=401)
    token = auth.emitir_token(user.strip())
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_TOKEN, token, max_age=auth.TOKEN_DIAS * 86400,
                    httponly=True, samesite="lax")
    return resp


@app.get("/sair")
def sair():
    resp = RedirectResponse("/entrar", status_code=303)
    resp.delete_cookie(COOKIE_TOKEN)
    resp.delete_cookie(SESSAO_COOKIE)
    return resp


@app.post("/hx/aquisicao")
async def hx_aquisicao(request: Request, fonte: str = Form("pesquisa"),
                       entrada: str = Form(default=""), limite: int = Form(6),
                       colecao: str = Form(default=""),
                       hf_ids: str = Form(default=""),
                       arquivos: list[UploadFile] | None = File(None)):
    """UMA entrada para todas as fontes — todo caminho termina na revisão."""
    entrada = (entrada or "").strip()
    colecao = colecao.strip() or None
    # 🤗 ids MARCADOS na lista de datasets (campo único vírgula-separado —
    # checkboxes com o MESMO name viram lista, mas o hx-post manda string)
    ids_hf = [i.strip() for i in hf_ids.split(",") if i.strip()] or None
    try:
        if fonte == "pesquisa":
            if not entrada:
                raise ValueError("informe o assunto da pesquisa")
            r = pesquisa_rota(PesquisaIn(assunto=entrada, colecao=colecao,
                                         fontes=limite))
            job, kind, rotulo = r["job"], "pesquisa", f"pesquisa · {entrada[:50]}"
        elif fonte == "pasta":
            if not entrada:
                raise ValueError("informe o caminho da pasta no servidor")
            r = ingest_preview(PreviewIn(fonte="pasta", pasta=entrada,
                                         colecao=colecao))
            job, kind, rotulo = r["job"], "preview", f"revisão · {entrada[:50]}"
        elif fonte == "hf":
            if not entrada and not ids_hf:
                raise ValueError("informe o que buscar no Hub (ou marque datasets)")
            r = ingest_preview(PreviewIn(fonte="hf", query=entrada,
                                         limite=limite, colecao=colecao,
                                         ids=ids_hf))
            _rot = f"{len(ids_hf)} dataset(s)" if ids_hf else entrada[:40]
            job, kind, rotulo = r["job"], "preview", f"huggingface · {_rot}"
        else:  # arquivos (upload) → dry-run (a rota salva em datasets/upload)
            if not arquivos:
                raise ValueError("selecione ao menos um arquivo")
            r = await ingest_upload(request, arquivos=arquivos,
                                    colecao=colecao or "", rapido=True,
                                    dry_run=True)
            job, kind, rotulo = r["preview_job"], "preview", f"revisão · {len(arquivos)} arquivo(s)"
        s = _preview.status(job, 0, "") if kind == "preview" else _pesquisa.status(job, 0, "")
        return TEMPLATES.TemplateResponse(request, "_job.html",
                                          {"request": request, "kind": kind,
                                           "job": job, "rotulo": rotulo,
                                           "linhas": s["lines"], "running": True})
    except (ValueError, HTTPException) as e:
        detalhe = e.detail if isinstance(e, HTTPException) and isinstance(e.detail, str) else str(e)
        return TEMPLATES.TemplateResponse(request, "_job.html",
                                          {"request": request, "kind": "erro",
                                           "job": "erro", "rotulo": fonte,
                                           "linhas": [], "running": False,
                                           "erro": detalhe}, status_code=200)


def _finalizar_midia_fundo(job: str) -> None:
    """Observa a tarefa de mídia até o FIM (agente no container / registro
    local) e grava o resultado na sessão do chat — mesmo que o navegador
    feche antes de concluir (o registro anterior dependia do polling da
    página). Desiste silenciosamente após 15 min."""
    import time as _t
    for _ in range(90):
        _t.sleep(10)
        s = None
        try:
            if config.EM_CONTAINER:
                import httpx as _hx
                rr = _hx.get(f"{modelos._agente_host()}/tarefas/status/{job}",
                             params={"cursor": 0}, timeout=8,
                             headers=modelos._agente_headers())
                if rr.status_code == 200:
                    s = rr.json()
            else:
                s = tarefas.status(job, 0)
        except Exception:
            s = None
        if s and not s.get("running") and not s.get("error"):
            _registrar_midia_sessao(job, s.get("result") or {})
            return


_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# barras de progresso do sd.cpp (todas as formas: |##| n/n · |==> n/n s/it
# · n/n - xMB/s) — o % vive na barra REAL do card, não no log
_RE_BARRA = re.compile(
    r"^\s*\|[#=>\s.]*\|?\s*\d+\s*/\s*\d+"          # |####| 138/138 / |==> 8/20
    r"|^\s*\d+\s*/\s*\d+\s*-\s*[\d.]+\s*[KM]B/s"   # 744/780 - 778.14MB/s
    r"|^\s*\|.*\d+\s*/\s*\d+.*s/it"                # |====> 8/20 - 13.33s/it
    r"|s/it\s*$"                                   # 12/20 - 13.40s/it (s/it no fim)
)
_RE_PCT = re.compile(r"^\s*\d+/\d+|\s+\d+(\.\d+)?(MB|GB|ms|s)/s\s*$", re.I)

# job de MÍDIA do chat ↔ sessão: o POST grava a pergunta; quando o job
# conclui, o hx_job grava o RESULTADO como mensagem da conversa
_TAREFA_CHAT: dict = {}


def _limpar_job_ativo(job: str) -> None:
    """Tarefa concluída: o job_ativo da sessão dona sai (o refresh volta a
    mostrar só o histórico — o card já cumpriu seu papel)."""
    try:
        info = _TAREFA_CHAT.get(job) or {}
        sid = info.get("sid")
        if not sid:
            return
        an = sessions.get_session(sid) or {}
        if (an.get("job_ativo") or {}).get("job") == job:
            sessions.save_session(an.get("raw") or [], sid=sid,
                                  owner=an.get("owner", ""),
                                  aprovacoes=an.get("aprovacoes", {}),
                                  raw=an.get("raw"), job_ativo=None)
    except Exception:
        pass


def _registrar_midia_sessao(job: str, res: dict) -> None:
    """Tarefa de mídia concluída → mensagem ASSISTENTE na sessão do chat
    (mídia renderizável ou texto da análise multimodal). Idempotente: o
    registro é consumido no 1º call."""
    info = _TAREFA_CHAT.pop(job, None)
    if not info:
        return
    try:
        anterior = sessions.get_session(info["sid"]) or {}
        bruto = anterior.get("raw") or []
        seg = res.get("segundos")
        if res.get("arquivo"):
            bruto.append({
                "role": "assistant",
                "content": f"mídia gerada ({res.get('tipo')}): {res['arquivo']}",
                "midia": {"tipo": res.get("tipo"), "arquivo": res["arquivo"]},
                "segundos": seg, "modelo": res.get("modelo")})
        elif (res.get("texto") or "").strip():
            bruto.append({
                "role": "assistant", "content": res["texto"],
                "html": _md_basico(res["texto"]), "segundos": seg})
        else:
            return
        sessions.save_session(bruto, sid=info["sid"],
                              owner=anterior.get("owner", ""), titulo=None,
                              modo=anterior.get("modo", ""),
                              colecoes=anterior.get("colecoes", []),
                              aprovacoes=anterior.get("aprovacoes", {}),
                              raw=bruto)
        print(f"💾 tarefa {job} registrada na sessão {info['sid'][:18]}…")
    except Exception as e:
        print(f"⚠️ sessão multimodal (resultado): {e}")


def _linhas_visual(lines: list) -> list:
    """Log LIMPO para o card: tira códigos ANSI, descarta linhas de BARRA de
    progresso (o % vive na barra do card), restos de carriage-return do
    sd.cpp, loader técnico e ruído vazio — só o que importa ao operador."""
    saida = []
    for l in lines:
        msg = str(l.get("msg", l) if isinstance(l, dict) else l)
        msg = _RE_ANSI.sub("", msg).replace("\r", "\n")
        # sd.cpp pinta barra por cima: sobra lixo após o \r — fica a última
        for parte in msg.split("\n"):
            parte = parte.strip()
            if not parte or _RE_BARRA.match(parte):
                continue
            # loader técnico do sd.cpp: mantém só o resumo "completed, taking"
            if "model_loader.cpp" in parte and "completed" not in parte:
                continue
            if parte in ("[INFO ]", "[INFO]", "[DEBUG]", "save:", "compute"):
                continue
            saida.append({"msg": parte,
                          "etapa": (l.get("etapa") if isinstance(l, dict) else None)})
    return saida


@app.get("/hx/job/{kind}/{job}")
def hx_job(kind: str, job: str, request: Request, r: int = 0):
    """Polling genérico: pesquisa/preview/ingest/seed/limpeza/tarefa — log
    INLINE linha por linha; ao concluir mostra o resultado (e o link da
    revisão quando há preview).

    AGENTE reindando/reiniciando (tarefa delegada): em vez de morrer com
    "job não encontrado", o card entra em "reconectando…" e SEGUE polando
    (bound de 12 tentativas via ?r=N) — o agente voltando no meio não
    mata mais o acompanhamento."""
    try:
        if kind == "tarefa":
            s = tarefas.status(job, 0)
            if s is None and config.EM_CONTAINER:
                # tarefa DELEGADA ao agente do host: o registro vive LÁ —
                # o polling transparentemente consulta o agente (o log da
                # geração aparece no chat linha por linha, como local).
                try:
                    import httpx as _hx
                    rr = _hx.get(f"{modelos._agente_host()}/tarefas/status/{job}",
                                 params={"cursor": 0}, timeout=8,
                                 headers=modelos._agente_headers())
                    if rr.status_code == 200:
                        s = rr.json()
                except Exception:
                    pass
            if s is None:
                if config.EM_CONTAINER and r < 12:
                    return TEMPLATES.TemplateResponse(
                        request, "_job.html",
                        {"request": request, "kind": kind, "job": job,
                         "rotulo": "geração", "running": True,
                         "reconectando": True, "r": r + 1,
                         "linhas": [{"msg": "⚠️ agente da GPU indisponível "
                                            "(reiniciando?) — reconectando…",
                                     "etapa": "aguardando"}],
                         "progresso": None, "etapa_atual": "aguardando",
                         "eta_s": None, "erro": None, "resumo_texto": "",
                         "preview_pid": None, "resultado_midia": None})
                raise HTTPException(status_code=404, detail="tarefa não encontrada")
        else:
            reg = {"pesquisa": _pesquisa, "preview": _preview, "ingest": _ingest,
                   "seed": _seed, "limpeza": _limpeza,
                   "manutencao": _manutencao}.get(kind)
            if reg is None:
                raise HTTPException(status_code=404, detail="tipo de job desconhecido")
            s = reg.status(job, 0, "")
    except HTTPException:
        s = {"running": False, "lines": [], "result": None,
             "error": "job não encontrado"}
    # JOB AUSENTE no registro (qualquer kind): API reiniciou (deploy) e o
    # RabbitMQ REPLAYA a execução — em vez de morrer com "job não
    # encontrado", o card entra em "reconectando…" e segue pollando (12
    # tentativas via ?r=N); depois, erro claro com o que fazer.
    if (not s.get("running") and s.get("error") == "job não encontrado"
            and r < 12):
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": kind, "job": job,
             "rotulo": kind, "running": True, "reconectando": True,
             "r": r + 1,
             "linhas": [{"msg": "⚠️ a API reiniciou (deploy?) — o job é "
                                "retomado pela fila; reconectando…",
                         "etapa": "aguardando"}],
             "progresso": None, "etapa_atual": "aguardando", "eta_s": None,
             "erro": None, "resumo_texto": "", "segundos": None,
             "preview_pid": None, "resultado_midia": None})
    if s.get("error") == "job não encontrado":
        s["error"] = ("job não encontrado — a API reiniciou e este job se "
                      "perdeu; dispare novamente")
    res = s.get("result") or {}
    if kind == "tarefa" and not s["running"] and not s.get("error"):
        _limpar_job_ativo(job)          # antes do registrar (este CONSOME o mapa)
        _registrar_midia_sessao(job, res)
    resumo = ""
    if not s["running"] and not s.get("error"):
        if res.get("preview"):
            # RESULTADO visivel no card: fontes, claims, sintese, descartes
            resumo = (f"{res.get('fontes_baixadas', 0)} fonte(s) · "
                      f"{res.get('claims', 0)} afirmacao(es) · "
                      f"{'sintese ok' if res.get('sintese') else 'sem sintese'} · "
                      f"{res.get('redundantes_descartadas', 0)} redundante(s)")
        elif res.get("chunks") is not None:
            resumo = (f"{res.get('chunks', '?')} pedaço(s) → "
                      f"coleção '{res.get('colecao', '?')}'")
        elif res.get("arquivo"):
            resumo = f"arquivo: {res['arquivo']}"
        elif res.get("texto"):
            resumo = f"análise: {str(res['texto'])[:180]}"
    ctx = {"request": request, "kind": kind, "job": job, "rotulo": kind,
           "linhas": _linhas_visual(s["lines"]), "running": s["running"],
           "erro": s.get("error"), "resumo_texto": resumo,
           # progresso REAL do motor (sd-cli/whisper parseados no core):
           # 0..1 → %; None = motor não reporta (só o log rola)
           "progresso": (round((s.get("progresso") or 0) * 100)
                         if isinstance(s.get("progresso"), (int, float)) else None),
           "etapa_atual": s.get("etapa"),
           "eta_s": s.get("eta_s"),
           "r": r,
           "segundos": (res.get("segundos") if isinstance(res, dict) else None),
           "preview_pid": (res.get("preview") if isinstance(res, dict) else None),
           "resultado_midia": ({ "tipo": res.get("tipo"), "arquivo": res.get("arquivo")}
                               if isinstance(res, dict) and res.get("arquivo") else None)}
    return TEMPLATES.TemplateResponse(request, "_job.html", ctx)


@app.get("/hx/colecao/{nome}/docs")
def hx_colecao_docs(nome: str, request: Request):
    """Documentos da coleção (drawer da Biblioteca): agrupa os CHUNKS por
    documento de origem (`arquivo`/`source`/`titulo` do metadata) e mostra
    nome (metadado `titulo` quando existe, senão o basename da origem),
    nº de pedaços e prévia do conteúdo — lazy (só ao abrir a coleção)."""
    _usuario(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    docs: dict[str, dict] = {}
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=10,
                              check_compatibility=False)
        for pts in _scroll_todos(client, nome, limite=800):
            for p in pts:
                md = p.payload or {}
                chave = str(md.get("arquivo") or md.get("source")
                            or md.get("titulo") or f"ponto {p.id}")
                titulo = str(md.get("titulo") or "").strip()
                if not titulo:
                    titulo = chave.replace("\\", "/").rsplit("/", 1)[-1] or "?"
                d = docs.setdefault(chave, {"chave": chave, "titulo": titulo,
                                            "chunks": 0, "previa": "", "ids": [],
                                            "nomeado": bool(md.get("titulo"))})
                d["chunks"] += 1
                if len(d["ids"]) < 400:
                    d["ids"].append(str(p.id))
                if not d["previa"]:
                    d["previa"] = str(md.get("page_content", ""))[:160]
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha ao ler: {str(e)[:140]}</p>")
    return TEMPLATES.TemplateResponse(
        request, "_colecao_docs.html",
        {"request": request, "nome": nome,
         # alfabético: leitura previsível (por tamanho confundia)
         "docs": sorted(docs.values(), key=lambda d: d["titulo"].lower())})


def _scroll_todos(client, colecao: str, limite: int):
    """Scroll paginado da coleção (gerador de lotes de Record)."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    offset = None
    while limite > 0:
        lote, offset = client.scroll(collection_name=colecao,
                                     limit=min(100, limite),
                                     offset=offset, with_payload=True,
                                     with_vectors=False)
        yield lote
        limite -= len(lote)
        if offset is None or not lote:
            break


@app.get("/hx/colecao/{nome}/doc")
def hx_colecao_doc(nome: str, request: Request, chave: str):
    """DETALHE de um documento da coleção (modal da Biblioteca): título,
    nº de pedaços e o CONTEÚDO dos primeiros chunks por inteiro (a lista
    corta em 160 chars — o modal mostra até 4 chunks completos)."""
    _usuario(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    achado = None
    total = 0
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=10,
                              check_compatibility=False)
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        for campo in ("arquivo", "source"):
            filtro = Filter(must=[FieldCondition(
                key=campo, match=MatchValue(value=chave))])
            pts, _ = client.scroll(collection_name=nome, limit=6,
                                   scroll_filter=filtro, with_payload=True,
                                   with_vectors=False)
            if pts:
                achado = pts
                total = client.count(nome, exact=True,
                                     count_filter=filtro).count
                break
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha: {str(e)[:140]}</p>")
    if not achado:
        return HTMLResponse("<p class='mut'>documento não encontrado.</p>")
    md0 = achado[0].payload or {}
    titulo = str(md0.get("titulo") or chave.replace("\\", "/")
                 .rsplit("/", 1)[-1] or "?")
    chunks = [str(p.payload.get("page_content", ""))[:4000] for p in achado]
    return TEMPLATES.TemplateResponse(
        request, "_colecao_doc.html",
        {"request": request, "nome": nome, "titulo": titulo, "chave": chave,
         "total": total, "chunks": chunks})


@app.post("/hx/colecao/{nome}/nomear")
def hx_colecao_nomear(nome: str, request: Request,
                      chave: str = Form(...), titulo: str = Form(...)):
    """GRAVA o nome do documento como metadado `titulo` em TODOS os chunks
    dele (set_payload em lote) — o nome passa a valer no Qdrant, não só na
    tela. Sem título no metadata, a Biblioteca mostrava basenames crus."""
    _exigir_admin(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome) or not titulo.strip():
        return HTMLResponse("<p class='erro-texto'>dados inválidos</p>")
    titulo = titulo.strip()[:200]
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    campo = "arquivo" if "\\" in chave or "/" in chave else "source"
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        filtro = Filter(must=[FieldCondition(key=campo,
                                              match=MatchValue(value=chave))])
        # tenta pelo campo real usado; se não achar, tenta o outro
        info = client.count(nome, exact=True, count_filter=filtro).count
        if not info:
            campo = "source" if campo == "arquivo" else "arquivo"
            filtro = Filter(must=[FieldCondition(key=campo,
                                                  match=MatchValue(value=chave))])
            info = client.count(nome, exact=True, count_filter=filtro).count
        if not info:
            return HTMLResponse("<p class='erro-texto'>documento não encontrado</p>")
        client.set_payload(collection_name=nome,
                           payload={"titulo": titulo},
                           filters=filtro)
        return HTMLResponse(f"<p class='mini'>✓ '{titulo}' gravado no Qdrant "
                            f"({info} pedaço(s))</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha: {str(e)[:140]}</p>")


@app.get("/hx/histlog/{job}")
def hx_histlog(job: str, request: Request):
    """Log COMPLETO de um job — SQLite primeiro (base persistente), fallback
    jsonl. HTML puro."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", job):
        return HTMLResponse("<p class='mini'>id inválido</p>")
    linhas = []
    try:
        from core import logsdb
        evs = logsdb.eventos_do_job(job)
        linhas = [f"<div><span class='hora'>{e['ts']}</span>{e['msg']}</div>"
                  for e in evs]
    except Exception:
        linhas = []
    if not linhas:  # jobs antigos (pré-SQLite) vivem no jsonl
        arq = PASTA_LOGS_JOBS / f"{job}.jsonl"
        if arq.is_file():
            for l in arq.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
                try:
                    d = json.loads(l)
                    linhas.append(f"<div><span class='hora'>{d.get('ts','')}</span>"
                                  f"{d.get('msg','')}</div>")
                except Exception:
                    continue
    if not linhas:
        return HTMLResponse("<p class='mini'>sem log gravado para este job</p>")
    return HTMLResponse("<div class='log'>" + "".join(linhas) + "</div>")


@app.post("/hx/resolucao")
def hx_resolucao(request: Request, problema: str = Form(...),
                  causa: str = Form(...), solucao: str = Form(...),
                  contexto: str = Form("")):
    '''Indexa uma resolucao de problema na base vetorial'''
    _usuario(request)
    try:
        r = resolucoes.registrar(problema, causa, solucao, contexto)
        return HTMLResponse("<p class='mini' style='color:var(--ok)'>&#10024; resolucao indexada em erros_comuns (o chat ja consulta)</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>{e}</p>")


@app.post("/hx/recategorizar")
def hx_recategorizar(request: Request):
    """🧠 Re-analisa TODA a biblioteca com a LLM (área/categoria/descrição
    de cada coleção no catálogo) — job com log ao vivo."""
    try:
        r = _manutencao_disparar("analisar")
        return TEMPLATES.TemplateResponse(
            request, "_job.html",
            {"request": request, "kind": "manutencao", "job": r.get("job"),
             "rotulo": "recategorizar biblioteca", "linhas": [], "running": True})
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>{e}</p>")


@app.post("/hx/enriquecer/{nome}")
def hx_enriquecer(nome: str, request: Request):
    """✨ Enriquece UMA coleção: a LLM re-lê amostras e regenera
    área/categoria/descrição no catálogo (meta_colecoes)."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    job = _manutencao.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            _manutencao.log(jid, f"✨ enriquecendo '{nome}' — a LLM lê amostras…")
            try:
                from core import analyze as _an, catalog as _cat
                client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                                      check_compatibility=False)
                amostras = _an._samples(client, nome, limit=6)
                if not amostras:
                    raise RuntimeError("coleção sem conteúdo para analisar")
                r = rag.analyze_collection(nome, amostras)
                _cat.save_collection(client, nome, r["categoria"], r["descricao"],
                                     area=r.get("area", ""))
                _manutencao.log(jid, f"   área: {r.get('area', '—')}")
                _manutencao.log(jid, f"   categoria: {r['categoria']}")
                _manutencao.log(jid, f"   descrição: {r['descricao']}")
                _manutencao.concluir(jid, result={"colecao": nome, **r})
            except Exception as e:
                _manutencao.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "manutencao", {"job": job}, _manutencao)
    return TEMPLATES.TemplateResponse(
        request, "_job.html",
        {"request": request, "kind": "manutencao", "job": job,
         "rotulo": f"enriquecer {nome}", "linhas": [], "running": True})


@app.post("/hx/colecao/{nome}/apagar")
def hx_colecao_apagar(nome: str, request: Request):
    """Apagar coleção como JOB (barra de progresso): coleções grandes
    (30k+ pontos) levam segundos — o card mostra apagando -> concluído."""
    _exigir_admin(request)
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    job = _manutencao.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            _manutencao.log(jid, f"🗑️ apagando coleção '{nome}' do Qdrant…")
            try:
                apagar_collection(nome)
                try:
                    catalog.remove_collection_meta(
                        QdrantClient(url=config.QDRANT_URL, timeout=30,
                                     check_compatibility=False), nome)
                except Exception:
                    pass
                _manutencao.log(jid, f"✅ '{nome}' apagada (pontos + catálogo)")
                _manutencao.concluir(jid, result={"colecao": nome,
                                                  "apagada": True})
            except Exception as e:
                _manutencao.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "manutencao", {"job": job}, _manutencao)
    return TEMPLATES.TemplateResponse(
        request, "_job.html",
        {"request": request, "kind": "manutencao", "job": job,
         "rotulo": f"apagar {nome}", "linhas": [], "running": True})


@app.get("/revisao/{pid}")
def pagina_revisao(pid: str, request: Request):
    from core import preview as _pv
    resp = _pv.ver(pid)
    if not resp:
        return TEMPLATES.TemplateResponse(request, "revisao.html",
                                          {"request": request,
                                           "resp": {"documentos": [], "descartados": [],
                                                    "clusters": [], "resumo": {},
                                                    "colecao_alvo": None, "tema_min": 0},
                                           "pid": pid, "expirou": True})
    return TEMPLATES.TemplateResponse(request, "revisao.html",
                                      {"request": request, "resp": resp, "pid": pid,
                                       "expirou": False})


@app.post("/hx/revisao/descartar")
def hx_revisao_descartar(body: dict, request: Request):
    """✕ REJEITAR aquisição: apaga o preview — NADA vai para o Qdrant."""
    _usuario(request)
    pid = (body or {}).get("pid", "")
    if re.fullmatch(r"[A-Za-z0-9]{1,16}", pid):
        try:
            from core import preview as _pv
            _pv._previews.pop(pid, None)
        except Exception:
            pass
    return {"descartado": True}


@app.post("/hx/revisao/aplicar")
def hx_revisao_aplicar(request: Request, preview: str = Form(...),
                       colecao: str = Form(...), ids: list[str] = Form(default=[])):
    try:
        r = preview_aplicar(PreviewAplicarIn(preview=preview,
                                             ids=[int(i) for i in ids],
                                             colecao=colecao))
        s = _ingest.status(r["job"], 0, "")
        return TEMPLATES.TemplateResponse(request, "_job.html",
                                          {"request": request, "kind": "ingest",
                                           "job": r["job"],
                                           "rotulo": f"aplicar em '{colecao}'",
                                           "linhas": s["lines"], "running": True})
    except (ValueError, HTTPException) as e:
        detalhe = e.detail if isinstance(e, HTTPException) and isinstance(e.detail, str) else str(e)
        return TEMPLATES.TemplateResponse(request, "_job.html",
                                          {"request": request, "kind": "erro",
                                           "job": "erro", "rotulo": "aplicar revisão",
                                           "linhas": [], "running": False,
                                           "erro": detalhe})


@app.post("/hx/settings")
async def hx_settings(request: Request):
    """Salva TODAS as chaves do registro FIELDS no .env e recarrega SEM
    restart (aplica na hora — TEMPERATURE/cache/scores/etc). Regras:
    tipo validado (422 em valor inválido); SEGREDO mascarado/vazio NÃO
    regrava (só troca explícita); bool aceita 1/0/true/false."""
    _exigir_admin(request)
    form = await request.form()
    erros = []
    trocas = 0
    for chave, (grupo, rotulo, tipo) in _campos_config().items():
        if chave not in form:
            continue
        valor = str(form.get(chave) or "").strip()
        if not valor or "•" in valor:
            continue   # vazio/máscara: mantém o atual (segredo nunca some)
        # segredo: SECRETOS fixos + chaves de PROVEDOR dinâmicas (PROV_*_KEY)
        if (tipo == "secret" and chave not in config.SECRETOS
                and not (chave.startswith("PROV_") and chave.endswith("_KEY"))):
            continue
        if tipo in ("int", "float"):
            try:
                (int if tipo == "int" else float)(valor)
            except ValueError:
                erros.append(f"{chave}: '{valor}' não é {('inteiro' if tipo == 'int' else 'número')}")
                continue
        if chave == "GPU_MODO" and valor not in ("todos", "somente_llms"):
            erros.append("GPU_MODO: use 'todos' ou 'somente_llms'")
            continue
        config.set_env_inplace(chave, valor)
        trocas += 1
    config.reload()
    if erros:
        raise HTTPException(status_code=422, detail="; ".join(erros)
                            + f" — os demais {trocos_label(trocas)} salvos")
    resp = RedirectResponse("/sistema", status_code=303)
    resp.headers["HX-Refresh"] = "true"
    return resp


def trocos_label(n: int) -> str:
    return f"{n} campo(s) foram"


@app.post("/hx/parar-tudo")
def hx_parar_tudo(request: Request):
    _exigir_admin(request)
    return parar_tudo(request)


@app.get("/api/models")
def models():
    """GGUFs de D:\\models categorizados (chat/embed/imagem/video) + o que está
    no ar (chat :8090, embed :8081). A troca é pela POST /api/models/ativar."""
    pasta = Path(os.getenv("MODELS_DIR", r"D:\models"))
    arquivos = [
        {"nome": p.stem, "arquivo": p.name, "gb": round(p.stat().st_size / 1e9, 1)}
        for p in sorted(pasta.rglob("*.gguf")) if p.is_file()
    ] if pasta.is_dir() else []
    no_ar = {}
    for chave, url in [("chat", config.LLM_BASE_URL), ("embed", config.EMBED_BASE_URL)]:
        try:
            r = httpx.get(f"{url}/models", timeout=1.5)
            no_ar[chave] = [m.get("model") or m.get("name")
                            for m in r.json().get("models", [])]
        except Exception:
            no_ar[chave] = []
    return {"pasta": str(pasta), "arquivos": arquivos, "no_ar": no_ar,
            "modelos": modelos.listar(),
            "vram_mi": modelos._vram_uso_mi()}


def _jobs_ativos() -> dict[str, int]:
    """Jobs de fundo em andamento, por tipo ({ingestão: 2, seed: 1, …}).
    Trocar modelo/embedding no meio derruba a LLM que o job está usando —
    chat em curso e tarefa do estúdio também contam (usavam a mesma LLM)."""
    ativos: dict[str, int] = {}
    for reg in TODOS_JOBS:  # uma lista única — novo tipo de job entra sozinho
        n = reg.ativos()
        if n:
            ativos[reg.rotulo] = n
    if tarefas.estudio_ocupado():
        ativos["estúdio"] = 1
    return ativos


def _podar_concluidos(dic: dict, manter: int = 10) -> None:
    """Deixa só os N últimos jobs CONCLUÍDOS no registry — os logs das
    execuções antigas acumulam memória para sempre sem isto (o ingest já
    podava; estende o mesmo comportamento a todos os tipos de job).

    ATENÇÃO: chame SEGURANDO o lock do registry (o threading.Lock não é
    reentrante — adquirir de novo aqui dentro seria deadlock, já aconteceu)."""
    for velho in [k for k, v in dic.items() if not v.get("running")][:-manter]:
        dic.pop(velho, None)


def _novo_job(dic: dict, lock: threading.Lock, job: str) -> None:
    """Cria (ou RE-criA, no replay pós-restart) a entrada de status do job.
    Segura o lock e poda os concluídos antigos. `picked=False` = aguardando
    o worker pegar da fila (o `_no_worker` marca True ao EXECUTAR)."""
    with lock:
        _podar_concluidos(dic)
        dic[job] = {"lines": [], "running": True, "result": None,
                    "error": None, "picked": False}


# ---------- JobRegistry: o boilerplate de família de jobs (era clonado 8x) ----
# Cada família (ingestão, seed, chat, …) repetia dict+Lock+count+log+status+
# cancelamento com pequenos drifts. `.jobs`/`.lock` seguem dict/Lock puros
# (compatíveis com _novo_job/_podar_concluidos/_REGISTROS).

TODOS_JOBS: list = []  # alimenta _jobs_ativos e o ⏹ Parar tudo (1 lista só)

# TODO log de job é GRAVADO (logs/jobs/{job}.jsonl, um por execução — a
# linha vira registro permanente: "registrar tudo e deixar gravado")
PASTA_LOGS_JOBS = Path("logs/jobs")


class JobRegistry:
    # sufixo ÚNICO por processo: o _seq reinicia a cada boot e o sbx_4 de
    # hoje colidia com o sbx_4 de ontem (jsonl em append misturava
    # execuções de dias diferentes no mesmo arquivo — visto em produção)
    _BOOT = os.urandom(2).hex()

    def __init__(self, prefixo: str, rotulo: str):
        self.prefixo, self.rotulo = prefixo, rotulo
        self.jobs: dict = {}
        self.lock = threading.Lock()
        self._seq = count(1)
        TODOS_JOBS.append(self)

    def novo_id(self) -> str:
        return f"{self.prefixo}_{next(self._seq)}-{self._BOOT}"

    def iniciar(self, jid: str) -> None:
        """Cria/recria a entrada de status (placeholder pré-pickup) e poda
        os arquivos de log antigos (retenção por contagem)."""
        _novo_job(self.jobs, self.lock, jid)
        try:  # fire-and-forget: retenção nunca atrasa o job
            PASTA_LOGS_JOBS.mkdir(parents=True, exist_ok=True)
            arquivos = sorted(PASTA_LOGS_JOBS.glob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            for velho in arquivos[400:]:  # mantém os 400 logs mais recentes
                velho.unlink(missing_ok=True)
        except Exception:
            pass

    def parcial(self, jid: str, texto: str) -> None:
        """TEXTO AO VIVO da geração (streaming): o polling mostra a
        resposta sendo escrita antes do job concluir."""
        with self.lock:
            st = self.jobs.get(jid)
            if st is not None:
                st["parcial"] = texto

    def log(self, jid: str, msg: str, **extra) -> None:
        """Anexa linha ao log ao vivo, grava no jsonl (compat) E no SQLite
        BASE PERSISTENTE (logs/logs.db) — o dashboard consulta por SQL;
        tudo sobrevive a restart/recreate da API."""
        with self.lock:
            if jid not in self.jobs:
                return
            # 🧹 DEDUPE: linha IDÊNTICA consecutiva no mesmo segundo é eco de
            # callback duplo (bug real: a linha 🪙 de tokens aparecia 2× no
            # raciocínio) — o jsonl/SQLite já não registra duplicata nenhuma
            linhas = self.jobs[jid]["lines"]
            agora = time.strftime("%H:%M:%S")
            if (linhas and linhas[-1].get("msg") == msg
                    and linhas[-1].get("ts") == agora):
                return
            linhas.append({"ts": agora, "msg": msg, **extra})
        try:
            from core import logsdb
            logsdb.log_evento(jid, str(msg), extra.get("etapa"))
        except Exception:
            pass
        try:
            caminho = PASTA_LOGS_JOBS / f"{jid}.jsonl"
            with open(caminho, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": agora, "msg": msg, **extra},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass  # disco cheio/permissão: o log ao vivo segue valendo

    def concluir(self, jid: str, result=None, error=None) -> None:
        """Fecha o job (running=False) gravando result e/ou error."""
        with self.lock:
            if jid in self.jobs:
                if result is not None:
                    self.jobs[jid]["result"] = result
                if error is not None:
                    self.jobs[jid]["error"] = error
                self.jobs[jid]["running"] = False

    def ativos(self) -> int:
        return sum(1 for j in self.jobs.values() if j.get("running"))

    def cancelar_todos(self, motivo: str) -> list[str]:
        """Marca toda tarefa ativa como cancelada (⏹ Parar tudo) — SEGURANDO
        o lock (antes o parar_tudo iterava 7 registries sem lock nenhum)."""
        with self.lock:
            cancelados = []
            for jid, j in self.jobs.items():
                if j.get("running"):
                    j["running"], j["error"] = False, motivo
                    j["lines"].append({"msg": f"⚠️ {motivo}"})
                    cancelados.append(jid)
            return cancelados

    def status(self, jid: str, cursor: int, msg404: str) -> dict:
        """Snapshot do job a partir do `cursor` (polling da webui)."""
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                raise HTTPException(status_code=404, detail=msg404)
            return {"running": j["running"], "total": len(j["lines"]),
                    "lines": j["lines"][cursor:], "result": j["result"],
                    "error": j["error"]}


def _rota_status(caminho: str, reg: "JobRegistry", msg404: str) -> None:
    """Registra a rota GET de status de uma família — o corpo era idêntico
    palavra por palavra em 8 lugares."""
    def status(job: str, cursor: int = 0):
        return reg.status(job, cursor, msg404)
    app.get(caminho)(status)


# cache do scan de coleções (~30 s): /api/status roda a cada 15 s na webui e
# o scan é N+1 no Qdrant (1 get_collection por coleção) — sem cache, cada
# consulta paga o preço cheio
_SCAN_CACHE: dict = {"t": 0.0, "data": {}}
_SCAN_CACHE_TTL = 30.0


def _scan_collections(client, incluir_sistema: bool = False, forcar: bool = False) -> dict:
    """{nome: {points, dim}} lendo a configuração de cada coleção do Qdrant.

    Por padrão esconde as coleções de sistema (só a webui lista — internamente
    quem precisa consulta direto pelo nome). Usa cache de 30 s (o scan é
    N+1: uma chamada get_collection por coleção)."""
    agora = time.time()
    if not forcar and not incluir_sistema and agora - _SCAN_CACHE["t"] < _SCAN_CACHE_TTL:
        return _SCAN_CACHE["data"]
    info = {}
    for c in client.get_collections().collections:
        if not incluir_sistema and c.name in COLECOES_SISTEMA:
            continue
        try:
            col = client.get_collection(c.name)
            vec = col.config.params.vectors
            dim = next(iter(vec.values())).size if isinstance(vec, dict) else vec.size
            info[c.name] = {"points": col.points_count, "dim": dim}
        except Exception:
            info[c.name] = {"points": None, "dim": None}
    _SCAN_CACHE.update(t=agora, data=info)
    return info


@app.post("/api/models/ativar")
def models_ativar(body: ModeloAtivarIn, request: Request):
    """Troca o modelo de conversa da :8090: libera a VRAM do anterior e sobe o
    novo (o embedding :8081 e o Qdrant continuam de pé). EXCLUSIVO do
    administrador. Pode levar ~1 min."""
    _exigir_admin(request)
    ativos = _jobs_ativos()
    if ativos:
        detalhe = ", ".join(f"{n} de {t}" for t, n in ativos.items())
        raise HTTPException(status_code=409,
                            detail=f"há job(s) em andamento ({detalhe}) — aguarde "
                                   "concluir para trocar o modelo")
    try:
        return modelos.ativar(body.modelo)
    except Exception as e:
        print(f"❌ Erro ao ativar modelo {body.modelo}: {e}")
        raise HTTPException(status_code=503, detail=str(e))


_ATIVOS_CACHE = {"t": 0.0, "dados": None}


def modelos_ativos() -> dict:
    """FONTE ÚNICA (SOLID) do que está NO AR AGORA — lida dos SERVIDORES
    (nunca do .env, que envelhece após trocas na estação): chat :8090,
    visão :8082, embedding :8081 + difusores disponíveis. Serve o topbar
    (badge 🧠/👁), a página Sistema e o dashboard — um conceito, um lugar.
    CACHE 10 s (o badge consulta a cada troca de tipo; a 1ª chamada
    cruzava o túnel até o agente e a UI ficava "…" pendurado)."""
    agora = time.time()
    if _ATIVOS_CACHE["dados"] is not None and agora - _ATIVOS_CACHE["t"] < 10:
        return _ATIVOS_CACHE["dados"]
    ativos = {"chat": None, "visao": None, "embed": None,
              "difusores": [], "vram_mi": None}
    try:
        ativos["chat"] = modelos.servido(modelos.CHAT_PORTA)
    except Exception:
        pass
    try:
        ativos["embed"] = bool(modelos.embedding_no_ar())
    except Exception:
        pass
    try:
        ativos["visao"] = modelos.servido(modelos.VL_PORTA)
    except Exception:
        pass
    try:
        if config.EM_CONTAINER:
            s = modelos._chamar_agente("/saude", timeout=3)
            ativos["vram_mi"] = s.get("vram_mi")
        else:
            ativos["vram_mi"] = modelos._vram_uso_mi()
    except Exception:
        pass
    for alias, alvo in modelos.REGISTRO.items():
        if alvo[1] in ("video", "imagem") and Path(alvo[0]).exists():
            ativos["difusores"].append(alias)
    # 👁 multimodal EXTERNO disponível (mesma fonte única — SOLID): se um
    # provedor tem modelo de visão, o Sistema/badge mostram como ativo
    # possível mesmo sem a :8082 local subida
    try:
        from core import provedores as _prov
        ativos["visao_externa"] = [
            f"{p['id']}:{m['nome']}"
            for p in _prov.listar() if p["externo"]
            for m in p["modelos"] if m.get("visao")][:6]
    except Exception:
        ativos["visao_externa"] = []
    _ATIVOS_CACHE.update(t=agora, dados=ativos)
    return ativos


@app.get("/api/modelo/ativo")
def modelo_ativo():
    """🧠 Qual modelo de conversa está NO AR AGORA — lido DIRETO do servidor
    (OpenAI-compatible /v1/models do llama-server, via túnel na produção;
    cache 10 s). É a fonte da verdade da UI: badge + combobox refletem
    isto, nunca um valor salvo no cliente. Inclui visão/embedding/difusores
    (mesma fonte única de `modelos_ativos`)."""
    a = modelos_ativos()
    return {"modelo": a["chat"], "provider": "llama-server",
            "online": bool(a["chat"]),
            "visao": a["visao"], "embed": a["embed"],
            "visao_externa": a.get("visao_externa", []),
            "difusores": a["difusores"], "vram_mi": a["vram_mi"]}


@app.post("/api/modelo/ativo")
def modelo_trocar(body: ModeloAtivarIn, request: Request):
    """Troca ASSÍNCRONA (natural): dispara a troca em background e devolve
    NA HORA — o cliente acompanha pelo GET /api/modelo/ativo até `modelo`
    bater com o pedido (a carga do GGUF leva ~1 min na GPU; síncrono
    morreria no 524 da borda)."""
    _exigir_admin(request)
    alias = (body.modelo or "").strip()
    if not alias:
        raise HTTPException(status_code=422, detail='informe {"modelo": "alias"}')
    m = next((x for x in modelos.listar() if x["nome"] == alias), None)
    if not m:
        raise HTTPException(status_code=404, detail=f"modelo desconhecido: {alias}")
    if m["categoria"] != "chat":
        raise HTTPException(status_code=422,
                            detail=f"'{alias}' é {m['categoria']} — só modelo de chat")
    if not m["compativel"]:
        raise HTTPException(status_code=422,
                            detail=f"'{alias}' não cabe na VRAM: {m['motivo']}")
    if modelos.normalizar(modelos.servido(modelos.CHAT_PORTA)) == modelos.normalizar(alias):
        return {"ok": True, "modelo": alias, "trocou": False}

    def _rodar():
        try:
            modelos.ativar(alias)
        except Exception as e:
            print(f"❌ troca manual → {alias}: {e}")
    threading.Thread(target=_rodar, daemon=True, name=f"troca-{alias}").start()
    return {"ok": True, "iniciada": alias}


def _check_redis() -> dict:
    """Ping no Redis com detalhe útil (entradas do cache + uso de tokens)."""
    try:
        cliente = cache._redis_client()
        if cliente is None:
            return {"name": "Redis", "ok": False, "detail": "sem conexão"}
        entradas = cliente.zcard("rag:cache:idx")
        uso = int(cliente.get("rag:uso:total:chamadas") or 0)
        return {"name": "Redis", "ok": True,
                "detail": f"cache {entradas} · {uso} chamadas LLM"}
    except Exception as e:
        return {"name": "Redis", "ok": False, "detail": str(e)[:60]}


_STATUS_CACHE: dict = {"t": 0.0, "dados": None}


@app.get("/api/status")
def status():
    """Saúde dos serviços (Qdrant, LLM, embedding, Rabbit, Redis), coleções
    e pontos. Em produção as checagens de LLM/Embed CRUZAM O TÚNEL (1-2 s
    cada) — SEQUENCIAL dava ~8 s de página. Agora: PARALELO
    (ThreadPoolExecutor — o total é o MAIOR, não a soma) + CACHE 8 s (o
    polling de 15 s da webui quase sempre bate no cache)."""
    import time as _t
    agora = _t.time()
    if _STATUS_CACHE["dados"] is not None and agora - _STATUS_CACHE["t"] < 8:
        return _STATUS_CACHE["dados"]

    from concurrent.futures import ThreadPoolExecutor

    def _svc_visao():
        try:
            return {"name": "Multimodal (imagem→texto)",
                    "ok": not modelos.vl_manual_off(),
                    "detail": ("desligado manualmente (Sistema)"
                               if modelos.vl_manual_off()
                               else "sobe na 1ª análise de imagem")}
        except Exception as e:
            return {"name": "Multimodal", "ok": False, "detail": str(e)[:60]}

    def _svc_scan():
        try:
            return _scan_collections(QdrantClient(
                url=config.QDRANT_URL, timeout=4,
                check_compatibility=False)) or {}
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        fut = {
            "qdrant": ex.submit(_check, "Qdrant", f"{config.QDRANT_URL}/healthz"),
            "llm": ex.submit(_check, "LLM", f"{config.LLM_BASE_URL}/models"),
            "embed": ex.submit(_check, "Embedding", f"{config.EMBED_BASE_URL}/models"),
            "rabbit": ex.submit(_estado_rabbit),
            "redis": ex.submit(_check_redis),
            "visao": ex.submit(_svc_visao),
            "_scan": ex.submit(_svc_scan),
        }
        services = {k: f.result(timeout=10) for k, f in fut.items() if k != "_scan"}
        collection_info = fut["_scan"].result(timeout=10)
    dados = {
        "services": services,
        "collections": list(collection_info),
        "collection_info": collection_info,
        "collection": config.COLLECTION,
        "modelo": config.LLM_MODEL,      # modelo de conversa ativo (:8090)
        "embedding": config.EMBED_MODEL, # embedding em uso (:8081)
        "gpu_modo": config.GPU_MODO,     # 'todos' | 'somente_llms' (badge 🎮)
        "embed_manual_off": modelos.embed_manual_off(),
        "llm_manual_off": modelos.llm_manual_off(),
        "vl_manual_off": modelos.vl_manual_off(),
        "mock": bool(getattr(config, "MOCK_LLM", False)),  # fita 🧪 na webui
    }
    _STATUS_CACHE.update(t=agora, dados=dados)
    return dados


def _estado_rabbit() -> dict:
    try:
        f = fila.estado()
        return {"name": "RabbitMQ", "ok": bool(f.get("online")),
                "detail": (f"{f.get('pendentes', 0)} na fila · "
                           f"{f.get('mortas', 0)} na DLQ"
                           if f.get("online") else "offline")}
    except Exception as e:
        return {"name": "RabbitMQ", "ok": False, "detail": str(e)[:60]}


@app.get("/api/collections")
def collections():
    """Coleções com pontos, dimensão e metadados do catálogo (categoria/descrição)."""
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=10, check_compatibility=False)
        info = _scan_collections(client)
        meta = catalog.list_meta(client)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Qdrant indisponível: {e}")
    return [
        {"nome": n, **i,
         **meta.get(n, {"categoria": "", "descricao": "", "grupo": ""}),
         "grupo": meta.get(n, {}).get("grupo", ""),
         "catalogada": n in meta}
        for n, i in sorted(info.items())
    ]


@app.delete("/api/collections/{nome}")
def apagar_collection(nome: str):
    """Apaga a coleção do Qdrant E a entrada do catálogo (sem desfazer)."""
    if nome == catalog.CATALOG_COLLECTION:
        raise HTTPException(status_code=400, detail="o catálogo não pode ser apagado")
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    if not client.collection_exists(nome):
        raise HTTPException(status_code=404, detail=f"Coleção '{nome}' não existe")
    pontos = client.count(nome, exact=True).count
    client.delete_collection(nome)
    catalog.remove_collection_meta(client, nome)
    print(f"🗑️  Coleção '{nome}' apagada ({pontos} pontos)")
    return {"removida": nome, "pontos": pontos}


# ---------- ingestão de pasta enviada pelo navegador (máx. 30 MB) ----------

UPLOAD_MAX = 30 * 1024 * 1024
_EXTS_UPLOAD = {".txt", ".md", ".mdx", ".rst", ".pdf",
                ".cs", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go",
                ".rs", ".java", ".rb", ".kt", ".swift", ".php", ".c", ".cpp",
                ".h", ".hpp", ".sh", ".ps1", ".vue", ".svelte", ".sql",
                ".csproj", ".json", ".yaml", ".yml", ".toml"}


@app.post("/api/ingest/upload")
async def ingest_upload(request: Request, files: list[UploadFile],
                        colecao: str = Form(""), rapido: bool = Form(True),
                        dry_run: bool = Form(False)):
    """Recebe os arquivos da pasta selecionada NO NAVEGADOR (máx. 30 MB no
    total) e dispara a ingestão como job — o mesmo pipeline do /api/ingest.

    O frontend envia cada arquivo com o caminho RELATIVO da pasta como nome,
    e a estrutura de subpastas é preservada em datasets/upload/.

    `colecao`/`rapido` são Form (multipart): sem o Form() o FastAPI lia da
    QUERY e o slug digitado na webui era silenciosamente ignorado — a
    coleção caía no nome do arquivo."""
    _usuario(request)  # exige login
    total = 0
    destinos = []
    raiz = Path("datasets/upload") / f"ing_{int(time.time() * 1000) % 10**10}"
    for f in files:
        rel = Path(f.filename or "arquivo")
        if ".." in rel.parts or rel.is_absolute():
            continue  # caminho suspeito: ignora
        if rel.suffix.lower() not in _EXTS_UPLOAD:
            continue  # só o que a ingestão lê
        conteudo = await f.read()
        total += len(conteudo)
        if total > UPLOAD_MAX:
            raise HTTPException(status_code=413,
                                detail="a pasta passa de 30 MB — selecione menos "
                                       "arquivos (só .txt/.md/.pdf entram)")
        destino = raiz / rel
        # mkdir + write em THREADPOOL: I/O de disco não bloqueia o event loop
        await asyncio.to_thread(destino.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destino.write_bytes, conteudo)
        destinos.append(destino)
    if not destinos:
        raise HTTPException(status_code=400,
                            detail="nenhum .txt/.md/.mdx/.rst/.pdf encontrado na seleção")
    print(f"📥 Upload de pasta: {len(destinos)} arquivo(s), {total/1e6:.1f} MB → {raiz}")

    # coleção: slug do operador vence; senão nome da PASTA selecionada; senão
    # nome do primeiro arquivo (arquivos soltos) — o job decide o resto
    nome_pasta = ""
    for d in sorted(destinos, key=lambda x: len(x.relative_to(raiz).parts)):
        partes = d.relative_to(raiz).parts
        if len(partes) > 1:
            nome_pasta = partes[0]
            break
    colecao_final = colecao.strip() or nome_pasta or destinos[0].stem or None

    # 👁️ MODO REVISÃO: dry-run — os arquivos seguem em datasets/upload/ e o
    # job devolve o pid do relatório (nada é gravado até o aplicar)
    if dry_run:
        from core import preview as _pv
        job = _preview_disparar(
            lambda log: _pv.docs_pasta(str(raiz), log=log), colecao_final)
        return {"preview_job": job, "arquivos": len(destinos), "bytes": total}

    # mesmo job do /api/ingest (reaproveita a fila) — FÁBRICA permite
    # re-executar após restart (os arquivos seguem em datasets/upload/)
    job = _ingest.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("ingestao")
            _ingest.iniciar(jid)
            try:
                _ingest.concluir(jid, result=ingest_folder(
                    str(raiz), colecao_final, rapido,
                    log=lambda m: _ingest_log(jid, m)))
            except Exception as e:
                print(f"❌ Erro na ingestão (upload): {e}")
                _ingest.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "ingest_upload", {"job": job}, _ingest)
    return {"job": job, "arquivos": len(destinos), "bytes": total}



# ---------- manutenção como JOB (analisar/agrupar/dividir com log ao vivo) ----

_manutencao = JobRegistry("man", "manutenção")


class ManutencaoIn(BaseModel):
    acao: str  # "analisar" | "agrupar" | "dividir"
    colecao: str | None = None
    apagar_original: bool = False


@app.post("/api/manutencao")
def manutencao(body: ManutencaoIn, request: Request):
    """Dispara análise/agrupamento/divisão como JOB (log ao vivo no popup)."""
    return _manutencao_disparar(body.acao, body.colecao, body.apagar_original)


def _manutencao_disparar(acao: str, colecao: str | None = None,
                         apagar_original: bool = False) -> dict:
    if acao not in ("analisar", "agrupar", "dividir"):
        raise HTTPException(status_code=400, detail=f"ação '{acao}' inválida")
    if acao == "dividir" and not colecao:
        raise HTTPException(status_code=400, detail="dividir exige uma coleção")
    job = _manutencao.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("manutencao")
            _manutencao.iniciar(jid)
            try:
                if acao == "analisar":
                    _manutencao.log(jid, "🤖 A LLM está lendo amostras de cada coleção…")
                    _manutencao.concluir(jid, result={"resultados": analyze_all()})
                elif acao == "agrupar":
                    _manutencao.log(jid, "🗂️ A LLM está agrupando as coleções por objetivo…")
                    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                                          check_compatibility=False)
                    _manutencao.concluir(jid, result=catalog.agrupar(client))
                else:
                    _manutencao.log(jid, f"📦 Dividindo '{colecao}' por tema "
                                         "(uma chamada de LLM por arquivo)…")
                    _manutencao.concluir(jid, result=enrich_collection(
                        colecao, apagar_original))
            except Exception as e:
                print(f"❌ Erro na manutenção ({acao}): {e}")
                _manutencao.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "manutencao", {"job": job}, _manutencao)
    return {"job": job, "acao": acao, "status": f"/api/manutencao/status/{job}"}


_rota_status("/api/manutencao/status/{job}", _manutencao,
             "Job de manutenção não encontrado")


_MASCARA = "••••••••"  # valor devolvido p/ segredos (PUT ignora quem mandar de volta)


@app.get("/api/settings")
def get_settings(request: Request):
    """Configurações atuais + metadados dos campos + presets de modelo
    (formulário). EXCLUSIVO do administrador. Segredos vêm MASCARADOS — o
    valor real nunca transita ao browser."""
    _exigir_admin(request)
    valores = dict(config.as_dict())
    if valores.get("SERPER_API_KEY"):
        valores["SERPER_API_KEY"] = _MASCARA
    return {"values": valores, "fields": config.FIELDS,
            "modelos": config.MODELOS, "embeddings": config.EMBEDDINGS}


@app.put("/api/settings")
def put_settings(body: SettingsIn, request: Request):
    """Grava as alterações no .env e recarrega a configuração em memória.
    EXCLUSIVO do administrador. Valores mascarados (segredos) são ignorados —
    quem quer trocar apaga o campo e escreve o novo."""
    _exigir_admin(request)
    _CHAVES_MODELO = ("LLM_MODEL", "LLM_BASE_URL", "EMBED_MODEL", "EMBED_BASE_URL")
    valores = {k: v for k, v in body.values.items() if v != _MASCARA}
    if any(k in _CHAVES_MODELO for k in valores) and _jobs_ativos():
        ativos = ", ".join(f"{n} de {t}" for t, n in _jobs_ativos().items())
        raise HTTPException(status_code=409,
                            detail=f"há job(s) em andamento ({ativos}) — aguarde "
                                   "concluir para trocar modelo/embedding")
    # valida os TIPOS antes de gravar: valor inválido no .env envenenaria o
    # config.reload() e quebraria o boot da API até correção manual
    invalidos = []
    for key, value in valores.items():
        tipo = config.FIELDS.get(key, (None, None, "str"))[2]
        try:
            if tipo == "int":
                int(str(value).strip())
            elif tipo == "float":
                float(str(value).strip())
        except (TypeError, ValueError):
            invalidos.append(key)
    if invalidos:
        raise HTTPException(status_code=422,
                            detail=f"valor inválido para: {', '.join(invalidos)} "
                                   f"(confira o tipo de cada campo)")
    changed = []
    for key, value in valores.items():
        if key in config.FIELDS:  # só aceita chaves conhecidas
            config.set_env_inplace(key, str(value))
            changed.append(key)
    config.reload()
    print(f"⚙️  Configurações salvas no .env: {changed}")
    return {"changed": changed, "settings": config.as_dict()}


# ---------- Ingestão com log ao vivo (wizard da webui) ----------

_INGEST_ETAPAS = (  # emoji inicial da linha -> etapa do wizard
    ("📂", "ler"), ("📄", "ler"), ("🏷️", "categorizar"), ("✂️", "dividir"),
    ("🧮", "embedding"), ("🆕", "colecao"), ("♻️", "colecao"), ("⬆️", "indexar"),
    ("🗂️", "catalogo"), ("✅", "fim"),
)
_ingest = JobRegistry("ing", "ingestão")


def _ingest_log(job: str, msg: str):
    """Callback de log da ingestão: guarda a linha + a etapa correspondente."""
    etapa = next((nome for emoji, nome in _INGEST_ETAPAS
                  if msg.lstrip().startswith(emoji)), None)
    _ingest.log(job, msg, etapa=etapa)


@app.post("/api/ingest")
def ingest(body: IngestIn):
    """Indexa em SEGUNDO PLANO; acompanhe as etapas em /api/ingest/status/{job}."""
    job = _ingest.novo_id()
    pasta, colecao, rapido = body.folder, body.collection, body.rapido

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("ingestao")
            _ingest.iniciar(jid)
            try:
                _ingest.concluir(jid, result=ingest_folder(
                    pasta, colecao, rapido, log=lambda m: _ingest_log(jid, m)))
            except Exception as e:
                print(f"❌ Erro na ingestão: {e}")
                _ingest.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "ingest", {"job": job}, _ingest)
    return {"job": job, "status": f"/api/ingest/status/{job}"}


# ---------- 🤗 HuggingFace como FONTE da ingestão ---------------------------

class HfIn(BaseModel):
    query: str
    colecao: str | None = None
    limite: int = 12


@app.post("/api/ingest/hf")
def ingest_hf(body: HfIn):
    """Busca DATASETS no HuggingFace Hub e ingere os CARDS (README.md)
    higienizados — mesma esteira de qualquer ingestão. Job na fila com
    log ao vivo em /api/ingest/status/{job}."""
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="informe o que buscar no Hub")
    job = _ingest.novo_id()
    query, colecao, limite = body.query.strip(), body.colecao, body.limite

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("ingestao")
            _ingest.iniciar(jid)
            try:
                _ingest.concluir(jid, result=hf.ingest_hf(
                    query, colecao, limite, log=lambda m: _ingest_log(jid, m)))
            except Exception as e:
                print(f"❌ Erro na ingestão HuggingFace: {e}")
                _ingest.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "ingest_hf", {"job": job}, _ingest)
    return {"job": job, "status": f"/api/ingest/status/{job}"}


# ---------- 👁️ Modo Revisão (Fase A): dry-run da ingestão ---------------

_preview = JobRegistry("prev", "pré-visualização")


class PreviewIn(BaseModel):
    fonte: str = "pasta"        # "pasta" (caminho no servidor) | "hf"
    pasta: str = ""
    query: str = ""             # hf: o que buscar no Hub
    limite: int = 8             # hf: máx. de datasets
    ids: list[str] | None = None  # hf: datasets MARCADOS na UI (seleção explícita)
    colecao: str | None = None  # alvo do gate de tema (opcional)


class PreviewAplicarIn(BaseModel):
    preview: str
    ids: list[int]
    colecao: str


def _preview_disparar(docs_fn, colecao_alvo: str | None) -> str:
    """Job de dry-run: docs_fn(log) devolve os Documents; o resultado traz o
    `preview` (pid) para a webui abrir o painel de revisão."""
    from core import preview as _pv
    job = _preview.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("ingestao")
            _preview.iniciar(jid)
            try:
                docs = docs_fn(lambda m, g='': _preview.log(jid, m, grupo=g or 'geral'))
                preparados, resp = _pv.analisar(
                    docs, colecao_alvo, log=lambda m, g='': _preview.log(jid, m, grupo=g or 'geral'))
                pid = uuid.uuid4().hex[:10]
                _pv.guardar(pid, preparados, resp)
                _preview.concluir(jid, result={"preview": pid, **resp["resumo"]})
            except Exception as e:
                print(f"❌ Erro na pré-visualização: {e}")
                _preview.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "preview", {"job": job}, _preview)
    return job


@app.get("/api/hf/datasets")
def hf_datasets(q: str = "", limite: int = 24, request: Request = None):
    """Datasets do HuggingFace para SELECIONAR na Biblioteca (pedido do
    dono: "aparecer tudo o que o HF oferece, selecionar e incluir numa
    coleção"). Busca por relevância com ordenação por downloads; `q` vazio
    lista os MAIS BAIXADOS (tudo, paginável pelo limite). Com HF_TOKEN a
    resposta ganha `meus` — TODOS os datasets da CONTA do dono (inclusive
    privados) em seção própria na UI. Sem token a API pública serve."""
    _usuario(request)
    from core import hf as _hf
    limite = max(1, min(limite, 200))
    if not (q or "").strip():
        achados = _hf.populares(limite, log=lambda m, g="": None)
    else:
        achados = _hf.buscar(q.strip(), limite, log=lambda m, g="": None)
    conta = _hf.meus(log=lambda m, g="": None)
    return {"datasets": achados,
            "token": bool(getattr(config, "HF_TOKEN", "")),
            "usuario": conta["usuario"],
            "meus": conta["datasets"]}


@app.post("/api/ingest/preview")
def ingest_preview(body: PreviewIn):
    """DRY-RUN da ingestão: pipeline inteiro (leitura → limpeza → chunks) e
    PARA antes do Qdrant. O relatório (como veio/como vai entrar, duplicados,
    categorias por cluster, aderência ao tema via reranker) sai em
    /api/ingest/preview/{pid}; aplicar os aprovados em
    /api/ingest/preview/aplicar."""
    from core import preview as _pv
    if body.fonte == "hf":
        if not body.ids and not body.query.strip():
            raise HTTPException(status_code=400, detail="informe o que buscar no Hub (ou marque datasets da lista)")
        query, limite = body.query.strip(), body.limite
        ids_sel = body.ids or None
        job = _preview_disparar(
            lambda log: _pv.docs_hf(query, limite, log=log, ids=ids_sel),
            body.colecao)
    elif body.fonte == "pasta":
        if not body.pasta.strip():
            raise HTTPException(status_code=400, detail="informe a pasta no servidor")
        pasta = body.pasta.strip()
        job = _preview_disparar(
            lambda log: _pv.docs_pasta(pasta, log=log), body.colecao)
    else:
        raise HTTPException(status_code=400,
                            detail=f"fonte '{body.fonte}' inválida (pasta|hf)")
    return {"job": job, "status": f"/api/ingest/preview/status/{job}"}


_rota_status("/api/ingest/preview/status/{job}", _preview,
             "Pré-visualização não encontrada")


@app.get("/api/ingest/preview/{pid}")
def preview_ver(pid: str):
    """Relatório completo do dry-run (documentos, chunks, clusters, tema)."""
    from core import preview as _pv
    resp = _pv.ver(pid)
    if not resp:
        raise HTTPException(status_code=410,
                            detail="pré-visualização expirada (30 min) — rode de novo")
    return resp


class WebSalvarIn(BaseModel):
    """Ensinar a base com as FONTES de uma resposta do chat (pedido do
    dono: 'o que precisa para o qdrant ficar inteligente' — o conhecimento
    que a 🌐 pesquisa-web trouxe ficava só NA CONVERSA e se perdia)."""
    colecao: str
    documentos: list[dict] = []   # [{titulo, url, content}]


@app.post("/api/ingest/web-salvar")
def web_salvar(body: WebSalvarIn, request: Request):
    """Grava os documentos usados numa resposta (web/base) na coleção —
    job normal de ingestão com proveniência 'web via chat'. A partir daí
    a MESMA pergunta responde no modo rag, sem web e sem custo."""
    _usuario(request)
    colecao = (body.colecao or "").strip()
    if not re.fullmatch(r"[a-z0-9_\-]{2,40}", colecao):
        raise HTTPException(status_code=400,
                            detail="coleção inválida (a-z, 0-9, _ -)")
    docs = [d for d in (body.documentos or [])
            if str(d.get("content") or d.get("page_content") or "").strip()]
    if not docs:
        raise HTTPException(status_code=400, detail="nenhum documento com conteúdo")
    job = _ingest.novo_id()

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("ingestao")
            _ingest.iniciar(jid)
            try:
                from datetime import datetime, timezone
                agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
                from langchain_core.documents import Document
                langdocs = []
                for d in docs[:12]:
                    conteudo = str(d.get("content") or d.get("page_content"))
                    url = str(d.get("url") or "")[:500]
                    titulo = str(d.get("titulo") or url or "fonte web")[:200]
                    langdocs.append(Document(
                        page_content=conteudo,
                        metadata={"arquivo": titulo, "titulo": titulo,
                                  "url": url, "adquirido_em": agora,
                                  "curadoria": "web via chat"}))
                _ingest_log(jid, f"📚 ensinando a base: {len(langdocs)} fonte(s) "
                                 f"→ '{colecao}' (proveniência: web via chat)")
                r = ingest_docs(langdocs, colecao, rapido=True,
                                log=lambda m: _ingest_log(jid, m))
                _ingest.concluir(jid, result=r)
            except Exception as e:
                _ingest.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "ingest_web", {"job": job}, _ingest)
    return {"job": job, "status": f"/api/ingest/status/{job}"}


@app.post("/api/ingest/preview/aplicar")
def preview_aplicar(body: PreviewAplicarIn):
    """Ingerir SÓ os documentos aprovados da revisão (job de ingestão
    normal — status em /api/ingest/status/{job})."""
    from core import preview as _pv
    job = _ingest.novo_id()
    pedido = body.model_dump()

    def fabricar(p: dict):
        jid = p["job"]
        corpo = PreviewAplicarIn(**pedido)

        def rodar():
            contadores.set_servico("ingestao")
            _ingest.iniciar(jid)
            try:
                _ingest.concluir(jid, result=_pv.aplicar(
                    corpo.preview, corpo.ids, corpo.colecao,
                    log=lambda m: _ingest_log(jid, m)))
            except Exception as e:
                print(f"❌ Erro ao aplicar pré-visualização: {e}")
                _ingest.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "preview_aplicar", {"job": job}, _ingest)
    return {"job": job, "status": f"/api/ingest/status/{job}"}


# ---------- 🔬 Pesquisa profunda com evidências (Fase B / F4) ---------------

_pesquisa = JobRegistry("pesq", "pesquisa profunda")


class PesquisaIn(BaseModel):
    assunto: str
    colecao: str | None = None   # alvo do gate de tema na revisão
    fontes: int = 6              # máx. de fontes baixadas


@app.post("/api/pesquisa")
def pesquisa_rota(body: PesquisaIn):
    """Pesquisa PROFUNDA como job: planner → busca (wikipedia/serper/ddg/
    github) → fetch da página inteira → claims com evidência → síntese com
    citações e conflitos. O resultado termina no MODO REVISÃO (dry-run):
    `result.preview` abre o painel e NADA entra no Qdrant sem aprovação."""
    if not body.assunto.strip():
        raise HTTPException(status_code=400, detail="informe o assunto")
    from core import pesquisa as _core_pesquisa, preview as _pv
    job = _pesquisa.novo_id()
    assunto, colecao, fontes = body.assunto.strip(), body.colecao, body.fontes

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("pesquisa")
            _pesquisa.iniciar(jid)
            try:
                docs, resumo = _core_pesquisa.pesquisar(
                    assunto, fontes,
                    log=lambda m, g="geral": _pesquisa.log(jid, m, grupo=g),
                    colecao_alvo=colecao)  # filtro incremental vs o índice
                preparados, resp = _pv.analisar(
                    docs, colecao,
                    log=lambda m, g="geral": _pesquisa.log(jid, m, grupo=g))
                # 📦 KI no metadata do doc de SÍNTESE (auditoria no Qdrant:
                # chunk→doc→fonte→data sem reabrir o job)
                ki = resumo.get("ki")
                if ki:
                    for d in preparados:
                        if d.metadata.get("sintese"):
                            d.metadata["ki"] = ki
                pid = uuid.uuid4().hex[:10]
                _pv.guardar(pid, preparados, resp)
                _pesquisa.concluir(jid, result={"preview": pid, **resumo,
                                                **resp["resumo"]})
            except Exception as e:
                print(f"❌ Erro na pesquisa profunda: {e}")
                _pesquisa.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "pesquisa", {"job": job}, _pesquisa)
    return {"job": job, "status": f"/api/pesquisa/status/{job}"}


_rota_status("/api/pesquisa/status/{job}", _pesquisa,
             "Job de pesquisa não encontrado")


# ---------- 📸 Snapshots restauráveis (F5) — reversibilidade ----------

@app.get("/api/snapshot")
def snapshot_listar():
    """Snapshots disponíveis (logs/snapshots) — coleção, pontos, motivo."""
    from core import snapshot as _snap
    return _snap.listar()


class SnapshotRestaurarIn(BaseModel):
    arquivo: str
    colecao: str | None = None  # padrão: a do snapshot


# ⚠️ ORDEM IMPORTA: /api/snapshot/restaurar precisa vir ANTES de
# /api/snapshot/{colecao} — o path-param engole rotas literais posteriores
# (bug pego em produção: POST .../restaurar virava snapshot da coleção
# "restaurar" → 500)
@app.post("/api/snapshot/restaurar")
def snapshot_restaurar(body: SnapshotRestaurarIn, request: Request):
    """Recria a coleção a partir do snapshot (APAGA a atual antes)."""
    _exigir_admin(request)
    from core import snapshot as _snap
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    return _snap.restaurar(client, body.arquivo, body.colecao,
                           log=lambda m: print(m))


@app.post("/api/snapshot/{colecao}")
def snapshot_criar_rota(colecao: str, request: Request,
                        motivo: str = ""):
    """Fotografa a coleção (id+vetor+payload) antes de uma reforma —
    admin only (o arquivo vive no disco do servidor)."""
    _exigir_admin(request)
    from core import snapshot as _snap
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    arq = _snap.criar(client, colecao, motivo=motivo,
                      log=lambda m: print(m))
    return {"arquivo": arq, "snapshots": len(_snap.listar())}


# ---------- ⏹ Parar tudo (botão de pânico do operador) ----------------------

@app.post("/api/parar_tudo")
def parar_tudo(request: Request):
    """Mata TODOS os jobs em curso, PURGA a fila Rabbit (+DLQ) publicando
    evento de cancelamento e DESMONTA todos os motores de GPU liberando a
    VRAM. EXCLUSIVO do administrador — o botão de pânio do operador."""
    import os
    import signal
    _exigir_admin(request)
    log = lambda m: print(f"⏹ {m}")  # noqa: E731
    log("PARAR TUDO acionado pela aplicação")
    # 1) mata jobs em curso (registros marcados como cancelados) — AGORA
    # cada lock é segurado pelo cancelar_todos (antes 7 registros eram
    # iterados SEM lock: corrida com _xxx_log podia dar KeyError)
    cancelados = []
    for reg in TODOS_JOBS:
        cancelados += reg.cancelar_todos("cancelado (⏹ parar tudo)")
    tarefas.cancelar_todas()
    log(f"jobs cancelados: {len(cancelados)}")
    # 2) fila: purga tarefas + DLQ e publica o evento de cancelamento
    purga = fila.purgar_tudo()
    log(f"fila Rabbit purgada: {purga}")
    # 3) motores: desmonta todos liberando a VRAM — NO HOST (em container,
    # os processos pertencem ao host: proxy ao agente; senão matava nada
    # e reportava "derrubado" mentindo)
    if config.EM_CONTAINER:
        try:
            motores = modelos._chamar_agente("/parar_tudo", timeout=120)
            log(f"motores (via agente): {motores}")
        except RuntimeError as e:
            motores = {"derrubados": [], "erro": str(e)[:200]}
    else:
        motores = modelos.derrubar_todos_motores(log=log)
    # 3b) reranker cross-encoder (CPU, residente no PRÓPRIO processo da API):
    # solta o modelo da memória junto com os motores (política Parar tudo)
    rerank.descarregar()
    telemetria.evento("rabbit", "⏹ PARAR TUDO executado",
                      jobs=len(cancelados), fila=purga.get("purgados"),
                      motores=motores.get("derrubados"))
    return {"ok": True, "jobs_cancelados": cancelados, "fila": purga,
            "motores": motores}


_rota_status("/api/ingest/status/{job}", _ingest,
             "Job de ingestão não encontrado")


# ---------- Higienização de coleções (ruído/duplicados/re-embed) ----------

_higieniza = JobRegistry("hig", "higienização")


@app.post("/api/higienizar")
def higienizar(body: HigienizarIn):
    """Limpa a coleção em SEGUNDO PLANO (texto, ruído, duplicados, re-embed);
    acompanhe em /api/higienizar/status/{job}?cursor=N."""
    job = _higieniza.novo_id()
    colecao = body.collection

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("limpeza")
            _higieniza.iniciar(jid)
            try:
                _higieniza.concluir(jid, result=higienizar_colecao(
                    colecao, log=lambda m, g='': _higieniza.log(jid, m, grupo=g or 'geral')))
            except Exception as e:
                print(f"❌ Erro na higienização: {e}")
                _higieniza.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "higienizar", {"job": job}, _higieniza)
    return {"job": job, "status": f"/api/higienizar/status/{job}"}


_rota_status("/api/higienizar/status/{job}", _higieniza,
             "Job de higienização não encontrado")


# ---------- Limpeza completa (higienização + varredura num job só) ----------

_limpeza = JobRegistry("lim", "limpeza")
# sandbox como JOB (imune ao 524 da borda): registro + rota de status
_sbx = JobRegistry("sbx", "sandbox")
_rota_status("/api/sandbox/status/{job}", _sbx,
             "Teste de sandbox não encontrado")


# ---------- 👁 Multimídia (análise multimodal como módulo próprio) ----------
# Decisão (27/08): NÃO forkar o SwarmUI — aplicação standalone C#/.NET
# focada em GERAÇÃO t2i via backends ComfyUI, sem análise (i2t) de
# provedores, sem RAG/chat/MCP/Qdrant. A bancada própria já tem tudo
# (legendar_imagem local+externo, upload, jobs com log); falta era o
# LUGAR na UI. Ver AGENTS.md "Módulo Multimídia".
_midia = JobRegistry("mid", "multimídia")
_rota_status("/api/midia/status/{job}", _midia,
             "Análise multimodal não encontrada")


class MidiaAnalisarIn(BaseModel):
    """Análise multimodal num arquivo subido (/api/upload → saidas/entrada):
    imagem → descrição/resposta com o modelo 👁 local (Qwen2.5-VL, pausa o
    chat e restaura) OU externo (`prov:modelo` — glm-4.5v/gpt-5/claude/
    gemini; GPU local intocada)."""
    arquivo: str
    pergunta: str = ""
    modelo: str = ""


@app.post("/api/midia/analisar")
def midia_analisar(body: MidiaAnalisarIn, request: Request):
    """Módulo Multimídia: ANALISA imagem com o multimodal escolhido — job
    com log ao vivo (subida do modelo local, telemetria de tokens) e
    resultado pronto para ENSINAR A BASE (web-salvar)."""
    _usuario(request)
    from core import midia as _m
    # ⚠️ anti path-traversal: o arquivo TEM que estar em saidas/entrada
    nome = Path(body.arquivo or "").name
    alvo = _m.ENTRADA / nome
    if not nome or not alvo.exists():
        raise HTTPException(422, f"arquivo '{nome}' não encontrado — suba "
                            "uma imagem no painel antes de analisar")

    def _fabricar(payload: dict):
        jid = payload["job"]

        def rodar():
            _midia.log(jid, f"👁 análise multimodal de {payload['nome']}"
                           + (f" com {payload['modelo']}"
                              if payload["modelo"] else " (local Qwen2.5-VL)"),
                       etapa="análise")
            try:
                modelo = payload["modelo"]
                if ":" in modelo:
                    from core import provedores as _prov
                    pid, _ = modelo.split(":", 1)
                    if not _prov.resolver(pid, modelo.split(":", 1)[1]):
                        raise RuntimeError(
                            f"provedor {pid.upper()} não configurado — cole a "
                            "chave dele em Sistema → ☁️ Cadastrar provedor "
                            "cloud e tente de novo")
                if not modelo and config.EM_CONTAINER:
                    # LOCAL em CONTAINER: a GPU está na ESTAÇÃO — a imagem
                    # (b64, o upload vive no volume DA VPS) viaja ao AGENTE
                    # do host, que sobe o :8082 e analisa lá
                    import base64 as _b64
                    with open(payload["arquivo"], "rb") as f:
                        img_b64 = _b64.b64encode(f.read()).decode()
                    r = modelos._chamar_agente(
                        "/visao", {"b64": img_b64, "nome": payload["nome"],
                                   "pergunta": payload["pergunta"]},
                        timeout=420)
                    analise = r.get("descricao", "")
                else:
                    analise = _m.legendar_imagem(
                        payload["arquivo"], payload["pergunta"] or None,
                        modelo=modelo,
                        # ⚠️ JobRegistry.log(jid, msg, **extra): grupo é KWARG
                        # (3 args posicionais mandavam o job pra DLQ)
                        log=lambda msg, g="": _midia.log(
                            jid, msg, **({"etapa": g} if g else {})))
                _midia.concluir(jid, result={
                    "analise": analise, "arquivo": payload["nome"],
                    "modelo": modelo or "local"})
            except Exception as e:
                _midia.concluir(jid, error=str(e)[:400])
        return rodar

    job = _midia.novo_id()
    _despachar(_fabricar, "midia",
               {"arquivo": str(alvo), "nome": nome,
                "pergunta": body.pergunta.strip(), "modelo": body.modelo.strip(),
                "job": job}, _midia)
    return {"job": job}


@app.get("/midia")
def midia_pagina(request: Request):
    """👁 MÓDULO MULTIMÍDIA (pedido do dono): analisar imagens com QUALQUER
    multimodal — TODOS listados (pedido: 'aparecer todos os modelos que
    tenho, tanto os locais quanto os cloud'): GGUFs de visão da estação,
    👁 dos provedores CADASTRADOS e 👁 típicos dos conhecidos 🔑 (que
    ainda não têm chave — o job orienta o cadastro ao usar)."""
    ctx = _paginas_ctx(request, "midia")
    grupos = [{"rotulo": "🖥 local (GPU da estação)", "modelos": []},
              {"rotulo": "🌐 provedores cadastrados", "modelos": []},
              {"rotulo": "🔑 conhecidos — requer cadastro/chave", "modelos": []}]
    # 1) locais: TODOS os GGUFs de visão da estação (modelos.listar)
    try:
        for m in modelos.listar():
            if m.get("categoria") == "visao":
                grupos[0]["modelos"].append({
                    "id": "", "nome": m["nome"],
                    "info": "analisa na GPU local (pausa o chat e restaura)"})
    except Exception:
        pass
    if not grupos[0]["modelos"]:
        grupos[0]["modelos"].append({"id": "", "nome": "Qwen2.5-VL (local)",
                                     "info": "GPU da estação"})
    # 2) cloud CADASTRADOS (cat=visao) · 3) conhecidos sem cadastro (🔑)
    cadastrados = set()
    try:
        from core import provedores as _prov
        for p in _prov.listar():
            cadastrados.add(p["id"])
            for m in p.get("modelos", []):
                if m.get("cat") == "visao":
                    grupos[1]["modelos"].append({
                        "id": f"{p['id']}:{m['nome']}",
                        "nome": f"{m['nome']} · {p['nome']}",
                        "info": (m.get("uso") or m.get("info") or "")
                                + (f" · ctx {m['ctx'] // 1000}k"
                                   if m.get("ctx") else "")})
        for pid, c in _prov.CONHECIDOS.items():
            if pid in cadastrados:
                continue   # já cadastrado: os modelos VEM da API dele
            for nome in c.get("visao", []):
                grupos[2]["modelos"].append({
                    "id": f"{pid}:{nome}", "nome": f"{nome} · {c['nome']}",
                    "info": f"requer chave — cadastre o provedor {pid.upper()} "
                            f"no Sistema ({c['site']})"})
    except Exception:
        pass
    ctx["grupos_visao"] = [g for g in grupos
                           if g["modelos"] or "cadastrados" in g["rotulo"]]
    # 🎨🎬 GERAÇÃO local (Flux/Wan da estação — pedido do dono: "cadê meus
    # modelos locais de geração?"): categorias imagem/video do modelos.listar
    try:
        ger = {"imagem": [], "video": []}
        for m in modelos.listar():
            if m.get("categoria") in ger:
                ger[m["categoria"]].append(
                    {"nome": m["nome"],
                     "gb": m.get("gb"),
                     "info": "pausa as LLMs durante a geração (8 GB de VRAM)"})
        ctx["geracao"] = ger
    except Exception:
        ctx["geracao"] = {"imagem": [], "video": []}
    return TEMPLATES.TemplateResponse(request, "midia.html", ctx)


@app.post("/api/limpeza")
def rota_limpeza(body: HigienizarIn):
    """LIMPEZA COMPLETA da coleção em 2º plano — as duas etapas que antes
    eram botões separados, agora numa sequência só:
    1) higienização: texto normalizado (frases reconstituídas, ruído de
       página fora), duplicados exatos removidos, o que mudou é re-embedado;
    2) varredura LLM: cada chunk julgado contra o assunto da coleção —
       só lixo claro sai (na dúvida, mantém).
    Acompanhe em /api/limpeza/status/{job}?cursor=N."""
    job = _limpeza.novo_id()
    colecao = body.collection

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("limpeza")
            resultado = {}
            _limpeza.iniciar(jid)
            try:
                _limpeza.log(jid, "🧹 ETAPA 1/2 — higienização de texto (reconstrução de frases, "
                                  "remoção de ruído e duplicados, re-embedding):")
                resultado["higienizacao"] = higienizar_colecao(
                    colecao, log=lambda m: _limpeza.log(jid, "   " + m))
                _limpeza.log(jid, "🔎 ETAPA 2/2 — varredura LLM (julgamento de cada chunk "
                                  "contra o assunto da coleção):")
                resultado["varredura"] = varredura_colecao(
                    colecao, log=lambda m: _limpeza.log(jid, "   " + m))
                _limpeza.concluir(jid, result=resultado)
            except Exception as e:
                print(f"❌ Erro na limpeza completa: {e}")
                _limpeza.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "limpeza", {"job": job}, _limpeza)
    return {"job": job, "status": f"/api/limpeza/status/{job}"}


_rota_status("/api/limpeza/status/{job}", _limpeza,
             "Job de limpeza não encontrado")


# ---------- Estúdio: modalidades, tarefas em 2º plano, upload, mídia -------

@app.get("/api/modalidades")
def get_modalidades():
    """As modalidades do estúdio com disponibilidade real e ETA calibrado."""
    return modalidades.listar()


# ---------- fluxos de geração: builtins locais + serviços externos ----------

@app.get("/api/fluxos")
def get_fluxos():
    """Registry de fluxos de geração (F1b-4): builtins do estúdio (sd-cli,
    wan2.2) e EXTERNOS (wan2gp, ComfyUI) com health-check na URL do .env —
    cada card da aba Estúdio mostra 1 linha do que faz + status."""
    from core import fluxos
    return fluxos.listar()


@app.get("/api/estudio")
def estudio():
    """Estado do estúdio: tarefa ocupando a VRAM + parâmetros de memória
    (grupo 'Estúdio · memória' do .env — editáveis em /api/settings) e o que
    está no ar. VRAM exibida é INFORMATIVA (o app não gerencia memória)."""
    return {"ocupado": tarefas.estudio_ocupado(),
            "memoria": {"pausar_chat": bool(config.ESTUDIO_PAUSAR_CHAT),
                        "assentamento_s": config.ESTUDIO_VRAM_ASSENTAMENTO_S,
                        "restore_tentativas": config.ESTUDIO_RESTORE_TENTATIVAS,
                        "vram_mi": modelos._vram_uso_mi()},
            "servicos": {"chat": modelos.servido(modelos.CHAT_PORTA),
                         "embed": modelos.servido(modelos.EMBED_PORTA)}}


@app.get("/api/estudio/sessoes")
def estudio_sessoes(request: Request):
    """Sessões do estúdio DO USUÁRIO com as mídias geradas (persistidas em
    saidas/estudio_sessoes.json com owner)."""
    return {"sessoes": sessoes.listar(owner=_usuario(request))}


@app.post("/api/estudio/sessoes")
def estudio_criar_sessao(body: SessaoEstudioIn, request: Request):
    """Cria uma sessão nova (do usuário logado) para agrupar as gerações."""
    if not body.nome.strip():
        raise HTTPException(status_code=400, detail="informe um nome para a sessão")
    return sessoes.criar(body.nome, owner=_usuario(request))


def _sessao_estudio_do_dono(sid: str, request: Request) -> dict:
    """Sessão do estúdio do usuário logado (ou 404) — isolamento por owner."""
    s = sessoes.obter(sid)
    dono = _usuario(request)
    if not s or (s.get("owner") and s.get("owner") != dono):
        raise HTTPException(status_code=404, detail=f"sessão '{sid}' não existe")
    return s


@app.patch("/api/estudio/sessoes/{sid}")
def estudio_renomear_sessao(sid: str, body: SessaoEstudioIn, request: Request):
    s = sessoes.renomear(sid, body.nome)
    if not s or (s.get("owner") and s.get("owner") != _usuario(request)):
        raise HTTPException(status_code=404, detail=f"sessão '{sid}' não existe")
    return s


@app.delete("/api/estudio/sessoes/{sid}")
def estudio_apagar_sessao(sid: str, request: Request):
    """Apaga a sessão do REGISTRO (as mídias continuam em saidas/)."""
    _sessao_estudio_do_dono(sid, request)
    if not sessoes.apagar(sid):
        raise HTTPException(status_code=404, detail=f"sessão '{sid}' não existe")
    return {"removida": sid}


@app.get("/api/cache")
def cache_info():
    """Estado do cache semântico (Redis)."""
    return cache.info()


@app.get("/api/contagem")
def contagem_tokens():
    """📊 Uso da LLM local (llama-server :8090) — TUDO é contado pelo usage
    que o servidor devolve: por serviço (chat, ingestão, seed, limpeza,
    estúdio, manutenção, sistema/testes) e total geral."""
    return contadores.totais()


@app.get("/api/fila")
def fila_estado():
    """🐇 Estado da fila de jobs (RabbitMQ): pendentes, mortas (DLQ) e o
    modo de operação (online = via fila; offline = threads diretas)."""
    return fila.estado()


@app.post("/api/fila/dlq/reprocessar")
def fila_reprocessar():
    """Devolve as mensagens da DLQ para a fila de tarefas (reexecutar)."""
    n = fila.reliquidar_dlq()
    print(f"🐇 {n} mensagem(ns) da DLQ devolvida(s) à fila")
    return {"reprocessadas": n}


# ---------- logs dos serviços (tail em tempo real p/ o topbar) -----------

_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOGS_FONTES = {
    "chat": lambda: _mais_recente("llama-chat-*.log"),
    "embed": lambda: (_LOGS_DIR / "llama-embed.log"),
    "visao": lambda: (_LOGS_DIR / "llama-vl.log"),
    "api": lambda: (_LOGS_DIR / "api-8000.log"),
}
LOG_TAIL_LINHAS = 120


def _mais_recente(padrao: str) -> Path:
    candidatos = sorted(_LOGS_DIR.glob(padrao), key=lambda p: p.stat().st_mtime)
    return candidatos[-1] if candidatos else None


@app.get("/api/historico")
def get_historico(tipo: str = "", limit: int = 40):
    """Histórico de execuções (ingestão/seed/limpeza/manutenção/tarefas):
    o que rodou, quando, quanto durou e o que produziu."""
    return {"execucoes": historico.ultimos(tipo or None, max(1, min(limit, 200)))}


@app.get("/api/historico/log/{job}")
def historico_log(job: str):
    """Log COMPLETO gravado de um job (logs/jobs/{job}.jsonl) — linha por
    linha com ts/grupo, exatamente como rodou. `job` sanitizado (anti
    path-traversal)."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", job):
        raise HTTPException(status_code=400, detail="id de job inválido")
    arq = PASTA_LOGS_JOBS / f"{job}.jsonl"
    if not arq.is_file():
        raise HTTPException(status_code=404, detail=f"não há log gravado para '{job}'")
    linhas = []
    for l in arq.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(l)
            linhas.append({"ts": str(d.get("ts", "")), "msg": str(d.get("msg", "")),
                           "grupo": str(d.get("grupo", d.get("etapa", "")))})
        except Exception:
            continue
    return {"job": job, "total": len(linhas), "lines": linhas}


# ---------- liga/desliga do llama-server (controle pela aplicação) ---------

@app.get("/api/llm/estado")
def llm_estado(request: Request):
    """O que está servindo na :8090 (alias, VRAM) — alimenta o toggle."""
    _usuario(request)
    return {"no_ar": bool(modelos.servido(modelos.CHAT_PORTA)),
            "modelo": modelos.servido(modelos.CHAT_PORTA),
            "vram_mi": modelos._vram_uso_mi()}


@app.post("/api/llm/ligar")
def llm_ligar(request: Request):
    """Sobe o llama-server do chat com o modelo do .env (LLM_MODEL) e libera
    o ciclo automático. EXCLUSIVO do administrador — afeta todos os usuários."""
    _exigir_admin(request)
    return modelos.ligar_llm_manual()


@app.post("/api/llm/desligar")
def llm_desligar(request: Request):
    """Derruba o llama-server do chat e TRAVA a religação automática (marker
    persistido — nem o boot do agente sobe). EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        # o processo vive no host: agente derruba; marker no volume compartilhado
        modelos._chamar_agente("/porta/derrubar",
                               {"porta": modelos.CHAT_PORTA}, timeout=60)
        return modelos.desligar_llm_manual(ja_derrubado=True)
    return modelos.desligar_llm_manual()


# ---------- embedding: liga/desliga MANUAL (busca/ingestão respeitam) -------

@app.post("/api/embed/ligar")
def embed_ligar(request: Request):
    """Religa o embedding (:8081) e libera o ciclo on-demand.
    EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/embed/ligar", timeout=240)
    return modelos.ligar_embedding_manual()


@app.post("/api/embed/desligar")
def embed_desligar(request: Request):
    """Desliga o embedding (:8081) e TRAVA a religação automática — buscas/
    ingestão falham com erro claro até religar. EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/embed/desligar", timeout=60)
    return modelos.desligar_embedding_manual()


# ---------- visão: liga/desliga MANUAL (análise de imagem respeita) --------

@app.post("/api/vl/ligar")
def vl_ligar(request: Request):
    """Religa a visão (:8082, remove o marker) e a pré-aquece (sobe o
    Qwen2.5-VL agora, ~1 min). EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/vl/ligar", timeout=420)
    return modelos.ligar_vl_manual()


@app.post("/api/vl/desligar")
def vl_desligar(request: Request):
    """Desliga a visão (:8082) e BLOQUEIA o ciclo on-demand — análises de
    imagem falham com erro claro até religar. EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        modelos._chamar_agente("/porta/derrubar",
                               {"porta": modelos.VL_PORTA}, timeout=60)
        return modelos.desligar_vl_manual(ja_derrubado=True)
    return modelos.desligar_vl_manual()


# ---------- modo da GPU: 'todos' x 'somente_llms' ---------------------------

class GpuModoIn(BaseModel):
    modo: str  # "todos" | "somente_llms"


# modalidades que usam a GPU FORA dos llama-servers (difusão sd-cli e
# whisper-cli): bloqueadas quando a GPU está em 'somente_llms'
_GPU_BLOQUEADAS = {"t2i", "t2v", "i2v", "a2v", "a2t", "v2t"}
_GPU_BLOQ_TIPOS = {"audio", "video", "gif"}  # contexto de mídia usa whisper-cli


def _checar_gpu_modo(mod: str | None = None, tipo: str | None = None) -> None:
    """Política de GPU: em 'somente_llms', cargas fora dos llama-servers são
    recusadas com erro claro (o operador escolheu reservar a GPU)."""
    if config.GPU_MODO != "somente_llms":
        return
    if (mod and mod in _GPU_BLOQUEADAS) or (tipo and tipo in _GPU_BLOQ_TIPOS):
        raise HTTPException(status_code=403,
                            detail="GPU em modo 'somente LLMs' — esta operação "
                                   "usa difusão/whisper. Altere o modo no badge "
                                   "🎮 da barra do topo (ou em ⚙️ Configurações, "
                                   "GPU_MODO)")


@app.post("/api/gpu/modo")
def gpu_modo(body: GpuModoIn, request: Request):
    """Alterna o modo de uso da GPU: 'todos' (aberta) ou 'somente_llms'
    (difusão/whisper recusados). Persiste no .env. EXCLUSIVO do administrador."""
    _exigir_admin(request)
    modo = body.modo.strip()
    if modo not in ("todos", "somente_llms"):
        raise HTTPException(status_code=400,
                            detail="modo inválido: 'todos' ou 'somente_llms'")
    config.set_env_inplace("GPU_MODO", modo)
    config.reload()
    print(f"🎮 GPU em modo '{modo}'")
    return {"modo": config.GPU_MODO}


@app.get("/hx/cachepanel")
def hx_cachepanel(request: Request):
    """Painel do CACHE (badge ⚡ do topbar — pedido do dono: "quero saber
    quais caches estão carregados"): entradas REAIS do Redis — pergunta,
    quando foi guardada, escopo e trecho da resposta pronta."""
    _usuario(request)
    try:
        info = cache.info()
    except Exception:
        info = {"online": False, "entradas": 0, "lista": []}
    return TEMPLATES.TemplateResponse(request, "_cachepanel.html",
                                      {"request": request, "info": info})


@app.get("/hx/logs/{fonte}")
def hx_logs(fonte: str, request: Request):
    """TAIL AO VIVO com fonte QUE EXISTE NO SERVIDOR (pedido do dono: "os
    logs não aparecem em tempo real" — as fontes antigas apontavam arquivos
    do llama-server que vivem SÓ na estação com GPU). Agora:
      llm      → telemetria filtrada (cada chamada LLM: modelo/tokens/s)
      eventos  → telemetria completa (llm + rabbit + cache + gerações)
      jobs     → últimas linhas dos logs/jobs/*.jsonl (o que rodou)
    """
    _usuario(request)
    import html as _h
    linhas: list[str] = []
    if fonte in ("llm", "eventos", "geracao"):
        evs = telemetria.ultimos(None if fonte == "eventos"
                                 else ("llm" if fonte == "llm" else "geracao"),
                                 40)
        linhas = [f"{e.get('ts','')} · {e.get('msg','')}" for e in evs]
        linhas.reverse()   # mais recente POR ÚLTIMO (como um log)
    elif fonte == "jobs":
        try:
            arqs = sorted(PASTA_LOGS_JOBS.glob("*.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:4]
            for arq in arqs:
                with open(arq, "rb") as f:
                    f.seek(max(0, (lambda s: s)(arq.stat().st_size) - 8192))
                    txt = f.read().decode("utf-8", errors="replace")
                for l in txt.splitlines()[-6:]:
                    try:
                        d = json.loads(l)
                        linhas.append(f"{d.get('ts','')} [{arq.stem}] "
                                      f"{str(d.get('msg',''))[:150]}")
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        raise HTTPException(status_code=404, detail="fonte desconhecida")
    html = "".join(f"<div>{_h.escape(l)}</div>" for l in linhas) \
        or '<div class="mini">sem linhas ainda…</div>'
    return HTMLResponse(f'<div class="log">{html}</div>')


@app.get("/api/telemetria")
def get_telemetria(tipo: str = "", limit: int = 60):
    """Histórico PERSISTENTE dos eventos de infraestrutura (tail do
    logs/telemetria.jsonl): `tipo` = llm|rabbit|redis (redis inclui cache;
    vazio = tudo). É o "como está trabalhando" de cada peça — cada chamada
    LLM (tokens/duração), cada job no Rabbit, cada hit/miss do cache."""
    return {"eventos": telemetria.ultimos(tipo or None, max(1, min(limit, 300)))}


@app.get("/api/logs")
def logs_servico(fonte: str = "chat"):
    """Últimas linhas do log de um serviço (chat | embed | visao | api) —
    o topbar faz polling e mostra o tail ao vivo. Lê só o FINAL do arquivo
    (seek 64 KB), não o arquivo inteiro na memória."""
    alvo = _LOGS_FONTES.get(fonte)
    arquivo = alvo() if alvo else None
    if not arquivo or not arquivo.exists():
        return {"fonte": fonte, "arquivo": None, "linhas": []}
    try:  # logs são append binário do servidor: utf-8 com tolerate
        with open(arquivo, "rb") as f:
            f.seek(0, 2)  # fim
            fim = f.tell()
            f.seek(max(0, fim - 65536))
            texto = f.read().decode("utf-8", errors="replace")
        linhas = texto.splitlines()[-LOG_TAIL_LINHAS:]
        return {"fonte": fonte, "arquivo": arquivo.name, "linhas": linhas}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- zip dos arquivos de uma resposta (gerado NA HORA, sob demanda) --

def _sanear_caminho(nome: str) -> str:
    """Normaliza o nome PRESERVANDO a estrutura de pastas do retorno
    ('src/domain/News.cs' continua src/domain/News.cs no zip) — mas sem
    traversal (remove .., drive, absolutos) e sem separadores repetidos."""
    partes = [p for p in re.split(r"[\\/]+", str(nome))
              if p not in ("", ".", "..") and ":" not in p]
    caminho = "/".join(partes)
    return (caminho[:120] or "arquivo.txt")


class ZipIn(BaseModel):
    arquivos: list[dict]  # [{nome, conteudo}] — saiu da própria resposta do chat


@app.post("/api/zip")
def zip_arquivos(body: ZipIn):
    """Empacota os arquivos de código de uma resposta num .zip — gerado só
    quando o operador pede (não onera toda resposta). O CAMINHO relativo de
    cada arquivo (src/domain/…) vira pasta dentro do zip, como no retorno."""
    import io
    import zipfile
    if not body.arquivos:
        raise HTTPException(status_code=400, detail="nenhum arquivo informado")
    buf = io.BytesIO()
    usados: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for a in body.arquivos[:100]:
            nome = _sanear_caminho(str(a.get("nome", "arquivo.txt")))
            while nome in usados:  # mesmo nome 2x: sufixo numérico
                nome = f"({len(usados)})".join(nome.rsplit(".", 1)) \
                       if "." in nome else f"{nome}({len(usados)})"
            usados.add(nome)
            z.writestr(nome, str(a.get("conteudo", ""))[:2_000_000])
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="projeto.zip"',
                             "Cache-Control": "no-store"})


# ---------- zip das MÍDIAS geradas (saída do estúdio/chat) -------------------

class MidiaZipIn(BaseModel):
    arquivos: list[str]  # refs 'pasta\arquivo' (o mesmo formato da galeria)


@app.post("/api/midia/zip")
def midia_zip(body: MidiaZipIn, request: Request):
    """Empacota as MÍDIAS GERADAS (imagens/vídeos/áudios de saidas/) num
    .zip final — o botão 'baixar tudo' da galeria quando a sessão tem mais
    de um arquivo. Cada ref passa pelo `_resolver_arquivo` (confinado ao
    projeto: nada fora de saidas/entra no pacote); o que não resolver é
    pulado com aviso no cabeçalho do zip."""
    import io
    import zipfile
    _usuario(request)
    if not body.arquivos:
        raise HTTPException(status_code=400, detail="nenhum arquivo informado")
    caminhos, perdidos = [], []
    for ref in body.arquivos[:200]:
        cam = _resolver_arquivo(ref)
        (caminhos.append(cam) if cam else perdidos.append(Path(ref).name))
    if not caminhos:
        raise HTTPException(status_code=404,
                            detail="nenhum arquivo encontrado em saidas/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for cam in caminhos:
            # nome prefixado pela pasta (imagens/, videos/…) — dois arquivos
            # com o mesmo base não se atropelam dentro do zip
            p = Path(cam)
            z.write(cam, arcname=f"{p.parent.name}/{p.name}")
        if perdidos:
            z.writestr("_arquivos_ausentes.txt",
                       "Estas refs não foram encontradas em saidas/ (a mídia "
                       "pode ter sido apagada):\n" + "\n".join(perdidos))
    nome_zip = f"ragaroy_midias_{time.strftime('%Y%m%d_%H%M')}.zip"
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{nome_zip}"',
                             "Cache-Control": "no-store"})


# ---------- voz do CHAT (STT/TTS leves em CPU — sem disputar VRAM) ---------

class VozFalarIn(BaseModel):
    texto: str


@app.get("/api/voz/disponivel")
def voz_disponivel():
    """O que está pronto (STT/TTS) — a webui habilita os botões por isto."""
    return voz.disponivel()


@app.post("/api/voz/falar")
def voz_falar(body: VozFalarIn, request: Request):
    """Texto → fala (piper pt_BR, CPU). Devolve .wav para tocar no browser."""
    _usuario(request)
    if not (body.texto or "").strip():
        raise HTTPException(status_code=400, detail="texto vazio")
    try:
        cam = voz.falar(body.texto,
                        f"saidas/audios/fala_{int(time.time() * 1000)}.wav",
                        log=lambda m: print(m))
        return FileResponse(cam, media_type="audio/wav",
                            filename=Path(cam).name,
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/api/voz/transcrever")
async def voz_transcrever(request: Request):
    """Fala → texto (whisper small CPU): o microfone do chat manda webm/wav,
    devolve o texto para o campo de pergunta."""
    _usuario(request)
    from fastapi import UploadFile
    form = await request.form()
    arquivo = form.get("file")
    # request.form() devolve Starlette UploadFile (fastapi.UploadFile é
    # SUBCLASSE — isinstance falha): checamos pelo contrato (filename/read)
    if arquivo is None or not getattr(arquivo, "filename", None) or \
            not hasattr(arquivo, "read"):
        raise HTTPException(status_code=400, detail="envie um áudio (file)")
    conteudo = await arquivo.read()
    if len(conteudo) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="áudio muito grande (máx 25 MB)")
    import tempfile
    sufixo = Path(arquivo.filename).suffix or ".webm"
    tmp = Path(tempfile.gettempdir()) / f"ragaroy_voz_{int(time.time() * 1000)}{sufixo}"
    wav = tmp.with_suffix(".16k.wav")
    try:
        await asyncio.to_thread(tmp.write_bytes, conteudo)
        # webm do MediaRecorder → wav 16 kHz (whisper entende direto, mas o
        # wav evita depender do container de áudio no decode)
        import subprocess
        proc = await asyncio.to_thread(
            subprocess.run, ["ffmpeg", "-y", "-i", str(tmp), "-ar", "16000",
                             "-ac", "1", str(wav)],
            **{"capture_output": True, "timeout": 120})
        alvo = str(wav) if proc.returncode == 0 and wav.exists() else str(tmp)
        texto = await asyncio.to_thread(voz.transcrever, alvo,
                                        lambda m: print(m))
        return {"texto": texto}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])
    finally:
        tmp.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


# ---------- Anexos do chat (contexto da SESSÃO — nada vai para o Qdrant) ----

class VisaoIn(BaseModel):
    arquivo: str  # caminho em saidas/entrada (veio do /api/upload)
    pergunta: str | None = None  # opcional: pergunta específica sobre a imagem


@app.post("/api/visao")
def visao(body: VisaoIn):
    """Descreve uma imagem anexada no chat (modelo de visão :8082) SEM
    indexar nada — o texto vira contexto da sessão, não coleção.
    Em CONTAINER, a análise roda NO HOST via agente (:8010)."""
    if config.EM_CONTAINER:
        try:
            import base64 as _b64
            with open(body.arquivo, "rb") as f:
                img_b64 = _b64.b64encode(f.read()).decode()
            r = modelos._chamar_agente("/visao",
                                       {"b64": img_b64,
                                        "nome": Path(body.arquivo).name,
                                        "pergunta": body.pergunta},
                                       timeout=420)
            return {"descricao": r.get("descricao", "")}
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except OSError as e:
            raise HTTPException(status_code=503, detail=str(e))
    try:
        return {"descricao": midia.legendar_imagem(body.arquivo, body.pergunta)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


_EXTS_ANEXO = {".txt", ".md", ".mdx", ".rst", ".pdf"}
ANEXO_MAX_CHARS = 12000  # contexto por anexo (cabe no histórico do chat)


@app.post("/api/anexo/texto")
async def anexo_texto(file: UploadFile):
    """Extrai o TEXTO de um documento anexado no chat (.txt/.md/.pdf) SEM
    ingerir no Qdrant — vira contexto apenas da sessão atual."""
    nome = Path(file.filename or "arquivo").name
    if Path(nome).suffix.lower() not in _EXTS_ANEXO:
        raise HTTPException(status_code=400,
                            detail=f"tipo não suportado ({', '.join(sorted(_EXTS_ANEXO))})")
    conteudo = await file.read()
    if len(conteudo) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="anexo acima de 30 MB")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=Path(nome).suffix, delete=False) as tmp:
        tmp.write(conteudo)
        caminho = tmp.name
    try:
        texto = await asyncio.to_thread(_extrair_anexo, caminho)
    finally:
        Path(caminho).unlink(missing_ok=True)
    if not texto.strip():
        raise HTTPException(status_code=422, detail="nenhum texto extraído do anexo")
    return {"nome": nome, "texto": texto[:ANEXO_MAX_CHARS],
            "truncado": len(texto) > ANEXO_MAX_CHARS}


def _extrair_anexo(caminho: str) -> str:
    """Lê pdf/txt/md de um arquivo temporário (threadpool)."""
    if caminho.lower().endswith(".pdf"):
        from langchain_community.document_loaders import PyPDFLoader
        return "\n".join(p.page_content for p in PyPDFLoader(caminho).load())
    return Path(caminho).read_text(encoding="utf-8", errors="replace")


@app.delete("/api/cache")
def cache_limpar():
    """Apaga todo o cache semântico."""
    n = cache.limpar()
    print(f"🧹 Cache semântico limpo ({n} entradas)")
    return {"removidas": n}


@app.post("/api/specs/reload")
def specs_reload():
    """Derruba o cache das specs — editar core/specs/*.md passa a valer SEM
    restart da API (o lru_cache deixava a versão antiga colada)."""
    from core import specs
    n = specs.recarregar()
    print(f"🔁 Specs recarregadas ({n} em cache foram descartadas)")
    return {"recarregadas": n}


@app.post("/api/midia/prompts")
def midia_prompts(body: MidiaPromptsIn):
    """Fases 1+2 do pipeline: a LLM de conversa gera 3 variações ancoradas no
    RAG de prompts (prompts_midia), critica e devolve o prompt final. Só usa
    o chat (:8090) — nada de VRAM de difusão aqui."""
    contadores.set_servico("estudio")
    try:
        variacoes = midia.sugerir_prompts(body.ideia, body.tipo)
        decisao = midia.criticar_prompts(body.ideia, variacoes["variacoes"])
    except Exception as e:
        print(f"❌ Erro no pipeline de prompts: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    return {**variacoes, **decisao}


class AssistenteIn(BaseModel):
    ideia: str = ""            # ideia inicial (1ª rodada)
    tipo: str = ""             # "imagem" | "video" quando já decidido
    msgs: list[dict] = []      # [{role: user|assistant, content}] da entrevista


@app.post("/api/estudio/assistente")
def estudio_assistente(body: AssistenteIn):
    """Assistente de criação: a LLM ENTREVISTA o operador (perguntas
    direcionadas: tipo, sujeito, ambiente, estilo, restrições) e devolve o
    prompt final pronto para gerar (spec estudio_assistente.md).

    O TIPO (imagem/vídeo) é detectado primeiro por LINGUAGEM NATURAL
    (keywords em pt/en na própria ideia) — a LLM só é chamada para o que
    ela é boa: entrevistar e escrever o prompt."""
    contadores.set_servico("estudio")
    from core.specs import spec as _spec
    # 1) detecção natural (sem LLM): se a ideia já diz o que quer, respeita
    tipo = (body.tipo or "").strip().lower()
    if not tipo:
        ideia = body.ideia.lower()
        if any(k in ideia for k in ("vídeo", "video", "animaç", "animat",
                                    "cinemat", "clipe", "movement", "camera")):
            tipo = "video"
        elif any(k in ideia for k in ("imagem", "image", "foto", "photo",
                                      "ilustraç", "illustrat", "desenho",
                                      "pintura", "retrato", "poster", "cartaz")):
            tipo = "imagem"
    # 2) histórico da entrevista como mensagens (o envelope é a spec)
    linhas = "\n".join(f"{m.get('role')}: {str(m.get('content', ''))[:400]}"
                       for m in body.msgs[-10:])
    bloco = f"TIPO PEDIDO: {tipo or '(pergunte)'}\nIDEIA INICIAL: {body.ideia or '(pergunte)'}\n"
    if linhas:
        bloco += f"\nENTREVISTA ATÉ AGORA:\n{linhas}\n"
    bloco += "\nETAPA: próxima fala do assistente (JSON da spec)."
    try:
        r = rag.llm(temperature=0.4).invoke(
            f"{_spec('estudio_assistente')}\n\n{bloco}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    d = rag._extract_json(r.content)
    pronto = bool(d.get("pronto"))
    prompt = str(d.get("prompt", "")).strip()
    if pronto and not prompt:
        pronto = False  # pronto sem prompt não vale: continua a entrevista
    return {"proximo": str(d.get("proximo", ""))[:400] if not pronto else "",
            "pronto": pronto, "prompt": prompt if pronto else "",
            "tipo": (str(d.get("tipo", "")).strip().lower() or tipo or None)}


UPLOAD_MIDIA_MAX = 200 * 1024 * 1024  # mídia de entrada (vídeos): 200 MB


@app.post("/api/upload")
async def upload(file: UploadFile):
    """Recebe um arquivo local (imagem/vídeo/áudio) para usar de entrada nas
    modalidades i2t/i2v/v2t/a2t — salva em saidas/entrada/.

    Escrita em threadpool (não bloqueia o event loop) com limite de tamanho:
    acima de 200 MB o arquivo é descartado e a chamada recusada (413)."""
    destino = midia.ENTRADA
    destino.mkdir(parents=True, exist_ok=True)
    nome = Path(file.filename or "arquivo").name  # limpo: sem caminho
    caminho = destino / nome
    total = 0
    with open(caminho, "wb") as f:
        while chunk := await file.read(1 << 20):
            total += len(chunk)
            if total > UPLOAD_MIDIA_MAX:
                f.close()
                caminho.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail="arquivo acima de 200 MB — corte/compacte antes "
                           "de enviar (limite das entradas do estúdio)")
            await asyncio.to_thread(f.write, chunk)
    print(f"📥 Upload: {nome} ({total // 1024} KB)")
    return {"arquivo": str(caminho), "nome": nome}


def _erro_modelo(modelo_pedido: str) -> HTTPException:
    """409 informando o modelo ATUAL, pesquisado na API na hora (regra do
    operador: estúdio ocupado não troca de modelo — apresenta o erro)."""
    atual = modelos.servido(modelos.CHAT_PORTA)  # None = chat pausado
    ocupado = tarefas.estudio_ocupado()
    detalhe = (f"modelo divergente: a sessão pediu '{modelo_pedido}', mas o modelo "
               f"atual na :{modelos.CHAT_PORTA} é "
               f"'{atual or 'nenhum (chat pausado pela tarefa em curso)'}'")
    if ocupado:
        detalhe += f" — estúdio ocupado com {ocupado['id']} ({ocupado['rotulo']})"
    return HTTPException(status_code=409, detail=detalhe)


def _esperar_chats(log, timeout_s: int = 60) -> bool:
    """Espera as respostas do chat em andamento acabarem antes de pausar a
    LLM (TOCTOU: a query validou 'estúdio livre' no início, mas a tarefa
    pode chegar depois — derrubar o servidor no meio da resposta deixava o
    job do chat com erro cru de conexão). True = drenou; False = timeout."""
    for i in range(timeout_s):
        em_curso = _query.ativos()
        if not em_curso:
            return True
        if i == 0:
            log(f"⏳ {em_curso} resposta(s) do chat em andamento — esperando "
                "concluir para não cortar no meio (máx. 60 s)…", "pausar")
        time.sleep(1)
    return False


# limites dos parâmetros numéricos por modalidade (o cliente não manda no
# tamanho: largura=10 travava a difusão; clamp aqui protege)
_LIMITES_PARAMS = {
    "largura": (64, 1536), "altura": (64, 1536),
    "frames": (9, 129), "seed": (0, 2**31 - 1),
}


def _sanear_params(p: dict) -> dict:
    s = dict(p)
    for k, (mn, mx) in _LIMITES_PARAMS.items():
        if isinstance(s.get(k), (int, float)):
            s[k] = int(max(mn, min(mx, s[k])))
    for k in ("largura", "altura"):  # difusão treina em múltiplos de 16
        if isinstance(s.get(k), int):
            s[k] = max(64, round(s[k] / 16) * 16)
    return s


def _rodar_tarefa(tid: str, body: TarefaIn):
    """Executa a modalidade numa thread: log/progresso → core.tarefas."""
    import time as _t
    t0 = _t.time()
    mod, p = body.modalidade, _sanear_params(body.params or {})

    def _log(msg, etapa=None):
        tarefas.log(tid, msg, etapa)

    def _prog(fracao):
        tarefas.progresso(tid, fracao)

    contadores.set_servico("estudio")  # prompts/crítica da geração contam aqui
    # 🎛️ CONJUNTO por tarefa: garante os motores certos no ar ANTES de
    # executar (troca = limpeza de VRAM; mesmo conjunto = cache quente)
    try:
        from core import conjuntos as _conjuntos
        _conjuntos.garantir(mod, log=lambda m, g="modelo": _log(m, "modelo"))
    except Exception as e:
        _log(f"⚠️ conjunto de modelos: {str(e)[:140]} — seguindo com o que está no ar")
    # 📖 HISTÓRIA da sessão: a difusão recebe a continuidade narrativa da
    # conversa (mesma personagem/ambiente/enredo) — pedido do dono. Anexada
    # como sufixo curto do prompt (bilíngue: umt5/t5xxl entendem PT)
    _prompt = str(body.texto or "")
    if p.get("historia") and mod in ("t2i", "t2v", "i2v"):
        _prompt = (f"{_prompt}. Continuidade da cena desta conversa "
                   f"(mantenha personagens/ambiente/enredo coerentes): "
                   f"{str(p['historia'])[:400]}")
        _log("📖 história da sessão anexada ao prompt (continuidade narrativa)",
             "contexto")
    if p.get("duracao_s"):
        _log(f"⏱️ duração pedida: {p['duracao_s']}s ({p.get('frames')} frames "
             "a 16 fps — spec midia_duracao)", "gerar")
    estado = None
    try:
        if mod in ("t2i", "t2v", "i2v", "a2v"):  # difusão: pausa o chat e sobe o sd-cli
            pesado = mod in ("t2v", "i2v", "a2v")  # vídeo: GPU 100% da difusão
            _esperar_chats(_log)  # não corta respostas em andamento no meio
            _log("⏸️ Pausando os servidores de LLM — a GPU é da difusão agora…",
                 "pausar")
            estado = midia.pausar_servicos(log=_log, pesado=pesado)
        if mod == "t2i":
            r = midia.gerar_imagem(_prompt, p.get("modelo"), p.get("largura", 1024),
                                   p.get("altura", 1024), p.get("seed"),
                                   negativo=p.get("negativo"),
                                   log=_log, progresso=_prog)
        elif mod in ("t2v", "i2v"):
            r = midia.gerar_video(_prompt,
                                  body.arquivo if mod == "i2v" else None,
                                  p.get("frames", 33), p.get("largura", 480),
                                  p.get("altura", 832), p.get("seed"),
                                  gif=bool(p.get("gif")),  # F1b-3: mp4 → .gif
                                  modelo=p.get("modelo"),  # Wan2.1/2.2 do combobox
                                  negativo=p.get("negativo"),
                                  log=_log, progresso=_prog)
        elif mod == "i2t":
            r = {"tipo": "texto",
                 "texto": midia.legendar_imagem(body.arquivo,
                                                body.texto or p.get("pergunta"),
                                                log=_log)}
        elif mod == "a2t":
            r = midia.transcrever(body.arquivo, log=_log, progresso=_prog)
        elif mod == "a2v":
            r = midia.audio_para_video(body.arquivo,
                                       p.get("frames", 33),
                                       log=_log, progresso=_prog)
        elif mod == "v2t":
            r = midia.video_para_texto(body.arquivo, log=_log, progresso=_prog)
        else:
            raise RuntimeError(f"modalidade '{mod}' não executa tarefa de estúdio")
        r.setdefault("segundos", round(_t.time() - t0))   # DURAÇÃO visível
        # 📊 TELEMETRIA na VPS: a geração roda NO HOST (agente) e o evento
        # era gravado só na telemetria da ESTAÇÃO — o dashboard de modelos
        # da produção nunca via wan/flux. Em container, a API registra.
        if config.EM_CONTAINER and r.get("modelo"):
            try:
                telemetria.evento(
                    "geracao", f"🎬 {r['modelo']}: {r.get('tipo', 'mídia')} em "
                    f"{r.get('segundos')}s", modelo=r["modelo"],
                    tipo=r.get("tipo"), frames=r.get("frames"),
                    segundos=r.get("segundos"))
            except Exception:
                pass
        tarefas.concluir(tid, r)
        if r.get("arquivo"):  # mídia gerada → fica ativa na sessão do estúdio
            try:
                sessoes.registrar(body.sessao or "s_principal",
                                  {**r, "modalidade": mod})
            except Exception as e:
                print(f"⚠️ mídia não registrada na sessão: {e}")
    except Exception as e:
        print(f"❌ Erro na tarefa {tid} ({mod}): {e}")
        tarefas.concluir(tid, erro=str(e))
    finally:
        if estado is not None:  # serviços de volta ao ar MESMO com erro
            try:
                midia.restaurar_servicos(estado, log=_log)
            except Exception as e:
                tarefas.log(tid, f"⚠️ Falha ao restaurar serviços: {e} — "
                                "rode servicos_llm.py", "restaurar")


def _resolver_arquivo(nome: str | None) -> str | None:
    """Normaliza o arquivo de entrada: aceita caminho absoluto (upload),
    'pasta\\arquivo' (preview da webui) ou nome seco (busca em saidas/entrada
    e nas pastas de mídia). Devolve caminho absoluto ou None.

    Caminhos absolutos só valem DENTRO do projeto (saidas/…) — o cliente não
    escolhe arquivo arbitrário do host para descrever/indexar."""
    if not nome:
        return None
    # refs vindas do navegador podem usar barra invertida (Windows); no
    # Linux do container "\" é caractere de nome, NÃO separador — sem
    # normalizar, "imagens\dev.png" não resolve e o zip vem 404
    p = Path(str(nome).replace("\\", "/"))
    if p.is_absolute():
        try:
            dentro = p.resolve().is_relative_to(midia.RAIZ.resolve())
        except Exception:
            dentro = False
        return str(p) if dentro and p.is_file() else None
    cand = [midia.RAIZ / "saidas" / nome,          # videos\t2v_x.webm (preview)
            midia.ENTRADA / p.name,                # enviado via upload
            *[d / p.name for d in midia.SAIDAS.values()]]  # nome seco
    return next((str(c) for c in cand if c.is_file()), None)


@app.post("/api/tarefas")
def criar_tarefa(body: TarefaIn, request: Request):
    """Dispara a tarefa da modalidade em segundo plano; acompanhe o andamento
    (log ao vivo, progresso, ETA) em /api/tarefas/status/{id}?cursor=N."""
    _checar_gpu_modo(mod=body.modalidade)  # política antes do guard de host:
    # o 403 "somente LLMs" é mais útil que o 400 de container
    # 👁 i2t com MULTIMODAL EXTERNO (openai:gpt-4o…): roda NA PRÓRIA API —
    # não toca a GPU nem o agente do host (a análise é uma chamada HTTP)
    if (body.modalidade == "i2t"
            and ":" in str(body.params.get("modelo") or body.modelo or "")):
        _modelo = str(body.params.get("modelo") or body.modelo)
        pid, nome = _modelo.split(":", 1)
        from core import provedores as _prov
        prov = _prov.resolver(pid.strip(), nome.strip())
        if not prov:
            raise HTTPException(status_code=400,
                                detail=f"provedor '{pid}' não configurado no "
                                       ".env (PROV_…_BASE_URL/API_KEY)")
        if not body.arquivo:
            raise HTTPException(status_code=400,
                                detail="'análise de imagem' precisa de anexo")
        if not body.sessao:
            body.sessao = sessoes.principal(_usuario(request))
        try:
            tid = tarefas.criar("i2t", trava_vram=False,
                                sessao=body.sessao)
        except RuntimeError as e:
            raise HTTPException(status_code=423, detail=str(e))

        def _i2t_ext(tid=tid, body=body, modelo=_modelo):
            import time as _t
            t0 = _t.time()
            tarefas.log(tid, f"🔍 visão EXTERNA {modelo} — analisando "
                             "(GPU local intocada)…", "analisar")
            try:
                txt = midia.legendar_imagem(_resolver_arquivo(body.arquivo),
                                            body.texto, log=lambda m, e=None:
                                            tarefas.log(tid, m, e),
                                            modelo=modelo)
                tarefas.concluir(tid, resultado={"tipo": "texto", "texto": txt,
                                                 "segundos": round(_t.time()-t0)})
            except Exception as e:
                tarefas.concluir(tid, erro=str(e)[:300])

        threading.Thread(target=_i2t_ext, daemon=True,
                         name=f"i2t-ext-{tid}").start()
        return {"tarefa": tid, "modalidade": "i2t",
                "rotulo": "analisar imagem (multimodal externo)",
                "estimativa_s": 30, "etapas": ["analisar"],
                "status": f"/api/tarefas/status/{tid}"}
    if config.EM_CONTAINER:
        # GPU/sd-cli são do HOST → a geração é DELEGADA ao agente (:8010),
        # que roda na máquina com a GPU e expõe /tarefas. O polling desta
        # API (/api/tarefas/status) faz fallback para o agente.
        try:
            return modelos._chamar_agente(
                "/tarefas",
                corpo=body.model_dump(exclude_none=True), timeout=30)
        except RuntimeError as e:
            raise HTTPException(status_code=400,
                                detail=f"{e} — o Estúdio precisa do agente "
                                       "do host (python -X utf8 -m "
                                       "api.agente_host na máquina com GPU; "
                                       "na VPS, exponha-o no túnel e aponte "
                                       "AGENTE_HOST_URL)")
    _exigir_host(f"estúdio ({body.modalidade})")
    m = modalidades.get(body.modalidade)
    if not m:
        raise HTTPException(status_code=404, detail=f"modalidade '{body.modalidade}' não existe")
    if not m["disponivel"]:
        raise HTTPException(status_code=400, detail=m["motivo"])
    # sem sessão informada: a 'Principal' DO USUÁRIO (a global sem owner
    # deixava a mídia gerada invisível para quem gerou)
    if not body.sessao:
        body.sessao = sessoes.principal(_usuario(request))
    body.arquivo = _resolver_arquivo(body.arquivo)
    if any(e in m["entra"] for e in ("imagem", "video", "audio")) and not body.arquivo:
        raise HTTPException(status_code=400,
                            detail=f"'{m['rotulo']}' precisa de um arquivo de entrada "
                                   f"({', '.join(e for e in m['entra'] if e != 'texto')})")
    if body.modelo and body.modelo != modelos.servido(modelos.CHAT_PORTA):
        # regra do operador: com o estúdio ocupado NÃO troca — erro com o modelo atual
        ocupado = tarefas.estudio_ocupado()
        if ocupado or modelos.servido(modelos.CHAT_PORTA) is None:
            raise _erro_modelo(body.modelo)
    ocup = tarefas.sessao_ocupada(body.sessao)
    if ocup:
        raise HTTPException(status_code=423, detail={
            "erro": f"a sessão está ocupada com a tarefa {ocup['id']} "
                    f"({ocup['rotulo']}) — aguarde concluir ou crie outra sessão",
            "tarefa": ocup})
    try:
        tid = tarefas.criar(body.modalidade, trava_vram=True, sessao=body.sessao)
    except RuntimeError as e:  # estúdio (VRAM) ocupado
        raise HTTPException(status_code=423, detail=str(e))
    threading.Thread(target=_rodar_tarefa, args=(tid, body), daemon=True).start()
    return {"tarefa": tid, "modalidade": m["id"], "rotulo": m["rotulo"],
            "estimativa_s": m["estimativa_s"], "etapas": m["etapas"],
            "status": f"/api/tarefas/status/{tid}"}


@app.get("/api/tarefas/status/{tid}")
def tarefa_status(tid: str, cursor: int = 0):
    """Linhas novas + progresso + ETA da tarefa (polling do fluxo da webui)."""
    s = tarefas.status(tid, cursor)
    if not s:
        # tarefa DELEGADA ao agente do host (EM_CONTAINER): o registro vive
        # lá — o polling desta API transparentemente consulta o agente.
        if config.EM_CONTAINER:
            try:
                import httpx as _hx
                r = _hx.get(f"{modelos._agente_host()}/tarefas/status/{tid}",
                            params={"cursor": cursor}, timeout=10,
                            headers=modelos._agente_headers())
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
        raise HTTPException(status_code=404, detail=f"tarefa '{tid}' não encontrada")
    return s


@app.post("/api/midia/contexto")
def midia_contexto(body: ContextoIn, request: Request):
    """Inclui a mídia no contexto do RAG: descreve (visão/whisper), embeda
    com o bge-m3 e indexa na coleção midia_gerada."""
    _checar_gpu_modo(tipo=body.tipo)
    _exigir_host("incluir mídia no contexto")
    # gif mora em saidas/videos e é descrito como vídeo (v2t)
    mod = {"imagem": "i2t", "video": "v2t", "audio": "a2t",
           "gif": "v2t"}.get(body.tipo)
    if not mod:
        raise HTTPException(status_code=400,
                            detail=f"tipo '{body.tipo}' inválido (imagem|video|audio|gif)")
    m = modalidades.get(mod)
    if not m["disponivel"]:
        raise HTTPException(status_code=400, detail=m["motivo"])
    orig = body.arquivo
    body.arquivo = _resolver_arquivo(body.arquivo)
    if not body.arquivo:
        raise HTTPException(status_code=400,
                            detail=f"arquivo '{orig}' não encontrado "
                                    "(use o caminho do upload ou 'pasta\\arquivo')")
    if not body.sessao:
        body.sessao = sessoes.principal(_usuario(request))
    ocup = tarefas.sessao_ocupada(body.sessao)
    if ocup:
        raise HTTPException(status_code=423, detail={
            "erro": f"a sessão está ocupada com a tarefa {ocup['id']} — aguarde concluir",
            "tarefa": ocup})
    try:
        tid = tarefas.criar(mod, trava_vram=True, sessao=body.sessao)
    except RuntimeError as e:
        raise HTTPException(status_code=423, detail=str(e))

    # o whisper descreve o gif como vídeo (frames + trilha); o tipo original
    # só importa para escolher a modalidade acima
    arquivo, tipo, prompt = body.arquivo, ("video" if body.tipo == "gif" else body.tipo), body.prompt

    def fabricar(p: dict):
        t = p.get("tid") or tid

        def rodar():
            contadores.set_servico("estudio")
            try:
                tarefas.concluir(
                    t, midia.incluir_no_contexto(
                        arquivo, tipo, prompt,
                        log=lambda msg, etapa=None: tarefas.log(t, msg, etapa)))
            except Exception as e:
                print(f"❌ Erro ao incluir mídia no contexto: {e}")
                tarefas.concluir(t, erro=str(e))
        return rodar

    # COM registry: o picked do worker impede a re-execução em reentrega
    # (era o único job da fila sem — uma reentrega indexava 2x)
    _despachar(fabricar, "midia_contexto", {"job": f"ctx_{tid}", "tid": tid},
               JobRegistry("ctx", "contexto de mídia"))
    return {"tarefa": tid, "modalidade": mod,
            "status": f"/api/tarefas/status/{tid}"}


_MIME = {"imagem": "image/png", "audio": "audio/mpeg", "entrada": "application/octet-stream"}
_VIDEO_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}


@app.get("/api/midia/{pasta}/{nome}")
def midia_arquivo(pasta: str, nome: str):
    """Serve as mídias geradas (saidas/imagens|videos|audios) e as enviadas
    (saidas/entrada) para <img>/<video>/<audio> na webui. `gif` é alias da
    pasta de vídeos com MIME de imagem (F1b-3: <img> renderiza direto).

    Em CONTAINER a mídia nasce NO HOST (quem tem a GPU): arquivo ausente
    aqui → PULL-BACK do agente (baixa uma vez, salva em saidas/ e serve)."""
    if pasta not in _MIME and pasta not in ("video", "gif"):
        raise HTTPException(status_code=404, detail=f"pasta '{pasta}' inválida")
    base = (midia.ENTRADA if pasta == "entrada"
            else midia.SAIDAS["video" if pasta == "gif" else pasta])
    caminho = base / Path(nome).name  # nome limpo: sem travessia de pasta
    if not caminho.is_file() and config.EM_CONTAINER:
        _puxar_do_agente(pasta, caminho)
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail=f"'{nome}' não encontrado em {pasta}")
    mime = ("image/gif" if pasta == "gif" else
            _VIDEO_MIME.get(caminho.suffix.lower(), "video/mp4")
            if pasta == "video" else _MIME[pasta])
    return FileResponse(str(caminho), media_type=mime)


def _puxar_do_agente(pasta_url: str, destino: Path) -> None:
    """Baixa a mídia gerada no HOST (agente /arquivo) para o disco local —
    silencioso: falha deixa o 404 original acontecer."""
    import httpx as _hx
    pasta_host = {"gif": "videos", "imagem": "imagens", "video": "videos",
                  "audio": "audios"}.get(pasta_url, pasta_url)
    nome = destino.name
    try:
        r = _hx.get(f"{modelos._agente_host()}/arquivo/{pasta_host}/{nome}",
                    headers=modelos._agente_headers(), timeout=120,
                    follow_redirects=True)  # túnel/CDN pode redirecionar
        if r.status_code == 200 and r.content:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(r.content)
            print(f"📥 pull-back da mídia do host: {pasta_host}/{nome} "
                  f"({len(r.content) // 1024} KB)")
        else:
            print(f"⚠️ pull-back {pasta_host}/{nome}: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️ pull-back {pasta_host}/{nome}: {e}")


# ---------- Seed profundo (job em 2º plano com log ao vivo) ----------

_seed = JobRegistry("seed", "seed")


@app.post("/api/seed")
def seed(body: SeedIn):
    """Seed profundo em SEGUNDO PLANO (definição da RAG → rodadas de busca →
    curadoria com scores → download + internos + repos → ingestão → catálogo);
    acompanhe o log completo em /api/seed/status/{job}?cursor=N."""
    job = _seed.novo_id()
    assunto, colecao, fontes = body.assunto, body.colecao, body.fontes

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("seed")
            _seed.iniciar(jid)
            try:
                _seed.concluir(jid, result=seed_collection(
                    assunto, colecao, fontes, log=lambda m, g='': _seed.log(jid, m, grupo=g or 'geral')))
            except Exception as e:
                print(f"❌ Erro no seed: {e}")
                _seed.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "seed", {"job": job}, _seed)
    return {"job": job, "status": f"/api/seed/status/{job}"}


_rota_status("/api/seed/status/{job}", _seed, "Job de seed não encontrado")


# ---------- Varredura LLM de coleções (lixo claro sai do Qdrant) ----------

_varredura = JobRegistry("var", "varredura")


@app.post("/api/varredura")
def varredura(body: VarreduraIn):
    """Varredura LLM em SEGUNDO PLANO: julga cada chunk contra a definição da
    coleção e apaga o lixo claro; acompanhe em /api/varredura/status/{job}."""
    job = _varredura.novo_id()
    colecao = body.collection

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("limpeza")
            _varredura.iniciar(jid)
            try:
                _varredura.concluir(jid, result=varredura_colecao(
                    colecao, log=lambda m, g='': _varredura.log(jid, m, grupo=g or 'geral')))
            except Exception as e:
                print(f"❌ Erro na varredura: {e}")
                _varredura.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "varredura", {"job": job}, _varredura)
    return {"job": job, "status": f"/api/varredura/status/{job}"}


_rota_status("/api/varredura/status/{job}", _varredura,
             "Job de varredura não encontrado")


def _bases_consultadas(client, colecoes) -> str | None:
    """Cabeçalho do contexto: o que cada coleção selecionada é (área · tema:
    descrição), lido do catálogo — é o que faz a LLM compreender o domínio
    das RAGs selecionadas antes de ler os fragmentos."""
    try:
        meta = catalog.list_meta(client)
    except Exception:
        return None
    linhas = []
    for nome in colecoes:
        m = meta.get(nome)
        if not m:
            continue
        rotulo = " · ".join(x for x in (m.get("area"), m.get("categoria")) if x)
        linha = f"- {nome}" + (f" ({rotulo})" if rotulo else "")
        if m.get("descricao"):
            linha += f": {m['descricao']}"
        linhas.append(linha)
    if not linhas:
        return None
    return ("Bases consultadas nesta conversa (o que cada uma contém):\n"
            + "\n".join(linhas))


# ---------- chat como JOB (2º plano com log em tempo real) ----------

_query = JobRegistry("qry", "chat")

# ── exclusão de conversa como JOB (fila Rabbit): a lista nunca trava e a
#    UX mostra "⏳ apagando…" até o job concluir (pedido do dono) ──
_conv = JobRegistry("conv", "exclusão de conversa")
# sids com exclusão EM CURSO: {sid: {job, owner}} — alimenta o estado da
# lista (item desabilitado + polling) e a idempotência do clique duplo
_APAGANDO: dict[str, dict] = {}


def _apagando_do_usuario(owner: str) -> set[str]:
    """Sids em exclusão em curso pertencentes ao usuário (estado da lista)."""
    return {s for s, v in _APAGANDO.items() if v.get("owner") == owner}


def _query_log(job: str, msg: str, grupo: str = "geral"):
    """Linha de execução do chat (o 'pensando…' da webui mostra em tempo real)."""
    _query.log(job, msg, grupo=grupo, ts=time.strftime("%H:%M:%S"))


@app.get("/api/provedores")
def api_provedores(force: bool = False):
    """Catálogo de LLMs: local (llama-server) + provedores EXTERNOS
    configurados no .env (glm/deepseek/openai/anthropic/…) com a lista REAL
    de modelos (GET /models do provedor; manual PROV_MODELOS como reserva),
    a marcação 👁 multimodal (i2t com visão externa) E os METADADOS
    automáticos (ctx = janela de contexto, info = descrição/preço quando a
    API entrega). Chaves NUNCA saem."""
    from core import provedores
    return {"provedores": provedores.listar(force=force)}


@app.get("/api/provedores/conhecidos")
def api_provedores_conhecidos():
    """☁️ Provedores PRINCIPAIS (Z.AI Coding Plan, ChatGPT, Claude, DeepSeek,
    OpenRouter, Gemini, Grok, Groq, Mistral) para o cadastro em 1 clique —
    o form do Sistema preenche id/nome/URL; falta só a chave."""
    from core import provedores
    return {"conhecidos": [{"id": k, **v} for k, v in
                           provedores.CONHECIDOS.items()]}


class ProvedorIn(BaseModel):
    """Cadastro de provedor cloud PELA UI (pedido do dono: 'preciso ter um
    cadastro de provedores cloud') — grava PROV_<ID>_* no .env na hora."""
    id: str
    base_url: str
    nome: str = ""
    api_key: str = ""
    modelos: str = ""   # opcional: lista manual separada por vírgula


@app.post("/api/provedores/cadastrar")
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


@app.post("/api/query")
def query(body: QueryIn):
    """Consulta: "rag" responde só com a base; "hibrido" base + modelo;
    "livre" só modelo; "auto" roteia. `job=true` roda em 2º PLANO e devolve
    {job} na hora — a webui acompanha cada etapa em /api/query/status/{job}
    (elimina o 524 do Cloudflare em respostas demoradas)."""
    if not body.job:
        try:
            return _processar_query(body)
        finally:
            rag.set_override(None)  # thread do pool é reusada
    job = _query.novo_id()
    pedido = body.model_dump()

    def fabricar(p: dict):
        jid = p["job"]
        corpo = QueryIn(**pedido)

        def rodar():
            _n_hist = len(corpo.history or [])
            # modo ORIGINAL pedido no composer (o roteador pode escalar para
            # híbrido DENTRO do _processar_query — o pedido foi "rag", é isto
            # que decide se o modelo aparece no header)
            _modo_pedido = corpo.mode
            _query_log(jid, f"📜 histórico da sessão: {_n_hist} msg(s) anteriores"
                          + (" (contexto ATIVO)" if corpo.history else " (SEM contexto)"),
                       "mensagem")
            contadores.set_servico("chat")

            # tokens de CADA chamada LLM aparecem no "pensando…" em tempo
            # real (o thread-local atravessa todas as chamadas do job)
            contadores.set_log(lambda m, g="tokens": _query_log(jid, m, g))
            _query.iniciar(jid)
            _t0 = time.time()
            try:
                res = _processar_query(
                    corpo, log=lambda m, g="geral": _query_log(jid, m, g),
                    on_token=lambda txt: _query.parcial(jid, txt))
                # ⚡ tok/s: velocidade REAL de GERAÇÃO (do 1º token em
                # diante — sem o pré-processamento do prompt; o cálculo
                # antigo dividia pelo total e "caía" com prompt grande)
                try:
                    _dur = max(time.time() - _t0, 0.001)
                    _tk = (res.get("tokens") or {}).get("saida") or 0 if isinstance(res, dict) else 0
                    if isinstance(res, dict) and _tk:
                        res["tok_s"] = (contadores.vel_geracao()
                                        or round(_tk / _dur, 1))
                        res["duracao_s"] = round(_dur, 1)
                except Exception:
                    pass
                if isinstance(res, dict) and res.get("tokens"):
                    t = res["tokens"]
                    _vel = f" · ⚡ {res.get('tok_s')} tok/s" if res.get("tok_s") else ""
                    _query_log(jid, f"🪙 pedido completo: 🔻{t['entrada']} recebidos · "
                                    f"🔺{t['saida']} gerados · {t['chamadas']} chamada(s){_vel}",
                               "tokens")
                # 🙈 MODELO SÓ QUANDO A LLM FOI CONSULTADA (pedido do dono):
                # zero chamadas (cache/resposta direta da base) OU pedido no
                # modo rag ("só a base" — mesmo escalado a híbrido pelo
                # roteador) → o header da mensagem não cita modelo
                try:
                    if isinstance(res, dict) and res.get("model"):
                        _ch = (res.get("tokens") or {}).get("chamadas") or 0
                        if _ch == 0 or _modo_pedido == "rag":
                            res["model"] = None
                except Exception:
                    pass
                _query.concluir(jid, result=res)
            except HTTPException as e:
                detalhe = (e.detail if isinstance(e.detail, str)
                           else json.dumps(e.detail, ensure_ascii=False))
                _query.concluir(jid, error=detalhe.strip() or "o serviço falhou sem detalhe")
            except Exception as e:
                _query.concluir(jid, error=str(e).strip()
                                or f"falha sem mensagem ({type(e).__name__})")
            finally:
                rag.set_override(None)  # worker do Rabbit REUSA a thread
        return rodar

    _despachar(fabricar, "query", {"job": job}, _query)
    return {"job": job, "status": f"/api/query/status/{job}"}


_rota_status("/api/query/status/{job}", _query,
             "Job de consulta não encontrado")


def _processar_query(body: QueryIn, log=None, on_token=None):
    """Processa uma consulta (compartilhada entre a rota síncrona e o job).
    `log(msg, grupo)` recebe as etapas — cache, busca, geração, tokens."""
    log = log or (lambda m, g="geral": None)
    contadores.set_servico("chat")
    contadores.balanco_reset()  # tokens DESTA resposta: balanço local (sem
    # cruzar com totais globais de outros processos — fim da divergência)
    # 🌐 PROVEDOR EXTERNO ("glm:glm-4.6" no model): override desta EXECUÇÃO
    # — a LLM sai do llama-server e vai para o endpoint configurado no .env.
    # A limpeza (set_override(None)) é feita nos CHAMADORES (finally) porque
    # o worker do Rabbit REUSA a thread entre jobs.
    if ":" in str(body.model or ""):
        _pid, _nome = str(body.model).split(":", 1)
        from core import provedores as _prov
        _pv = _prov.resolver(_pid.strip(), _nome.strip())
        if not _pv:
            raise HTTPException(
                status_code=400,
                detail=f"provedor '{_pid}' não configurado no .env — "
                       "defina PROV_%s_BASE_URL/API_KEY" % _pid.upper())
        rag.set_override(_pv)
        log(f"🌐 provedor externo [{_pv['provedor']}] {_pv['model']} — "
            "a resposta vem da API configurada (GPU local intocada)", "modelo")
    else:
        rag.set_override(None)
    # 🧪 MOCK_LLM (F1b-5): ANTES de tudo — sem cache, sem Qdrant, sem LLM,
    # sem os guards de sessão/estúdio (o objetivo é validar a UI com a
    # máquina toda desligada; o job em si roda de verdade, com log ao vivo)
    if getattr(config, "MOCK_LLM", False):
        from core import mock as _mock
        return _mock.responder(body, log)
    # sessão ocupada (tarefa de mídia em curso): espera — pode navegar, não executar
    ocup = tarefas.sessao_ocupada(body.sessao)
    if ocup:
        raise HTTPException(status_code=423, detail={
            "erro": f"a sessão está ocupada com a tarefa {ocup['id']} "
                    f"({ocup['rotulo']}, {ocup['etapa']}) — aguarde concluir ou "
                    "crie outra sessão", "tarefa": ocup})
    # estúdio rodando difusão → o chat está PAUSADO: erro com o modelo atual (live)
    est = tarefas.estudio_ocupado()
    if est:
        atual = modelos.servido(modelos.CHAT_PORTA)
        raise HTTPException(status_code=409, detail={
            "erro": f"o estúdio está gerando mídia ({est['rotulo']}, etapa "
                    f"'{est['etapa']}') e o chat está pausado — modelo atual: "
                    f"'{atual or 'nenhum (pausado)'}'", "tarefa": est})
    # escopo efetivo da consulta (TRI-ESTADO explícito — regra do dono):
    #   collections=[] (webui sem nada marcado) → SEM coleções: NÃO busca
    #   collections=None (CLI/API antiga)        → TODAS as visíveis
    #   collection="x" (compat)                  → uma
    # A regra vive NO BACKEND; a webui só mostra o estado.
    log(f"mensagem recebida (modo {body.mode})", "mensagem")
    if body.collections is not None and body.collections:
        colecoes = list(body.collections)
    elif body.collections == [] and not body.collection:
        colecoes = []  # seleção VAZIA explícita: sem busca na base
    elif body.collection:
        colecoes = [body.collection]
    else:
        try:
            client0 = QdrantClient(url=config.QDRANT_URL, timeout=10,
                                   check_compatibility=False)
            colecoes = list(_scan_collections(client0))
        except Exception:
            colecoes = [config.COLLECTION]
    log("escopo: " + (", ".join(colecoes) if colecoes
                      else "SEM coleções — sem busca na base (seleção vazia)"),
        "mensagem")
    # 👋 SAUDAÇÃO/PERGUNTA TRIVIAL (sem conteúdo recuperável): buscar contexto
    # 📎 IMAGEM DO PAINEL incluída no contexto: a visão (Qwen2.5-VL, via
    # agente no container) DESCREVE o arquivo e a descrição entra na
    # pergunta — o RAG busca com ela, a LLM responde com ela. Falha na
    # visão NÃO derruba a pergunta (segue sem a descrição, log claro).
    if body.anexo_imagem:
        log(f"📎 imagem anexada ({Path(body.anexo_imagem).name}) — "
            "descrevendo com a visão (Qwen2.5-VL)…", "anexo")
        try:
            _cam = _resolver_arquivo(Path(body.anexo_imagem).name)
            if not _cam:
                raise ValueError("arquivo não encontrado em saidas/")
            _vis = visao(VisaoIn(arquivo=_cam,
                                 pergunta=("descreva a imagem de forma objetiva "
                                           "e completa para servir de contexto")))
            _desc = ((_vis or {}).get("descricao") or "").strip()
            if _desc:
                body.question = (body.question
                                 + "\n\n[imagem anexada — conteúdo]: "
                                 + _desc[:1500])
                log(f"📎 descrição anexada ao contexto "
                    f"({len(_desc)} caracteres)", "anexo")
            else:
                log("⚠️ visão devolveu vazia — seguindo sem a descrição", "anexo")
        except Exception as e:
            log(f"⚠️ visão indisponível ({str(e)[:120]}) — seguindo SEM a "
                "descrição da imagem", "anexo")
    # para "oi tudo bem?" é desperdício puro (3k tokens e fragmentos
    # aleatórios anexados). Responde direto, com o porquê no log.
    # Detecção por TOKENS: mensagem curta cujas TODAS as palavras são de
    # saudação/identidade ("oi tudo bem?", "quem é você", "obrigado").
    _TRIVIAIS = {
        "oi", "olá", "ola", "opa", "eai", "e", "aí", "ai", "hey", "hi",
        "hello", "bom", "boa", "dia", "tarde", "noite", "tudo", "bem",
        "bom", "como", "vai", "voce", "você", "é", "e", "um", "uma",
        "robo", "robô", "ia", "quem", "o", "que", "faz", "qual", "seu",
        "nome", "obrigado", "obrigada", "valeu", "vlw", "thanks", "thank",
        "you", "beleza", "com", "está", "esta", "tchau", "adeus",
    }
    _palavras = re.findall(r"[a-zà-ú]+", (body.question or "").lower())
    # 🧭 LANGGRAPH — A INTELIGÊNCIA VEM ANTES (pedido do dono): o grafo
    # roteia a pergunta ANTES de cache/Qdrant/rerank — pedido de CRIAÇÃO
    # em modo rag é orientado na hora (antes: pagava ~7 s de busca para
    # no fim recusar), saudação responde direto. "fluxo" não toca em nada.
    try:
        _rota = grafo.rotear(body.question, body.mode, log=log,
                             historia=body.history)
    except Exception as _e:
        _rota = {"rota": "fluxo", "tipo": "", "motivo": f"erro: {str(_e)[:40]}"}
    if _rota["rota"] == "criar_como_hibrido":
        # 🧭 ESCALADA AUTOMÁTICA (pedido do dono: "como ele não sabe o que
        # estou falando?"): criação é impossível no rag (só o que está na
        # base) — em vez de recusar com um texto genérico, ESTA pergunta
        # sobe para o híbrido sozinha: a base orienta o estilo e o modelo
        # ESCREVE o que foi pedido. O modo escolhido segue salvo para as
        # próximas (perguntas factuais continuam no rag).
        log("🧭 pedido de CRIAÇÃO no modo rag — respondendo no modo "
            "HÍBRIDO automaticamente (a base orienta o estilo)", "mensagem")
        body.mode = "hibrido"
    if _rota["rota"] == "conversa" and body.mode != "livre":
        log("👋 roteador: saudação — resposta direta, SEM busca", "busca")
        contadores.set_etapa("resposta (roteador)")
        answer = rag.answer_free(body.question, body.history)
        contadores.set_etapa(None)
        return {"question": body.question, "mode": body.mode,
                "collections": colecoes, "docs": [], "answer": answer,
                "erros": {}, "ferramentas": [], "mcp_erros": {},
                "pendente": None, "aprovacoes_sessao": body.aprovacoes_sessao or {},
                "pergunta_busca": "", "bussola": None,
                "model": modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL,
                "provider": body.provider or "llama-server",
                "tokens": contadores.balanco_ler(), "busca": None}
    # MCP_WEB não conta como "ferramenta MCP" para a trivialidade: saudação
    # com busca marcada continua saudação (não se pesquisa "oi" na web)
    _mcps_reais = [m for m in (body.mcps or []) if m != MCP_WEB]
    eh_trivial = (body.mode == "hibrido" and not body.history and not _mcps_reais
                  and not body.estado_agente and 0 < len(_palavras) <= 6
                  and all(w in _TRIVIAIS for w in _palavras))
    if eh_trivial:
        log("👋 saudação/mensagem trivial — SEM busca na base (economia de "
            "contexto: nada recuperável numa saudação)", "busca")
        contadores.set_etapa("resposta (híbrido, sem busca)")
        answer = rag.answer_free(body.question, body.history)
        contadores.set_etapa(None)
        return {"question": body.question, "mode": body.mode,
                "collections": colecoes, "docs": [], "answer": answer,
                "erros": {}, "ferramentas": [], "mcp_erros": {},
                "pendente": None, "aprovacoes_sessao": body.aprovacoes_sessao or {},
                "pergunta_busca": "", "bussola": None,
                "model": modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL,
                "provider": body.provider or "llama-server",
                "tokens": contadores.balanco_ler()}
    # ⚡ CACHE SEMÂNTICO (Redis): TODOS os modos de texto passam por ele
    # (rag/livre/híbrido — pedido do dono: "os comandos sempre pelo cache"),
    # INCLUSIVE com histórico: o limiar 0.97 só casa pergunta PRATICAMENTE
    # idêntica ("e agora?" nunca bate) — repetir o comando traz a resposta
    # na hora. O STORE continua só de respostas SEM histórico (autocontidas
    # — resposta de follow-up citando a conversa não é guardada).
    _motivos_cache = []
    if body.mode not in ("rag", "livre", "hibrido"):
        _motivos_cache.append(f"modo {body.mode}")
    if body.mcps:
        _motivos_cache.append("ferramentas MCP")
    if body.aprovacao:
        _motivos_cache.append("aprovação pendente")
    if body.anexo_imagem:
        _motivos_cache.append("imagem anexada")
    if body.estado_agente:
        _motivos_cache.append("estado do agente")
    cacheavel = not _motivos_cache
    # DONO da sessão (guardrail: cache contextualizado por usuário —
    # sessão de OUTRO usuário nunca vê esta resposta). ⚠️ MESMO FALLBACK
    # do save_session: sessão AINDA NÃO SALVA (1ª mensagem — o job roda
    # antes do POST gravar o arquivo) tem owner "" e o arquivo depois
    # ganha AUTH_ADMIN_USER — sem o fallback o store gravava "" e o
    # lookup da 2ª ("rodney") NUNCA batia o escopo (bug real do cache).
    try:
        owner = (sessions.get_session(body.sessao) or {}).get("owner", "") \
            if body.sessao else ""
    except Exception:
        owner = ""
    owner = owner or getattr(config, "AUTH_ADMIN_USER", "") or ""
    # chave de MODELO = o que o SERVIDOR está servindo AGORA (nunca o
    # .env da VPS, que fica velho depois de trocas feitas na estação)
    modelo_cache = modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL
    if cacheavel:
        hit = cache.lookup(body.question, colecoes, modelo_cache, owner=owner)
        if hit:
            print(f"⚡ Cache semântico ({hit['similaridade']:.3f}): {body.question[:60]}")
            log(f"⚡ cache semântico ({hit['similaridade']:.3f}) — resposta imediata", "cache")
            return {"question": body.question, "mode": body.mode,
                    "collections": hit["colecoes"], "docs": [],
                    "answer": hit["resposta"], "erros": {},
                    "cache": {"usado": True, "similaridade": hit["similaridade"],
                              "pergunta_original": hit["pergunta"],
                              "modo": hit.get("modo", "?"),
                              "modelo": hit.get("modelo", ""),
                              "criado_em": hit.get("criado_em", 0),
                              "resumo": (hit["resposta"] or "")[:280]},
                    "model": modelo_cache,
                    "provider": body.provider or "llama-server",
                    "tokens": contadores.balanco_ler()}
        log("⚡ cache semântico: sem equivalente — seguindo o fluxo completo", "cache")
    else:
        log("⚡ cache semântico: não se aplica ("
            + ", ".join(_motivos_cache) + ")", "cache")
    # 🧭 F3 — BÚSSOLA PRÉ-TOKEN: "já respondi isso numa sessão passada?"
    # embedding contra a coleção de sistema sessoes_chat (escopo por owner).
    # GUARDRAIL (pedido do dono): a bússola NUNCA responde sozinha com o
    # assunto de outra sessão — outro assunto não pode DIRIGIR a conversa.
    # Ela apenas SUGERE ("isto lembra a conversa X") e a resposta é gerada
    # normal; o cruzamento de sessões fica VISÍVEL, nunca automático.
    bussola_sugestao = None
    if cacheavel and not getattr(config, "MOCK_LLM", False):
        b = bussola.consultar(body.question, owner, log=log)
        if b and b["similaridade"] >= bussola.SUGESTAO:
            log(f"🧭 bússola: pergunta parecida com a conversa "
                f"“{b['titulo'][:50]}” ({b['similaridade']:.2f}) — "
                f"SUGESTÃO anexada (a resposta é gerada normal)", "cache")
            bussola_sugestao = {"similaridade": b["similaridade"],
                                "sessao": b["sessao"], "titulo": b["titulo"]}
    # modelo solicitado ≠ modelo no ar → troca antes de responder. A
    # comparação é NORMALIZADA (alias↔stem, minúsculo): o servidor pode
    # reportar o nome do arquivo enquanto a UI manda o alias — sem
    # normalizar, o MESMO modelo era recarregado a cada mensagem.
    _servido_agora = modelos.servido(modelos.CHAT_PORTA)
    # 🌐 EXTERNO ("prov:modelo") NÃO troca nada local — o override desta
    # execução já aponta a LLM para o endpoint externo (o valor com ":"
    # nunca é alias do REGISTRO; sem isto a estação ficava "carregando"
    # um modelo que não existe)
    if body.model and ":" in str(body.model):
        pass
    elif body.model and modelos.normalizar(body.model) != modelos.normalizar(_servido_agora):
        log(f"🔁 trocando modelo para {body.model} (libera VRAM, ~1 min)…", "modelo")
        try:
            troca = modelos.ativar(body.model,
                                   log=lambda m, g="modelo": log(m, g))
            print(f"🔁 Troca na chamada: {troca['modelo']}"
                  f"{' em ' + str(troca.get('segundos', '?')) + 's' if troca['trocou'] else ' (já estava no ar)'}")
            log(f"✅ {body.model} no ar"
                + (f" em {troca.get('segundos', '?')}s" if troca.get("trocou") else ""),
                "modelo")
        except Exception as e:
            # 🛟 A TROCA NUNCA MATA A RESPOSTA (caso tacacá: agente da estação
            # fora → o job morria no 🔁 e a mensagem ficava "(sem resposta)").
            # Responde com o modelo ATUAL e segue — a próxima mensagem tenta
            # de novo quando a estação voltar.
            motivo = str(e).strip() or "estação com a GPU não respondeu"
            if len(motivo) > 160 or motivo.lstrip().startswith("<"):
                motivo = "estação com a GPU não respondeu (agente offline?)"
            log(f"⚠️ troca para {body.model} falhou ({motivo[:140]}) — "
                f"respondendo com o modelo atual "
                f"({_servido_agora or config.LLM_MODEL})", "modelo")
    elif body.model:
        pass  # modelo já no ar: silêncio (linha "🧠 ... sem recarga" era ruído —
              # pedido do dono; trocas/recargas seguem logadas acima)
    modelo_usado = _servido_agora or modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL
    # com override externo, o modelo desta resposta (cache/telemetria) é o
    # ESCOLHIDO — não o servido localmente
    _ov = rag._override()
    if _ov:
        modelo_usado = f"[{_ov['provedor']}] {_ov['model']}"
    provider = body.provider or "llama-server"
    if body.mode == "livre":
        log("✍️ gerando resposta (conhecimento do modelo, sem busca)…", "geração")
        contadores.set_etapa("resposta (livre)")
        resposta = rag.answer_free(body.question, body.history, on_token=on_token)
        contadores.set_etapa(None)
        if cacheavel and not body.history:
            # STORE só de resposta SEM histórico (autocontida — resposta de
            # follow-up citando a conversa não é guardada); MESMO escopo do
            # lookup (o modo livre não busca, mas a pergunta veio com as
            # coleções resolvidas — gravar vazio nunca bateria); modelo =
            # o que está NO AR neste momento (pode ter TROCADO no meio da
            # resposta — a chave acompanha o servidor)
            cache.store(body.question, resposta, "livre", colecoes,
                        modelo=modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL,
                        owner=owner, sid=body.sessao or "")
            # 🧭 F3: caminho EARLY-RETURN do livre precisa registrar também
            # (o return do fim do fluxo não é alcançado aqui)
            if resposta and not getattr(config, "MOCK_LLM", False):
                bussola.registrar(body.question, resposta, body.sessao, owner,
                                  "livre", colecoes, log=log)
        print(f"\n🧠 Pergunta (modo livre): {body.question}\n🤖 Resposta: {resposta}\n")
        return {"question": body.question, "mode": "livre", "collections": [],
                "docs": [], "answer": resposta, "erros": {},
                "model": modelo_usado, "provider": provider,
                "tokens": contadores.balanco_ler()}
    if body.mode == "auto":  # roteador decide: base / web / livre (+crítica)
        log("🤖 roteador decidindo (base / web / livre)…", "auto")
        try:
            client = QdrantClient(url=config.QDRANT_URL, timeout=30)
            out = responde_auto(client, body.question, body.history, log=log)
        except Exception as e:
            print(f"❌ Erro no modo auto: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        print(f"\n🤖 Pergunta (modo auto): {body.question}\n📚 {len(out['found'])} "
              f"fragmento(s)\n🤖 Resposta: {out['answer']}\n")
        return {"question": body.question, "mode": "auto",
                "collections": out["decisao"].get("colecoes", []),
                "docs": out["found"], "answer": out["answer"], "erros": {},
                "ferramentas": [], "mcp_erros": {}, "pendente": None,
                "aprovacoes_sessao": body.aprovacoes_sessao or {},
                "pergunta_busca": out["consulta"],
                "decisao": {"acao": out["decisao"]["acao"],
                            "motivo": out["decisao"]["motivo"]},
                "model": modelo_usado, "provider": provider,
                "tokens": contadores.balanco_ler()}
    # `colecoes` já foi resolvido no topo (tri-estado: [] = sem busca);
    # na retomada de aprovação o contexto já vem no estado do agente
    found, docs, erros, bases = [], [], {}, None
    resposta_direta = None  # texto que responde por si (score alto; já sanitizado)
    pergunta_busca = body.question  # reformulada na busca (quando há histórico)
    # 🗄️ métrica da busca p/ o rodapé (None = não houve busca: cache/sem coleções)
    busca_stats = None
    if body.estado_agente:
        colecoes = body.collections or colecoes
    elif not colecoes:
        # seleção VAZIA explícita: sem reformulação, sem Qdrant, sem rerank
        # — vai direto para a geração (rag dirá "não possuo dados…",
        # híbrido responde só com o modelo)
        log("📚 sem coleções selecionadas — SEM busca na base "
            "(marque coleções ou 'todas' para consultar)", "busca")
    else:
        try:
            t_busca0 = time.time()  # 🗄️ métrica do QDRANT no rodapé
            client = QdrantClient(url=config.QDRANT_URL, timeout=30)
            if body.history:
                log("🔎 reformulando a pergunta com o histórico…", "busca")
                contadores.set_etapa("reformulação")
            else:
                log("🔎 consultando a base…", "busca")
            pergunta_busca = rag.reformula(body.question, body.history)
            contadores.set_etapa(None)
            if pergunta_busca != body.question:
                print(f"🔎 Busca reformulada: {pergunta_busca}")
                log(f"consulta efetiva: {pergunta_busca[:220]}", "busca")
            # seleção velha do navegador (coleção apagada/renomeada no
            # Qdrant) NÃO é erro de infra: sai do escopo em silêncio —
            # mesmo critério de "coleções com problema não derrubam a
            # busca" do rag.search. Escopo todo morto = busca vazia
            # (modo rag responde "sem dados"; híbrido responde só com
            # o modelo), NUNCA 503.
            vivas = [c for c in colecoes if client.collection_exists(c)]
            if len(vivas) != len(colecoes):
                mortas = [c for c in colecoes if c not in vivas]
                log("🗑️ fora do escopo (não existem mais): "
                    + ", ".join(mortas), "busca")
                colecoes = vivas
            # base unificada: buscando qualquer coleção de linguagem de
            # programação, a visão unificada entra JUNTA automaticamente
            escopo = list(colecoes)
            if (any(c in LINGUAGENS for c in escopo)
                    and BASE_UNIFICADA not in escopo
                    and client.collection_exists(BASE_UNIFICADA)):
                escopo.append(BASE_UNIFICADA)
            if not escopo:
                log("📚 escopo vazio (nada existe) — seguindo sem "
                    "contexto da base", "busca")
            achados, erros = rag.search(client, escopo, pergunta_busca,
                                        log=lambda m, g="busca": log(m, g))
            # ⚠️ top_score NASCE AQUI (0 quando a densa não traz nada): os
            # RESGATES adiante (rerank/EN/versão) podem REABASTECER achados
            # depois do `if achados` — e o rodapé lê top_score fora dele
            # (bug real: ".NET 10" com densa vazia + resgate por versão
            # preenchendo → UnboundLocalError matava a resposta)
            top_score = (max(float(s) for _, s, _ in achados)
                         if achados else 0.0)
            # 🔁 RESGATE PT→EN (bug real do dono: "Cria uma api no padrão
            # .net 10" → 0 fragmentos numa coleção dotnet de 28k docs): a
            # pergunta em PORTUGUÊS contra base EM INGLÊS afunda o score
            # denso E o full-text (termos PT não existem no texto EN; o
            # termo técnico "api" caía no filtro >3 chars). A pesquisa da
            # Biblioteca SEMPRE normalizou p/ inglês (idioma.py) — o chat
            # não tinha. NADA acima do corte → 1 chamada barata (temp 0)
            # traduz a pergunta e REBUSCA; só roda sem histórico (com
            # histórico a reformulação já cumpre o papel) e 1x.
            if (not achados and not body.history and escopo
                    and body.mode in ("rag", "hibrido")):
                try:
                    from core import idioma as _idioma
                    pergunta_en = _idioma.para_busca_inglesa(
                        pergunta_busca, log=lambda m, g="busca": log(m, g))
                    if pergunta_en.strip().lower() != pergunta_busca.strip().lower():
                        log(f"🔁 resgate: rebuscando com a pergunta em "
                            f"inglês “{pergunta_en[:80]}”…", "busca")
                        achados2, _ = rag.search(
                            client, escopo, pergunta_en,
                            log=lambda m, g="busca": log(m, g))
                        if achados2:
                            achados = achados2
                            pergunta_busca = pergunta_en
                            log(f"🔁 resgate trouxe {len(achados)} "
                                "fragmento(s) — seguindo com eles", "busca")
                except Exception as e:
                    log(f"⚠️ resgate PT→EN indisponível ({str(e)[:60]})",
                        "busca")
            # TODAS as coleções EXISTENTES falharam (embedding desligado,
            # Qdrant fora): resposta vazia não ajuda — erro claro com a CAUSA
            if escopo and erros and not achados and len(erros) >= len(escopo):
                causa = next(iter(erros.values()))
                raise HTTPException(status_code=503, detail=causa)
            bases = _bases_consultadas(client, escopo)
            por_colecao = {}
            for _, _, c in achados:
                por_colecao[c] = por_colecao.get(c, 0) + 1
            log(f"📚 {len(achados)} fragmento(s): "
                + ", ".join(f"{c} ({n})" for c, n in por_colecao.items()), "busca")
            # 🎛️ reranker (F2-7): com contexto largo, o cross-encoder local
            # reordena os top por relevância real e devolve os 4 melhores
            # (menos ruído no prompt). Degrada em silêncio sem torch/flag.
            # ⚡ TOP-8 (era 15): ~1 s/par na CPU de 2 vCPUs da VPS — 15 pares
            # eram 15 s por pergunta; o top-4 sai de 8 candidatos fácil e o
            # gate fraco revalida os MESMOS 8 (cache de pares = grátis).
            if body.mode in ("rag", "hibrido") and len(achados) >= 6:
                rr = rerank.rerank(pergunta_busca, achados[:8], top_n=4,
                                   log=lambda m, g="busca": log(m, g))
                if rr:
                    ordenados, topo = rr
                    log(f"🎛️ rerank {len(achados)}→{len(ordenados)} "
                        f"(top {topo:.3f})", "busca")
                    achados = ordenados
            # 🎯 SCORE COMO GUARDRAIL (pedido do dono): o score do melhor
            # fragmento diz o que fazer com ele —
            #   ≥ SCORE_DIRETO: a base RESPONDE por si → resposta direta do
            #     fragmento, SEM consultar a LLM (economia total de tokens);
            #   < SCORE_FRACO: fragmentos fracos NÃO sustentam a pergunta →
            #     fora do prompt (o modelo responde sozinho em vez de
            #     alucinar em cima de contexto irrelevante — caso vatapá:
            #     Fabada asturiana 0.470 virava "receita" de vatapá);
            #   entre os dois: RAG normal (base + modelo).
            if achados:
                top_score = max(float(s) for _, s, _ in achados)
                if not body.history and top_score >= config.SCORE_DIRETO:
                    doc_top = max(achados, key=lambda t: float(t[1]))[0]
                    conteudo = doc_top.page_content
                    # 🧹 HTML CRU (página mal ingerida que virou chunk): a
                    # resposta direta NUNCA devolve marcação — extrai o texto
                    # legível; sem conteúdo aproveitável, segue para o modelo.
                    # ⚠️ BLINDADO: isto roda DENTRO do try da busca e um erro
                    # aqui era mascarado como "Qdrant indisponível" — a
                    # sanitização falhou = entrega o texto cru ao modelo.
                    try:
                        if limpeza.parece_pagina_html(conteudo):
                            texto = limpeza.html_para_texto(conteudo)
                            if texto:
                                log(f"🧹 fragmento era HTML cru — texto extraído "
                                    f"({len(texto)} chars) para a resposta direta",
                                    "geração")
                                resposta_direta = texto
                            else:
                                log("🧹 fragmento era HTML cru sem texto "
                                    "aproveitável — seguindo para o modelo",
                                    "geração")
                        else:
                            # 🧹 HEADER DE CHUNK fora (bug real do dono: a
                            # resposta-direta abria com "[frango ao leite
                            # passo a passo]" — o cabeçalho contextual do
                            # chunk é METADADO de indexação, não conteúdo)
                            limpo = re.sub(r"^\s*\[[^\]\n]{1,140}\][ \t]*\r?\n",
                                           "", conteudo, count=1)
                            resposta_direta = (limpo.strip() or conteudo).strip()
                    except Exception as e:
                        log(f"⚠️ sanitização do fragmento falhou "
                            f"({str(e)[:80]}) — seguindo para o modelo",
                            "geração")
                    if resposta_direta is not None:
                        log(f"🎯 score {top_score:.3f} ≥ {config.SCORE_DIRETO} — "
                            "a base responde por si: resposta DIRETA do fragmento "
                            "(sem consulta à LLM)", "geração")
                elif top_score < config.SCORE_FRACO:
                    # 🛟 RESGATE pelo RERANKER (pedido "deve ser real"):
                    # pergunta PT × base EN deprime o score DENSO — o
                    # chunk REAL do Tucupi pontuava 0.547 e o gate
                    # descartava contexto verdadeiro (o modelo respondia
                    # sozinho e alucinava "Nordeste" em vez de Pará). O
                    # cross-encoder é BILÍNGUE (lê pergunta×texto JUNTOS):
                    # nota alta = relevante de verdade → MANTÉM (reordenado
                    # pela nota); nota baixa = descarta como antes (é o
                    # que separa Tucupi 0.7 do Fabada irrelevante 0.003).
                    notas = None
                    try:
                        notas = rerank.notas_de(
                            pergunta_busca,
                            [d.page_content for d, _, _ in achados[:8]],
                            log=lambda m, g="busca": log(m, g))
                    except Exception:
                        pass
                    if notas and max(notas) >= 0.10:
                        pares = sorted(zip(achados[:len(notas)], notas),
                                       key=lambda t: -t[1])
                        achados = [a for a, n in pares if n >= 0.05][:4]
                        log(f"🎛️ rerank RESGATOU {len(achados)} fragmento(s) "
                            f"(top {max(notas):.2f}) — score denso deprimido "
                            "(pergunta e base em idiomas diferentes?) — "
                            "contexto real mantido", "busca")
                    else:
                        log(f"⚠️ fragmentos fracos (top {top_score:.3f} < "
                            f"{config.SCORE_FRACO}) — a base não sustenta esta "
                            "pergunta; descartados do contexto", "busca")
                        achados = []
                        # 🔁 2º resgate PT→EN: o rerank bilíngue também não
                        # salvou — última carta é traduzir a PERGUNTA e
                        # rebuscar (conhecimento real que caiu na zona fraca
                        # por idioma; criação/mistura de assuntos não salva —
                        # a recusa orientada cuida desses)
                        if not body.history and body.mode in ("rag", "hibrido"):
                            try:
                                from core import idioma as _idioma2
                                _en = _idioma2.para_busca_inglesa(
                                    pergunta_busca,
                                    log=lambda m, g="busca": log(m, g))
                                if _en.strip().lower() != pergunta_busca.strip().lower():
                                    achados2, _ = rag.search(
                                        client, escopo, _en,
                                        log=lambda m, g="busca": log(m, g))
                                    if achados2 and max(float(s) for _, s, _ in achados2) >= config.SCORE_FRACO:
                                        achados = achados2
                                        pergunta_busca = _en
                                        log(f"🔁 resgate tardio (EN) trouxe "
                                            f"{len(achados)} fragmento(s) "
                                            "relevantes", "busca")
                            except Exception:
                                pass
        except HTTPException:
            raise  # 503 já traz a CAUSA — não re-empacota como "Qdrant indisponível"
        except Exception as e:
            print(f"❌ Erro na consulta: {e}")
            raise HTTPException(status_code=503, detail=f"Qdrant indisponível: {e}")
        # 🎯 RESGATE POR VERSÃO CITADA (fora do try da busca — roda com os
        # ACHADOS FINAIS; caso real do dono: 978 pontos de ".NET 10" na
        # coleção dotnet e NENHUM vinha — a pergunta multi-assunto
        # "gastronomia + .NET 10" dilui o vetor denso e os slots iam para
        # o outro assunto). Versão citada ausente dos recuperados →
        # busca LEXICAL (com ÍNDICE full-text criado lazy pelo
        # rag._busca_lexical — scroll cru SEM índice é full-scan e
        # SATUROU o Qdrant em produção) SÓ nas coleções DEV do escopo.
        if body.mode in ("rag", "hibrido") and escopo:
            try:
                from core.linguagens import EH_DEV as _EH_DEV
                docs_atuais = [d for d, _, _ in (achados or [])]
                # sem docs recuperados, TODA versão citada é "ausente" (a
                # base pode ter o material mesmo com a densa vazia — era o
                # caso e o guard `not docs` calava o resgate)
                ausentes = (rag.versoes_ausentes(body.question, docs_atuais)
                            or sorted(rag._pares_versao(body.question)))
                if ausentes:
                    termos_crus = [m.group(0) for m in
                                   rag._RE_TECH_VERSAO.finditer(body.question or "")]
                    extras = []
                    vistos = {d.page_content for d in docs_atuais}
                    for termo in termos_crus[:2]:
                        for col in escopo:
                            if col not in _EH_DEV:
                                continue  # versão de TECH busca em coleção DEV
                            for d in rag._busca_lexical(
                                    client, col, termo, limite=3):
                                if d.page_content and d.page_content not in vistos:
                                    vistos.add(d.page_content)
                                    extras.append((d, config.SCORE_MIN, col))
                    if extras:
                        achados = list(achados or []) + extras[:4]
                        # top_score honesto: os extras trazem score próprio
                        top_score = max([top_score]
                                        + [float(s) for _, s, _ in extras[:4]])
                        log(f"🎯 resgate por versão: +{min(len(extras), 4)} "
                            "fragmento(s) do material específico de "
                            f"{', '.join(termos_crus[:2])} (full-text "
                            "indexado — a busca densa multi-assunto não os "
                            "trouxe)", "busca")
            except Exception as e:
                log(f"⚠️ resgate por versão falhou: {str(e)[:60]}", "busca")
        found = [
            {
                "score": round(float(score), 4),
                "source": d.metadata.get("source"),
                "titulo": d.metadata.get("titulo"),
                "secao": d.metadata.get("secao"),
                "categoria": d.metadata.get("categoria"),
                "descricao": d.metadata.get("descricao"),
                "resumo_pt": d.metadata.get("resumo_pt") or d.metadata.get("proposito"),
                "linguagem": d.metadata.get("linguagem"),
                "qualidade": d.metadata.get("qualidade"),
                "colecao": colecao,
                "content": d.page_content,
            }
            for d, score, colecao in achados
        ]
        docs = [d for d, _, _ in achados]
        # 🗄️ MÉTRICA DA BUSCA (pedido do dono: tokens/velocidade SEMPRE no
        # rodapé — e "isso se aplica ao qdrant também"): tempo real da
        # consulta (Qdrant + rerank), nº de fragmentos e o top score —
        # visível inclusive na resposta-direta (que não gasta LLM)
        busca_stats = {"ms": int((time.time() - t_busca0) * 1000),
                       "fragmentos": len(found),
                       "top": (round(top_score, 3)
                               if achados else None)}
    # 🔎 PESQUISA-WEB (NATIVA) marcada no seletor do chat: o motor de busca
    # do RagAroy roda AGORA e as páginas INTEIRAS baixadas (DuckDuckGo/
    # Serper → download real via Trafilatura — não snippets) entram como
    # fragmentos numerados [n]: citáveis na resposta e visíveis no painel
    # de fontes. Contextualiza a sessão com informação ATUAL.
    if MCP_WEB in (body.mcps or []):
        try:
            log("🌐 pesquisa-web (nativa) marcada — buscando páginas "
                "completas na web…", "mcp")
            web_docs = _web_aprofundado(
                body.question, pergunta_busca or body.question, log=log)
            if web_docs:
                docs = list(docs or []) + web_docs
                found += [{
                    "score": None,
                    "source": d.metadata.get("source"),
                    "titulo": d.metadata.get("titulo"),
                    "secao": None, "categoria": None,
                    "descricao": d.metadata.get("descricao"),
                    "resumo_pt": None, "linguagem": None, "qualidade": None,
                    "colecao": "🌐 web", "content": d.page_content,
                } for d in web_docs]
                log(f"🌐 {len(web_docs)} página(s) da web no contexto — "
                    "cite [n]; as origens entram no painel de fontes", "mcp")
            else:
                log("⚠️ a busca web não trouxe nada — seguindo sem ela", "mcp")
        except Exception as e:
            log(f"⚠️ pesquisa-web falhou: {str(e)[:120]}", "mcp")
    usos, mcp_erros, pendente, aprovacoes = [], {}, None, body.aprovacoes_sessao or {}
    # 🛡️ GUARDA DE VERSÃO (dinâmica, contra o Qdrant — pedido do dono): a
    # pergunta cita "C# 14/.NET 10/python 3.12" e NENHUM fragmento traz?
    # O contexto do prompt ganha o aviso (rag proíbe inventar; híbrido
    # exige declarar) — e o "pensando…" mostra o porquê da resposta curta
    try:
        _ausentes = rag.versoes_ausentes(body.question, docs or [])
        if _ausentes:
            log(f"⚠️ versão citada ({', '.join(_ausentes)}) não está nos "
                "fragmentos — modelo instruído a não inventar (responda "
                "com o que a base cobre)", "busca")
    except Exception:
        pass
    # 📦 TRANSPARÊNCIA DO PROMPT (pedido: "sem ruídos — specs, digitado,
    # cache e rag"): o que EXATAMENTE vai para o modelo, em tokens
    # estimados — dá para ver de onde vem o tamanho antes de gerar
    try:
        from core.specs import spec as _spec_txt
        _partes = []
        _nome_spec = "chat" if body.mode == "rag" else (
            "hibrido" if body.mode == "hibrido" else "")
        if _nome_spec:
            _partes.append(f"spec ~{len(_spec_txt(_nome_spec)) // 4}")
        if docs:
            _partes.append(f"rag ~{len(rag.format_docs(docs)) // 4} ({len(docs)} frag.)")
        if body.history:
            _hc = sum(len(str(m.get('content') or '')) for m in body.history)
            _partes.append(f"histórico ~{_hc // 4} ({len(body.history)} msg)")
        _partes.append(f"pergunta ~{len(body.question) // 4}")
        log("📦 prompt: " + " · ".join(_partes) + " tokens (estimativa)", "geração")
    except Exception:
        pass
    # 🔎 busca web marcada = o usuário quer síntese com o motor de busca;
    # a resposta-direta-da-base (score alto) ficaria pela metade
    if resposta_direta is not None and MCP_WEB in (body.mcps or []):
        resposta_direta = None
    if resposta_direta is not None:
        # 🎯 resposta DIRETA da base: o fragmento já é a resposta — zero
        # chamada à LLM, sai na hora (as fontes seguem em `found`/painel)
        log("✍️ devolvendo o fragmento da base como resposta (0 chamada à "
            "LLM)", "geração")
        answer = resposta_direta
    elif body.mode == "hibrido" and _mcps_reais:  # servidores MCP marcados: ferramentas entram na resposta
        log(f"🔌 conectando {len(_mcps_reais)} servidor(es) MCP…", "mcp")
        ferramentas, mcp_erros = mcp_registry.carregar_ferramentas(
            _mcps_reais, log=lambda m: log(m, "mcp"))
        if ferramentas:
            log(f"🔧 agente ReAct com {len(ferramentas)} ferramenta(s)…", "mcp")
            answer, usos, pendente, aprovacoes = agent.responde(
                body.question, docs, ferramentas, body.history,
                body.estado_agente, body.aprovacao, body.aprovacoes_sessao,
                bases=bases, log=lambda m: log(m, "mcp"))
        else:  # nenhum servidor respondeu: híbrido comum
            log("⚠️ nenhum MCP respondeu — respondendo sem ferramentas", "mcp")
            answer = rag.answer_hybrid(body.question, docs, body.history, bases, on_token=on_token)
    elif body.mode == "hibrido":
        log(f"✍️ gerando resposta (base + modelo, {len(docs)} fragmento(s))…", "geração")
        contadores.set_etapa("resposta (híbrido)")
        answer = rag.answer_hybrid(body.question, docs, body.history, bases)
        contadores.set_etapa(None)
    else:
        log(f"✍️ gerando resposta só com a base ({len(docs)} fragmento(s))…", "geração")
        if found:
            contadores.set_etapa("resposta (rag)")
            answer = rag.answer(body.question, docs, body.history, bases, on_token=on_token)
            contadores.set_etapa(None)
        else:
            # spec restritiva (F2-8): SEM contexto não há o que responder —
            # a frase exata, sem gastar chamada de LLM.
            # 💡 PEDIDO DE CRIAÇÃO em modo rag (bug real do dono: "quero uma
            # página em .net 10 sobre culinária" → recusa nua): criar algo
            # novo é trabalho do HÍBRIDO (base orienta o estilo, modelo
            # escreve) — a recusa ORIENTA em vez de só negar
            _RE_CRIAR = re.compile(
                r"\b(quer[oa]|cri[ae]|criar|faç|faz|fazer|mont[ae]|montar|"
                r"ger[ae]|gerar|escrev|desenvolv|implement|constru|code|"
                r"program)\w*", re.I)
            _RE_COISA = re.compile(
                r"\b(p[áa]gina|site|c[óo]digo|api|app|aplica|projeto|"
                r"programa|script|componente|aba|tela|formul[áa]rio|"
                r"banco|tabela|servidor|fun[çc][ãa]|classe)\w*", re.I)
            if body.mode == "rag" and _RE_CRIAR.search(body.question or "") \
                    and _RE_COISA.search(body.question or ""):
                answer = ("Não possuo dados confiáveis o suficiente nos "
                          "documentos para responder.\n\n💡 Seu pedido parece "
                          "ser de **criação** (página, código, API…): troque "
                          "para o modo **híbrido** — a base orienta o estilo "
                          "e o modelo escreve. Dica: para código, selecione "
                          "só a coleção da tecnologia (ex.: dotnet) — misturar "
                          "assuntos dilui a busca.")
            else:
                answer = ("Não possuo dados confiáveis o suficiente nos "
                          "documentos para responder.")
    if pendente:
        log(f"🔐 aguardando sua aprovação para usar {pendente['ferramenta']}", "mcp")
        print(f"\n🔐 Ferramenta aguardando aprovação: {pendente['ferramenta']} "
              f"← {pendente['argumento'][:120]}")
    else:
        print(f"\n🔗 Pergunta (modo {body.mode}): {body.question} [coleções: {colecoes}]"
              f"\n🔌 MCPs: {body.mcps or []} — {len(usos)} chamada(s) de ferramenta"
              f"\n📚 {len(found)} documento(s)\n🤖 Resposta: {answer}\n")
    if cacheavel and not pendente and not body.history:
        # STORE só de respostas SEM histórico (autocontidas) — o lookup roda
        # sempre, mas guardar resposta de follow-up (que cita a conversa)
        # serviria mal uma sessão limpa no futuro
        cache.store(body.question, answer, body.mode, colecoes,
                    modelo=modelos.servido(modelos.CHAT_PORTA) or config.LLM_MODEL,
                    owner=owner, sid=body.sessao or "")
        # 🧭 F3: registra (pergunta→resposta) para a PRÓXIMA sair de graça
        if answer and not getattr(config, "MOCK_LLM", False):
            bussola.registrar(body.question, answer, body.sessao, owner,
                              body.mode, colecoes, log=log)
    return {"question": body.question, "mode": body.mode, "collections": colecoes, "docs": found,
            "answer": answer, "erros": erros, "ferramentas": usos, "mcp_erros": mcp_erros,
            "pendente": pendente, "aprovacoes_sessao": aprovacoes,
            "pergunta_busca": pergunta_busca if not body.estado_agente else "",
            "bussola": bussola_sugestao,
            "model": modelo_usado, "provider": provider,
            "busca": busca_stats,
            "tokens": contadores.balanco_ler()}  # ÚNICO método (o antigo
            # uso_desde(marcador) cruzava processos e divergia — removido)


@app.get("/api/docs")
def list_docs(collection: str, limit: int = 20, cursor: str | None = None):
    """Lista os documentos (chunks) da coleção mostrando o payload como está."""
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30, check_compatibility=False)
        total = client.count(collection, exact=True).count
        pontos, proximo = client.scroll(collection_name=collection, limit=limit,
                                        with_payload=True, offset=cursor or None)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao ler '{collection}': {e}")
    docs = [
        {
            "id": str(p.id),
            "page_content": (p.payload or {}).get("page_content", ""),
            "metadata": (p.payload or {}).get("metadata", {}),
        }
        for p in pontos
    ]
    return {"collection": collection, "total": total,
            "cursor_proximo": str(proximo) if proximo else None, "docs": docs}


@app.put("/api/docs")
def edit_doc(body: DocEditIn):
    """Edita um documento: metadados (mantém o vetor) e/ou texto (re-embeda)."""
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=60, check_compatibility=False)
        achados = client.retrieve(collection_name=body.collection, ids=[body.id],
                                  with_payload=True, with_vectors=True)
        if not achados:
            raise HTTPException(status_code=404,
                                detail=f"Documento {body.id} não encontrado em '{body.collection}'")
        p = achados[0]
        payload = dict(p.payload or {})
        if body.metadata is not None:  # merge: só as chaves enviadas mudam
            payload["metadata"] = {**(payload.get("metadata") or {}), **body.metadata}
        if body.page_content is not None and body.page_content != payload.get("page_content"):
            if isinstance(p.vector, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Coleção com vetores nomeados: só é possível editar os metadados, "
                           "não o texto.")
            payload["page_content"] = body.page_content
            vetor = rag.embeddings().embed_query(body.page_content)  # texto novo => vetor novo
        else:
            vetor = p.vector  # texto igual (ou só metadata): reaproveita o vetor
        client.upsert(collection_name=body.collection,
                      points=[PointStruct(id=p.id, vector=vetor, payload=payload)])
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Erro ao editar documento: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    print(f"✏️  Documento {body.id} editado em '{body.collection}'")
    return {"id": str(p.id), "page_content": payload.get("page_content", ""),
            "metadata": payload.get("metadata", {})}


@app.delete("/api/docs")
def delete_docs(body: DocDeleteIn):
    """Apaga documentos (chunks) da coleção pelos ids."""
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30, check_compatibility=False)
        client.delete(collection_name=body.collection, points_selector=body.ids)
    except Exception as e:
        print(f"❌ Erro ao apagar documentos: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    print(f"🗑️  {len(body.ids)} documento(s) apagado(s) de '{body.collection}'")
    return {"apagados": len(body.ids), "collection": body.collection}


# ---------- Sessões do chat (salvar/carregar conversas) ----------

# id de sessão válido: hex/uuid simples — bloqueia path traversal
# (ex.: id="../users" leria/apagaria .json FORA de sessions/)
_RE_SID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _sid_valido(sid: str | None) -> bool:
    return bool(sid) and bool(_RE_SID.match(sid))


@app.get("/api/sessions")
def list_sessoes(request: Request):
    """Resumo das sessões SALVAS DO USUÁRIO, da mais recente para a mais antiga."""
    return sessions.list_sessions(owner=_usuario(request))


@app.post("/api/sessions")
def save_sessao(body: SessionIn, request: Request):
    """Cria/atualiza a sessão (upsert pelo id; sem id = nova) e grava também
    no Qdrant (sessões com embedding — conversas ficam semanticamente
    pesquisáveis sem vazar para coleções de conteúdo)."""
    dono = _usuario(request)
    if body.id and (not _sid_valido(body.id)
                    or (sessions.get_session(body.id) or {}).get("owner") not in ("", None, dono)):
        # id malicioso OU sessão de outro usuário: vira sessão NOVA (não
        # sobrescreve conversa alheia)
        body.id = None
    resumo = sessions.save_session(body.messages, body.titulo, body.id,
                                   body.modo, body.colecoes, body.aprovacoes,
                                   owner=dono, raw=body.raw)
    print(f"💾 Sessão '{resumo['titulo'][:40]}' salva ({resumo['mensagens']} mensagens)")
    threading.Thread(target=_embed_sessao, args=(resumo["id"], dono), daemon=True).start()
    return resumo


SESSOES_QDRANT = "sessoes_chat"


def _embed_sessao(sid: str, dono: str):
    """Indexa a sessão no Qdrant (embedding bge-m3) — falha silenciosa: a
    conversa já está salva em JSON; o vetor é um extra para busca."""
    try:
        dados = sessions.get_session(sid)
        if not dados:
            return
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        if not client.collection_exists(SESSOES_QDRANT):
            from qdrant_client.models import Distance, VectorParams
            dim = len(rag.embeddings().embed_query("dim"))
            client.create_collection(
                collection_name=SESSOES_QDRANT,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        msgs = dados.get("messages", [])
        ultima = next((m["content"] for m in reversed(msgs)
                       if m.get("role") == "assistant" and m.get("content")), "")
        texto = (f"[sessão de chat]\n{dados.get('titulo', '')}\n\n"
                 + "\n".join(f"{m['role']}: {m['content']}" for m in msgs[-8:]))
        ponto = str(uuid.uuid5(uuid.NAMESPACE_URL, f"sessao:{sid}"))
        client.upsert(collection_name=SESSOES_QDRANT, points=[PointStruct(
            id=ponto,
            vector=rag.embeddings().embed_query(texto[:4000]),
            payload={"page_content": texto[:8000],
                     "metadata": {"tipo": "sessao", "sessao_id": sid, "owner": dono,
                                  "titulo": dados.get("titulo", ""),
                                  "resumo": ultima[:400],
                                  "atualizada": dados.get("atualizada", "")}})])
    except Exception as e:
        print(f"⚠️  Sessão '{sid}' não indexada no Qdrant: {e}")


@app.get("/api/sessions/{sid}")
def get_sessao(sid: str, request: Request):
    """Sessão completa, com todas as mensagens (só do dono)."""
    if not _sid_valido(sid):
        raise HTTPException(status_code=400, detail="id de sessão inválido")
    dados = sessions.get_session(sid)
    dono = _usuario(request)
    if not dados or dados.get("owner") != dono:
        raise HTTPException(status_code=404, detail=f"Sessão '{sid}' não encontrada")
    return dados


@app.delete("/api/sessions/{sid}")
def delete_sessao(sid: str, request: Request):
    """Apaga a sessão salva (e solta o lock se estiver ocupada) — só o dono."""
    if not _sid_valido(sid):
        raise HTTPException(status_code=400, detail="id de sessão inválido")
    dados = sessions.get_session(sid)
    dono = _usuario(request)
    if not dados or dados.get("owner") != dono:
        raise HTTPException(status_code=404, detail=f"Sessão '{sid}' não encontrada")
    sessions.delete_session(sid)
    tarefas.limpar_sessao(sid)
    print(f"🗑️  Sessão '{sid}' apagada")
    return {"removida": sid}


# ---------- Servidores MCP (ferramentas do modo híbrido) ----------

@app.get("/api/mcp")
def lista_mcp():
    """Servidores MCP registrados (mcp_servers.json)."""
    return mcp_registry.list_servers()


@app.post("/api/mcp")
def salva_mcp(body: McpIn, request: Request):
    """Registra/atualiza um servidor MCP (stdio, http ou sse).

    EXCLUSIVO do administrador: registrar um servidor stdio executa um
    processo no host quando o agente conecta."""
    _exigir_admin(request)
    try:
        servidor = mcp_registry.save_server(body.nome, body.transport,
                                            body.command, body.args, body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    print(f"🔌 MCP '{body.nome}' registrado [{body.transport}]")
    return servidor


@app.get("/api/mcp/conhecidos")
def mcp_conhecidos():
    """Catálogo de MCPs conhecidos — instaláveis com um clique na webui."""
    return mcp_registry.list_conhecidos()


class McpTestarIn(BaseModel):
    entrada: str  # URL http · comando (npx/uvx…) · github.com/owner/repo


@app.post("/api/mcp/testar")
def mcp_testar(body: McpTestarIn, request: Request):
    """Testa a conexão SEM registrar: conecta, lista as ferramentas e devolve
    o que encontrou (o botão 'testar' da webui). EXCLUSIVO do administrador —
    testar stdio executa o comando no host."""
    _exigir_admin(request)
    try:
        return mcp_registry.testar(body.entrada)
    except Exception as e:
        return {"ok": False, "erro": str(e)[:300]}


# instalação de MCP como JOB (logs ao vivo no popup — igual ingestão/seed)
_mcp = JobRegistry("mcp", "instalação de MCP")


class McpInstalarEntradaIn(BaseModel):
    entrada: str = ""            # campo único (URL/comando/github) — se vazio, usa nome do catálogo
    nome: str = ""               # nome do catálogo (instalação por clique)
    params: dict = {}            # {{param}} do catálogo (pasta permitida, etc.)
    env: dict = {}               # chaves opcionais (API keys) → .env


@app.post("/api/mcp/instalar-job")
def mcp_instalar_job(body: McpInstalarEntradaIn, request: Request):
    """Instala um MCP como JOB em 2º plano com log completo: detecta a
    entrada (URL/comando/repo git → clona), conecta para TESTAR (lista as
    ferramentas), registra no mcp_servers.json e grava chaves no .env.
    Acompanhe em /api/mcp/instalar-job/status/{job}.

    EXCLUSIVO do administrador: instalação executa processos no host
    (npx/uvx/git) e grava variáveis no .env."""
    _exigir_admin(request)
    # chaves de API aceitas: nome de env simples (API_KEY_*, *_TOKEN…) —
    # nunca as chaves de infra/auth (allowlist acima, junto ao SettingsIn)
    for chave in (body.env or {}):
        if not _RE_ENV_OK.match(chave.upper()) or chave.upper() in _ENV_PROIBIDAS:
            raise HTTPException(status_code=400,
                                detail=f"chave de ambiente '{chave}' não é permitida "
                                       "(use nomes como API_KEY_XXX)")
    job = _mcp.novo_id()
    pedido = body.model_dump()

    def fabricar(p: dict):
        jid = p["job"]
        corpo = McpInstalarEntradaIn(**pedido)

        def rodar():
            contadores.set_servico("manutencao")
            _mcp.iniciar(jid)
            try:
                log = lambda m, g='': _mcp.log(jid, m, grupo=g or 'geral')  # noqa: E731
                entrada = corpo.entrada.strip()
                if entrada:
                    log(f"🔎 detectando o que é: {entrada[:80]}")
                    reg = mcp_registry.detectar(entrada)
                    if reg["transport"] == "git":
                        log(f"📥 repo GitHub — {reg['url']}")
                        reg = mcp_registry.clonar_git(reg["url"], log)
                else:
                    alvo = next((s for s in mcp_registry.list_conhecidos()
                                 if s["nome"] == corpo.nome), None)
                    if not alvo:
                        raise ValueError(f"'{corpo.nome}' não está no catálogo")
                    log(f"📦 catálogo: {alvo['rotulo']}")
                    params = corpo.params or {}
                    faltando = [p["chave"] for p in alvo.get("params", [])
                                if not (params.get(p["chave"]) or "").strip()]
                    if faltando:
                        raise ValueError("parâmetros obrigatórios: " + ", ".join(faltando))
                    reg = {"nome": alvo["nome"], "transport": alvo["transport"],
                           "command": alvo.get("command", ""),
                           "args": [mcp_registry._substituir(a, params)
                                    for a in alvo.get("args", [])],
                           "url": alvo.get("url", "")}
                # chaves opcionais → .env (o operador vê em ⚙️ Configurações)
                for chave, valor in (corpo.env or {}).items():
                    if str(valor).strip():
                        config.set_env_inplace(chave.upper(), str(valor).strip())
                        log(f"🔑 {chave.upper()} gravada no .env")
                        os.environ[chave.upper()] = str(valor).strip()
                log(f"🔌 testando conexão com '{reg['nome']}'…")
                cliente = mcp_registry.MultiServerMCPClient(
                    {reg["nome"]: mcp_registry._config_cliente(reg)})
                ferramentas = asyncio.run(cliente.get_tools())
                log(f"✅ conectou — {len(ferramentas)} ferramenta(s): "
                    + ", ".join(f.name for f in ferramentas[:8])
                    + ("…" if len(ferramentas) > 8 else ""))
                servidor = mcp_registry.save_server(
                    reg["nome"], reg["transport"], reg["command"], reg["args"], reg["url"])
                log(f"💾 '{reg['nome']}' registrado")
                _mcp.concluir(jid, result={"servidor": servidor,
                                           "ferramentas": [f.name for f in ferramentas]})
            except Exception as e:
                print(f"❌ Instalação de MCP falhou: {e}")
                _mcp.concluir(jid, error=str(e)[:300])
        return rodar

    _despachar(fabricar, "mcp_instalar", {"job": job}, _mcp)
    return {"job": job}


_rota_status("/api/mcp/instalar-job/status/{job}", _mcp,
             "Job de instalação não encontrado")


class McpInstalarIn(BaseModel):
    nome: str  # nome no catálogo (mcp_conhecidos.json)
    params: dict = {}  # {"pasta_permitida": "...", ...} conforme o servidor


@app.post("/api/mcp/instalar")
def mcp_instalar(body: McpInstalarIn, request: Request):
    """Instala um MCP do catálogo: registra no mcp_servers.json com os
    parâmetros do operador (npx/uvx baixam o resto na 1ª execução)."""
    _exigir_admin(request)
    try:
        servidor = mcp_registry.instalar_conhecido(body.nome, body.params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    print(f"🔌 MCP '{body.nome}' instalado do catálogo")
    return servidor


@app.delete("/api/mcp/{nome}")
def remove_mcp(nome: str, request: Request):
    """Remove um servidor MCP do registro (exclusivo do administrador)."""
    _exigir_admin(request)
    if not mcp_registry.remove_server(nome):
        raise HTTPException(status_code=404, detail=f"MCP '{nome}' não registrado")
    print(f"🔌 MCP '{nome}' removido")
    return {"removido": nome}


# (A UI é server-rendered: templates/ + static/ — a webui React foi removida.)
