"""Rotas de biblioteca — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split
from api.routers.chat import query  # noqa: F401 — chamada cross-router (era namespace global do monólito)

from fastapi import APIRouter

router = APIRouter()
@router.post("/hx/aquisicao")
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
    except (ValueError, HTTPException, JobNaoEncontrado) as e:
        detalhe = e.detail if isinstance(e, HTTPException) and isinstance(e.detail, str) else str(e)
        return TEMPLATES.TemplateResponse(request, "_job.html",
                                          {"request": request, "kind": "erro",
                                           "job": "erro", "rotulo": fonte,
                                           "linhas": [], "running": False,
                                           "erro": detalhe}, status_code=200)


@router.get("/hx/job/{kind}/{job}")
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
    except (HTTPException, JobNaoEncontrado):
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


def _md_ponto(p) -> dict:
    """Metadata de um ponto do Qdrant nos DOIS formatos que o projeto já
    gravou: payload PLANO (pontos migrados: arquivo/titulo no topo) e
    payload ANINHADO (ingestão langchain: page_content + metadata{...}).
    O /docs e o /doc liam só o plano — docs novos caíam no fallback
    'ponto {id}' e o modal abria vazio (bug real pego na bateria pré-merge).
    """
    pl = p.payload or {}
    md = pl.get("metadata") if isinstance(pl.get("metadata"), dict) else {}
    out = dict(md)
    for k in ("arquivo", "source", "titulo", "secao", "url", "i", "n"):
        if k in pl and k not in out:
            out[k] = pl[k]
    out.setdefault("page_content", pl.get("page_content", ""))
    return out


@router.get("/hx/colecao/{nome}/docs")
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
                md = _md_ponto(p)
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


@router.get("/hx/colecao/{nome}/doc")
def hx_colecao_doc(nome: str, request: Request, chave: str):
    """DETALHE de um documento da coleção (modal da Biblioteca): título,
    nº de pedaços e o CONTEÚDO dos primeiros chunks por inteiro (a lista
    corta em 160 chars — o modal mostra até 4 chunks completos)."""
    _usuario(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    achado = None
    filtro_casado = None
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=10,
                              check_compatibility=False)
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        # AMBOS os formatos de payload: plano (migrado) e aninhado (langchain)
        for campo in ("arquivo", "metadata.arquivo", "source", "metadata.source"):
            filtro = Filter(must=[FieldCondition(
                key=campo, match=MatchValue(value=chave))])
            pts, _ = client.scroll(collection_name=nome, limit=6,
                                   scroll_filter=filtro, with_payload=True,
                                   with_vectors=False)
            if pts:
                achado = pts
                filtro_casado = filtro
                break
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha: {str(e)[:140]}</p>")
    if not achado:
        return HTMLResponse("<p class='mut'>documento não encontrado.</p>")
    # DOCUMENTO COMPLETO: scroll por filtro (arquivo|source) traz TODOS os
    # chunks do documento — ordenados por i/n do metadata (pedido do dono
    # 28/08: o modal mostra o documento inteiro legível/editável, não só
    # os 4-6 primeiros). Usa o MESMO filtro da descoberta: o campo físico
    # (plano OU metadata.*) já foi casado acima — re-derivar do payload
    # mesclado é bug: _md_ponto ACHATA metadata.*, devolveria "arquivo"
    # mesmo quando o campo real é "metadata.arquivo" (modal "documento
    # vazio" pego na bateria pré-merge 29/08).
    # _scroll_todos devolve LOTES (gerador) — achata para pontos
    todos = [p for lote in _scroll_todos(
        client, nome, limite=4000, filtro=filtro_casado) for p in lote]
    todos.sort(key=lambda p: _md_ponto(p).get("i", 0))
    md0 = _md_ponto(achado[0])
    titulo = str(md0.get("titulo") or chave.replace("\\", "/")
                 .rsplit("/", 1)[-1] or "?")
    chunks = [str((p.payload or {}).get("page_content", "")) for p in todos]
    texto_completo = "\n\n".join(chunks)
    return TEMPLATES.TemplateResponse(
        request, "_colecao_doc.html",
        {"request": request, "nome": nome, "titulo": titulo, "chave": chave,
         "total": len(chunks), "chunks": chunks,
         "texto_completo": texto_completo})


@router.post("/hx/colecao/{nome}/editar")
def hx_colecao_editar(nome: str, request: Request,
                      chave: str = Form(...), texto: str = Form(...)):
    """EDITA o documento INTEIRO (pedido do dono): apaga TODOS os chunks
    daquele `arquivo`/`source` na coleção e REINGERE o texto editado pelo
    MESMO pipeline (limpeza → split semântico → re-embed → dedupe) com o
    mesmo `arquivo` de origem — o documento continua uma unidade na
    Biblioteca, agora com o conteúdo corrigido. Snapshot automático antes
    de apagar (reversível).
    """
    _exigir_admin(request)
    if not re.fullmatch(r"[A-Za-z0-9_\\-]{1,64}", nome):
        return HTMLResponse("<p class='erro-texto'>nome inválido</p>")
    texto = (texto or "").strip()
    if not texto:
        return HTMLResponse("<p class='erro-texto'>texto vazio — nada a "
                            "reingestar</p>")
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        # acha o campo real nos DOIS formatos (plano e metadata.* aninhado)
        filtro = None
        total = 0
        for campo in ("arquivo", "metadata.arquivo", "source", "metadata.source"):
            filtro = Filter(must=[FieldCondition(
                key=campo, match=MatchValue(value=chave))])
            total = client.count(nome, exact=True,
                                 count_filter=filtro).count
            if total:
                break
        if not total:
            return HTMLResponse("<p class='erro-texto'>documento não "
                                "encontrado</p>")
        # snapshot ANTES de apagar (reversível)
        try:
            from core import snapshot as _snap
            _snap.criar(client, nome, motivo=f"edição {chave[:60]}")
        except Exception:
            pass
        client.delete(collection_name=nome, points_selector=filtro)
        # reingere PELO PIPELINE (mesma limpeza/split/embed da ingestão)
        from langchain_core.documents import Document as _Doc
        titulo = Path(chave).name
        doc = _Doc(page_content=texto, metadata={
            "arquivo": chave, "source": chave, "titulo": titulo})
        from core.ingest import ingest_docs
        resumo = ingest_docs([doc], collection=nome, rapido=True,
                             log=lambda *a, **k: None)
        return HTMLResponse(
            f"<p class='mini'>✓ reingerido: {total} chunk(s) antigo(s) "
            f"apagado(s); novo documento com {resumo.get('pontos', '?')} "
            f"ponto(s). Snapshot de segurança gravado.</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha: {str(e)[:160]}</p>")


@router.post("/hx/colecao/{nome}/nomear")
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
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        # AMBOS os formatos de payload (plano e metadata.* aninhado) — o
        # /nomear tentava só arquivo|source top-level e devolvia
        # "documento não encontrado" para docs NOVOS (langchain grava
        # metadata.arquivo) — mesma classe de bug do /doc (bateria 29/08)
        filtro = None
        info = 0
        for campo in ("arquivo", "metadata.arquivo", "source", "metadata.source"):
            filtro = Filter(must=[FieldCondition(
                key=campo, match=MatchValue(value=chave))])
            info = client.count(nome, exact=True, count_filter=filtro).count
            if info:
                break
        if not info:
            return HTMLResponse("<p class='erro-texto'>documento não encontrado</p>")
        client.set_payload(collection_name=nome,
                           payload={"titulo": titulo},
                           filters=filtro)
        return HTMLResponse(f"<p class='mini'>✓ '{titulo}' gravado no Qdrant "
                            f"({info} pedaço(s))</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='erro-texto'>falha: {str(e)[:140]}</p>")


@router.get("/hx/histlog/{job}")
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


@router.post("/hx/resolucao")
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


@router.post("/hx/recategorizar")
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


@router.post("/hx/enriquecer/{nome}")
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


@router.post("/hx/colecao/{nome}/apagar")
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


@router.post("/hx/revisao/descartar")
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


@router.post("/hx/revisao/aplicar")
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


@router.get("/api/collections")
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


@router.delete("/api/collections/{nome}")
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


@router.post("/api/ingest/upload")
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


@router.post("/api/manutencao")
def manutencao(body: ManutencaoIn, request: Request):
    """Dispara análise/agrupamento/divisão como JOB (log ao vivo no popup)."""
    return _manutencao_disparar(body.acao, body.colecao, body.apagar_original)


@router.post("/api/ingest")
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


@router.post("/api/ingest/hf")
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


@router.get("/api/hf/datasets")
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


@router.post("/api/ingest/preview")
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


@router.get("/api/ingest/preview/{pid}")
def preview_ver(pid: str):
    """Relatório completo do dry-run (documentos, chunks, clusters, tema)."""
    from core import preview as _pv
    resp = _pv.ver(pid)
    if not resp:
        raise HTTPException(status_code=410,
                            detail="pré-visualização expirada (30 min) — rode de novo")
    return resp


@router.post("/api/ingest/web-salvar")
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


@router.post("/api/ingest/preview/aplicar")
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


@router.post("/api/pesquisa")
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


@router.get("/api/snapshot")
def snapshot_listar():
    """Snapshots disponíveis (logs/snapshots) — coleção, pontos, motivo."""
    from core import snapshot as _snap
    return _snap.listar()


# ⚠️ ORDEM IMPORTA: /api/snapshot/restaurar precisa vir ANTES de
# /api/snapshot/{colecao} — o path-param engole rotas literais posteriores
# (bug pego em produção: POST .../restaurar virava snapshot da coleção
# "restaurar" → 500)
@router.post("/api/snapshot/restaurar")
def snapshot_restaurar(body: SnapshotRestaurarIn, request: Request):
    """Recria a coleção a partir do snapshot (APAGA a atual antes)."""
    _exigir_admin(request)
    from core import snapshot as _snap
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    return _snap.restaurar(client, body.arquivo, body.colecao,
                           log=lambda m: print(m))


@router.post("/api/snapshot/{colecao}")
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


@router.post("/api/higienizar")
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


@router.post("/api/limpeza")
def rota_limpeza(body: HigienizarIn):
    """LIMPEZA (higienização) da coleção em 2º plano — DETERMINÍSTICA:
    texto normalizado (frases reconstituídas, ruído de página fora),
    duplicados exatos removidos, o que mudou é re-embedado no mesmo id.

    A etapa 2 (varredura LLM) foi APOSENTADA em 29/08 por decisão do dono:
    o 7B local julga mal (77% falso-positivo) e apagava conteúdo valioso
    — ver /api/varredura (410). Acompanhe em /api/limpeza/status/{job}."""
    job = _limpeza.novo_id()
    colecao = body.collection

    def fabricar(p: dict):
        jid = p["job"]

        def rodar():
            contadores.set_servico("limpeza")
            resultado = {}
            _limpeza.iniciar(jid)
            try:
                _limpeza.log(jid, "🧹 higienização de texto (reconstrução de frases, "
                                  "remoção de ruído e duplicados, re-embedding):")
                resultado["higienizacao"] = higienizar_colecao(
                    colecao, log=lambda m: _limpeza.log(jid, "   " + m))
                _limpeza.concluir(jid, result=resultado)
            except Exception as e:
                print(f"❌ Erro na limpeza: {e}")
                _limpeza.concluir(jid, error=str(e))
        return rodar

    _despachar(fabricar, "limpeza", {"job": job}, _limpeza)
    return {"job": job, "status": f"/api/limpeza/status/{job}"}


@router.post("/api/seed")
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


@router.post("/api/varredura")
def varredura(body: VarreduraIn):
    """APOSENTADA (29/08, decisão do dono) — responde 410 Gone.

    Prova definitiva de 28/08: o 7B local não executa o julgamento
    estruturado (N trechos → JSON seletivo) de forma confiável — 77% de
    falso-positivo com motivos auto-contraditórios, mesmo com spec
    corrigida E strip do cabeçalho contextual. A limpeza vive nas camadas
    DETERMINÍSTICAS: gate `score_chunk` na ingestão, higienização
    (POST /api/limpeza) e cura por score na Revisão. A rota segue
    registrada (remoção quebraria a prova de paridade do split)."""
    raise HTTPException(status_code=410, detail=(
        "varredura LLM aposentada (29/08): o modelo local de 7B julga mal "
        "e apagava conteúdo valioso — use POST /api/limpeza (higienização "
        "determinística) ou a cura por score na Revisão de ingestão"))


@router.get("/api/docs")
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


@router.put("/api/docs")
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


@router.delete("/api/docs")
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


