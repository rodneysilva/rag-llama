"""Snapshot restaurável de coleções (F5) — reversibilidade para operações
destrutivas em massa.

Toda reforma (reembed, unificação, F5) mexe no Qdrant de forma difícil de
desfazer. O snapshot copia a coleção INTEIRA (id + vetor + payload) para
`logs/snapshots/<colecao>_<ts>.jsonl` e `restaurar()` recria a coleção a
partir do arquivo — ponto a ponto, com o VETOR gravado (não re-embeda:
o estado volta exatamente como estava).

Custo: ~1 linha JSON por ponto (vetor 1024d arredondado a 6 casas ≈ 9 KB).
Para coleções de dezenas de milhares de pontos o arquivo pesa — snapshots
são para JANELAS DE REFORMA, não para backup contínuo (isso é papel do
volume do Qdrant).
"""
import json
import threading
import time
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from . import config

SNAP_DIR = Path(__file__).resolve().parent.parent / "logs" / "snapshots"
_lock = threading.Lock()


def _client() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL, timeout=60,
                        check_compatibility=False)


def criar(client: QdrantClient | None, colecao: str, motivo: str = "",
          log=print) -> str:
    """Fotografa a coleção (id+vetor+payload por ponto) → caminho do jsonl."""
    client = client or _client()
    if not client.collection_exists(colecao):
        raise ValueError(f"coleção '{colecao}' não existe")
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    arq = SNAP_DIR / f"{colecao}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    total = 0
    with _lock, open(arq, "w", encoding="utf-8") as f:
        f.write(json.dumps({"colecao": colecao, "motivo": motivo[:200],
                            "criado": time.strftime("%Y-%m-%d %H:%M:%S")},
                           ensure_ascii=False) + "\n")
        offset = None
        while True:
            pontos, offset = client.scroll(collection_name=colecao, limit=256,
                                           with_payload=True,
                                           with_vectors=True, offset=offset)
            for p in pontos:
                f.write(json.dumps({
                    "id": str(p.id),
                    "vetor": [round(float(x), 6) for x in (p.vector or [])],
                    "payload": p.payload or {},
                }, ensure_ascii=False) + "\n")
                total += 1
            if offset is None:
                break
    log(f"📸 snapshot de '{colecao}': {total} ponto(s) → {arq.name}")
    return str(arq)


def restaurar(client: QdrantClient | None, arquivo: str,
              colecao: str | None = None, log=print) -> dict:
    """Recria a coleção a partir do snapshot (APAGA a atual antes — o
    snapshot é a fonte da verdade do momento da foto). Vetores voltam como
    estavam: zero re-embedding."""
    client = client or _client()
    arq = Path(arquivo)
    if not arq.is_file() or arq.parent != SNAP_DIR:
        raise ValueError("arquivo de snapshot inválido (use um caminho de "
                         f"{SNAP_DIR})")
    linhas = arq.read_text(encoding="utf-8").splitlines()
    cabecalho = json.loads(linhas[0])
    destino = colecao or cabecalho["colecao"]
    pontos, dim = [], 0
    for l in linhas[1:]:
        d = json.loads(l)
        vetor = [float(x) for x in d.get("vetor") or []]
        dim = dim or len(vetor)
        pontos.append({"id": d["id"], "vector": vetor, "payload": d["payload"]})
    if not pontos:
        raise ValueError("snapshot sem pontos")
    if client.collection_exists(destino):
        client.delete_collection(destino)
        log(f"🗑️ coleção atual '{destino}' apagada (restaurando snapshot)")
    client.create_collection(
        collection_name=destino,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    for i in range(0, len(pontos), 256):
        client.upsert(collection_name=destino, points=pontos[i:i + 256])
    total = client.count(destino, exact=True).count
    log(f"⏪ '{destino}' restaurada: {total} ponto(s) (de {arq.name})")
    return {"colecao": destino, "pontos": total, "arquivo": str(arq)}


def listar() -> list[dict]:
    """Snapshots disponíveis (mais recente primeiro)."""
    saida = []
    if not SNAP_DIR.exists():
        return saida
    for arq in SNAP_DIR.glob("*.jsonl"):
        try:
            cab = json.loads(arq.read_text(encoding="utf-8")
                             .splitlines()[0])
            saida.append({"arquivo": str(arq), "colecao": cab.get("colecao", ""),
                          "motivo": cab.get("motivo", ""),
                          "criado": cab.get("criado", ""),
                          "pontos": max(0, len(arq.read_text(encoding='utf-8')
                                               .splitlines()) - 1),
                          "mb": round(arq.stat().st_size / 1e6, 1)})
        except Exception:
            continue
    return sorted(saida, key=lambda s: s["criado"], reverse=True)
