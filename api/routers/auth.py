"""Rotas de auth — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.post("/api/auth/login")
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


@router.post("/api/auth/register")
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


@router.post("/api/auth/logout")
def logout(request: Request):
    """Derruba o cookie da sessão (o token Bearer é stateless — expira sozinho)."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_TOKEN)
    return resp


@router.get("/api/auth/me")
def auth_me(request: Request):
    """Quem está logado (valida o token do frontend) + se é admin."""
    user = _usuario(request)
    return {"user": user, "admin": user == config.AUTH_ADMIN_USER}


