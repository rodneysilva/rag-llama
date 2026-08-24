"""
CLI interativo de consulta RAG — mesma esteira da API: busca HÍBRIDA
(densa + full-text fundidas por RRF) com SCORE_MIN e diversificação.
Rodar a partir da raiz: python -X utf8 -m core.main
"""
import sys

from qdrant_client import QdrantClient

from . import config, rag


def banner():
    """Mostra as configurações em uso na inicialização."""
    print("=" * 60)
    print("  RAG local — Qdrant + LLM local + BGE-M3 (busca híbrida)")
    print("=" * 60)
    print(f"  LLM       : {config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    print(f"  Embedding : {config.EMBED_MODEL} @ {config.EMBED_BASE_URL}")
    print(f"  Qdrant    : {config.QDRANT_URL} (coleção '{config.COLLECTION}')")
    print("=" * 60)


def main():
    banner()
    client = QdrantClient(url=config.QDRANT_URL)
    print("🗂️  Coleções no Qdrant:", [c.name for c in client.get_collections().collections])

    if not client.collection_exists(config.COLLECTION):
        sys.exit(f"❌ Coleção '{config.COLLECTION}' não existe. Rode: python -m core.ingest <pasta>")

    while True:
        try:
            question = input("\n🔎 Pergunta (ou 'sair'): ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not question or question.lower() in ("sair", "exit", "quit"):
            break

        # 1) busca HÍBRIDA — o mesmo caminho do chat (RRF + SCORE_MIN +
        #    diversificação por arquivo; antes era densa crua no vectorstore)
        achados, erros = rag.search(client, [config.COLLECTION], question)
        if erros and not achados:
            print(f"❌ busca falhou: {next(iter(erros.values()))}")
            continue
        print(f"\n📚 {len(achados)} fragmento(s) recuperado(s):")
        for i, (doc, score, colecao) in enumerate(achados, 1):
            print(f"\n  [{i}] score={score:.4f} | {colecao} | "
                  f"{doc.metadata.get('source', '?')}")
            print(f"      {doc.page_content[:400]}")

        if not achados:
            print("⚠️  Nada encontrado acima do SCORE_MIN.")
            continue

        # 2) resposta da LLM usando os fragmentos como contexto
        print("\n🤖 Resposta:")
        for piece in rag.answer_stream(question, [d for d, _, _ in achados]):
            print(piece, end="", flush=True)
        print()

    print("\n👋 Até mais!")


if __name__ == "__main__":
    main()
