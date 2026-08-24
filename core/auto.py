"""
Modo Auto: RAG com poder de decisão (roteador → recuperação → crítica →
fallback web).

Desenho inspirado em Adaptive RAG + Corrective RAG (CRAG), adaptado ao que
cabe num 7B local: um roteador LLM (specs/roteador.md) decide POR PERGUNTA a
estratégia (base local / busca na web / só modelo), escolhe as coleções
certas pelo catálogo e escreve a consulta de busca.

WEB-FIRST (spec pesquisa_web.md): quando a rota é a web, a primeira onda é
DuckDuckGo (sem custo); a crítica avalia a qualidade e, se não sustenta,
APROFUNDA até 5 níveis — consulta refinada pela LLM a cada nível — antes de
desistir. Serper entra como respaldo (informação mais atual) quando o DDG
não traz nada. Nada é ingerido: são fragmentos efêmeros marcados 🌐 web.
"""
from langchain_core.documents import Document

from . import catalog, config, contadores, rag
from .linguagens import LINGUAGENS
from .seed import _serper, _duckduckgo
from .specs import spec

SCORE_MIN_FORTE = 0.45  # crítica: abaixo disso a recuperação não sustenta
NIVEIS_WEB_MAX = 5      # aprofundamento da busca web (níveis de refinamento)


def _decide(question, history, meta, log=None) -> dict:
    """Roteador LLM: {acao, colecoes, consulta, motivo} (spec roteador.md)."""
    log = log or (lambda m, g="auto": None)
    linhas = "\n".join(
        f"- {nome} [{m.get('area') or m.get('categoria', '')}]: "
        f"{m.get('descricao', '')[:100]}"
        for nome, m in sorted(meta.items()))
    historico = "\n".join(
        f"{m.get('role')}: {str(m.get('content', ''))[:150]}"
        for m in (history or [])[-4:])
    prompt = (f"{spec('roteador')}\n\nCATÁLOGO de coleções:\n{linhas or '(vazio)'}\n\n"
              f"Histórico recente:\n{historico or '(sem histórico)'}\n\n"
              f"Pergunta do usuário: {question}\n\nETAPA: decisão de rota.")
    log("roteador LLM decidindo a rota…", "auto")
    contadores.set_etapa("roteador (auto)")
    r = rag.llm(temperature=0).invoke(prompt)
    contadores.set_etapa(None)
    d = rag._extract_json(r.content)
    acao = d.get("acao") if d.get("acao") in ("base", "web", "livre") else "base"
    colecoes = [c for c in (d.get("colecoes") or []) if c in meta]
    consulta = str(d.get("consulta") or question).strip() or question
    log(f"decisão: {acao} — {str(d.get('motivo', ''))[:100]}", "auto")
    return {"acao": acao, "colecoes": colecoes, "consulta": consulta,
            "motivo": str(d.get("motivo", ""))[:160]}


def _web_docs(consulta: str, n: int = 6, log=None) -> list[Document]:
    """Resultados da web como documentos efêmeros (não vão para o Qdrant).

    DuckDuckGo PRIMEIRO (sem custo/chave); Serper como respaldo quando o
    DDG não traz nada ou falha (informação mais atual)."""
    log = log or (lambda m, g="web": None)
    docs: list[Document] = []
    for r in _duckduckgo([consulta])[:n]:
        docs.append(Document(
            page_content=f"{r['titulo']} — {r.get('resumo', '') or r['titulo']}",
            metadata={"source": r["link"], "colecao": "🌐 web", "area": "web",
                      "descricao": r.get("titulo", "")[:120]}))
    if docs:
        log(f"🌐 DuckDuckGo: {len(docs)} resultado(s) para “{consulta[:60]}”", "web")
        return docs
    try:  # DDG vazio/falhou → Serper (mais atual, exige chave)
        for r in _serper([consulta], por_query=n)[:n]:
            docs.append(Document(
                page_content=f"{r['titulo']} — {r['resumo']}",
                metadata={"source": r["link"], "colecao": "🌐 web", "area": "web",
                          "descricao": r.get("titulo", "")[:120]}))
        if docs:
            log(f"🌐 Serper (respaldo): {len(docs)} resultado(s)", "web")
    except Exception as e:
        log(f"⚠️ Serper falhou: {str(e)[:80]}", "web")
    return docs


def _refinar_consulta(pergunta: str, consulta: str, resultados: list[Document]) -> str:
    """A LLM olha o que JÁ veio e escreve a próxima consulta (ângulo novo)."""
    amostra = "\n".join(f"- {d.page_content[:100]}" for d in resultados[:6])
    contadores.set_etapa("refinamento da consulta (web)")
    r = rag.llm(temperature=0.2).invoke(
        f"{spec('pesquisa_web')}\n\nPergunta original: {pergunta}\n"
        f"Consulta já feita: {consulta}\n\nResultados obtidos:\n{amostra}\n\n"
        "ETAPA: refinamento — escreva UMA query NOVA (mais específica, inglês) "
        "para o ângulo que ainda não foi coberto. Responda só a query.")
    contadores.set_etapa(None)
    texto = r.content.strip().strip('"').splitlines()[0][:160]
    return texto or consulta


def _web_aprofundado(pergunta: str, consulta: str, log=None) -> list[Document]:
    """Busca web com PÁGINAS REAIS (Fase B+1 — o fim do snippet).

    A cada nível: busca URLs (DuckDuckGo primeiro, Serper de respaldo) e
    BAIXA a página inteira (Trafilatura/README via core.pesquisa._baixar) —
    o contexto da resposta passa a ser conteúdo de verdade, com URL de
    proveniência, não "título — snippet". A LLM só escreve a consulta de
    aprofundamento; a crítica de suficiência é por MATERIAL BAIXADO."""
    from .pesquisa import _baixar as _baixar_pagina  # tardio: evita ciclo
    log = log or (lambda m, g="web": None)
    MAX_PAGINAS = 3          # páginas inteiras bastam (contexto do chat é 6k)
    CAP_PAGINA = 4000        # corta a página p/ caber no prompt com folga
    baixadas: dict[str, Document] = {}
    titulos: list[str] = []
    consulta_atual = consulta
    for nivel in range(1, NIVEIS_WEB_MAX + 1):
        lote = _web_docs(consulta_atual, n=6, log=log)
        for r in lote:
            if len(baixadas) >= MAX_PAGINAS:
                break
            url = r.metadata.get("source", "")
            titulo = r.metadata.get("descricao") or r.page_content[:80]
            if not url or url in baixadas:
                continue
            doc = _baixar_pagina(url, titulo, log=log)
            if doc is None:
                continue
            baixadas[url] = Document(
                page_content=doc.page_content[:CAP_PAGINA],
                metadata={"source": url, "url": url,
                          "titulo": doc.metadata.get("titulo", "")[:120],
                          "colecao": "🌐 web", "area": "web",
                          "descricao": str(titulo)[:120]})
            titulos.append(doc.metadata.get("titulo", url)[:60])
            log(f"📥 página baixada: {doc.metadata.get('titulo', url)[:60]} "
                f"({len(doc.page_content) // 1000} kB)", "web")
        log(f"🧭 nível {nivel}/{NIVEIS_WEB_MAX}: {len(baixadas)} página(s) "
            "inteira(s) baixada(s)", "web")
        if len(baixadas) >= 2:
            log("✅ material suficiente para responder (conteúdo real, não snippet)", "web")
            break
        if nivel < NIVEIS_WEB_MAX and lote:
            consulta_atual = _refinar_consulta(
                pergunta, consulta_atual,
                [Document(page_content=t) for t in titulos] or lote)
            log(f"🔎 aprofundando: “{consulta_atual[:80]}”", "web")
    if not baixadas:  # nada baixou (rede/bloqueio): cai nos snippets mesmo
        log("⚠️ nenhuma página pôde ser baixada — usando resultados brutos", "web")
        return _web_docs(consulta, n=6, log=log)
    return list(baixadas.values())


def _bases_header(meta, colecoes, com_web: bool) -> str | None:
    linhas = [
        f"- {n} ({m.get('area') or '?'} · {m.get('categoria', '')}): "
        f"{m.get('descricao', '')[:80]}"
        for n, m in meta.items() if n in colecoes]
    if com_web:
        linhas.append("- 🌐 web (DuckDuckGo/Serper): resultados de busca da "
                      "internet, fragmentos efêmeros — cite como web")
    return "Bases consultadas:\n" + "\n".join(linhas) if linhas else None


def responde_auto(client, question, history, log=None) -> dict:
    """Pipeline completo do modo Auto. Retorna {answer, found, decisao, consulta}."""
    log = log or (lambda m, g="auto": None)
    meta = catalog.list_meta(client)
    decisao = _decide(question, history, meta, log)
    print(f"🤖 Auto: {decisao['acao']} {decisao['colecoes']} — {decisao['motivo']}")

    if decisao["acao"] == "livre":
        log("✍️ gerando com o conhecimento do modelo…", "geração")
        contadores.set_etapa("resposta (auto/livre)")
        answer = rag.answer_free(question, history)
        contadores.set_etapa(None)
        return {"answer": answer, "found": [],
                "decisao": decisao, "consulta": decisao["consulta"]}

    docs, found, com_web = [], [], False
    if decisao["acao"] == "base":
        colecoes = decisao["colecoes"] or list(meta)
        # base unificada de arquitetura entra junto quando a busca toca em
        # coleções de linguagem de programação (mesma regra do /api/query —
        # agora o rodeiro de linguagens é ÚNICO: core/linguagens.py)
        if (any(c in LINGUAGENS for c in colecoes)
                and "arquitetura_unificada" not in colecoes):
            try:
                if client.collection_exists("arquitetura_unificada"):
                    colecoes.append("arquitetura_unificada")
            except Exception:
                pass
        achados, _ = rag.search(client, colecoes, decisao["consulta"],
                                log=log)
        docs = [d for d, _, _ in achados]
        found = [
            {"score": round(float(s), 4), "source": d.metadata.get("source"),
             "categoria": d.metadata.get("categoria"),
             "descricao": d.metadata.get("descricao"),
             "colecao": c, "content": d.page_content}
            for d, s, c in achados]
        # crítica (CRAG): recuperação fraca → completa com a web (aprofundada)
        top = max((f["score"] for f in found), default=0.0)
        if len(found) < 2 or top < SCORE_MIN_FORTE:
            log(f"🧐 crítica: recuperação fraca (top={top:.2f}) → web com "
                "aprofundamento", "auto")
            docs += _web_aprofundado(question, decisao["consulta"], log)
            com_web = True
    else:  # web direto: DuckDuckGo primeiro, aprofundando até dar bom
        docs = _web_aprofundado(question, decisao["consulta"], log)
        com_web = bool(docs)

    found += [{"score": 0.0, "source": d.metadata.get("source"),
               "categoria": None, "descricao": d.metadata.get("descricao"),
               "colecao": d.metadata.get("colecao"), "content": d.page_content}
              for d in docs if d.metadata.get("colecao") == "🌐 web"]

    log(f"✍️ gerando resposta ({len(docs)} fragmento(s), "
        f"{'com' if com_web else 'sem'} web)…", "geração")
    contadores.set_etapa("resposta (auto)")
    answer = rag.answer_hybrid(
        question, docs, history,
        _bases_header(meta, decisao.get("colecoes", []), com_web))
    contadores.set_etapa(None)

    exibe = dict(decisao)
    if com_web and decisao["acao"] == "base":
        exibe["acao"] += "+web"
    return {"answer": answer, "found": found, "decisao": exibe,
            "consulta": decisao["consulta"]}
