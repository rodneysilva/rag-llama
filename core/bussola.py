"""Bússola pré-token (F3) — "já sei a resposta?" ANTES de consumir LLM.

Cada pergunta respondida com sucesso é indexada (embedding bge-m3) na
coleção de sistema `sessoes_chat` junto da resposta e da sessão de origem.
Na consulta seguinte (sem histórico, como o cache):

- similaridade ≥ 0.95 → resposta DIRETA reaproveitada, ZERO token, citando
  a conversa de origem (o cache Redis cuida do igual-exato ≥0.97 com MESMO
  escopo; a bússola cobre CROSS-SESSÃO e sobrevive a flush do Redis);
- 0.85–0.95 → segue o fluxo normal com uma SUGESTÃO anexada ("já respondi
  algo parecido" — campo `bussola` na resposta; a webui pode oferecer o
  1-clique "usar a anterior" / "gerar nova").

Escopo por OWNER (contas isoladas — pergunta de uma conta nunca reaparece
para outra). Id determinístico (owner+pergunta normalizada) = upsert: a
mesma pergunta atualiza a resposta, não empilha pontos. Tudo degrade em
silêncio: bússola fora do ar nunca derruba o chat.
"""
import re
import threading
import time
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, VectorParams

from . import config, rag

COLECAO = "sessoes_chat"   # nome já reservado em COLECOES_SISTEMA (api/app.py)
DIRETO = 0.95              # ≥: responde sem token, citando a sessão de origem
SUGESTAO = 0.85            # ≥: anexa a sugestão à resposta normal
MAX_RESPOSTA = 20000       # teto do texto guardado no metadata (payloads KB)
_lock = threading.Lock()
_pronta = False            # coleção garantida 1x por processo


def _norm(pergunta: str) -> str:
    return re.sub(r"\s+", " ", (pergunta or "").strip().lower())


def _id(owner: str, pergunta: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"{owner or '_'}|{_norm(pergunta)}"))


def _client() -> QdrantClient:
    return QdrantClient(url=config.QDRANT_URL, timeout=15,
                        check_compatibility=False)


def garantir() -> None:
    """Cria a coleção 1x por processo (dim do embedding em uso)."""
    global _pronta
    with _lock:
        if _pronta:
            return
        client = _client()
        if not client.collection_exists(COLECAO):
            dim = len(rag.embeddings().embed_query("dimensão"))
            client.create_collection(
                collection_name=COLECAO,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        _pronta = True


def registrar(pergunta: str, resposta: str, sid: str | None, owner: str,
              mode: str, colecoes: list | None, log=print) -> None:
    """Indexa (pergunta → resposta) para as próximas perguntas saírem grátis."""
    if not pergunta or not resposta:
        return
    try:
        garantir()
        vetor = rag.embeddings().embed_query(pergunta)
        _client().upsert(
            collection_name=COLECAO,
            points=[{
                "id": _id(owner, pergunta),
                "vector": vetor,
                "payload": {
                    "page_content": pergunta,
                    "metadata": {
                        "resposta": resposta[:MAX_RESPOSTA],
                        "sessao": sid or "",
                        "owner": owner or "",
                        "mode": mode or "",
                        "colecoes": sorted(colecoes or []),
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                },
            }],
            wait=False)
    except Exception as e:
        log(f"⚠️ bússola não registrou ({str(e)[:60]}) — sem impacto no chat", "cache")


def consultar(pergunta: str, owner: str, log=print) -> dict | None:
    """Melhor correspondência passada deste owner (ou None).

    Devolve {similaridade, resposta, sessao, titulo} — quem chama decide o
    faixa (DIRETO/SUGESTAO). Sem owner (CLI sem sessão) não filtra."""
    try:
        garantir()
        client = _client()
        vetor = rag.embeddings().embed_query(pergunta)
        filtros = []
        if owner:
            filtros.append(FieldCondition(key="metadata.owner",
                                          match=MatchValue(value=owner)))
        r = client.query_points(
            collection_name=COLECAO, query=vetor, limit=3, with_payload=True,
            query_filter=Filter(must=filtros) if filtros else None)
        for p in r.points:
            md = (p.payload or {}).get("metadata") or {}
            resposta = str(md.get("resposta") or "")
            if not resposta:
                continue
            return {"similaridade": round(float(p.score), 4),
                    "resposta": resposta,
                    "sessao": md.get("sessao", ""),
                    "titulo": (md.get("sessao") and _titulo_da_sessao(md["sessao"]))
                              or pergunta[:60],
                    "ts": md.get("ts", "")}
    except Exception as e:
        log(f"⚠️ bússola indisponível ({str(e)[:60]}) — seguindo sem ela", "cache")
    return None


def _titulo_da_sessao(sid: str) -> str:
    """Título da conversa de origem (para citar de onde veio a resposta)."""
    try:
        from . import sessions
        s = sessions.get_session(sid) or {}
        return str(s.get("titulo", ""))[:80]
    except Exception:
        return ""
