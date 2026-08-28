"""Rotas de chat — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.get("/hx/contagem")
def hx_contagem(request: Request):
    """CONTADORES EM TEMPO REAL (partial do topbar): LLM CARREGADA (sutil,
    pedido do dono) + tokens enviados/gerados."""
    _usuario(request)
    tot = contadores.totais() or {}
    t = tot.get("total") or {}
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
                                       "chamadas": t.get("chamadas", 0)})


@router.get("/hx/jobsbar")
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


@router.get("/hx/conversa/copy")
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


@router.post("/hx/chat")
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
                    # LOCAL (sem ":") em CONTAINER -> AGENTE do host (a GPU
                    # e o GGUF vivem na estacao; direto aqui procura
                    # D:\models no Linux e morre). EXTERNO prov:nome NA API.
                    if config.EM_CONTAINER and ":" not in modelo:
                        import base64 as _b64
                        with open(alvo, "rb") as f:
                            img_b64 = _b64.b64encode(f.read()).decode()
                        t_vl = time.time()
                        r = modelos._chamar_agente(
                            "/visao", {"b64": img_b64,
                                       "nome": Path(alvo).name,
                                       "pergunta": payload["pergunta"]},
                            timeout=420)
                        analise = r.get("descricao", "")
                        # REGRAVA o multimodal na telemetria DA VPS (o
                        # evento da estacao nao atravessa o tunel — sem
                        # isto o Dashboard nunca via o qwen-vl local)
                        try:
                            u = r.get("usage") or {}
                            telemetria.evento(
                                "llm", "qwen2.5-vl (multimodal)",
                                entrada=int(u.get("entrada") or 0),
                                saida=int(u.get("saida") or 0),
                                duracao_s=round(time.time() - t_vl, 1),
                                modelo="qwen2.5-vl-7b",
                                servico="multimodal")
                        except Exception:
                            pass
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
             "running": True, "pergunta": question, "sid": sid,
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
           "running": True, "pergunta": question, "sid": sid,
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


@router.get("/hx/chat/{job}")
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


@router.post("/hx/nova")
def hx_nova():
    resp = JSONResponse({})
    resp.delete_cookie(SESSAO_COOKIE)
    resp.headers["HX-Redirect"] = "/"
    return resp


@router.post("/hx/prompt-melhorar")
@router.post("/hx/prompt-midia")   # compat: páginas abertas no deploy ainda chamam
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
    # ⚡ FALLBACK EXTERNO (pedido do dono 28/08): a ✨ usava SÓ a LLM local
    # — com ela desligada o botão ficava "…" eterno. Local fora do ar → o
    # PRIMEIRO modelo de conversa dos provedores cadastrados assume.
    if not modelos.servido(modelos.CHAT_PORTA):
        from core import provedores as _pv
        _ext = None
        try:
            for _pid in _pv.ids():
                for _m in (_pv.modelos(_pid) or []):
                    if _m.get("cat") in ("conversa", "raciocinio",
                                         "programacao"):
                        _ext = _pv.resolver(_pid, _m["nome"])
                        break
                if _ext:
                    break
        except Exception:
            _ext = None
        if _ext:
            rag.set_override(_ext)  # thread-local: só nesta chamada
            try:
                return PlainTextResponse(
                    _prompt.melhorar(ideia, tipo, contexto))
            finally:
                rag.set_override(None)
        raise HTTPException(
            status_code=503,
            detail="a LLM local está fora do ar e nenhum provedor de "
                   "conversa está cadastrado — religue 🧠 no topo ou "
                   "cadastre a chave em Sistema → ☁️ Provedores")
    return PlainTextResponse(_prompt.melhorar(ideia, tipo, contexto))


@router.get("/hx/conversa/copy")
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


@router.get("/hx/conversas")
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


@router.post("/hx/conversa/nova")
def hx_conversa_nova(request: Request):
    """Nova conversa SEM reload: troca só o palco (cookie sai, palco vazio)."""
    usuario = _usuario(request)
    resp = _palco_response(request, None, usuario)
    resp.delete_cookie(SESSAO_COOKIE)
    return resp


@router.post("/hx/conversa/{sid}/abrir")
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


@router.delete("/hx/conversa/{sid}")
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


@router.post("/hx/voz")
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


@router.get("/hx/tts")
def hx_tts(texto: str, request: Request):
    _usuario(request)
    wav = voz.falar_bytes(texto[:2000])
    return Response(wav, media_type="audio/wav")


@router.post("/api/visao")
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
            _desc = r.get("descricao", "")
            # 🛡️ guard: servidor de visão SEM mmproj devolve o ERRO como se
            # fosse descrição (bug real) — vira 503 com orientação, nunca
            # entra no contexto como "descrição" da imagem
            if "does not support image input" in (_desc or ""):
                raise RuntimeError(
                    "o servidor de visão no ar não aceita imagens (sem "
                    "mmproj) — reinicie a visão no Sistema (🖼️ subir visão)")
            return {"descricao": _desc}
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except OSError as e:
            raise HTTPException(status_code=503, detail=str(e))
    try:
        return {"descricao": midia.legendar_imagem(body.arquivo, body.pergunta)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/api/anexo/texto")
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


@router.post("/api/query")
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
                # 📅 GUARD DE DATA (pedido do dono 28/08: "quando a resposta
                # da LLM for diferente [do dia real], consultas mais
                # aprofundadas"): perguntas sobre hoje/agora cuja RESPOSTA
                # cita ano que não é o atual = data do treinamento VAZANDO.
                # A correção do RELÓGIO é anexada (aviso visível, resposta
                # original preservada); DuckDuckGo/Serper entram no modo
                # web/Auto para eventos, que é onde busca ajuda de verdade.
                try:
                    import re as _re2
                    from datetime import datetime as _dt2
                    if isinstance(res, dict) and res.get("answer"):
                        _perg = str(corpo.question or "").lower()
                        _atuais = any(w in _perg for w in (
                            "hoje", "agora", "atual", "ontem", "amanh"))
                        _anos = {int(x) for x in _re2.findall(
                            r"\b(?:19|20)\d{2}\b", str(res["answer"]))}
                        if (_atuais and _anos
                                and _dt2.now().year not in _anos):
                            _rel = _resposta_relogio()
                            res["answer"] = (
                                str(res["answer"]).rstrip()
                                + "\n\n---\n⚠️ **Correção do relógio do "
                                "servidor**: " + _rel["answer"]
                                + "\n_(a data citada acima veio do corte de "
                                "treinamento do modelo)_")
                            _query_log(jid, "📅 guard de data: a resposta "
                                       "citava ano fora do atual — correção "
                                       "do relógio anexada", "resposta")
                except Exception:
                    pass
                # ⚡ tok/s: velocidade REAL de GERAÇÃO (do 1º token em
                # diante — sem o pré-processamento do prompt; o cálculo
                # antigo dividia pelo total e "caía" com prompt grande)
                try:
                    _dur = max(time.time() - _t0, 0.001)
                    _tk = (res.get("tokens") or {}).get("saida") or 0 if isinstance(res, dict) else 0
                    if isinstance(res, dict) and _tk:
                        # ⚡ tok/s HONESTO: geração pura (streaming) ou a
                        # ÚLTIMA CHAMADA LLM — nunca o job inteiro (busca
                        # web + fila inflavam o denominador: 3.744 tok /
                        # 80 s = "46 tok/s" quando a chamada gerou a ~370)
                        _uc = contadores.ultima_chamada()
                        res["tok_s"] = (contadores.vel_geracao()
                                        or (round(_uc["saida"] / _uc["duracao_s"], 1)
                                            if _uc.get("saida") and _uc.get("duracao_s")
                                            else round(_tk / _dur, 1)))
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


@router.post("/api/query/cancel/{job}")
def query_cancel(job: str, request: Request):
    """Interrompe o job do chat em curso (pedido do dono: "toda vez que eu
    mandar mensagem estiver pensando, pare o raciocínio e mande a nova
    INCLUINDO o contexto da interrompida"). A thread termina sozinha e o
    resultado é descartado; a pergunta interrompida já está na sessão —
    entra no histórico da nova mensagem."""
    _usuario(request)
    ok = _query.cancelar(job, "interrompido — nova mensagem enviada")
    return {"cancelado": ok}


