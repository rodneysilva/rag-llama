"""
Análise das coleções do Qdrant com a LLM: gera categoria e descrição em
português do que cada coleção contém/faz e registra tudo no catálogo.

CLI (a partir da raiz): python -X utf8 -m core.analyze
"""
import json

from qdrant_client import QdrantClient

from . import catalog, config, rag


def _samples(client: QdrantClient, nome: str, limit=5) -> list[str]:
    """Pega alguns pedaços de texto da coleção para dar contexto à LLM."""
    pontos, _ = client.scroll(collection_name=nome, limit=limit, with_payload=True)
    textos = []
    for p in pontos:
        pl = p.payload or {}
        t = pl.get("page_content") or pl.get("content") or pl.get("text") or ""
        if t:
            textos.append(str(t)[:600])
    return textos


def analyze_all() -> list[dict]:
    """Varre todas as coleções, analisa com a LLM e registra no catálogo."""
    client = QdrantClient(url=config.QDRANT_URL, timeout=30)
    nomes = [c.name for c in client.get_collections().collections
             if c.name != catalog.CATALOG_COLLECTION]

    resultados = []
    for nome in nomes:
        pontos = client.get_collection(nome).points_count
        amostras = _samples(client, nome)
        print(f"\n🔍 Analisando '{nome}' ({pontos} pontos, {len(amostras)} amostra(s))…")
        r = rag.analyze_collection(nome, amostras)
        catalog.save_collection(client, nome, r["categoria"], r["descricao"],
                                area=r.get("area", ""))
        resultados.append({"colecao": nome, "pontos": pontos, **r})
        print(f"   area     : {r.get('area', 'indeterminado')}")
        print(f"   categoria: {r['categoria']}")
        print(f"   descricao: {r['descricao']}")
        if r.get("resumo"):
            print(f"   resumo   : {r['resumo']}")

    catalog.save_specs(client)
    print(f"\n✅ {len(resultados)} coleção(ões) catalogadas + specs registradas")
    return resultados


def main():
    for linha in analyze_all():
        print(json.dumps(linha, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
