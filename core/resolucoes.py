"""Base vetorial de RESOLUÇÃO DE PROBLEMAS (coleção erros_comuns no Qdrant).

Problema real + diagnóstico + causa + solução, indexado por embedding —
o chat consulta e responde com a solução JÁ APLICADA (não com palpite).
Quem grava: o operador (botão/forma) e o agente ao final de cada correção
de bug real — a memória institucional de troubleshooting do projeto.

Estrutura do chunk (spec modelo_dados):
  problema   — o sintoma como o operador viu
  causa      — a raiz encontrada (diagnóstico)
  solucao    — o que foi feito (arquivo/comando)
  contexto   — onde aplica (chat/biblioteca/vps/estacao)
"""
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from . import config, rag

COLECAO = "erros_comuns"


def registrar(problema: str, causa: str, solucao: str,
              contexto: str = "", log=print) -> dict:
    """Grava UMA resolução na base vetorial (upsert por hash do problema)."""
    import hashlib, time
    client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                          check_compatibility=False)
    # coleção segue a dimensão do embedding ativo (bge-m3 = 1024)
    try:
        client.get_collection(COLECAO)
    except Exception:
        from qdrant_client.models import Distance, VectorParams
        client.create_collection(
            collection_name=COLECAO,
            vectors_config=VectorParams(size=1024, distance=Distance.COSINE))

    texto = (f"### Problema\n{problema}\n\n### Causa\n{causa}\n\n"
             f"### Solução\n{solucao}\n\n### Contexto\n{contexto or 'geral'}")
    emb = rag.embeddings()
    vetor = emb.embed_query(f"{problema} {causa}"[:2000])
    pid = int(hashlib.md5((problema + causa).encode()).hexdigest()[:12], 16)
    client.upsert(
        collection_name=COLECAO,
        points=[PointStruct(id=pid, vector=vetor, payload={
            "page_content": texto,
            "metadata": {
                "arquivo": "resolucoes.md", "titulo": problema[:80],
                "secao": "resolucao de problemas", "area": "tecnologia",
                "categoria": "troubleshooting",
                "descricao": causa[:120], "url": "",
                "resolvido_em": time.strftime("%Y-%m-%d %H:%M:%S"),
                "contexto": contexto or "geral",
            },
        })])
    log(f"✅ resolução indexada em '{COLECAO}' (problema: {problema[:60]}…)")
    return {"ok": True, "id": pid, "colecao": COLECAO}


def main():
    p = Path(__file__)
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("problema"); ap.add_argument("causa"); ap.add_argument("solucao")
    ap.add_argument("--contexto", default="")
    a = ap.parse_args()
    print(registrar(a.problema, a.causa, a.solucao, a.contexto))


if __name__ == "__main__":
    main()
