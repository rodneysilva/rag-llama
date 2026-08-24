"""Motor de PESQUISA PROFUNDA com evidências (Fase B / F4).

O anti-snippet: o resultado de busca NÃO é conhecimento — aqui a página é
BAIXADA inteira, o texto extraído com estrutura (Trafilatura, via seed) e a
LLM trabalha em funções específicas com contrato:

  PLANNER  (spec pesquisa_planner) → JSON {escopo, consultas, frescor}
  BUSCA    Serper + DuckDuckGo + Wikipedia API + README de repos GitHub
  FETCH    página/README → markdown (nunca só título+snippet)
  CLAIMS   (spec evidencia) → JSON de afirmações com evidência por documento
  SÍNTESE  (spec sintese) → documento consolidado COM citações [Fn] e
           conflitos declarados (quem diz o quê)

O BUDGET é do código, não da LLM: máx MAX_CONSULTAS queries, `fontes`
documentos, MAX_DOCS_CLAIMS extrações de claims, 1 síntese.

Tudo termina no MODO REVISÃO (core/preview): quem chama recebe os Documents
(fonte + síntese) e o `preview.analisar` monta o relatório "como veio/como
vai entrar" — NADA entra no Qdrant sem aprovação.
"""
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from . import config, rag
from .seed import UA, _baixar_html, _duckduckgo, _html_texto, _serper

MAX_CONSULTAS = 6      # teto de queries do plano (o planner sugere, o código corta)
MAX_DOCS_CLAIMS = 5    # documentos que passam por extração de claims
MIN_TEXTO = 500        # abaixo disto a página não vira documento (igual seed)
TIMEOUT = 20


# ---------- 1) PLANNER ----------

def _plano(assunto: str, log=print) -> dict:
    """Plano de investigação via LLM (spec) com fallback determinístico."""
    fallback = {
        "escopo": assunto,
        "consultas": [assunto,
                      f"what is {assunto}",
                      f"{assunto} tutorial",
                      f"{assunto} best practices"],
        "frescor": "historico",
    }
    try:
        plano = rag._ask_json("pesquisa_planner", assunto)
    except Exception as e:
        log(f"   ⚠️ planner indisponível ({e}) — usando plano padrão")
        return fallback
    consultas = [str(q)[:120] for q in plano.get("consultas") or []
                 if str(q).strip()][:MAX_CONSULTAS]
    if not consultas:
        return fallback
    return {"escopo": str(plano.get("escopo") or assunto)[:300],
            "consultas": consultas,
            "frescor": str(plano.get("frescor") or "historico")}


# ---------- 2) FONTES (busca) ----------

def _wikipedia(consultas: list[str], por_query: int = 3,
               log=print) -> list[dict]:
    """Wikipedia é fonte ESTRUTURADA: busca en → pt (as consultas do planner
    são SEMPRE em inglês — pt primeiro trazia páginas irrelevantes:
    "tratamento de exceção" para uma query de asyncio)."""
    achados: list[dict] = []
    for consulta in consultas[:2]:
        for lang in ("en", "pt"):
            try:
                r = httpx.get(f"https://{lang}.wikipedia.org/w/api.php",
                              params={"action": "query", "list": "search",
                                      "srsearch": consulta, "srlimit": por_query,
                                      "format": "json"},
                              headers={"User-Agent": UA}, timeout=TIMEOUT)
                for s in r.json().get("query", {}).get("search", []):
                    achados.append({
                        "titulo": s.get("title", ""),
                        "link": f"https://{lang}.wikipedia.org/wiki/"
                                f"{quote(s.get('title', '').replace(' ', '_'))}",
                    })
                break  # achou no idioma: não tenta o próximo
            except Exception:
                continue
    return achados


def _github_readme(url: str) -> str | None:
    """README.md cru de um repo (main→master) — markdown já estruturado.
    Front-matter Jekyll (--- … ---) sai FORA: é configuração do site, não
    conteúdo (vazava "{:.no_toc}" como título do documento na Revisão)."""
    m = re.match(r"(?:https?://)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
                 url.strip())
    if not m:
        return None
    dono, repo = m.group(1), m.group(2)
    for ramo in ("main", "master"):
        try:
            r = httpx.get(f"https://raw.githubusercontent.com/{dono}/{repo}/"
                          f"{ramo}/README.md",
                          headers={"User-Agent": UA}, timeout=TIMEOUT,
                          follow_redirects=True)
            if r.status_code == 200 and len(r.text.strip()) >= MIN_TEXTO:
                return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", r.text,
                              count=1, flags=re.S)
        except Exception:
            continue
    return None


def _motores_gerais(consultas: list[str], log=print) -> list[dict]:
    """Serper (com chave) senao DuckDuckGo — rodada sequencial interna.
    Chamada EM PARALELO com a wikipedia por _candidatos."""
    web: list[dict] = []
    t0 = time.time()
    try:
        log(f"   🔎 serper: {len(consultas)} consulta(s)...")
        web = _serper(consultas, 6)
        log(f"   🔎 serper: {len(web)} resultado(s) ({time.time() - t0:.0f}s)")
    except Exception as e:
        web = []
        log(f"   🔎 serper indisponivel - usando DuckDuckGo")
    if not web:
        t0 = time.time()
        try:
            log(f"   🦆 duckduckgo: {len(consultas)} consulta(s) (pode levar ~1 min)...")
            web = _duckduckgo(consultas, 6)
            log(f"   🦆 duckduckgo: {len(web)} resultado(s) ({time.time() - t0:.0f}s)")
        except Exception:
            pass
    return web


def _candidatos(consultas: list[str], log=print) -> list[dict]:
    """União das fontes: wikipedia primeiro (estruturada), github README em
    seguida, depois os motores gerais (Serper se houver chave, senão DDG).

    LOG POR ETAPA com tempo: cada motor é uma rodada sequencial de consultas
    (até ~20 s cada) — sem essas linhas o job fica MINUTOS em silêncio e
    parece travado (foi exatamente o que o operador viu)."""
    # ================================================================
    # DESCOBERTA PARALELA: wikipedia x motores gerais AO MESMO TEMPO
    # (eram sequenciais = soma dos tempos; agora = max dos dois).
    # ================================================================
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as _pool:
        _fut_wiki = _pool.submit(_wikipedia, consultas, log=log)
        _fut_web = _pool.submit(_motores_gerais, consultas, log=log)
    try:
        achados = _fut_wiki.result() or []
    except Exception:
        achados = []
    try:
        web = _fut_web.result() or []
    except Exception:
        web = []
    log(f"   📚 wikipedia: {len(achados)} pagina(s) ({time.time() - t0:.0f}s)")
    log(f"   🔎 motores gerais: {len(web)} resultado(s) ({time.time() - t0:.0f}s)")
    # github merece prioridade sobre blogs: sobe os repos para o topo do web
    repos = [w for w in web if "github.com" in w.get("link", "")]
    resto = [w for w in web if "github.com" not in w.get("link", "")]
    achados += repos + resto
    # dedupe por URL normalizada
    vistos, unicos = set(), []
    for a in achados:
        chave = a.get("link", "").rstrip("/").lower()
        if chave and chave not in vistos:
            vistos.add(chave)
            unicos.append(a)
    return unicos


# ---------- 3) FETCH (página inteira, não snippet) ----------

def _revisao_wikipedia(url: str) -> str | None:
    """Data da ÚLTIMA revisão do artigo (proveniência temporal — a Wikipedia
    versiona; o chunk saber 'de quando' é o dado faltante)."""
    m = re.search(r"wikipedia\.org/wiki/([^?#]+)", url)
    if not m:
        return None
    lang = url.split("//")[1].split(".")[0] if "//" in url else "en"
    try:
        r = httpx.get(f"https://{lang}.wikipedia.org/w/api.php",
                      params={"action": "query", "prop": "revisions",
                              "rvprop": "timestamp", "titles": m.group(1),
                              "format": "json", "redirects": 1},
                      headers={"User-Agent": UA}, timeout=TIMEOUT)
        for pg in r.json().get("query", {}).get("pages", {}).values():
            revs = pg.get("revisions") or []
            if revs and revs[0].get("timestamp"):
                return str(revs[0]["timestamp"])[:10]
    except Exception:
        pass
    return None


def _baixar(url: str, titulo: str, log=print) -> Document | None:
    """Baixa a FONTE e extrai com estrutura. GitHub → README cru; demais →
    HTML → Trafilatura → markdown (o caminho do seed). Wikipedia ganha a
    data da última revisão no metadata (proveniência temporal)."""
    try:
        readme = _github_readme(url)
        if readme:
            texto = readme
        else:
            html = _baixar_html(url)
            if not html:
                return None
            texto = _html_texto(html)  # trafilatura → markdown → limpeza
        if not texto or len(texto) < MIN_TEXTO:
            return None
        titulo = titulo or Path(re.sub(r"[?#].*$", "", url)).stem or url
        md = {"source": url, "url": url, "titulo": titulo[:120]}
        if "wikipedia.org/wiki/" in url:
            rev = _revisao_wikipedia(url)
            if rev:
                md["revisado_em"] = rev
        return Document(page_content=texto, metadata=md)
    except Exception as e:
        log(f"   ⚠️ {url[:70]}: {str(e)[:60]}")
        return None


# ---------- 4) CLAIMS + SÍNTESE ----------

_ANO = re.compile(r"\b(?:19|20)\d{2}\b")   # NÃO capturar: findall com grupo
_VERSAO = re.compile(r"\bv?\d+(?:\.\d+)+\b")  # devolvia "19" em vez de "2013"
_STOP = {"de", "da", "do", "the", "and", "was", "for", "with", "que", "com",
         "por", "uma", "um", "sua", "seu", "em", "no", "na", "of", "in",
         "to", "como", "pela", "para", "pelo", "aos", "das", "dos", "sao",
         "ser", "esta", "este", "isso", "mais", "muito", "each", "from"}


def _valores(texto: str) -> set[str]:
    """Números que carregam fato: anos e versões (fonte de conflito clássico)."""
    return set(_ANO.findall(texto)) | set(_VERSAO.findall(texto.lower()))


def _conflitos(claims_por_doc: list[tuple]) -> list[str]:
    """Conflito DETERMINÍSTICO entre fontes: claims de fontes DIFERENTES com
    tema parecido e valores numéricos distintos (ano/versão). A síntese
    declara; aqui o CÓDIGO detecta."""
    achados: list[str] = []
    planos = []  # (fonte_n, valores, tokens)
    for n, (_, claims) in enumerate(claims_por_doc, 1):
        for c in claims:
            valores = _valores(c["texto"])
            # tokens do TEMA: sem stopwords e SEM os próprios valores (o
            # tema é o assunto; os números são o que se compara)
            tokens = {t for t in re.split(r"\W+", c["texto"].lower())
                      if len(t) > 3 and t not in _STOP} - valores \
                - {t for t in re.split(r"\W+", c["texto"].lower())
                   if any(v in t for v in valores)}
            if not tokens:
                continue
            planos.append((n, valores, tokens, c["texto"][:80]))
    for i in range(len(planos)):
        for j in range(i + 1, len(planos)):
            n1, v1, t1, txt1 = planos[i]
            n2, v2, t2, txt2 = planos[j]
            if n1 == n2 or not v1 or not v2:
                continue  # conflito é ENTRE fontes, com valores presentes
            comum = t1 & t2
            # tema parecido: cobertura da lista MENOR (claims curtas com
            # 2 palavras-chave em comum já são o mesmo assunto)
            similar = len(comum) >= 2 and len(comum) / (min(len(t1), len(t2)) or 1) >= 0.5
            if similar and v1 != v2:
                achados.append(
                    f"F{n1} e F{n2} divergem (tema: {' '.join(sorted(comum))[:60]}): "
                    f"F{n1} diz {sorted(v1)} · F{n2} diz {sorted(v2)} — "
                    f"(“{txt1}” × “{txt2}”)")
    return achados[:6]  # teto: os primeiros são os mais óbvios


def _claims(doc: Document, log=print) -> list[dict]:
    """Afirmações factuais com evidência (spec evidencia) — a LLM lê o
    documento e devolve JSON atômico; falha vira lista vazia (não para)."""
    conteudo = (f"Fonte: {doc.metadata.get('titulo')} — "
                f"{doc.metadata.get('url')}\n\n{doc.page_content[:6000]}")
    try:
        r = rag._ask_json("evidencia", conteudo)
    except Exception as e:
        log(f"   ⚠️ claims de {doc.metadata.get('titulo', '?')[:40]}: {e}")
        return []
    claims = r.get("claims") or []
    return [{"texto": str(c.get("texto", ""))[:300],
             "evidencia": str(c.get("evidencia", ""))[:250],
             "confianca": float(c.get("confianca", 0.5))}
            for c in claims if c.get("texto")][:8]


def _sintese(assunto: str, docs: list[Document],
             claims_por_doc: list[tuple[Document, list[dict]]],
             log=print, conflitos: list[str] | None = None) -> str | None:
    """Documento consolidado COM citações [Fn] e conflitos declarados —
    inclusive os detectados deterministicamente pelo código (anos/versões)."""
    if not any(c for _, c in claims_por_doc):
        return None
    partes = [f"Assunto: {assunto}\n\nClaims extraídas por fonte:\n"]
    for n, (doc, claims) in enumerate(claims_por_doc, 1):
        rev = doc.metadata.get("revisado_em")
        partes.append(f"[F{n}] {doc.metadata.get('titulo')}"
                      + (f" (revisado em {rev})" if rev else "")
                      + f" — {doc.metadata.get('url')}\n")
        for c in claims:
            partes.append(f"- ({c['confianca']:.1f}) {c['texto']}\n"
                          f"  evidência: \"{c['evidencia']}\"\n")
        partes.append("\n")
    if conflitos:
        partes.append("CONFLITOS detectados pelo sistema (declare-os na seção "
                      "de conflitos, NÃO escolha um calado):\n")
        partes.extend(f"- {cx}\n" for cx in conflitos)
        partes.append("\n")
    prompt = ChatPromptTemplate.from_messages([("system", "{spec}"),
                                              ("human", "{conteudo}")])
    return (prompt | rag.llm() | StrOutputParser()).invoke(
        {"spec": rag.spec("sintese"), "conteudo": "".join(partes)})


# ---------- pipeline completo ----------

REDUNDANTE_LOTE = 0.92   # cosseno ≥ vs doc já aceito NO LOTE: é a mesma coisa
REDUNDANTE_IDX = 0.95    # cosseno ≥ vs índice da coleção-alvo: já indexado


def _coss(a, b) -> float:
    na = (sum(x * x for x in a) ** 0.5) or 1.0
    nb = (sum(x * x for x in b) ** 0.5) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def pesquisar(assunto: str, fontes: int = 8, log=print,
              colecao_alvo: str | None = None) -> tuple[list, dict]:
    """Pipeline completo → (Documents prontos para o MODO REVISÃO, resumo).

    Os documentos são as FONTES baixadas + 1 síntese com citações; quem
    chama encaminha ao `preview.analisar` — o Qdrant só vê o aprovado.

    FILTRO INCREMENTAL (F5 — "embeda a cada iteração, não no final"): cada
    página aceita é embedada NA HORA e comparada (a) com as já aceitas no
    lote (≥0.92 = redundante) e (b) com o ÍNDICE da `colecao_alvo` quando
    informada (≥0.95 = já está na base) — redundante não ocupa vaga de
    fonte nem chega à Revisão. O que já foi aceito está APROVEITÁVEL se o
    processo parar no meio.

    O resumo traz o `ki` (Knowledge Item): fontes com URL/revisão/claims,
    conflitos e consultas — gravado no metadata do doc de síntese pela API
    (a resposta do chat passa a apontar chunk→doc→fonte→data)."""
    log = log or print
    assunto = assunto.strip()
    if not assunto:
        raise ValueError("informe o assunto da pesquisa")
    # 🌐 BUSCA NO IDIOMA MUNDIAL (pedido do dono): o usuário digita em
    # qualquer idioma, mas a pesquisa roda em INGLÊS NEUTRO — sem viés
    # regional (a região só entra quando É o assunto)
    from . import idioma
    assunto = idioma.para_busca_inglesa(assunto, log=log)
    fontes = max(3, min(int(fontes or 8), 12))

    # ================================================================
    # CRONOMETRO visivel: cada fase aparece com o tempo acumulado —
    # a pesquisa nunca mais parece travada em silencio.
    # ================================================================
    _T0 = time.time()
    def _fase(nome: str):
        log(f"⏱️ [{time.time() - _T0:5.1f}s] {nome}")

    _fase(f"pesquisa iniciada · alvo: {fontes} fonte(s)")
    _fase("planejando (a LLM escreve escopo e consultas)…")
    plano = _plano(assunto, log)
    _fase(f"escopo: {plano['escopo']}")
    _fase(f"consultas: {', '.join(plano['consultas'])}")

    log("🔎 BUSCANDO fontes (wikipedia + motores + repos)…")
    candidatos = _candidatos(plano["consultas"], log=log)
    log(f"   {len(candidatos)} candidato(s) único(s) por URL")

    # filtro incremental precisa de embedding — sobe on-demand (idempotente)
    emb = rag.embeddings()
    client = None
    if colecao_alvo:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                              check_compatibility=False)
        if not client.collection_exists(colecao_alvo):
            colecao_alvo = None  # alvo inexistente: filtro só intra-lote
            client = None

    log(f"📥 BAIXANDO até {fontes} página(s) INTEIRA(S) (nunca snippet)…")
    docs: list[Document] = []
    vetores: list[list[float]] = []
    descartadas = 0
    # ═══ DOWNLOAD PARALELO (era sequencial: 8 fontes x 2-20s = minutos).
    # Baixa em paralelo os primeiros candidatos (margem p/ descartes de
    # redundância) e O LOOP ABAIXO só filtra — rede fora do caminho crítico.
    from concurrent.futures import ThreadPoolExecutor
    _margem = min(len(candidatos), fontes * 2, 16)
    _t0dl = time.time()
    with ThreadPoolExecutor(max_workers=6) as _pool:
        _baixados = list(_pool.map(
            lambda c: _baixar(c.get("link", ""), c.get("titulo", ""), log=log),
            candidatos[:_margem]))
    _baixados = [d for d in _baixados if d]
    log(f"   ⚡ {_margem} download(s) em paralelo: {len(_baixados)} página(s) "
        f"válida(s) em {time.time() - _t0dl:.0f}s")
    for doc in _baixados:
        if len(docs) >= fontes:
            break
        # (a) quase-duplicada de algo JÁ ACEITO neste lote
        vetor = emb.embed_query(doc.page_content[:4000])
        dup = next((d for d, v in zip(docs, vetores)
                    if _coss(vetor, v) >= REDUNDANTE_LOTE), None)
        if dup is not None:
            descartadas += 1
            log(f"   ♻️ redundante com “{dup.metadata['titulo'][:40]}” — descartada")
            continue
        # (b) redundante com o que JÁ ESTÁ NO ÍNDICE da coleção-alvo
        if client is not None:
            try:
                r = client.query_points(collection_name=colecao_alvo,
                                        query=vetor, limit=1, with_payload=False)
                if r.points and float(r.points[0].score) >= REDUNDANTE_IDX:
                    existente = (r.points[0].payload or {}).get("metadata", {})
                    descartadas += 1
                    log(f"   ♻️ já indexada em '{colecao_alvo}' "
                        f"(“{str(existente.get('titulo'))[:40]}”) — descartada")
                    continue
            except Exception as e:
                log(f"   ⚠️ filtro contra índice falhou ({str(e)[:50]}) — segue")
        docs.append(doc)
        vetores.append(vetor)
        log(f"   ✅ {doc.metadata['titulo'][:60]} "
            f"({len(doc.page_content) // 1000} kB)")
    if not docs:
        raise RuntimeError("nenhuma fonte pôde ser baixada — tente outro "
                           "assunto ou verifique a conectividade")
    if descartadas:
        log(f"   filtro incremental: {descartadas} redundante(s) fora "
            "(embedada na hora, comparada com lote e índice)")

    log(f"🔬 EXTRANDO evidências (claims) dos {min(len(docs), MAX_DOCS_CLAIMS)} "
        "documento(s) principais…")
    claims_por_doc: list[tuple[Document, list[dict]]] = []
    total_claims = 0
    for doc in docs[:MAX_DOCS_CLAIMS]:
        claims = _claims(doc, log)
        claims_por_doc.append((doc, claims))
        total_claims += len(claims)
        log(f"   🔎 {doc.metadata['titulo'][:50]}: {len(claims)} claim(s)")

    log("⚔️ CONFERINDO conflitos determinísticos entre as fontes (anos/versões)…")
    conflitos = _conflitos(claims_por_doc)
    for cx in conflitos:
        log(f"   ⚔️ {cx[:120]}")
    if not conflitos:
        log("   ✅ nenhum conflito numérico óbvio entre as fontes")

    log("✍️ SINTETIZANDO o documento consolidado (citações [Fn] + conflitos)…")
    sintese = _sintese(assunto, docs, claims_por_doc, log, conflitos=conflitos)
    if sintese:
        docs.append(Document(
            page_content=sintese,
            metadata={"source": f"pesquisa:{assunto[:60]}",
                      "url": None, "titulo": f"{assunto} — síntese com fontes",
                      "sintese": True}))
        log("   ✅ síntese pronta (com citações e seção de conflitos)")

    # 📦 Knowledge Item (F5): a ficha estruturada da aquisição — viaja no
    # resumo (job), no metadata do doc de síntese (Qdrant) e na auditoria
    ki = {
        "gerado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
        "escopo": plano["escopo"],
        "consultas": plano["consultas"],
        "frescor": plano["frescor"],
        "fontes": [{
            "titulo": (doc.metadata.get("titulo") or "")[:120],
            "url": doc.metadata.get("url"),
            "revisado_em": doc.metadata.get("revisado_em"),
            "claims": len(claims),
        } for doc, claims in claims_por_doc],
        "conflitos": conflitos,
        "colecao_alvo": colecao_alvo,
    }
    resumo = {"escopo": plano["escopo"], "frescor": plano["frescor"],
              "consultas": plano["consultas"], "fontes_baixadas": len(docs) - (1 if sintese else 0),
              "claims": total_claims, "sintese": bool(sintese),
              "redundantes_descartadas": descartadas, "ki": ki}
    return docs, resumo
