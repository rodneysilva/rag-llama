"""Modo Revisão de ingestão (Fase A) — dry-run: NADA entra no Qdrant sem
aprovação.

Executa o pipeline INTEIRO da ingestão (leitura → limpeza → chunks com
cabeçalho contextual) e PARA antes do embedding em massa/Qdrant/catálogo.
O operador vê, para cada documento, "como veio" (bruto) e "como vai
entrar" (limpo + chunks), além de:

- duplicados exatos (md5) e QUASE-duplicados (cosseno ≥ QUASE_DUP);
- categorias: clusters por similaridade (cosseno ≥ CLUSTER_MIN) com rótulo
  LLM (spec rotulo_cluster.md) — a "divisão de categorias" consolidada;
- aderência ao tema: quando uma `colecao_alvo` é informada, cada documento
  recebe a nota do RERANKER contra a definição da coleção no catálogo —
  abaixo de TEMA_MIN fica marcado "revisar" (é o rerank atuando como gate
  de aquisição, não de consulta).

`aplicar(pid, ids, colecao)` ingerir só os aprovados (com proveniência:
adquirido_em + curadoria). Previews vivem 30 min na memória da API.
"""
import hashlib
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import QdrantClient
from langchain_core.documents import Document

from . import catalog, config, hf, rag, rerank
from .ingest import _dividir, _eh_codigo, _preparar_docs, _require, load_docs
from .limpeza import e_lixo_documento

LIMITE_BRUTO = 6000      # trecho do bruto/limpo exibido na webui
LIMITE_CHUNK = 1200      # trecho de cada chunk na prévia
QUASE_DUP = 0.92         # cosseno ≥ isto = quase-duplicado
CLUSTER_MIN = 0.75       # cosseno ≥ isto = mesmo tema (cluster)
TEMA_MIN = 0.30          # cosseno abaixo disto = "fora do tema"
MAX_DOCS = 3000          # teto SANITÁRIO (memória) — NÃO é limite de uso:
                         # pedidos do dono: sem limites para conhecimento;
                         # 3000 documentos por preview cobre datasets inteiros
TTL_S = 2 * 60 * 60      # preview vive 2 h (revisar 100+ chunks demora)

_previews: dict = {}     # pid -> {"docs": [...], "resp": {...}, "t": float}
_lock = threading.Lock()
# ═══ PREVIEW PERSISTENTE ═════════════════════════════════════════════
# Antes o preview vivia SÓ na memória do processo: cada restart/rebuild
# do container (deploy!) apagava tudo → "pré-visualização expirada" no
# meio da revisão. Agora cada preview é um JSON em logs/previews/
# (volume persistente) — recarregado sob demanda, podado pelo TTL.
from pathlib import Path as _P
PREVIEW_DIR = _P(__file__).resolve().parent.parent / "logs" / "previews"


def _caminho(pid: str) -> _P:
    return PREVIEW_DIR / f"{pid}.json"


def _carregar(pid: str) -> dict | None:
    """Preview do DISCO (cacheando em memória) — None se expirado/sumido."""
    with _lock:
        p = _previews.get(pid)
        if p:
            return p if time.time() - p["t"] <= TTL_S else None
    arq = _caminho(pid)
    if not arq.is_file():
        return None
    try:
        import json as _json
        p = _json.loads(arq.read_text(encoding="utf-8"))
        if time.time() - p.get("t", 0) > TTL_S:
            arq.unlink(missing_ok=True)
            return None
        if p.get("docs") and isinstance(p["docs"][0], dict):
            p["docs"] = [Document(page_content=d["page_content"],
                                   metadata=d.get("metadata", {}))
                          for d in p["docs"]]
        with _lock:
            _previews[pid] = p
        return p
    except Exception:
        return None


# ---------- fontes → Documents (mesmas leituras da ingestão) ----------

def docs_pasta(pasta: str, log=print) -> list:
    """Lê a pasta no servidor (.txt/.md/.pdf/código) — igual à ingestão."""
    pasta = str(Path(pasta))
    if not Path(pasta).is_dir():
        raise ValueError(f"Pasta não encontrada: {pasta}")
    log(f"📂 Lendo arquivos de '{pasta}'…")
    docs = load_docs(pasta)
    log(f"📄 {len(docs)} documento(s) lido(s)")
    return docs


def docs_hf(query: str, limite: int, log=print) -> list:
    """Cards (README) + LINHAS dos datasets do HuggingFace — sem gravar
    nada. O card sozinho não é 'o dataset': os DADOS de verdade vêm do
    datasets-server (mesma esteira do ingest_hf)."""
    achados = hf.buscar(query, limite, log=log)
    docs = []
    for a in achados:
        c = hf.card(a["id"], log=log)
        if c:
            docs.append(c["doc"])
        docs.extend(hf.dados(a["id"], log=log))
    log(f"🤗 {len(docs)} documento(s) (cards + dados)")
    return docs


# ---------- o dry-run ----------

def _coss(a, b) -> float:
    na, nb = (sum(x * x for x in a) ** 0.5), (sum(x * x for x in b) ** 0.5)
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def _md5(texto: str) -> str:
    return hashlib.md5(re.sub(r"\s+", " ", texto).strip()
                       .encode("utf-8")).hexdigest()


def analisar(docs: list, colecao_alvo: str | None = None,
             log=print) -> tuple[list, dict]:
    """Roda o pipeline SEM gravar. Devolve (docs_preparados, resp); o
    chamador guarda os preparados (para o `aplicar`) e mostra o resp."""
    log = log or print
    if len(docs) > MAX_DOCS:
        # SEM ERRO (pedido do dono): avalia os primeiros MAX_DOCS e AVISA
        # no log — o conhecimento nunca é recusado
        log(f"⚠️ {len(docs)} documento(s): o preview avalia os primeiros "
            f"{MAX_DOCS} nesta rodada — aplique e repita para o restante")
        docs = docs[:MAX_DOCS]
    _require(f"{config.QDRANT_URL}/healthz", "Qdrant")
    _require(f"{config.EMBED_BASE_URL}/models", "Servidor de embedding (BGE-M3)")

    # documento SEM TEXTO REVISÁVEL (só banners/imagens markdown) sai ANTES
    # de virar sugestão — "revisar o que não dá pra revisar" não é revisão
    _IMG = re.compile(r"!\[[^\]]*\]\([^)]+\)")

    def _motivo_descarte(d) -> str | None:
        fonte = str(d.metadata.get("source", "?"))
        if _eh_codigo(fonte):
            if len(d.page_content.strip()) < 50:
                return "código com menos de 50 caracteres"
            return None
        texto_sem_img = _IMG.sub("", d.page_content)
        if len(texto_sem_img.strip()) < 120 and _IMG.search(d.page_content):
            return "sem texto revisável (só imagens/banners)"
        if e_lixo_documento(d.page_content):
            return "documento virou ruído na limpeza (curto/sem semântica)"
        return None

    revisaveis = []
    descartados = []
    for d in docs:
        motivo = _motivo_descarte(d)
        if motivo:
            descartados.append({"fonte": str(d.metadata.get("source", "?")),
                                "motivo": motivo})
        else:
            revisaveis.append(d)
    docs = revisaveis
    brutos = {id(d): d.page_content for d in docs}

    # 1) limpeza + metadata (idêntico à ingestão de verdade)
    log("🧹 Limpando texto (o mesmo pipeline da ingestão)…")
    preparados = _preparar_docs(list(docs), log)
    if not preparados:
        raise ValueError("nenhum documento útil — tudo foi descartado na limpeza")
    log(f"📄 {len(preparados)} documento(s) útil(is) "
        f"({len(descartados)} descartado(s) na limpeza)")

    # 2) chunks EXATAMENTE como entrarão (cabeçalho [doc · seção], i/n)
    log("✂️ Dividindo em pedaços contextuais (dry-run — nada é gravado)…")
    chunks = _dividir(preparados, log)
    # PDFs geram vários docs com o MESMO arquivo: os chunks da fonte ficam
    # no 1º documento dela (para exibição é suficiente; a ingestão real
    # divide do mesmo jeito)
    por_doc: dict[int, list] = {}
    por_arquivo: dict[str, int] = {}
    for i, d in enumerate(preparados):
        ar = d.metadata.get("arquivo") or "?"
        if ar not in por_arquivo:
            por_arquivo[ar] = i
    for c in chunks:
        ar = c.metadata.get("arquivo") or "?"
        por_doc.setdefault(por_arquivo.get(ar, 0), []).append(c)

    # 3) consolidado: embedding por documento → duplicados + clusters
    log("🧮 Embedding dos documentos (consolidação: duplicados e temas)…")
    textos = [d.page_content[:LIMITE_BRUTO] for d in preparados]
    emb = rag.embeddings()
    vetores = emb.embed_documents(textos)
    log(f"   {len(vetores)} vetor(es) de {len(set(map(len, vetores))) or 1} dim")

    # duplicados exatos
    vistos: dict[str, int] = {}
    dup_exato: dict[int, int] = {}
    for i, d in enumerate(preparados):
        chave = _md5(d.page_content)
        if chave in vistos:
            dup_exato[i] = vistos[chave]
        else:
            vistos[chave] = i

    # quase-duplicados e clusters (greedy por similaridade máxima)
    quase: dict[int, list[int]] = {i: [] for i in range(len(preparados))}
    cluster_de: list[int] = [-1] * len(preparados)
    membros: list[list[int]] = []
    for i in range(len(preparados)):
        melhor_cluster, melhor_sim = -1, 0.0
        for ci, membro_ids in enumerate(membros):
            sim = max(_coss(vetores[i], vetores[j]) for j in membro_ids)
            if sim >= QUASE_DUP:
                quase[i].append(membro_ids[0])
            if sim > melhor_sim:
                melhor_sim, melhor_cluster = sim, ci
        if melhor_sim >= CLUSTER_MIN:
            membros[melhor_cluster].append(i)
            cluster_de[i] = melhor_cluster
        else:
            membros.append([i])
            cluster_de[i] = len(membros) - 1

    # rótulos por cluster (LLM; sem servidor → "tema N")
    rotulos: list[str] = []
    log(f"🏷️ Rotulando {len(membros)} grupo(s) temático(s)…")
    for ci, membro_ids in enumerate(membros):
        rotulo = f"tema {ci + 1}"
        if ci < 12:  # teto de chamadas por preview
            amostra = "\n\n".join(
                f"[{(preparados[j].metadata.get('titulo') or '')[:80]}]\n"
                f"{preparados[j].page_content[:300]}"
                for j in membro_ids[:3])
            try:
                r = rag._ask_json("rotulo_cluster", amostra)
                if r.get("rotulo"):
                    rotulo = str(r["rotulo"])[:60]
            except Exception as e:
                log(f"   ⚠️ rótulo do grupo {ci + 1}: LLM indisponível ({e})")
        rotulos.append(rotulo)

    # 4) gate de tema (rerank): documento × definição da coleção-alvo
    aderencias: list[float | None] = [None] * len(preparados)
    definicao = None
    if colecao_alvo:
        try:
            client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                                  check_compatibility=False)
            m = catalog.list_meta(client).get(colecao_alvo)
            if m:
                definicao = " · ".join(x for x in (m.get("area"),
                                                   m.get("categoria")) if x)
                if m.get("descricao"):
                    definicao += f": {m['descricao']}"
        except Exception as e:
            log(f"   ⚠️ catálogo indisponível para o gate de tema: {e}")
    if definicao:
        # Gate por EMBEDDING (cosseno bge-m3): mesma escala da busca. O
        # reranker cross-encoder dava ~0.0 para doc-inteiro x definicao
        # curta (treinado p/ query curta x passagem) e marcava TUDO fora
        # do tema. Cosseno usa a MESMA metrica do Qdrant (~0.35 = o
        # SCORE_MIN da busca) — escala familiar e calibravel.
        log(f"🎛️ Gate de tema contra '{colecao_alvo}' (embedding cosseno)…")
        try:
            emb = rag.embeddings()
            v_def = emb.embed_query(definicao[:1500])
            aderencias = []
            for texto in textos:
                v_doc = emb.embed_query(str(texto)[:4000])
                dot = sum(a * b for a, b in zip(v_def, v_doc))
                na = sum(a * a for a in v_def) ** .5
                nb = sum(b * b for b in v_doc) ** .5
                aderencias.append(round(dot / (na * nb), 3) if na and nb else 0.0)
        except Exception as e:
            log(f"   ⚠️ gate de tema indisponível ({e}) — sem aderência")

    # 4.5) JÁ EXISTE NA BASE? comparação por CHUNK (unidade do índice):
    # um chunk novo ≈ idêntico a um chunk do Qdrant = informação repetida.
    # Compara até 6 chunks por documento (amostra suficiente; custo baixo)
    # contra a coleção-alvo (ou as principais, sem alvo) — ANTES de virar
    # sugestão; repetido vem DESMARCADO (pedido do dono).
    ja_existe: list[dict | None] = [None] * len(preparados)
    try:
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        cols = ([colecao_alvo] if colecao_alvo else
                [c.name for c in client.get_collections().collections
                 if not c.name.startswith(("meta_", "sessoes_", "midia_gerada",
                                           "prompts_midia", "arquitetura_",
                                           "mnemosyne_"))][:8])
        # amostra de chunks por doc (o doc inteiro × chunk isolado não bate
        # score — a granularidade do índice é o chunk)
        amostras: list[str] = []
        de_doc: list[int] = []
        for i in range(len(preparados)):
            for c in por_doc.get(i, [])[:6]:
                amostras.append(c.page_content[:2000])
                de_doc.append(i)
        log(f"♻️ verificando {len(amostras)} pedaço(s) de "
            f"{len(preparados)} documento(s) contra {len(cols)} coleção(ões)…")
        vetores_chunks = (rag.embeddings().embed_documents(amostras)
                          if amostras else [])
        from qdrant_client.models import QueryRequest
        for col in cols:
            try:
                reqs = [QueryRequest(query=list(v), limit=1, with_payload=True)
                        for v in vetores_chunks]
                try:
                    lotes = client.query_batch_points(collection_name=col,
                                                      requests=reqs)
                except Exception:
                    lotes = [client.query_points(collection_name=col,
                                                 query=list(v), limit=1,
                                                 with_payload=True)
                             for v in vetores_chunks]
                for k, lote in enumerate(lotes):
                    pts = (lote.points if hasattr(lote, "points")
                           else (lote or []))
                    if not pts:
                        continue
                    score = getattr(pts[0], "score", 0.0) or 0.0
                    if score < QUASE_DUP:
                        continue
                    i = de_doc[k]
                    atual = ja_existe[i]
                    if atual and atual["score"] >= score:
                        continue
                    md0 = pts[0].payload or {}
                    ja_existe[i] = {
                        "score": round(float(score), 3), "colecao": col,
                        "onde": str(md0.get("arquivo") or md0.get("source")
                                    or md0.get("titulo") or "")[:80]}
            except Exception as e:
                log(f"   ⚠️ dedup vs base em '{col}': {str(e)[:90]}")
        n_rep = sum(1 for x in ja_existe if x)
        if n_rep:
            log(f"♻️ {n_rep} documento(s) JÁ EXISTEM na base "
                f"(chunk ≈ idêntico, similaridade ≥ {QUASE_DUP}) — "
                f"vêm DESMARCADOS na revisão")
    except Exception as e:
        log(f"   ⚠️ verificação contra a base indisponível ({str(e)[:90]})")

    # 5) montagem do relatório
    lista = []
    for i, d in enumerate(preparados):
        md = d.metadata
        lista.append({
            "id": i,
            "titulo": md.get("titulo") or Path(str(md.get("source", "?"))).stem,
            "fonte": md.get("source", "?"),
            "url": md.get("url"),
            "revisado_em": md.get("revisado_em"),  # wikipedia: proveniência temporal
            "bytes": len(brutos.get(id(d), "")),
            "chars_limpo": len(d.page_content),
            "bruto": brutos.get(id(d), "")[:LIMITE_BRUTO],
            "limpo": d.page_content[:LIMITE_BRUTO],
            "duplicado_exato_de": dup_exato.get(i),
            "quase_duplicados": quase[i],
            "ja_existe": ja_existe[i],
            "cluster": cluster_de[i],
            "categoria": rotulos[cluster_de[i]] if cluster_de[i] >= 0 else "?",
            "aderencia": aderencias[i],
            "fora_tema": (aderencias[i] is not None
                          and aderencias[i] < TEMA_MIN),
            "chunks": [{
                "i": c.metadata.get("i"), "n": c.metadata.get("n"),
                "secao": c.metadata.get("secao"),
                "texto": c.page_content[:LIMITE_CHUNK],
            } for c in por_doc.get(i, [])],
        })

    # ═══ RESUMO EM PORTUGUÊS (LLM): o que esta aquisição traz, para o
    # operador validar em um olhar antes de aplicar. Resume tema central
    # e cobertura — não substitui a revisão documento a documento.
    resumo_pt = ""
    try:
        amostra = "\n".join(
            f"- {d['titulo'][:70]} ({len(d['chunks'])} pedaços)"
            for d in lista[:12])
        pedido = ("Resuma em PORTUGUÊS, em 2 a 4 frases diretas, o que esta "
                  "aquisição de conhecimento traz (documentos abaixo). Diga "
                  "o tema central, a cobertura e algo digno de nota. Não "
                  "liste documento por documento.\n\n" + amostra)
        _r = rag.llm(temperature=0.2).invoke(pedido)
        resumo_pt = str(getattr(_r, "content", _r))[:600]
    except Exception as e:
        log(f"   ⚠️ resumo PT indisponível ({str(e)[:60]})")

    resp = {
        "resumo_pt": resumo_pt,
        "documentos": _ordenar_revisao(lista),
        "descartados": descartados,
        "clusters": [{"rotulo": rotulos[ci], "docs": [j for j in m]}
                     for ci, m in enumerate(membros)],
        "resumo": {
            "documentos": len(preparados),
            "chunks": len(chunks),
            "descartados": len(descartados),
            "duplicados": len(dup_exato),
            "grupos": len(membros),
            "fora_do_tema": sum(1 for a in aderencias
                                if a is not None and a < TEMA_MIN),
            "ja_existem": n_rep,
            "colecao_alvo": colecao_alvo,
        },
        "colecao_alvo": colecao_alvo,
        "tema_min": TEMA_MIN,
    }
    log(f"👁️ pronto: {len(preparados)} documento(s), {len(chunks)} pedaço(s), "
        f"{len(membros)} grupo(s) — revise e aplique")
    return preparados, resp


# ---------- guardar / ver / aplicar ----------

def _ordenar_revisao(lista: list) -> list:
    """MAIS PRECISOS NO TOPO (pedido do dono): recomendados primeiro,
    ordenados por aderência desc (sem gate = neutro 0.5); os que exigem
    atenção (♻️ já na base / duplicado / fora do tema) vão para o FIM —
    a revisão lê do melhor para o que precisa de decisão."""
    def _ordem(d):
        atencao = bool(d.get("fora_tema") or d.get("ja_existe")
                       or d.get("duplicado_exato_de") is not None)
        ad = d.get("aderencia")
        ad = 0.5 if ad is None else ad
        return (atencao, -ad, str(d.get("titulo", "")).lower())
    return sorted(lista, key=_ordem)


def guardar(pid: str, docs: list, resp: dict) -> None:
    """Grava o preview em DISCO (logs/previews/) — sobrevive a restart do
    container; o aplicar posterior pode acontecer a qualquer momento
    dentro do TTL de 2 h."""
    import json as _json
    with _lock:
        agora = time.time()
        for velho in [k for k, v in _previews.items()
                      if agora - v["t"] > TTL_S]:
            _previews.pop(velho, None)
        _previews[pid] = {"docs": docs, "resp": resp, "t": agora}
    try:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        # poda os expirados do disco (retenção silenciosa)
        for arq in PREVIEW_DIR.glob("*.json"):
            try:
                if agora - arq.stat().st_mtime > TTL_S:
                    arq.unlink(missing_ok=True)
            except Exception:
                pass
        _caminho(pid).write_text(
            _json.dumps({"docs": [
                {"page_content": d.page_content, "metadata": d.metadata}
                for d in docs], "resp": resp, "t": agora},
                ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ preview não persistido em disco ({e}) — só memória")


def ver(pid: str) -> dict | None:
    p = _carregar(pid)
    return p["resp"] if p else None


def aplicar(pid: str, ids: list[int], colecao: str,
            log=print) -> dict:
    """Ingerir SÓ os documentos aprovados (com proveniência da curadoria)."""
    from .ingest import ingest_docs  # import tardio evita ciclo no topo
    p = _carregar(pid)
    if not p:
        raise ValueError("pré-visualização expirada (2 h) — rode a aquisição de novo")
    docs = p["docs"]
    sel = [d for i, d in enumerate(docs) if i in set(ids)]
    if not sel:
        raise ValueError("nenhum documento aprovado no lote")
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in sel:
        d.metadata["adquirido_em"] = agora
        d.metadata["curadoria"] = "revisado (modo Revisão)"
    log(f"✅ aplicando {len(sel)} documento(s) aprovado(s) "
        f"(proveniência: adquirido_em)…")
    return ingest_docs(sel, colecao, rapido=True, log=log)
