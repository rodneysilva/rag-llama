"""Rotas de sandbox — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.post("/api/sandbox/testar")
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
    # SLUG da sessão do CHAT de origem (cookie) — entra na URI do app vivo
    _slug_sessao = request.cookies.get(SESSAO_COOKIE, "")

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
                                   log=lambda m, g="": _sbx.log(jid, m),
                                   slug_sessao=payload.get("slug", ""))
                _sbx.concluir(jid, result=r)
            except Exception as e:
                _sbx.concluir(jid, error=str(e)[:400])
        return rodar

    job = _sbx.novo_id()
    _despachar(_fabricar, "sandbox",
               {"principal": principal, "arquivos": body.arquivos,
                "timeout": body.timeout, "slug": _slug_sessao,
                "job": job}, _sbx)
    return {"job": job}


@router.get("/api/sandbox/linguagens")
def sandbox_linguagens(request: Request):
    """Versões instaladas na sandbox (para a UI avisar o que dá pra testar)."""
    _usuario(request)
    from core import sandbox
    try:
        return {"ok": True, "linguagens": sandbox.linguagens()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/sandbox/ver/{arquivo:path}")
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


@router.get("/sandbox", include_in_schema=False)
@router.get("/sandbox/", include_in_schema=False)
def sandbox_raiz(request: Request):
    """Raiz do subdomínio sandbox.*: explica o que vive ali (antes: 404 cru
    do Cloudflare — bug real do dono)."""
    return _pag_fora(request, "raiz")


@router.api_route("/sandbox/app/{chave}/{path:path}",
               methods=["GET", "POST", "HEAD"])
async def sandbox_app(chave: str, path: str, request: Request):
    """APP VIVO do teste de site — HOSPEDADO TEMPORARIAMENTE (pedido do
    dono): o link público {chave} = porta+expiração assinadas (HMAC do
    AUTH_SECRET, ~30 min). Sem login: quem TEM o link acessa.

    Aceita TAMBÉM o formato COM SLUG da sessão (/sandbox/app/{sid}/{chave}/
    …): esta rota é declarada ANTES da variante com slug e a {path:path}
    engole tudo — se `chave` não tem formato de chave e o 1º segmento do
    path TEM, é o caso slug (revalida com ele)."""
    from core import sandbox as _sb
    porta = _sb.chave_app_ok(chave)
    if not porta and _RE_CHAVE_APP.match(chave or "") is None:
        partes = (path or "").split("/", 1)
        if partes and _RE_CHAVE_APP.match(partes[0]):
            chave, path = partes[0], (partes[1] if len(partes) > 1 else "")
            porta = _sb.chave_app_ok(chave)
    if not porta:
        return _pag_fora(request, "expirou")
    return await _proxy_app_api(porta, request, "/" + (path or ""), chave=chave)


@router.api_route("/sandbox/app/{p1}/{p2}/{path:path}",
               methods=["GET", "POST", "HEAD"])
async def sandbox_app_slug(p1: str, p2: str, path: str, request: Request):
    from core import sandbox as _sb
    chave = p2 if _RE_CHAVE_APP.match(p2) else p1
    porta = _sb.chave_app_ok(chave)
    if not porta:
        return _pag_fora(request, "expirou")
    return await _proxy_app_api(porta, request, "/" + (path or ""), chave=chave)


@router.api_route("/sandbox/app/{p1}/{p2}", methods=["GET", "POST", "HEAD"])
async def sandbox_app_slug_raiz(p1: str, p2: str, request: Request):
    return await sandbox_app_slug(p1, p2, "", request)


@router.post("/api/sandbox/limpar")
def sandbox_limpar(request: Request):
    """Limpa os ARQUIVOS do último teste (o container segue de pé — rápido).
    Chamado ao FECHAR o modal de teste (pedido do dono)."""
    _usuario(request)
    from core import sandbox
    return {"ok": sandbox.limpar()}


