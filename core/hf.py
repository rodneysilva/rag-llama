"""HuggingFace Hub como FONTE DE CONHECIMENTO da ingestão.

O Hub é uma das melhores fontes de documentação viva de IA/ML: cada dataset
tem um CARD (README.md em markdown) com descrição, uso, licença e exemplos.
Este módulo busca datasets pela REST pública e devolve os cards como
Documentos — que entram NO MESMO pipeline de higienização da ingestão
(`core/limpeza` + `e_lixo` + chunks contextuais). Nada de texto quebrado.

`HF_TOKEN` opcional no .env (rate-limit maior); sem token, a API pública
serve. Papers (hf papers, também markdown) ficam expostos para o motor de
pesquisa profunda (F4) usar depois.
"""
from pathlib import Path

import httpx
from langchain_core.documents import Document

from . import config

_API = "https://huggingface.co/api"
_TIMEOUT = 30


def _headers() -> dict:
    token = str(getattr(config, "HF_TOKEN", "") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def populares(limite: int = 12, log=print) -> list[dict]:
    """Datasets MAIS BAIXADOS do Hub (o "vitrine" — q vazia na Biblioteca).
    Mesmo formato de buscar(): [{id, descricao, downloads, likes, url}]."""
    try:
        r = httpx.get(f"{_API}/datasets",
                      params={"sort": "downloads", "direction": -1,
                              "limit": limite},
                      headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return [_resumo(d) for d in r.json() if _resumo(d)][:limite]
    except Exception as e:
        log(f"⚠️ HF populares: {str(e)[:80]}")
        return []


def _resumo(d: dict) -> dict | None:
    """Item da API do Hub → formato da UI (sem campos desnecessários)."""
    rid = d.get("id") or ""
    if not rid:
        return None
    return {"id": rid,
            "descricao": (d.get("description") or d.get("cardData", {})
                          .get("pretty_name") or "")[:200],
            "downloads": d.get("downloads") or 0,
            "likes": d.get("likes") or 0,
            "url": f"https://huggingface.co/datasets/{rid}"}


def buscar(query: str, limite: int = 12, log=print) -> list[dict]:
    """Datasets do Hub por relevância/downloads: [{id, descricao, downloads,
    likes, url}]. Ordenado por downloads (mais usados = mais maduros).

    O `search` do Hub casa o ID do dataset (termo único): frase com
    espaços ou '+' retorna 0. Estratégia: tenta a frase (às vezes casa),
    senão cai para o TERMO PRINCIPAL (maior palavra sem stopword) — e se
    ainda 0, para a primeira palavra >3 chars.

    BUSCA EM INGLÊS NEUTRO (pedido do dono): query em qualquer idioma é
    normalizada antes (os IDs/descrições do Hub são do idioma mundial)."""
    from . import idioma
    query = idioma.para_busca_inglesa(query, log=log)
    # linguagens com simbolo: o search do Hub nao acha "c#"/"c++"
    # literal — mapeia para o nome que o Hub usa (csharp/cpp/fsharp)
    _alias = {"c#": "csharp", "c++": "cpp", "f#": "fsharp",
              "c sharp": "csharp", "c plus plus": "cpp",
              "objective-c": "objectivec", "node.js": "nodejs"}
    ql = query.strip().lower()
    if ql in _alias:
        query = _alias[ql]
    import re as _re
    query = _re.sub(r"[^\w\s+-]", " ", query)   # c# residual -> "c "
    termos = [t for t in query.split() if len(t) > 2]
    # ordem de queda: a FRASE (às vezes casa) → o 1º TERMO (geralmente o
    # distintivo: "retrieval" em "retrieval augmented generation") → o
    # termo mais longo como última carta
    candidatas = [query.strip()] if " " in query.strip() else []
    if termos:
        candidatas += [termos[0],
                       max(termos, key=len)]
    candidatas = list(dict.fromkeys(candidatas)) or [query.strip()]
    for tentativa in candidatas:
        r = httpx.get(
            f"{_API}/datasets",
            params={"search": tentativa, "sort": "downloads", "direction": -1,
                    "limit": max(1, min(limite, 50))},
            headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        achados = []
        for d in r.json():
            achados.append({
                "id": d.get("id", ""),
                "descricao": (d.get("description") or "")[:300],
                "downloads": d.get("downloads") or 0,
                "likes": d.get("likes") or 0,
                "url": f"https://huggingface.co/datasets/{d.get('id', '')}",
            })
        if achados:
            log(f"   🤗 {len(achados)} dataset(s) para “{query}” "
                f"(busca: “{tentativa}”)")
            return achados
    log(f"   🤗 0 dataset(s) para “{query}” (tentativas: {candidatas})")
    return []


def card(dataset_id: str, log=print) -> dict:
    """CARD (README.md) de UM dataset como Document — markdown puro, o
    formato que o pipeline de limpeza melhor mastiga. Devolve {} se o
    dataset não tiver card público."""
    try:
        r = httpx.get(f"https://huggingface.co/datasets/{dataset_id}/raw/main/README.md",
                      headers=_headers(), timeout=_TIMEOUT, follow_redirects=True)
        if r.status_code != 200 or not r.text.strip():
            return {}
        # corta o front-matter yaml (metadados) — o corpo é o conteúdo
        texto = r.text
        if texto.startswith("---"):
            fim = texto.find("\n---", 3)
            if fim > 0:
                texto = texto[fim + 4:]
        return {
            "doc": Document(
                page_content=texto,
                metadata={"source": f"huggingface.co/datasets/{dataset_id}",
                          "titulo": dataset_id.split("/")[-1],
                          # arquivo ÚNICO por card: sem isto o _dividir agrupa
                          # todos os chunks em "?" e os i/n saem globais
                          "arquivo": dataset_id.replace("/", "_") + ".md",
                          "url": f"https://huggingface.co/datasets/{dataset_id}"}),
            "bytes": len(texto),
        }
    except Exception as e:
        log(f"   ⚠️ card de {dataset_id}: {e}")
        return {}


_ROWS = "https://datasets-server.huggingface.co"
_LINHAS_POR_DOC = 25          # linhas agrupadas por Document (chunk context)
_CAMPO_MAX = 600              # corta valores gigantes (html/base64) por campo


def dados(dataset_id: str, max_linhas: int = 5000, log=print) -> list:
    """As LINHAS do dataset (datasets-server do Hub: /splits → /rows
    paginado) como Documents — é o CONTEÚDO, não só o card. Um Document a
    cada _LINHAS_POR_DOC linhas ("campo: valor" por linha, campos
    escalares; valores cortados em _CAMPO_MAX). Datasets sem server
    público (gated/erros) devolvem [] com log claro — o card segue."""
    try:
        r = httpx.get(f"{_ROWS}/splits", params={"dataset": dataset_id},
                      headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        splits = r.json().get("splits") or []
    except Exception as e:
        log(f"   ⚠️ {dataset_id}: sem dados públicos ({str(e)[:80]}) — só o card")
        return []
    # preferência: split de treino, 1º config alfabético
    alvo = next((s for s in splits if "train" in str(s.get("split", "")).lower()),
                splits[0] if splits else None)
    if not alvo:
        return []
    config, split = alvo.get("config"), alvo.get("split")
    docs, linhas, offset = [], 0, 0
    log(f"   🤗 baixando LINHAS de {dataset_id} ({config}/{split}, "
        f"máx {max_linhas})…")
    while linhas < max_linhas:
        try:
            r = httpx.get(f"{_ROWS}/rows",
                          params={"dataset": dataset_id, "config": config,
                                  "split": split, "offset": offset,
                                  "length": min(100, max_linhas - linhas)},
                          headers=_headers(), timeout=60)
            if r.status_code != 200:
                break
            rows = (r.json() or {}).get("rows") or []
        except Exception:
            break
        if not rows:
            break
        for i in range(0, len(rows), _LINHAS_POR_DOC):
            lote = rows[i:i + _LINHAS_POR_DOC]
            texto = [f"# {dataset_id} · {config}/{split} "
                     f"(linhas {offset + i + 1}–{offset + i + len(lote)})"]
            for n, row in enumerate(lote):
                campos = []
                for k, v in (row.get("row") or {}).items():
                    if v is None:
                        continue
                    valor = v if isinstance(v, (str, int, float, bool)) else (
                        ", ".join(str(x) for x in v[:8]) if isinstance(v, list)
                        else str(v))
                    valor = str(valor).strip()
                    if valor:
                        campos.append(f"{k}: {valor[:_CAMPO_MAX]}")
                if campos:
                    texto.append(f"[linha {offset + i + n + 1}] " + " | ".join(campos))
            if len(texto) > 1:
                docs.append(Document(
                    page_content="\n".join(texto),
                    metadata={
                        "source": f"huggingface.co/datasets/{dataset_id}",
                        "titulo": f"{dataset_id.split('/')[-1]} · dados",
                        "arquivo": dataset_id.replace("/", "_") + "_dados.md",
                        "url": f"https://huggingface.co/datasets/{dataset_id}",
                    }))
        linhas += len(rows)
        offset += len(rows)
    log(f"   🤗 {linhas} linha(s) → {len(docs)} documento(s) de dados")
    return docs


def ingest_hf(query: str, colecao: str | None = None, limite: int = 12,
              log=print) -> dict:
    """Busca datasets no Hub e ingere CARDS + DADOS (datasets-server) na
    coleção (default: slug da query). Mesmo pipeline de qualquer ingestão
    — wizard, limpeza, chunks contextuais, catálogo."""
    from . import ingest as _ingest  # import tardio (evita ciclo)
    achados = buscar(query, limite, log=log)
    if not achados:
        raise RuntimeError(f"nenhum dataset no Hub para “{query}”")
    alvo = colecao or query.strip().lower().replace(" ", "_")[:40] or "huggingface"
    docs, baixados = [], 0
    log(f"📥 baixando cards (README) + DADOS dos {len(achados)} mais usados…")
    for a in achados:
        c = card(a["id"], log=log)
        if c:
            c["doc"].metadata["downloads"] = a["downloads"]
            docs.append(c["doc"])
            baixados += 1
        # DADOS de verdade (linhas) — o card sozinho NÃO é "o dataset"
        docs.extend(dados(a["id"], log=log))
    if not docs:
        raise RuntimeError("nenhum card público entre os datasets encontrados")
    # reutiliza o wizard de ingestão sobre os docs já em memória
    resultado = _ingest.ingest_docs(docs, alvo, log=log)
    resultado["fonte"] = "huggingface"
    resultado["datasets"] = [a["id"] for a in achados]
    return resultado
