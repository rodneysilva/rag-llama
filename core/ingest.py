"""
Ingestão: lê documentos de uma pasta, LIMPA o texto (core/limpeza), divide em
pedaços com contexto (documento · seção no cabeçalho de cada chunk) e salva
no Qdrant com metadata rica (arquivo, titulo, secao, url, i/n).

CLI (a partir da raiz): python -X utf8 -m core.ingest <pasta_com_documentos>
API: ingest_folder(pasta) -> dict com todo o resultado
Aceita .txt, .md, .mdx, .rst e .pdf (incluindo subpastas).
"""
import hashlib
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from . import catalog, config, contadores, rag
from .limpeza import e_lixo, e_lixo_documento, limpar_texto, score_chunk, titulo_de, url_de


def _slug_pasta(nome: str) -> str:
    """Nome de coleção a partir do nome da pasta (sem acentos, minúsculo)."""
    import re as _re
    import unicodedata as _ud
    t = _ud.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return _re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_") or "documentos"


def _require(url: str, name: str):
    """Falha no início com mensagem clara se um serviço estiver fora do ar."""
    try:
        r = httpx.get(url, timeout=2)
        if r.status_code >= 400:
            raise RuntimeError(f"{name} respondeu HTTP {r.status_code} em {url}")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError(f"{name} está FORA DO AR ({url}) — suba o serviço e tente de novo")


# código-fonte entra como texto (camada própria: sem limpeza agressiva,
# análise LLM antes de virar base, quando não está em modo lote)
CODE_EXTS = {".cs", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
             ".java", ".rb", ".kt", ".swift", ".php", ".c", ".cpp", ".h",
             ".hpp", ".sh", ".ps1", ".vue", ".svelte", ".sql", ".csproj",
             ".json", ".yaml", ".yml", ".toml"}


def load_docs(folder):
    """Lê .txt, .md, .mdx, .rst, .pdf e CÓDIGO-FONTE da pasta (e subpastas)."""
    docs = []
    for path in sorted(Path(folder).rglob("*")):
        if path.suffix.lower() == ".pdf":
            docs += PyPDFLoader(str(path)).load()
        elif path.suffix.lower() in (".txt", ".md", ".mdx", ".rst", *CODE_EXTS):
            docs += TextLoader(str(path), encoding="utf-8").load()
    return docs


def _eh_codigo(arquivo: str) -> bool:
    return Path(arquivo).suffix.lower() in CODE_EXTS


def _preparar_docs(docs, log=None):
    """Prepara cada documento: CÓDIGO preserva indentação/estrutura e ganha a
    camada `codigo`; texto comum é limpo (frases reconstituídas, ruído fora).
    """
    limpos = []
    for d in docs:
        origem = str(d.metadata.get("source", "?"))
        if _eh_codigo(origem):
            # código: NÃO passa pela limpeza de prosa (destruiria indentação
            # e comentários); só normaliza espaços exóticos do export
            for de, para in {"\xa0": " ", "\ufeff": ""}.items():
                d.page_content = d.page_content.replace(de, para)
            d.metadata["camada"] = "codigo"
            d.metadata["arquivo"] = Path(origem).name
            d.metadata["titulo"] = Path(origem).name
            if len(d.page_content.strip()) >= 50:
                limpos.append(d)
            continue
        url = url_de(d.page_content)
        d.page_content = limpar_texto(d.page_content)
        d.metadata["arquivo"] = Path(origem).name
        d.metadata["titulo"] = titulo_de(d.page_content, Path(origem).stem)
        if url:
            d.metadata["url"] = url
        if d.page_content and not e_lixo_documento(d.page_content):
            limpos.append(d)
    return limpos


def _analisar_codigo(docs, log):
    """Camada de conhecimento: a LLM analisa CADA arquivo de código antes de
    ele virar base (spec analise_codigo) — linguagem, versão, propósito,
    padrões, bibliotecas, resumo em português e se o estilo é atual. Tudo
    vira metadata (e o resumo vira o título recuperável do arquivo)."""
    from . import rag  # import tardio: rag importa specs, não ingest
    analisados = 0
    for d in docs:
        if d.metadata.get("camada") != "codigo":
            continue
        try:
            info = rag._ask_json("analise_codigo",
                                 f"Arquivo: {d.metadata['arquivo']}\n\n"
                                 f"{d.page_content[:6000]}")
        except Exception:
            info = {}
        if not info.get("linguagem"):
            continue
        d.metadata.update({
            "linguagem": str(info.get("linguagem", ""))[:40],
            "versao": str(info.get("versao", ""))[:30],
            "proposito": str(info.get("proposito", ""))[:200],
            "padroes": ", ".join(map(str, info.get("padroes", [])))[:200],
            "bibliotecas": ", ".join(map(str, info.get("bibliotecas", [])))[:200],
            "resumo_pt": str(info.get("resumo_pt", ""))[:500],
            "qualidade": str(info.get("qualidade", ""))[:20],
        })
        if info.get("proposito"):
            d.metadata["titulo"] = str(info["proposito"])[:120]
        analisados += 1
        log(f"   🔎 {d.metadata['arquivo']}: {d.metadata.get('qualidade', '?')} · "
            f"{d.metadata.get('linguagem', '?')} — {d.metadata.get('proposito', '')[:60]}")
    if analisados:
        log(f"🔬 {analisados} arquivo(s) de código analisados "
            "(linguagem/versão/padrões/resumo em PT) → metadata da base")


_CABECALHOS_MD = [("#", "h1"), ("##", "h2"), ("###", "h3")]


def _dividir(docs, log):
    """Divide por SEÇÕES (markdown) e depois por tamanho; cada pedaço recebe
    um cabeçalho contextual "[documento · seção]" no início do texto.

    O cabeçalho entra no embedding (é isso que faz o vetor representar o
    contexto, não só o trecho solto) e viaja no metadata (titulo/secao) para
    a webui exibir de onde veio. Pedaços sem semântica (menu, referências,
    fragmentos curtos) e duplicados exatos são descartados.
    """
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_CABECALHOS_MD, strip_headers=False)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP)

    pedacos = []
    for d in docs:
        herda = {k: d.metadata[k] for k in
                 ("arquivo", "titulo", "categoria", "descricao", "area",
                  "camada", "linguagem", "versao", "proposito", "padroes",
                  "bibliotecas", "resumo_pt", "qualidade")
                 if d.metadata.get(k)}
        if d.metadata.get("url"):
            herda["url"] = d.metadata["url"]
        secoes = [d]
        if (str(d.metadata.get("source", "")).lower().endswith((".md", ".mdx"))
                and d.metadata.get("camada") != "codigo"):
            try:  # .md: 1ª passada por cabeçalhos — a seção fica inteira
                secoes = header_splitter.split_text(d.page_content) or [d]
            except Exception:
                secoes = [d]
        for sec in secoes:
            headers = {k: v for k, v in (getattr(sec, "metadata", None) or {}).items()
                       if k in ("h1", "h2", "h3") and v}
            if headers:
                herda = {**herda, "secao": " · ".join(headers.values())}
            for c in splitter.split_documents([sec]):
                c.metadata.update(herda)
                pedacos.append(c)

    mantidos, vistos, descartados = [], set(), 0
    _rej_score = []  # (motivo, qtd) — relatório do gate de qualidade
    for c in pedacos:
        chave = hashlib.md5(
            re.sub(r"\s+", " ", c.page_content).strip().encode("utf-8")).hexdigest()
        # código não passa pela heurística de prosa (e_lixo derrubaria
        # trechos legítimos): só dedupe + mínimo de conteúdo
        eh_cod = c.metadata.get("camada") == "codigo"
        if chave in vistos or (not eh_cod and e_lixo(c.page_content)) \
                or (eh_cod and len(c.page_content.strip()) < 60):
            descartados += 1
            continue
        # GATE DE QUALIDADE (padrão comunidade RAG, pedido do dono 28/08):
        # score 0-1 por chunk; abaixo do limiar do .env = rejeitado com
        # motivo (links/repetição/palavras/alfa/JSON/tabela/lista de nomes)
        if not eh_cod:
            nota, motivos = score_chunk(c.page_content)
            c.metadata["score"] = nota
            if nota < config.SCORE_CHUNK_MIN:
                for m in motivos or ["score baixo"]:
                    _rej_score.append(m)
                descartados += 1
                continue
        vistos.add(chave)
        mantidos.append(c)

    totais = Counter(c.metadata.get("arquivo", "?") for c in mantidos)
    indice = Counter()
    for c in mantidos:
        ar = c.metadata.get("arquivo", "?")
        indice[ar] += 1
        c.metadata["i"], c.metadata["n"] = indice[ar], totais[ar]
        cab = " · ".join(x for x in (c.metadata.get("titulo"),
                                     c.metadata.get("secao")) if x)
        if cab:
            c.page_content = f"[{cab}]\n{c.page_content}"
    log(f"✂️  {len(mantidos)} pedaço(s) válido(s) "
        f"({descartados} descartado(s): ruído de página, curto ou duplicado)")
    if _rej_score:
        from collections import Counter as _C
        por_motivo = _C(_rej_score).most_common()
        resumo = ", ".join(f"{m} ({q})" for m, q in por_motivo)
        log(f"🎛️  gate de qualidade: {len(_rej_score)} chunk(s) abaixo de "
            f"{config.SCORE_CHUNK_MIN} — {resumo}")
    return mantidos


def ingest_folder(folder, collection=None, rapido=False, log=None):
    """Pipeline completo de ingestão: registra cada etapa e retorna o resultado.

    `log` opcional recebe cada linha de progresso (padrão: print) — a API
    passa um callback que alimenta o wizard de ingestão na webui em tempo real.
    `collection` opcional define em qual coleção salvar (vazio: uma coleção
    nova a partir do nome da pasta). `rapido=True` (modo lote): pula a
    categorização por arquivo com a LLM — 1 chamada por arquivo é inviável
    em bases grandes (ex.: documentação oficial inteira).
    """
    log = log or print
    if not collection:  # sem coleção informada: uma por pasta (slug do nome)
        collection = _slug_pasta(Path(folder).name)
    folder = str(Path(folder))
    if not Path(folder).is_dir():
        raise ValueError(f"Pasta não encontrada: {folder}")

    # 0) Garantir que Qdrant e embedding estão no ar antes de começar
    _require(f"{config.QDRANT_URL}/healthz", "Qdrant")
    _require(f"{config.EMBED_BASE_URL}/models", "Servidor de embedding (BGE-M3)")

    # 1) Ler, limpar o texto e descartar arquivos que viraram só ruído
    log(f"📂 Lendo arquivos de '{folder}'…")
    docs = _preparar_docs(load_docs(folder), log)
    log(f"📄 {len(docs)} documento(s) útil(is) de '{folder}' (após a limpeza)")
    if len(docs) <= 30:
        for d in docs:
            log(f"   - {d.metadata.get('source', '?')}")
    if not docs:
        raise ValueError(f"Nenhum conteúdo útil encontrado em '{folder}' "
                         "(tudo foi descartado na limpeza ou não há "
                         ".txt/.md/.pdf/código)")
    return ingest_docs(docs, collection, rapido, log)


def ingest_docs(docs, collection=None, rapido=False, log=None):
    """Núcleo do pipeline a partir de documentos JÁ LIDOS E LIMPOS (por
    ingest_folder, pelo HuggingFace (core/hf) ou por qualquer fonte nova
    que produza Documents): categoriza → divide → embeda → grava → cataloga."""
    log = log or print
    if not collection:
        collection = "documentos"
    if not docs:
        raise ValueError("nenhum documento útil para ingerir")

    # 1b) Camada de código: análise LLM por arquivo (pula no modo lote)
    if not rapido and any(d.metadata.get("camada") == "codigo" for d in docs):
        log("🔬 Analisando código-fonte com a LLM (linguagem, versão, padrões…)…")
        _analisar_codigo(docs, log)

    # 2) Categorizar cada arquivo com a LLM (regras em specs/categorizacao.md)
    if rapido:
        log(f"\n🏷️  Modo lote: sem LLM por arquivo — categoria fixa '{collection}'")
        for d in docs:
            d.metadata.update({
                "categoria": collection,
                "descricao": f"documento: {Path(d.metadata.get('source', '?')).name}",
            })
    else:
        log("\n🏷️  Categorização dos arquivos:")

        def _categorizar(d):
            """Categoriza um documento; LLM fora do ar não para a ingestão.
            Marca o SERVIÇO dentro da worker: o thread-local do chamador
            não atravessa o ThreadPoolExecutor (sem isso, contava como
            'sistema')."""
            contadores.set_servico("ingestao")
            origem = d.metadata.get("source", "?")
            try:
                return rag.categorize(d.page_content, origem)
            except Exception as e:
                log(f"   ⚠️  LLM indisponível ({e}) — seguindo sem categoria")
                return {"categoria": "sem_categoria", "descricao": ""}

        # Chamadas em paralelo (o llama-server atende N slots ao mesmo tempo,
        # então a ingestão conversa com a LLM sem esperar 1 arquivo por vez).
        # Cada resultado é registrado assim que fica pronto (log ao vivo,
        # contador n/total); a montagem final preserva a ordem dos docs.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(_categorizar, d): i for i, d in enumerate(docs)}
            resultados = [None] * len(docs)
            for n, fut in enumerate(as_completed(futs), 1):
                i = futs[fut]
                resultados[i] = fut.result()
                log(f"   ({n}/{len(docs)}) {Path(docs[i].metadata.get('source', '?')).name}: "
                    f"[{resultados[i]['categoria']}] {resultados[i]['descricao']}")
        for d, info in zip(docs, resultados):
            d.metadata.update(info)  # os pedaços herdam categoria e descricao

    chunks = _dividir(docs, log)

    # 2) Testar o embedding BGE-M3 e descobrir a dimensão do vetor
    emb = rag.embeddings()
    dim = len(emb.embed_query("teste de dimensão"))
    log(f"🧮 Embedding OK: '{config.EMBED_MODEL}' gera vetores com {dim} dimensões")

    # 3) Criar a coleção no Qdrant (se não existir) e salvar os pedaços
    client = QdrantClient(url=config.QDRANT_URL)
    created = not client.collection_exists(collection)
    if created:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        log(f"🆕 Coleção '{collection}' criada (COSINE, dim={dim})")
    else:
        log(f"♻️  Coleção '{collection}' já existe — pedaços serão adicionados")

    log(f"⬆️ Indexando {len(chunks)} pedaço(s) no Qdrant ('{collection}')…")
    rag.vectorstore(client, collection).add_documents(chunks)

    # 4) Registrar a coleção no catálogo (categoria + descricao em PT)
    meta = catalog.list_meta(client)
    if collection not in meta:
        cats = [d.metadata.get("categoria", "") for d in docs]
        categoria = Counter(cats).most_common(1)[0][0] if cats else "sem_categoria"
        areas = [d.metadata.get("area", "") for d in docs if d.metadata.get("area")]
        resumo = "; ".join(
            f"{Path(d.metadata.get('source', '?')).name}: {d.metadata.get('descricao', '')}"
            for d in docs)[:400]
        catalog.save_collection(client, collection, categoria, resumo,
                                area=Counter(areas).most_common(1)[0][0] if areas else "")
        log(f"🗂️  Catálogo atualizado: '{collection}' → [{categoria}]")
    else:
        log(f"🗂️  '{collection}' já catalogada (reanalise para atualizar a descricao)")

    # 5) Confirmação final direto do Qdrant
    total = client.count(collection).count
    log(f"✅ Ingestão concluída! Coleção '{collection}' tem {total} pontos no Qdrant")

    return {
        "colecao": collection,
        "folder": str(docs[0].metadata.get("source", "?")).split("/")[0]
                  if docs else None,  # origem (pasta ou fonte como o HF)
        "files": [d.metadata.get("source") for d in docs],
        "documents": len(docs),
        "chunks": len(chunks),
        "categorias": {Path(d.metadata.get("source", "?")).name: d.metadata.get("categoria")
                       for d in docs},
        "descricoes": {Path(d.metadata.get("source", "?")).name: d.metadata.get("descricao")
                       for d in docs},
        "sample": chunks[0].page_content[:300],
        "embedding_dim": dim,
        "collection": collection,
        "collection_created": created,
        "total_points": total,
    }


def main():
    """Entrada do CLI."""
    if len(sys.argv) < 2:
        sys.exit("Uso: python -m core.ingest <pasta_com_documentos> [colecao] [--rapido]")
    pasta, colecao, rapido = sys.argv[1], None, "--rapido" in sys.argv
    if len(sys.argv) > 2 and sys.argv[2] != "--rapido":
        colecao = sys.argv[2]
    print(ingest_folder(pasta, colecao, rapido))


if __name__ == "__main__":
    main()
