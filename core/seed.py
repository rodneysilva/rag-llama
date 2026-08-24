"""
Seed de coleção por assunto — pesquisa PROFUNDA com definição prévia da RAG.

Antes de importar qualquer coisa, a LLM escreve a DEFINIÇÃO da base (escopo,
tópicos, tipos de fonte) — é ela que guia: buscas em RODADAS (Serper e, como
respaldo sem chave, DuckDuckGo), curadoria com SCORES de relevância, a
escolha dos recursos internos de cada fonte aprovada e o clone esparso de
repos oficiais de documentação (mesmo modelo das bases .NET/Python). O texto
baixado é limpo (core.limpeza) antes de virar chunk.

CLI (a partir da raiz): python -X utf8 -m core.seed "angular" [colecao] [--fontes 12]
API: job em 2º plano — POST /api/seed → GET /api/seed/status/{job}?cursor=N
"""
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from qdrant_client import QdrantClient

from . import catalog, config, ingest, rag
from .limpeza import limpar_texto
from .specs import spec

SERPER_URL = "https://google.serper.dev/search"
MAX_BYTES = 3_000_000    # corta downloads gigantes
MAX_TEXTO = 120_000      # corta páginas enormes
MIN_TEXTO = 500          # menos que isso: página sem conteúdo útil
SEED_DIR = Path("datasets/seed")
RODADAS_MAX = 3          # buscas refinam até fechar o alvo de fontes
SCORE_MIN_FONTE = 6      # curadoria: abaixo disso a fonte não entra
SCORE_MIN_INTERNO = 7    # páginas internas: mais exigente que a fonte
INTERNOS_POR_FONTE = 3   # máx. de páginas internas por fonte aprovada
REPOS_MAX = 2            # máx. de repos oficiais clonados por seed
UA = "Mozilla/5.0 (rag-local seed)"


def _slug(texto: str) -> str:
    """Nome de coleção/pasta a partir do assunto (sem acentos, minúsculo)."""
    ascii_ = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_.lower()).strip("_") or "colecao"


def _json_da_resposta(resposta: str):
    """Primeiro array JSON válido da resposta (modelos emolduram texto)."""
    ini = resposta.find("[") if "[" in resposta else resposta.find("{")
    for fim in range(len(resposta), ini, -1):
        try:
            return json.loads(resposta[ini:fim])
        except Exception:
            continue
    return None


def _json_obj(resposta: str):
    """Primeiro objeto JSON válido da resposta ({ ... })."""
    ini, fim = resposta.find("{"), resposta.rfind("}")
    if ini < 0 or fim <= ini:
        return None
    try:
        return json.loads(resposta[ini:fim + 1])
    except Exception:
        return None


# ---------- 1) definição da RAG (informada por busca contextual) ----------

def _explorar(assunto: str, log) -> list[dict]:
    """Fase 0: busca contextual rápida (Serper → DuckDuckGo) para a definição
    da RAG ser INFORMADA pelo que existe de fato sobre o assunto."""
    try:
        cands = _serper([assunto], por_query=8)
    except Exception:
        cands = []
    if len(cands) < 6:
        try:
            cands += _duckduckgo([assunto])[:8 - len(cands)]
        except Exception:
            pass
    log(f"   🔭 busca contextual: {len(cands)} resultado(s) para informar a definição")
    return cands


def _definir_rag(assunto: str, exploratoria: list[dict], log) -> dict:
    """Passo 1 (spec seed.md): a LLM define o que a base É antes de buscar —
    em INGLÊS (input em qualquer idioma), informada pelos resultados da
    busca contextual."""
    contexto = ""
    if exploratoria:
        contexto = ("\n\nRESULTADOS DA BUSCA EXPLORATÓRIA (títulos | resumos — use para "
                    "escrever uma definição INFORMADA do que existe sobre o assunto):\n"
                    + "\n".join(f"- {c['titulo'][:80]} | {c.get('resumo', '')[:100]}"
                                for c in exploratoria[:10]))
    r = rag.llm(temperature=0.2).invoke(
        f"{spec('seed')}\n\nAssunto da nova coleção: {assunto}\n{contexto}\n\n"
        "ETAPA: 1 (definição da RAG — em inglês).")
    d = _json_obj(r.content) or {}
    queries = [q.strip() for q in (d.get("queries") or [])
               if isinstance(q, str) and q.strip()][:8]
    if len(queries) < 4:  # fallback: o assunto + ângulos padrão, em inglês
        queries = [assunto, f"{assunto} documentation", f"{assunto} tutorial",
                   f"{assunto} best practices", f"{assunto} official docs"]
    definicao = {
        "escopo": str(d.get("escopo") or f"Knowledge base about {assunto}"),
        "topicos": [str(t) for t in (d.get("topicos") or [])][:10],
        "tipos_fonte": [str(t) for t in (d.get("tipos_fonte") or [])][:6],
        "queries": queries,
    }
    log(f"🎯 Definição da RAG: {definicao['escopo']}")
    log(f"   tópicos: {', '.join(definicao['topicos']) or '—'}")
    return definicao


# ---------- 2) buscas (Serper + DuckDuckGo de respaldo) ----------

def _serper(queries: list[str], por_query: int = 8) -> list[dict]:
    """Serper (Google) devolve candidatos {titulo, link, resumo}."""
    if not config.SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY não configurada — cole a chave no "
                           "painel ⚙️ Configurações (ou no .env) e tente de novo")
    candidatos: dict[str, dict] = {}
    for q in queries:
        r = httpx.post(SERPER_URL, headers={"X-API-KEY": config.SERPER_API_KEY},
                       json={"q": q, "num": por_query}, timeout=20)
        r.raise_for_status()
        for item in r.json().get("organic", []):
            link = item.get("link", "")
            if link.startswith("http"):
                candidatos[link] = {"titulo": item.get("title", ""),
                                    "link": link, "resumo": item.get("snippet", "")}
    return list(candidatos.values())


def _duckduckgo(queries: list[str], por_query: int = 8) -> list[dict]:
    """Respaldo sem chave: endpoint HTML do DuckDuckGo (mesmo formato)."""
    candidatos: dict[str, dict] = {}
    for q in queries:
        try:
            r = httpx.get("https://html.duckduckgo.com/html/", params={"q": q},
                          headers={"User-Agent": UA}, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.result__a")[:por_query]:
                href = a.get("href") or ""
                if "uddg=" in href:  # link de redirecionamento do DDG
                    qs = parse_qs(urlparse(
                        "https:" + href if href.startswith("//") else href).query)
                    if "uddg" in qs:
                        href = unquote(qs["uddg"][0])
                if href.startswith("http"):
                    candidatos[href] = {"titulo": a.get_text(strip=True),
                                        "link": href, "resumo": ""}
        except Exception:
            continue
    return list(candidatos.values())


# ---------- 3) curadoria com scores + refinamento ----------

def _curadoria(assunto, definicao, candidatos, maximo, log) -> list[dict]:
    """Passo 2 da spec: score 0-10 por candidato; só score ≥ 6 entra.

    LOTES de 40 (máx 3): antes só os primeiros 40 eram avaliados e o resto
    morria CEGO — bases com muitos resultados perdiam as boas fontes de
    baixo por nunca terem sido olhadas."""
    por_url = {c["link"]: c for c in candidatos}
    aprovadas = []
    for lote_ini in range(0, min(len(candidatos), 120), 40):
        if len(aprovadas) >= maximo * 2:  # já há margem: para de gastar LLM
            break
        fatia = candidatos[lote_ini:lote_ini + 40]
        lista = "\n".join(f"- {c['titulo'][:70]} | {c['link']} | {c['resumo'][:80]}"
                          for c in fatia)
        r = rag.llm(temperature=0.0).invoke(
            f"{spec('seed')}\n\nAssunto: {assunto}\nDefinição da RAG:\n"
            f"{definicao['escopo']}\nMáximo de fontes: {maximo}\n"
            f"Resultados da busca:\n{lista}\n\nETAPA: 2 (curadoria com scores).")
        for a in (_json_da_resposta(r.content) or []):
            url = a.get("url", "")
            c = por_url.get(url)
            if not c:
                continue
            try:
                score = int(a.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if score < SCORE_MIN_FONTE:
                continue
            aprovadas.append({**c, "score": score,
                              "motivo": str(a.get("motivo", ""))[:120]})
    aprovadas.sort(key=lambda x: x["score"], reverse=True)
    saida, dominios = [], set()
    for a in aprovadas:  # 1 por domínio (a melhor), como a spec manda
        dom = urlparse(a["link"]).netloc
        if dom in dominios:
            continue
        dominios.add(dom)
        saida.append(a)
    return saida[:maximo]


def _refinar_queries(assunto, definicao, aprovadas, log) -> list[str]:
    """Passo 3 da spec: queries novas para ângulos ainda não cobertos (em EN)."""
    urls = "\n".join(a["link"] for a in aprovadas)
    r = rag.llm(temperature=0.3).invoke(
        f"{spec('seed')}\n\nAssunto: {assunto}\nDefinição da RAG:\n"
        f"{definicao['escopo']}\nFontes já aprovadas:\n{urls}\n\n"
        "ETAPA: 3 (refinamento das buscas — há valor em aprofundar?).")
    candidatas = [q.strip() for q in (_json_da_resposta(r.content) or [])
                  if isinstance(q, str)]
    # filtro anti-genérico: query boa é ESPECÍFICA (≥3 palavras e fala do
    # assunto) — queries soltas tipo "arquitetura limpa" ou "SOLID" trazem
    # conteúdo genérico que não pertence à coleção da linguagem
    chaves = [t.lower() for t in [assunto, *definicao["topicos"]] if t]
    boas = []
    for q in candidatas:
        if len(q.split()) < 3:
            continue
        baixa = q.lower()
        if chaves and not any(k.split()[0].lower() in baixa for k in chaves):
            continue  # nem toca no assunto
        boas.append(q)
    if not boas:  # nada específico: mergulha nos tópicos da definição (EN)
        boas = [f"{assunto} {t}" for t in definicao["topicos"][:3] if t]
    return boas[:6]


# ---------- 4) download, recursos internos e repos oficiais ----------

def _html_texto(html: str) -> str:
    """HTML → texto LIMPO COM ESTRUTURA (Fase A.1).

    Trafilatura extrai o CONTEÚDO PRINCIPAL em markdown (títulos, listas e
    código sobrevivem — antes o get_text colapsava tudo em sopa e o split
    por seções do ingest ficava cego). Fallback: o caminho BeautifulSoup
    antigo (site exótico/JS que o trafilatura não reconhece)."""
    try:
        import trafilatura
        md = trafilatura.extract(html, output_format="markdown",
                                 include_tables=True, include_links=False,
                                 favor_precision=True)
        if md and len(md.strip()) >= 200:
            return limpar_texto(md)
    except ImportError:
        pass  # trafilatura não instalado: usa o caminho antigo
    except Exception:
        pass  # página que o parser não deu conta: idem
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "form", "noscript", "iframe"]):
        tag.decompose()
    return limpar_texto(soup.get_text("\n"))


def _baixar_html(url: str) -> str | None:
    try:
        r = httpx.get(url, timeout=25, follow_redirects=True,
                      headers={"User-Agent": UA})
        if r.status_code != 200 or len(r.content) > MAX_BYTES:
            return None
        if "html" not in r.headers.get("content-type", ""):
            return None
        return r.text
    except Exception:
        return None


def _links_internos(html: str, url: str) -> list[dict]:
    """Páginas do MESMO domínio (conteúdo, não mídia/cta/login/índice)."""
    base = urlparse(url)
    soup = BeautifulSoup(html, "html.parser")
    vistos, out = set(), []
    for a in soup.select("a[href]"):
        href = urljoin(url, a.get("href") or "")
        p = urlparse(href)
        if p.netloc != base.netloc or not href.startswith("http"):
            continue
        caminho = p.path.rstrip("/")
        # âncora da MESMA página / homepage / índices gerais do site: fora
        if caminho == urlparse(url).path.rstrip("/") or caminho == "":
            continue
        if caminho.rsplit("/", 1)[-1].lower() in (
                "blog", "news", "articles", "posts", "category", "categories",
                "tag", "tags", "archive", "search", "about", "contact"):
            continue
        if p.path.endswith((".zip", ".png", ".jpg", ".jpeg", ".svg", ".pdf",
                            ".css", ".js", ".ico", ".webp", ".mp4", ".xml", ".json")):
            continue
        if any(k in href for k in ("login", "signup", "register", "cart",
                                   "checkout", "facebook", "twitter", "x.com",
                                   "instagram", "youtube", "tiktok", "pinterest",
                                   "linkedin", "whatsapp", "mailto:")):
            continue
        if href.rstrip("/") == url.rstrip("/") or href in vistos:
            continue
        vistos.add(href)
        out.append({"url": href, "texto": a.get_text(" ", strip=True)[:80]})
        if len(out) >= 40:
            break
    return out


def _escolher_internos(assunto, definicao, links) -> list[str]:
    """Passo 4 da spec: a LLM pontua as páginas internas (≥ 7 entram)."""
    if not links:
        return []
    lista = "\n".join(f"- {l['texto'][:60]} | {l['url']}" for l in links)
    r = rag.llm(temperature=0.0).invoke(
        f"{spec('seed')}\n\nAssunto: {assunto}\nDefinição da RAG:\n"
        f"{definicao['escopo']}\nPáginas internas da fonte aprovada:\n{lista}\n\n"
        "ETAPA: 4 (escolha dos recursos internos).")
    por_url = {l["url"] for l in links}
    saida = []
    for e in (_json_da_resposta(r.content) or []):
        u = e.get("url", "")
        if u in por_url:
            try:
                score = int(e.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if score >= SCORE_MIN_INTERNO and u not in saida:
                saida.append(u)
    return saida[:INTERNOS_POR_FONTE]


def _repo_github(url: str) -> str | None:
    """URL de repo GitHub (owner/repo) → URL canônica; senão None."""
    p = urlparse(url)
    if p.netloc not in ("github.com", "www.github.com"):
        return None
    partes = [x for x in p.path.split("/") if x]
    if len(partes) < 2:
        return None
    return f"https://github.com/{partes[0]}/{partes[1].removesuffix('.git')}"


def _clonar_repo(url: str, destino: Path, log) -> Path | None:
    """Clone esparso do repo oficial — só as pastas de documentação."""
    if destino.exists():
        return destino
    try:
        log(f"   📁 clonando repo oficial (esparso, só docs): {url}")
        destino.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none",
             "--sparse", url, str(destino)],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(f"   ⚠️ clone falhou: {r.stderr.strip()[:100]}")
            return None
        ls = subprocess.run(["git", "-C", str(destino), "ls-tree", "-d",
                             "--name-only", "HEAD"], capture_output=True, text=True)
        raizes = ls.stdout.split()
        docs = [d for d in raizes if any(
            k in d.lower() for k in ("doc", "content", "guide", "tutorial",
                                     "learn", "adev"))][:4]
        if not docs:
            log("   ⚠️ repo sem pastas de documentação — ignorado")
            shutil.rmtree(destino, ignore_errors=True)
            return None
        subprocess.run(["git", "-C", str(destino), "sparse-checkout", "set", *docs],
                       capture_output=True, timeout=900)
        mds = [*destino.rglob("*.md"), *destino.rglob("*.txt"), *destino.rglob("*.mdx")]
        if len(mds) < 3:
            log(f"   ⚠️ só {len(mds)} arquivo(s) de texto nas pastas de docs — ignorado")
            shutil.rmtree(destino, ignore_errors=True)
            return None
        log(f"   ✅ {len(mds)} arquivo(s) de documentação em {destino.name}/"
            f"{{{','.join(d.rstrip('/') for d in docs)}}}")
        return destino
    except Exception as e:
        log(f"   ⚠️ repo: {e}")
        shutil.rmtree(destino, ignore_errors=True)
        return None


def _proximo_indice(pasta: Path) -> int:
    """Continua a numeração dos arquivos já existentes na pasta."""
    nums = [int(p.name[:2]) for p in pasta.glob("[0-9][0-9]_*") if p.name[:2].isdigit()]
    return (max(nums) + 1) if nums else 0


def _gravar(pasta: Path, indice: int, titulo: str, url: str, texto: str) -> tuple[int, Path]:
    nome = _slug(titulo or url)[:60] or _slug(url)[:40]
    caminho = pasta / f"{indice:02d}_{nome}.md"
    caminho.write_text(f"# {titulo or nome}\n> fonte: {url}\n\n{texto[:MAX_TEXTO]}",
                       encoding="utf-8")
    return indice + 1, caminho


# ---------- pipeline completo ----------

def seed_collection(assunto: str, colecao: str | None = None, fontes: int = 8,
                    log=None) -> dict:
    """Definição → rodadas de busca → curadoria com scores → download +
    internos + repos → ingestão em lote → catalogação. Log completo."""
    log = log or print
    colecao = colecao or _slug(assunto)
    log(f"🌱 Seed de '{assunto}' → coleção '{colecao}' (alvo: {fontes} fontes)")

    log("\n0️⃣  Busca contextual (o que existe de fato sobre o assunto):")
    exploratoria = _explorar(assunto, log)

    log("\n1️⃣  Definição da RAG (o que a base É — antes de importar qualquer coisa):")
    definicao = _definir_rag(assunto, exploratoria, log)

    # 2) ondas de pesquisa (spec pesquisa_web.md): Serper para o ATUAL na
    # 1ª onda; DuckDuckGo para as internas/aprofundamento; a LLM decide se
    # ainda vale aprofundar comparando a cobertura com a definição.
    aprovadas: dict[str, dict] = {}
    vistos: set[str] = set()
    queries = list(definicao["queries"])
    rodada = 0
    while rodada < RODADAS_MAX and len(aprovadas) < fontes:
        rodada += 1
        motor_txt = "Serper (informação atual)" if rodada == 1 else "DuckDuckGo (aprofundamento)"
        log(f"\n2️⃣  Pesquisa — onda {rodada}/{RODADAS_MAX} · {motor_txt} ({len(queries)} queries):")
        for q in queries:
            log(f"   - {q}")
        try:
            if rodada == 1:
                candidatos = _serper(queries)
                log(f"   Serper: {len(candidatos)} candidato(s)")
                if len(candidatos) < 12:
                    ddg = _duckduckgo(queries)
                    novos = [c for c in ddg if c["link"] not in {x["link"] for x in candidatos}]
                    log(f"   DuckDuckGo (respaldo): +{len(novos)} candidato(s)")
                    candidatos += novos
            else:
                candidatos = _duckduckgo(queries)
                log(f"   DuckDuckGo: {len(candidatos)} candidato(s)")
                if len(candidatos) < 12:  # DDG raso — Serper completa
                    try:
                        ser = _serper(queries)
                        ja = {c["link"] for c in candidatos}
                        novos = [c for c in ser if c["link"] not in ja]
                        log(f"   Serper (respaldo): +{len(novos)} candidato(s)")
                        candidatos += novos
                    except Exception:
                        pass
        except Exception as e:
            log(f"   ⚠️ Serper falhou ({str(e)[:80]}) — usando só o DuckDuckGo")
            candidatos = _duckduckgo(queries)
        novos = [c for c in candidatos if c["link"] not in vistos]
        if not novos:
            log("   sem candidatos novos — encerrando as ondas")
            break
        for c in novos:
            vistos.add(c["link"])
        faltam = max(fontes * 2 - len(aprovadas), 4)  # folga p/ perdas no download
        log(f"   🧠 curadoria com scores ({len(novos)} candidatos, faltam {faltam})…")
        for a in _curadoria(assunto, definicao, novos, faltam, log):
            aprovadas.setdefault(a["link"], a)
            log(f"   ✅ [{a['score']}] {a['link'][:70]}\n        {a['motivo']}")
        log(f"   → {len(aprovadas)} fonte(s) aprovada(s) no total")
        if len(aprovadas) < fontes and rodada < RODADAS_MAX:
            log("   🧠 a LLM avalia se vale aprofundar (tópicos da definição sem cobertura)…")
            queries = _refinar_queries(assunto, definicao, list(aprovadas.values()), log)
            if not queries:
                log("   cobertura suficiente — encerrando as ondas")
                break
    if not aprovadas:
        raise ValueError("Nenhuma fonte aprovada — tente formular o assunto de outro jeito")

    # 3) download das fontes + recursos internos + repos oficiais
    log(f"\n3️⃣  Download ({len(aprovadas)} fonte(s)) + recursos internos + repos:")
    pasta = SEED_DIR / colecao
    pasta.mkdir(parents=True, exist_ok=True)
    indice = _proximo_indice(pasta)
    arquivos, repos, repos_url = [], [], set()
    textos_vistos: set[str] = set()  # mesma página por âncora/redirect ≠ nova fonte

    def _gravar_novo(titulo: str, url: str, texto: str) -> str | None:
        """Grava se o CONTEÚDO é novo (hash); devolve o caminho ou None."""
        import hashlib as _h
        chave = _h.md5(texto[:8000].encode("utf-8")).hexdigest()
        if chave in textos_vistos:
            return None
        textos_vistos.add(chave)
        nonlocal indice
        indice, caminho = _gravar(pasta, indice, titulo, url, texto)
        return str(caminho)

    for a in list(aprovadas.values())[:fontes]:
        url = a["link"]
        repo = _repo_github(url)
        if repo and len(repos) < REPOS_MAX and repo not in repos_url:
            repos_url.add(repo)
            destino = SEED_DIR / f"{colecao}_repo" / repo.rstrip("/").rsplit("/", 1)[-1]
            clonado = _clonar_repo(repo, destino, log)
            if clonado:
                repos.append(clonado)
            continue
        html = _baixar_html(url)
        if html is None:
            log(f"   ⏭️  {url[:70]} — sem HTML útil, descartada")
            continue
        texto = _html_texto(html)
        if len(texto) < MIN_TEXTO:
            log(f"   ⏭️  {url[:70]} — pouco conteúdo, descartada")
            continue
        caminho = _gravar_novo(a["titulo"], url, texto)
        if not caminho:
            log(f"   ⏭️  {url[:70]} — conteúdo repetido, descartada")
            continue
        arquivos.append(caminho)
        log(f"   📄 {Path(caminho).name} ({len(texto):,} chars)")
        internos = _escolher_internos(assunto, definicao, _links_internos(html, url))
        for u in internos:
            h2 = _baixar_html(u)
            if h2 is None:
                continue
            t2 = _html_texto(h2)
            if len(t2) < MIN_TEXTO:
                continue
            c2 = _gravar_novo(u.rsplit("/", 1)[-1].replace("-", " ")[:60] or u, u, t2)
            if not c2:
                continue
            arquivos.append(c2)
            log(f"      ↳ interno: {u[:70]} ({len(t2):,} chars)")
    if not arquivos and not repos:
        raise ValueError("Nenhuma fonte baixada teve conteúdo suficiente")

    # 4) ingestão em lote (pasta da web + repos clonados)
    log("\n4️⃣  Ingestão no Qdrant (modo lote, texto limpo):")
    total = {"documents": 0, "chunks": 0}
    total_points = 0
    for alvo in [pasta, *repos]:
        log(f"   ▶ {alvo}")
        r = ingest.ingest_folder(str(alvo), colecao, rapido=True,
                                 log=lambda m: log("   " + m))
        total["documents"] += r.get("documents", 0)
        total["chunks"] += r.get("chunks", 0)
        total_points = r.get("total_points", total_points)

    # 5) catalogação (confere se a coleção faz sentido)
    log("\n5️⃣  Catalogação (a LLM confere se a coleção faz sentido):")
    client = QdrantClient(url=config.QDRANT_URL, timeout=30,
                          check_compatibility=False)
    amostras = [Path(f).read_text(encoding="utf-8")[:600] for f in arquivos[:5]]
    info = rag.analyze_collection(colecao, amostras)
    catalog.save_collection(client, colecao, info["categoria"], info["descricao"],
                            area=info.get("area", ""))
    log(f"   categoria: {info['categoria']}")
    log(f"   descricao: {info['descricao']}")

    resumo = {
        "assunto": assunto, "colecao": colecao,
        "definicao": {"escopo": definicao["escopo"], "topicos": definicao["topicos"]},
        "rodadas": rodada, "candidatos": len(vistos),
        "fontes_aprovadas": list(aprovadas),
        "arquivos": arquivos,
        "repos": [r.name for r in repos],
        "ingestao": {**total, "total_points": total_points},
        "catalogo": info,
    }
    log(f"\n🎉 Concluído: coleção '{colecao}' — {len(resumo['fontes_aprovadas'])} "
        f"fonte(s) → {len(arquivos)} página(s) + {len(repos)} repo(s) → "
        f"{total['chunks']} chunks · {total_points} pontos no Qdrant")
    return resumo


def main():
    """Entrada do CLI."""
    if len(sys.argv) < 2:
        sys.exit('Uso: python -X utf8 -m core.seed "assunto" [colecao] [--fontes N]')
    args = sys.argv[1:]
    assunto = args[0]
    colecao = args[1] if len(args) > 1 and not args[1].startswith("--") else None
    fontes = int(args[args.index("--fontes") + 1]) if "--fontes" in args else 8
    print(seed_collection(assunto, colecao, fontes))


if __name__ == "__main__":
    main()
