"""
Unifica os pontos de ARQUITETURA entre as linguagens: para cada conceito
universal (SOLID, DDD, clean architecture, hexagonal, CQRS…), busca os
melhores chunks de cada coleção arquitetura_* e copia (com o vetor original)
para uma coleção unificada — uma consulta "como aplicar SRP" traz as
perspectivas de todas as linguagens de uma vez.

Uso: python -X utf8 -m core.unificar_arquiteturas
"""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import config, rag
from .linguagens import LINGUAGENS

DESTINO = "arquitetura_unificada"
# coleções de origem: nomeadas por LINGUAGEM de programação
# LINGUAGENS vem do rodeiro único (core/linguagens.py) — antes era uma lista
# local de 7 que divergia das outras duas cópias do projeto
# conceitos universais (queries em inglês — base é toda bilingue/EN)
CONCEITOS = [
    "single responsibility principle SOLID",
    "open closed principle SOLID",
    "dependency inversion principle SOLID",
    "interface segregation liskov substitution SOLID",
    "domain driven design entities value objects aggregates",
    "bounded context ubiquitous language DDD",
    "clean architecture layers dependency rule",
    "hexagonal architecture ports and adapters",
    "cqrs command query responsibility segregation",
    "repository pattern unit of work",
    "use cases application layer",
    "testing strategy unit integration architecture",
]
POR_CONCEITO = 5             # melhores chunks por conceito (todas as linguagens)
POR_LING_CONCEITO = 2        # máx. por linguagem em cada conceito
TOTAL_LING = 12              # máx. total por linguagem (diversidade)


def _slug_conceito(texto: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")[:40]


def unificar(log=print) -> dict:
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    origens = sorted(c.name for c in client.get_collections().collections
                     if c.name in LINGUAGENS)
    if not origens:
        raise ValueError("nenhuma coleção de linguagem encontrada "
                         f"({', '.join(sorted(LINGUAGENS))}) — rode os seeds primeiro")
    log(f"🔗 unificando {len(origens)} coleção(ões): {', '.join(origens)}")
    # SNAPSHOT da base unificada antes de recriá-la (F5: reversível)
    try:
        from . import snapshot
        if client.collection_exists(DESTINO):
            snapshot.criar(client, DESTINO, motivo="unificar_arquiteturas", log=log)
    except Exception as e:
        log(f"⚠️ snapshot falhou ({e}) — seguindo SEM reversibilidade")

    if client.collection_exists(DESTINO):
        client.delete_collection(DESTINO)
    dim = len(rag.embeddings().embed_query("dim"))
    client.create_collection(
        collection_name=DESTINO,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE))

    pontos, vistos = [], set()
    for conceito in CONCEITOS:
        achados, _ = rag.search(client, origens, conceito, k=POR_CONCEITO * 2)
        por_origem: dict[str, int] = {}
        for doc, score, colecao in achados:
            if por_origem.get(colecao, 0) >= POR_LING_CONCEITO:
                continue
            if len([1 for _, _, c2, _ in pontos if c2 == colecao]) >= TOTAL_LING:
                continue
            chave = doc.page_content[:200]
            if chave in vistos:
                continue
            vistos.add(chave)
            por_origem[colecao] = por_origem.get(colecao, 0) + 1
            pontos.append((doc, score, colecao, conceito))
            if len([p for p in pontos if p[3] == conceito]) >= POR_CONCEITO:
                break
    log(f"   {len(pontos)} chunk(s) selecionados "
        f"({len(CONCEITOS)} conceitos × até {POR_LING_CONCEITO}/linguagem)")

    emb = rag.embeddings()
    import uuid
    conteudos = [f"[{c}]\n{d.page_content}" for d, _, _, c in pontos]
    vetores = emb.embed_documents(conteudos)
    for (doc, score, colecao, conceito), vetor in zip(pontos, vetores):
        md = dict(doc.metadata)
        md.update({"colecao_origem": colecao, "conceito": _slug_conceito(conceito)})
        client.upsert(collection_name=DESTINO, points=[PointStruct(
            id=str(uuid.uuid4()), vector=vetor,
            payload={"page_content": f"[{c}]\n{doc.page_content}"
                     if not doc.page_content.startswith("[") else doc.page_content,
                     "metadata": md})])
    total = client.count(DESTINO, exact=True).count
    log(f"✅ '{DESTINO}' com {total} pontos — visão unificada das linguagens")
    return {"colecao": DESTINO, "pontos": total, "origens": origens}


def main():
    print(unificar())


if __name__ == "__main__":
    main()
