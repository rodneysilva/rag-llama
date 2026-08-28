"""Rotas de agentico — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.get("/api/sessions")
def list_sessoes(request: Request):
    """Resumo das sessões SALVAS DO USUÁRIO, da mais recente para a mais antiga."""
    return sessions.list_sessions(owner=_usuario(request))


@router.post("/api/sessions")
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


@router.get("/api/sessions/{sid}")
def get_sessao(sid: str, request: Request):
    """Sessão completa, com todas as mensagens (só do dono)."""
    if not _sid_valido(sid):
        raise HTTPException(status_code=400, detail="id de sessão inválido")
    dados = sessions.get_session(sid)
    dono = _usuario(request)
    if not dados or dados.get("owner") != dono:
        raise HTTPException(status_code=404, detail=f"Sessão '{sid}' não encontrada")
    return dados


@router.delete("/api/sessions/{sid}")
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


@router.get("/api/mcp")
def lista_mcp():
    """Servidores MCP registrados (mcp_servers.json)."""
    return mcp_registry.list_servers()


@router.post("/api/mcp")
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


@router.get("/api/mcp/conhecidos")
def mcp_conhecidos():
    """Catálogo de MCPs conhecidos — instaláveis com um clique na webui."""
    return mcp_registry.list_conhecidos()


@router.post("/api/mcp/testar")
def mcp_testar(body: McpTestarIn, request: Request):
    """Testa a conexão SEM registrar: conecta, lista as ferramentas e devolve
    o que encontrou (o botão 'testar' da webui). EXCLUSIVO do administrador —
    testar stdio executa o comando no host."""
    _exigir_admin(request)
    try:
        return mcp_registry.testar(body.entrada)
    except Exception as e:
        return {"ok": False, "erro": str(e)[:300]}


@router.post("/api/mcp/instalar-job")
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


@router.post("/api/mcp/instalar")
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


@router.delete("/api/mcp/{nome}")
def remove_mcp(nome: str, request: Request):
    """Remove um servidor MCP do registro (exclusivo do administrador)."""
    _exigir_admin(request)
    if not mcp_registry.remove_server(nome):
        raise HTTPException(status_code=404, detail=f"MCP '{nome}' não registrado")
    print(f"🔌 MCP '{nome}' removido")
    return {"removido": nome}


