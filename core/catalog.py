"""
Catálogo de coleções: guarda numa coleção própria do Qdrant (meta_colecoes)
a categoria e a descrição em português de cada coleção, além das specs do
sistema — tudo com vetor de embedding, ou seja, consultável como RAG.
"""
import json
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import rag
from .specs import all_specs, spec

CATALOG_COLLECTION = "meta_colecoes"


def _id(chave: str) -> str:
    """ID determinístico: mesma chave = mesmo ponto (re-analisar atualiza)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rag-local:{chave}"))


def ensure(client: QdrantClient):
    """Cria a coleção do catálogo se ainda não existir."""
    if not client.collection_exists(CATALOG_COLLECTION):
        dim = len(rag.embeddings().embed_query("dimensão"))
        client.create_collection(
            collection_name=CATALOG_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"🆕 Catálogo '{CATALOG_COLLECTION}' criado (COSINE, dim={dim})")


def save(client, chave: str, texto: str, tipo: str, **extra):
    """Salva/atualiza um ponto do catálogo (coleção ou spec)."""
    ensure(client)
    client.upsert(
        collection_name=CATALOG_COLLECTION,
        points=[PointStruct(
            id=_id(chave),
            vector=rag.embeddings().embed_query(texto[:1500]),
            # formato que o LangChain lê: texto em page_content, resto em metadata
            payload={"page_content": texto,
                     "metadata": {"tipo": tipo, "chave": chave,
                                  "source": f"catalogo:{chave}", **extra}},
        )],
    )


def save_collection(client, nome: str, categoria: str, descricao: str, area: str = "",
                    grupo: str = ""):
    """Registra os metadados de uma coleção (área + categoria + descricao em PT)."""
    save(client, f"colecao:{nome}", f"{nome} — {categoria}: {descricao}",
         tipo="colecao", nome=nome, categoria=categoria, descricao=descricao,
         **({"area": area} if area else {}),
         **({"grupo": grupo} if grupo else {}))


# REGRA FIXA (código, não LLM): programação = "Desenvolvimento" — sem
# subdivisões tipo "backend"/"Arquitetura de Software" que o modelo inventa.
# Linguagens, frameworks, ferramentas e arquitetura são TODOS desenvolvimento
# (rodeiro único em core/linguagens.py — antes era a 3ª cópia divergente)
from .linguagens import EH_DEV as _DEV_FIXO


def _eh_dev(nome: str) -> bool:
    n = (nome or "").lower()
    return n in _DEV_FIXO or n.startswith(("docs_", "linguagem_", "framework_"))


def agrupar(client: QdrantClient, log=print) -> dict:
    """Atribui um GRUPO temático a cada coleção (spec agrupamento.md) —
    coleções com o mesmo objetivo ficam juntas na webui e no chat.
    Programação é FORÇADA para "Desenvolvimento" (regra fixa em código:
    a LLM de 7B insistia em subdividir apesar da spec)."""
    nomes = []
    for c in client.get_collections().collections:
        if c.name == CATALOG_COLLECTION or c.name.startswith("mnemosyne_"):
            continue
        nomes.append(c.name)
    if not nomes:
        return {"grupos": {}}
    meta = list_meta(client)
    lista = "\n".join(
        f"- {n} · categoria: {meta.get(n, {}).get('categoria') or '?'} · "
        f"descrição: {(meta.get(n, {}).get('descricao') or '?')[:100]}"
        for n in nomes)
    r = rag.llm(temperature=0.0).invoke(
        f"{spec('agrupamento')}\n\nColeções:\n{lista}\n\nETAPA: agrupamento.")
    ini, fim = r.content.find("{"), r.content.rfind("}")
    grupos: dict[str, str] = {}
    if ini >= 0:
        try:
            d = json.loads(r.content[ini:fim + 1])
            grupos = {str(k): str(v)[:40] for k, v in (d.get("grupos") or {}).items()
                      if k in nomes and v}
        except Exception:
            grupos = {}
    for n in nomes:
        g = grupos.get(n, "")
        if _eh_dev(n):  # regra fixa vence a LLM
            g = "Desenvolvimento"
            grupos[n] = g
        m = meta.get(n, {})
        if g or m:
            save_collection(client, n, m.get("categoria", ""), m.get("descricao", ""),
                            area=m.get("area", ""), grupo=g)
        log(f"   {n} → {g or '(sem grupo)'}")
    log(f"🗂️  {sum(1 for v in grupos.values() if v)} de {len(nomes)} coleção(ões) agrupada(s)")
    return {"grupos": grupos}


def save_specs(client):
    """Registra as specs do core como documentos RAG no catálogo."""
    specs = all_specs()
    for nome, conteudo in specs.items():
        save(client, f"spec:{nome}", f"[spec {nome}] {conteudo}", tipo="spec", nome=nome)
    print(f"📜 {len(specs)} specs registradas no catálogo ('{CATALOG_COLLECTION}')")


def remove_collection_meta(client, nome: str):
    """Apaga a entrada de uma coleção do catálogo."""
    try:
        client.delete(collection_name=CATALOG_COLLECTION,
                      points_selector=[_id(f"colecao:{nome}")])
    except Exception:
        pass


def list_meta(client) -> dict:
    """Lê o catálogo: {nome_da_colecao: {area, categoria, descricao}}.

    Scroll PAGINADO até o fim — antes limit=256 único truncava SILENCIOSAMENT
    (catálogo grande = roteador do Auto e agrupamento trabalhavam cegos)."""
    meta = {}
    try:
        offset = None
        while True:
            pontos, offset = client.scroll(
                collection_name=CATALOG_COLLECTION, limit=256,
                with_payload=True, offset=offset)
            for p in pontos:
                pl = p.payload or {}
                md = pl.get("metadata", pl)  # aceita formato novo (aninhado) e antigo (plano)
                if md.get("tipo") == "colecao":
                    meta[md.get("nome", "?")] = {
                        "area": md.get("area", ""),
                        "grupo": md.get("grupo", ""),
                        "categoria": md.get("categoria", ""),
                        "descricao": md.get("descricao", ""),
                    }
            if offset is None:
                break
    except Exception:
        pass  # catálogo ainda não criado
    return meta
