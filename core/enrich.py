"""
Destrincha uma coleção: agrupa os pontos por arquivo de origem, categoriza
cada grupo com a LLM e redistribui em coleções menores por tema.

CLI (a partir da raiz): python -X utf8 -m core.enrich <coleção> [--apagar]
"""
import sys
import unicodedata
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import catalog, config, rag


def _slug(texto: str) -> str:
    """Converte a categoria num nome de coleção válido no Qdrant."""
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = "".join(c if c.isalnum() else "_" for c in t.lower())
    while "__" in t:
        t = t.replace("__", "_")
    return t.strip("_")[:48] or "sem_categoria"


def _todos_pontos(client: QdrantClient, colecao: str) -> list:
    """Lê todos os pontos da coleção (em lotes de 256)."""
    pontos, offset = [], None
    while True:
        lote, offset = client.scroll(
            collection_name=colecao, limit=256, with_payload=True,
            with_vectors=True, offset=offset)
        pontos += lote
        if offset is None:
            return pontos


def enrich_collection(colecao: str, apagar: bool = False) -> dict:
    """Destrincha a coleção em várias coleções por tema (uma por arquivo)."""
    if colecao == catalog.CATALOG_COLLECTION:
        raise ValueError("O catálogo (meta_colecoes) não pode ser destrinchado")
    client = QdrantClient(url=config.QDRANT_URL, timeout=60)

    pontos = _todos_pontos(client, colecao)
    if not pontos:
        raise ValueError(f"Coleção '{colecao}' está vazia ou não existe")

    # 1) Agrupar por arquivo de origem e categorizar cada grupo com a LLM
    grupos = {}
    for p in pontos:
        md = (p.payload or {}).get("metadata") or {}
        chave = md.get("source") or f"ponto:{p.id}"
        grupos.setdefault(chave, []).append(p)
    print(f"📦 '{colecao}': {len(pontos)} pontos em {len(grupos)} arquivo(s)")

    destinos = {}  # slug -> {"pontos": [(ponto, descricao)], "arquivos": [...]}
    for arquivo, grupo in grupos.items():
        amostra = str((grupo[0].payload or {}).get("page_content", ""))
        try:
            info = rag.categorize(amostra, arquivo)
        except Exception:
            info = {"categoria": "sem_categoria", "descricao": ""}
        nome = _slug(info["categoria"])
        d = destinos.setdefault(nome, {"pontos": [], "arquivos": []})
        d["pontos"] += [(p, info["descricao"]) for p in grupo]
        d["arquivos"].append(arquivo)
        print(f"   {arquivo} → [{nome}] {info['descricao'][:70]}")

    # 2) Gravar nas coleções de destino (mantendo o vetor original)
    novas = {}
    for nome, d in destinos.items():
        vetores = [p.vector for p, _ in d["pontos"] if p.vector]
        if not vetores:
            continue  # nada a copiar
        dim = len(vetores[0])
        if not client.collection_exists(nome):
            client.create_collection(
                collection_name=nome,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
            print(f"🆕 Coleção '{nome}' criada (COSINE, dim={dim})")
        lote = []
        for p, descricao in d["pontos"]:
            payload = dict(p.payload or {})
            md = dict(payload.get("metadata") or {})
            md["categoria"], md["descricao"] = nome, descricao
            payload["metadata"] = md
            lote.append(PointStruct(id=p.id, vector=p.vector, payload=payload))
            if len(lote) >= 100:
                client.upsert(collection_name=nome, points=lote)
                lote = []
        if lote:
            client.upsert(collection_name=nome, points=lote)
        total = client.count(nome).count
        novas[nome] = {"pontos": total, "arquivos": d["arquivos"]}
        print(f"✅ '{nome}' ← {len(d['pontos'])} ponto(s) ({len(d['arquivos'])} arquivo(s))")

        # 3) Catalogar a coleção nova
        resumo = "; ".join(Path(a).name for a in d["arquivos"])[:400]
        catalog.save_collection(client, nome, nome, f"{len(d['arquivos'])} arquivo(s): {resumo}")

    # 4) Apagar a origem, se pedido
    if apagar:
        client.delete_collection(colecao)
        catalog.remove_collection_meta(client, colecao)
        print(f"🗑️  Coleção original '{colecao}' apagada")

    print(f"\n🎉 '{colecao}' destrinchada em {len(novas)} coleção(ões): {list(novas)}")
    return {"colecao": colecao, "pontos": len(pontos), "arquivos": len(grupos),
            "apagada": apagar, "novas_colecoes": novas}


def main():
    if len(sys.argv) < 2:
        sys.exit("Uso: python -X utf8 -m core.enrich <coleção> [--apagar]")
    enrich_collection(sys.argv[1], "--apagar" in sys.argv)


if __name__ == "__main__":
    main()
