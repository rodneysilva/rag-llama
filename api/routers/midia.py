"""Rotas de midia — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.post("/api/midia/analisar")
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


@router.post("/api/midia/enviar")
def midia_enviar(body: MidiaEnviarIn, request: Request):
    """Envio ÚNICO do módulo Multimídia — job com raciocínio ao vivo que ao
    CONCLUIR grava o item na sessão (histórico) e limpa o job_ativo."""
    from core import midia as _m
    from core import midia_sessoes
    owner = _usuario(request)
    # 🔄 CICLO DO CHAT (28/08): 1º envio numa sessão VIRTUAL ("/midia" do
    # zero) CRIA a sessão agora — nada existia no disco antes disso.
    sid_uso = (body.sessao or "").strip()
    if not sid_uso:
        sid_uso = midia_sessoes.criar(owner)["id"]
    sessao = midia_sessoes.abrir(sid_uso, owner)
    if sessao is None:
        raise HTTPException(404, "sessão multimídia não encontrada")
    body.sessao = sid_uso
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(422, "escreva o que quer (pergunta ou prompt)")
    modelo = (body.modelo or "").strip()
    referencia = (body.referencia or "").strip()
    # tipo pelo modelo
    if ":" in modelo:
        from core import provedores as _prov
        pid, mnome = modelo.split(":", 1)
        info = next((mm for mm in (_prov.listar_por(pid) or [])
                     if mm["nome"] == mnome), None)
        cat = (info or {}).get("cat", "visao")
    else:
        cat = next((m.get("categoria") for m in modelos.listar()
                    if m["nome"] == modelo), "visao")
    if cat == "visao" and not referencia:
        raise HTTPException(422, "análise precisa de imagem — anexe (📎)")
    nome_ref = Path(referencia).name if referencia else ""
    alvo_ref = (_m.ENTRADA / nome_ref) if nome_ref else None
    if nome_ref and (not alvo_ref or not alvo_ref.exists()):
        raise HTTPException(422, f"anexo '{nome_ref}' não encontrado — suba de novo")
    tipo = ("analise" if cat == "visao"
            else "video" if cat == "video" else "imagem")
    # 🎞 GIF explícito: tipo GIF (renderiza <img>) e frames FIXOS de loop —
    # a duração do seletor NÃO se aplica (gif bom = ciclo de ~1,5 s; a spec
    # midia_duracao manda). Bug do dono 03/09: 🎞 marcado gerava item
    # tipo "video" com arquivo .gif → <video src=*.gif> não renderiza NADA
    # na tela e a duração pedida era silenciosamente ignorada.
    if body.gif and cat == "video":
        tipo = "gif"
    if tipo == "imagem" and referencia:
        tipo = "melhoria"

    job = _midia.novo_id()
    midia_sessoes.marcar_job(body.sessao, job, tipo, modelo,
                             prompt=prompt)

    def _fabricar(payload: dict):
        jid = payload["job"]

        def rodar():
            t0 = time.time()
            _midia.log(jid, f"{'👁 análise' if payload['tipo'] == 'analise' else '🎨 geração'}"
                           f" · {payload['modelo'] or 'local'}"
                           + (f" · {payload['tipo']}" if payload["tipo"] in ("melhoria", "video") else ""),
                       etapa="multimídia")
            resultado, erro = None, None
            try:
                if payload["tipo"] == "analise":
                    modelo = payload["modelo"]
                    if ":" in modelo:
                        from core import provedores as _prov
                        pid, _ = modelo.split(":", 1)
                        if not _prov.resolver(pid, modelo.split(":", 1)[1]):
                            raise RuntimeError(
                                f"provedor {pid.upper()} não configurado — "
                                f"cadastre em /sistema?prov={pid.lower()}")
                    # LOCAL (com OU sem nome — o value do seletor é
                    # "qwen2.5-vl-7b") em CONTAINER → AGENTE do host: a GPU
                    # e o GGUF vivem na ESTAÇÃO; direto aqui o _subir_vl
                    # procura D:\models no Linux e morre "ausente" (bug
                    # real do dono — igual ao que já corrigi no i2t do chat)
                    if config.EM_CONTAINER and ":" not in modelo:
                        import base64 as _b64
                        with open(payload["referencia"], "rb") as f:
                            img_b64 = _b64.b64encode(f.read()).decode()
                        t_vl = time.time()
                        r = modelos._chamar_agente(
                            "/visao", {"b64": img_b64,
                                       "nome": payload["nome_ref"],
                                       "pergunta": payload["prompt"]},
                            timeout=420)
                        texto = r.get("descricao", "")
                        # REGRAVA o evento MULTIMODAL na VPS (a telemetria da
                        # estação não atravessa o túnel — sem isto o Dashboard
                        # de produção nunca via o qwen-vl: "bug antigo")
                        try:
                            u = r.get("usage") or {}
                            telemetria.evento(
                                "llm", "🖼️ qwen2.5-vl (multimodal)",
                                entrada=int(u.get("entrada") or 0),
                                saida=int(u.get("saida") or 0),
                                duracao_s=round(time.time() - t_vl, 1),
                                modelo="qwen2.5-vl-7b",
                                servico="multimodal")
                        except Exception:
                            pass
                    else:
                        texto = _m.legendar_imagem(
                            payload["referencia"], payload["prompt"] or None,
                            modelo=modelo,
                            log=lambda msg, g="": _midia.log(
                                jid, msg, **({"etapa": g} if g else {})))
                    resultado = {"tipo": "analise", "texto": texto or "(vazio)",
                                 "modelo": modelo or "local",
                                 "referencia": payload["nome_ref"]}
                elif payload["tipo"] in ("imagem", "melhoria"):
                    if ":" in payload["modelo"]:
                        from core import provedores as _prov
                        pid, mnome = payload["modelo"].split(":", 1)
                        resultado = _prov.gerar_imagem(
                            pid, mnome, payload["prompt"],
                            log=lambda msg, e=None: _midia.log(
                                jid, msg, **({"etapa": e} if e else {})))
                        resultado = {**resultado, "tipo": "imagem",
                                     "modelo": payload["modelo"]}
                    elif config.EM_CONTAINER:
                        # GPU/GGUFs na ESTAÇÃO: tarefa via AGENTE (o caminho
                        # direto procurava D:\\models no Linux da VPS)
                        resultado = _midia_local_agente(
                            payload, jid, "t2i",
                            {"modelo": payload["modelo"],
                             "init": payload["tipo"] == "melhoria"})
                    else:
                        # LOCAL: conjuntos pausam/restauram as LLMs
                        from core import conjuntos as _conj
                        try:
                            _conj.garantir("difusao",
                                           log=lambda m, g="": _midia.log(
                                               jid, m, **({"etapa": g} if g else {})))
                        except Exception as e:
                            _midia.log(jid, f"⚠️ conjunto: {str(e)[:120]} — seguindo",
                                       etapa="modelo")
                        estado = _m.pausar_servicos(
                            log=lambda msg, g="": _midia.log(
                                jid, msg, **({"etapa": g} if g else {})),
                            pesado=False)
                        try:
                            r = _m.gerar_imagem(
                                payload["prompt"], payload["modelo"],
                                imagem_inicial=(payload.get("referencia")
                                                if payload["tipo"] == "melhoria" else None),
                                log=lambda msg, g="": _midia.log(
                                    jid, msg, **({"etapa": g} if g else {})))
                            resultado = {**r, "tipo": "imagem",
                                         "modelo": payload["modelo"]}
                        finally:
                            _m.restaurar_servicos(
                                estado, log=lambda msg, g="": _midia.log(
                                    jid, msg, **({"etapa": g} if g else {})))
                elif config.EM_CONTAINER:
                    # GPU/GGUFs na ESTAÇÃO: tarefa via AGENTE (bug do dono:
                    # "modelos ausentes em D:\\models/video" com tudo lá)
                    resultado = _midia_local_agente(
                        payload, jid, "t2v",
                        {"modelo": payload["modelo"],
                         "frames": (17 if payload["gif"]
                                    else int(payload.get("duracao") or 3) * 16 + 1),
                         "gif": bool(payload["gif"])})
                else:  # vídeo/gif (host direto)
                    from core import conjuntos as _conj
                    try:
                        _conj.garantir("difusao",
                                       log=lambda m, g="": _midia.log(
                                           jid, m, **({"etapa": g} if g else {})))
                    except Exception as e:
                        _midia.log(jid, f"⚠️ conjunto: {str(e)[:120]} — seguindo",
                                   etapa="modelo")
                    estado = _m.pausar_servicos(
                        log=lambda msg, g="": _midia.log(
                            jid, msg, **({"etapa": g} if g else {})), pesado=True)
                    try:
                        params = ({"gif": True, "frames": 17}
                                  if payload["gif"]
                                  else {"frames": payload["duracao"] * 16 + 1})
                        r = _m.gerar_video(
                            payload["prompt"], frames=params.get("frames", 49),
                            gif=params.get("gif", False),
                            log=lambda msg, g="": _midia.log(
                                jid, msg, **({"etapa": g} if g else {})))
                        resultado = {**r, "tipo": "gif" if payload["gif"] else "video",
                                     "modelo": payload["modelo"]}
                    finally:
                        _m.restaurar_servicos(
                            estado, log=lambda msg, g="": _midia.log(
                                jid, msg, **({"etapa": g} if g else {})))
            except Exception as e:
                erro = str(e)[:400]
            # ⏱ tempo de retorno em TODOS os resultados (pedido do dono —
            # chat já mostra; multimídia agora também)
            if resultado is not None:
                resultado = {**resultado, "segundos": round(time.time() - t0)}
            # HISTÓRICO: item gravado na sessão em AMBOS os caminhos
            linhas = []
            try:
                with _midia.lock:
                    linhas = [dict(l) for l in _midia.jobs.get(jid, {}).get("lines", [])]
            except Exception:
                pass
            midia_sessoes.anexar_item(
                payload["sessao"],
                {"id": jid, "ts": time.strftime("%H:%M:%S"),
                 "tipo": payload["tipo"], "modelo": payload["modelo"] or "local",
                 "prompt": payload["prompt"],
                 "referencia": payload["nome_ref"],
                 "linhas": linhas[-60:],
                 "resultado": resultado or {"erro": erro}},
                titulo=payload["prompt"])
            if erro:
                _midia.concluir(jid, error=erro)
            else:
                _midia.concluir(jid, result=resultado)
        return rodar

    _despachar(_fabricar, "midia",
               {"sessao": body.sessao, "prompt": prompt, "modelo": modelo,
                "referencia": str(alvo_ref) if alvo_ref else "",
                "nome_ref": nome_ref, "tipo": tipo,
                "duracao": body.duracao, "gif": body.gif, "job": job},
               _midia)
    return {"job": job, "tipo": tipo, "sessao": body.sessao}


@router.post("/api/midia/cancel/{job}")
def midia_cancel(job: str, request: Request):
    """Interrompe o envio multimídia em curso (novo envio cancela o anterior)."""
    _usuario(request)
    return {"cancelado": _midia.cancelar(job, "interrompido — novo envio")}


@router.get("/hx/midia/item/{job}")
def hx_midia_item(job: str, request: Request, s: str = ""):
    """Partial do item CONCLUÍDO (o JS troca o card em andamento por ele —
    sem reload: o fluxo pergunta → resposta fica visível na hora)."""
    from core import midia_sessoes
    owner = _usuario(request)
    sessao = midia_sessoes.abrir(s, owner) if s else None
    if sessao is None:
        return HTMLResponse("<div class='rodape'>⚠️ sessão não encontrada</div>")
    it = next((x for x in sessao.get("itens", []) if x.get("id") == job), None)
    if it is None:
        return HTMLResponse("<div class='rodape'>⚠️ item não encontrado</div>")
    return TEMPLATES.TemplateResponse(request, "_midia_item.html",
                                      {"request": request, "it": it})


@router.post("/hx/midia/nova")
def midia_nova(request: Request):
    """Nova sessão multimídia — HX-Redirect para a URI COM SLUG."""
    from core import midia_sessoes
    owner = _usuario(request)
    d = midia_sessoes.criar(owner)
    r = HTMLResponse("", status_code=200)
    r.headers["HX-Redirect"] = f"/midia/{d['id']}"
    return r


@router.delete("/api/midia/sessao/{sid}")
def midia_sessao_apagar(sid: str, request: Request):
    """Apaga UMA sessão multimídia (owner conferido — padrão do chat)."""
    from core import midia_sessoes
    owner = _usuario(request)
    if not midia_sessoes.apagar(sid, owner):
        raise HTTPException(404, "sessão não encontrada (ou não é sua)")
    return {"ok": True}


@router.get("/api/modalidades")
def get_modalidades():
    """As modalidades do estúdio com disponibilidade real e ETA calibrado."""
    return modalidades.listar()


@router.get("/api/fluxos")
def get_fluxos():
    """Registry de fluxos de geração (F1b-4): builtins do estúdio (sd-cli,
    wan2.2) e EXTERNOS (wan2gp, ComfyUI) com health-check na URL do .env —
    cada card da aba Estúdio mostra 1 linha do que faz + status."""
    from core import fluxos
    return fluxos.listar()


@router.get("/api/estudio")
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


@router.get("/api/estudio/sessoes")
def estudio_sessoes(request: Request):
    """Sessões do estúdio DO USUÁRIO com as mídias geradas (persistidas em
    saidas/estudio_sessoes.json com owner)."""
    return {"sessoes": sessoes.listar(owner=_usuario(request))}


@router.post("/api/estudio/sessoes")
def estudio_criar_sessao(body: SessaoEstudioIn, request: Request):
    """Cria uma sessão nova (do usuário logado) para agrupar as gerações."""
    if not body.nome.strip():
        raise HTTPException(status_code=400, detail="informe um nome para a sessão")
    return sessoes.criar(body.nome, owner=_usuario(request))


@router.patch("/api/estudio/sessoes/{sid}")
def estudio_renomear_sessao(sid: str, body: SessaoEstudioIn, request: Request):
    s = sessoes.renomear(sid, body.nome)
    if not s or (s.get("owner") and s.get("owner") != _usuario(request)):
        raise HTTPException(status_code=404, detail=f"sessão '{sid}' não existe")
    return s


@router.delete("/api/estudio/sessoes/{sid}")
def estudio_apagar_sessao(sid: str, request: Request):
    """Apaga a sessão do REGISTRO (as mídias continuam em saidas/)."""
    _sessao_estudio_do_dono(sid, request)
    if not sessoes.apagar(sid):
        raise HTTPException(status_code=404, detail=f"sessão '{sid}' não existe")
    return {"removida": sid}


@router.post("/api/zip")
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


@router.post("/api/midia/zip")
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


@router.post("/api/midia/prompts")
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


@router.post("/api/estudio/assistente")
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


@router.post("/api/upload")
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


@router.post("/api/tarefas")
def criar_tarefa(body: TarefaIn, request: Request):
    """Dispara a tarefa da modalidade em segundo plano; acompanhe o andamento
    (log ao vivo, progresso, ETA) em /api/tarefas/status/{id}?cursor=N."""
    _checar_gpu_modo(mod=body.modalidade)  # política antes do guard de host:
    # o 403 "somente LLMs" é mais útil que o 400 de container
    # 🎨 t2i com GERADOR EXTERNO (zai:glm-image, openai:gpt-image-1…): roda
    # NA PRÓPRIA API (POST /images/generations do provedor) — sem GPU/agente
    # (pedido do dono: "geração pode usar modelos de provedores também")
    if (body.modalidade == "t2i"
            and ":" in str(body.params.get("modelo") or "")):
        _modelo = str(body.params.get("modelo"))
        pid, nome = _modelo.split(":", 1)
        from core import provedores as _prov
        if not _prov.resolver(pid.strip(), nome.strip()):
            raise HTTPException(status_code=400,
                                detail=f"provedor '{pid}' não configurado no "
                                       ".env (Sistema → ☁️ cadastre a chave)")
        if not body.sessao:
            body.sessao = sessoes.principal(_usuario(request))
        try:
            tid = tarefas.criar("t2i", trava_vram=False, sessao=body.sessao)
        except RuntimeError as e:
            raise HTTPException(status_code=423, detail=str(e))

        def _t2i_ext(tid=tid, body=body, modelo=_modelo):
            from core import provedores as _prov
            try:
                r = _prov.gerar_imagem(
                    modelo.split(":", 1)[0], modelo.split(":", 1)[1],
                    body.texto,
                    log=lambda m, e=None: tarefas.log(tid, m, e))
                tarefas.concluir(tid, resultado=r)
            except Exception as e:
                tarefas.concluir(tid, erro=str(e)[:400])

        threading.Thread(target=_t2i_ext, daemon=True,
                         name=f"t2i-ext-{tid}").start()
        return {"tarefa": tid, "modalidade": "t2i",
                "rotulo": f"gerar imagem (externo {_modelo})",
                "estimativa_s": 30, "etapas": ["gerar"],
                "status": f"/api/tarefas/status/{tid}"}
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


@router.get("/api/tarefas/status/{tid}")
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


@router.post("/api/midia/contexto")
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


@router.get("/api/midia/{pasta}/{nome}")
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


