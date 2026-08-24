"""Re-embeda coleções cujos vetores foram zerados (corrupção de storage).

O crash do Docker Desktop pode zerar os VETORES em disco (payloads e ids
sobrevivem — a busca volta scores 0.0 para tudo). Este utilitário percorre
cada coleção, identifica pontos com vetor de norma ~0, re-embeda o
`page_content` pelo bge-m3 ativo e grava o vetor NO MESMO id (upsert).
Idempotente: pontos saudáveis não são tocados.

Uso: python -X utf8 -m core.reembed [colecao ...]   (sem args: todas)
"""
import math
import sys
import time

from qdrant_client import QdrantClient
from qdrant_client.models import PointVectors

from . import config, rag


def reembed(colecao: str, cliente: QdrantClient, embedder, log=print) -> dict:
    """Re-embeda pontos de norma ~0 no mesmo id (idempotente). SNAPSHOT
    antes de mexer (F5): operação em massa precisa ser reversível."""
    if not cliente.collection_exists(colecao):
        return {"erro": f"coleção '{colecao}' não existe"}
    try:
        from . import snapshot
        snapshot.criar(cliente, colecao, motivo="reembed", log=log)
    except Exception as e:
        log(f"⚠️ snapshot falhou ({e}) — seguindo SEM reversibilidade")
    """Re-embeda os pontos de vetor zerado de UMA coleção. Devolve contagem."""
    if not cliente.collection_exists(colecao):
        log(f"⏭️  {colecao}: não existe")
        return {"pontos": 0, "reembedados": 0}
    total = cliente.count(colecao, exact=True).count
    log(f"🔄 {colecao}: {total} ponto(s) — varrendo em busca de vetores zerados…")
    zerados, cursor, vistos = [], None, 0
    while True:
        pontos, cursor = cliente.scroll(collection_name=colecao, limit=256,
                                        with_payload=False, with_vectors=True,
                                        offset=cursor)
        for p in pontos:
            v = p.vector
            if isinstance(v, dict):  # vetores nomeados
                v = next(iter(v.values()))
            if not v or math.sqrt(sum(x * x for x in v)) < 0.01:
                zerados.append(p.id)
        vistos += len(pontos)
        if cursor is None:
            break
    if not zerados:
        log(f"✅ {colecao}: {vistos} ponto(s) saudáveis — nada a fazer")
        return {"pontos": vistos, "reembedados": 0}
    log(f"🩹 {colecao}: {len(zerados)} de {vistos} com vetor zerado — re-embedando…")
    t0 = time.time()
    feito = 0
    for i in range(0, len(zerados), 64):
        ids_lote = zerados[i:i + 64]
        # recupera o TEXTO de cada ponto (payload)
        lotes_pts, cur2 = [], None
        alvo = set(ids_lote)
        while len(lotes_pts) < len(ids_lote):
            pts, cur2 = cliente.scroll(collection_name=colecao, limit=256,
                                       with_payload=True, with_vectors=False,
                                       offset=cur2)
            lotes_pts += [p for p in pts if p.id in alvo]
            if cur2 is None or len(lotes_pts) >= len(ids_lote):
                break
        textos = [(p.payload or {}).get("page_content") or " " for p in lotes_pts]
        vetores = embedder.embed_documents(textos)
        cliente.update_vectors(collection_name=colecao,
                               points=[PointVectors(id=pid, vector=vec)
                                       for pid, vec in zip(ids_lote, vetores)])
        feito += len(ids_lote)
        log(f"   {feito}/{len(zerados)} ({time.time() - t0:.0f}s)")
    log(f"✅ {colecao}: {feito} ponto(s) re-embedados")
    return {"pontos": vistos, "reembedados": feito}


def main() -> None:
    alvo = sys.argv[1:]
    cliente = QdrantClient(url=config.QDRANT_URL, timeout=120,
                           check_compatibility=False)
    embedder = rag.embeddings()
    if alvo:
        colecoes = alvo
    else:
        colecoes = [c.name for c in cliente.get_collections().collections]
    print(f"🧬 re-embed de {len(colecoes)} coleção(ões) via {config.EMBED_MODEL}")
    resumo = {}
    for c in colecoes:
        try:
            resumo[c] = reembed(c, cliente, embedder)
        except Exception as e:
            print(f"❌ {c}: {e}")
    tot = sum(r["reembedados"] for r in resumo.values())
    print(f"\n🎉 concluído: {tot} ponto(s) re-embedado(s)")


if __name__ == "__main__":
    main()
