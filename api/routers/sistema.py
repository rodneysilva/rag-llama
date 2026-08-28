"""Rotas de sistema — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.post("/hx/settings")
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


@router.post("/hx/parar-tudo")
def hx_parar_tudo(request: Request):
    _exigir_admin(request)
    return parar_tudo(request)


@router.get("/api/models")
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


@router.post("/api/models/ativar")
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


@router.get("/api/modelo/ativo")
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


@router.post("/api/modelo/ativo")
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


@router.get("/api/status")
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

    with ThreadPoolExecutor(max_workers=4) as ex:
        fut = {
            "qdrant": ex.submit(_check, "Qdrant", f"{config.QDRANT_URL}/healthz"),
            "llm": ex.submit(_check, "LLM", f"{config.LLM_BASE_URL}/models"),
            "embed": ex.submit(_check, "Embedding", f"{config.EMBED_BASE_URL}/models"),
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


@router.get("/api/settings")
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


@router.put("/api/settings")
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


@router.post("/api/parar_tudo")
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
    # 2) motores: desmonta todos liberando a VRAM — NO HOST (em container,
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
    # 2b) reranker cross-encoder (CPU, residente no PRÓPRIO processo da API):
    # solta o modelo da memória junto com os motores (política Parar tudo)
    rerank.descarregar()
    telemetria.evento("jobs", "⏹ PARAR TUDO executado",
                      jobs=len(cancelados),
                      motores=motores.get("derrubados"))
    return {"ok": True, "jobs_cancelados": cancelados, "motores": motores}


@router.get("/api/llm/estado")
def llm_estado(request: Request):
    """O que está servindo na :8090 (alias, VRAM) — alimenta o toggle."""
    _usuario(request)
    return {"no_ar": bool(modelos.servido(modelos.CHAT_PORTA)),
            "modelo": modelos.servido(modelos.CHAT_PORTA),
            "vram_mi": modelos._vram_uso_mi()}


@router.post("/api/llm/ligar")
def llm_ligar(request: Request):
    """Sobe o llama-server do chat com o modelo do .env (LLM_MODEL) e libera
    o ciclo automático. EXCLUSIVO do administrador — afeta todos os usuários."""
    _exigir_admin(request)
    return modelos.ligar_llm_manual()


@router.post("/api/llm/desligar")
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


@router.post("/api/embed/ligar")
def embed_ligar(request: Request):
    """Religa o embedding (:8081) e libera o ciclo on-demand.
    EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/embed/ligar", timeout=240)
    return modelos.ligar_embedding_manual()


@router.post("/api/embed/desligar")
def embed_desligar(request: Request):
    """Desliga o embedding (:8081) e TRAVA a religação automática — buscas/
    ingestão falham com erro claro até religar. EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/embed/desligar", timeout=60)
    return modelos.desligar_embedding_manual()


@router.post("/api/vl/ligar")
def vl_ligar(request: Request):
    """Religa a visão (:8082, remove o marker) e a pré-aquece (sobe o
    Qwen2.5-VL agora, ~1 min). EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        return modelos._chamar_agente("/vl/ligar", timeout=420)
    return modelos.ligar_vl_manual()


@router.post("/api/vl/desligar")
def vl_desligar(request: Request):
    """Desliga a visão (:8082) e BLOQUEIA o ciclo on-demand — análises de
    imagem falham com erro claro até religar. EXCLUSIVO do administrador."""
    _exigir_admin(request)
    if config.EM_CONTAINER:
        modelos._chamar_agente("/porta/derrubar",
                               {"porta": modelos.VL_PORTA}, timeout=60)
        return modelos.desligar_vl_manual(ja_derrubado=True)
    return modelos.desligar_vl_manual()


@router.post("/api/gpu/modo")
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


@router.post("/api/specs/reload")
def specs_reload():
    """Derruba o cache das specs — editar core/specs/*.md passa a valer SEM
    restart da API (o lru_cache deixava a versão antiga colada)."""
    from core import specs
    n = specs.recarregar()
    print(f"🔁 Specs recarregadas ({n} em cache foram descartadas)")
    return {"recarregadas": n}


