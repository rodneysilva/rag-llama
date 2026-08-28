"""
Limpeza de texto para ingestão e higienização de coleções.

Texto de web (seed) e de wikis chega cheio de ruído que degrada a busca
semântica: links viram quebras de linha no MEIO das frases ("impor as
\\nmassas\\n de farinha"), marcas de edição ("[editar | editar código]"),
citações ("[50]"), menus de navegação, widgets de avaliação, referências
("↑ ... op. cit."). O embedding desses pedaços não representa o conteúdo —
são eles que aparecem nas pesquisas "sem sentido".

Este módulo é puro (sem LLM, sem rede): `limpar_texto` normaliza um texto
qualquer; `e_lixo` decide se um pedaço já limpo carrega semântica útil ou é
só estrutura de página. Usado em core/ingest (na entrada), core/seed (na
conversão do HTML) e core/higieniza (coleções já gravadas).
"""
import re
import unicodedata

# ---------- normalização básica ----------

# espaços exóticos que o BeautifulSoup deixa (\xa0, \u202f…)
_ESPACOS = {"\xa0": " ", "\u202f": " ", "\u2009": " ", "\u200b": "",
            "\ufeff": "", "­": ""}

# marcas de edição de wiki: "[editar | editar código]", "[editar código]"
_RE_EDITAR = re.compile(r"\[\s*editar(?:\s*código)?(?:\s*\|\s*editar(?:\s*código)?)?\s*\]", re.I)
# citação de referência: "[50]", "[ 12 ]" (nunca "[3]" colado em palavra tipo array?)
_RE_CITACAO = re.compile(r"\[\s*\d{1,3}\s*\]")
# "op. cit." e setas de referência
_RE_REF = re.compile(r"(?:^|\n)\s*(?:↑|\^).{0,160}", re.S)
# linhas de "ver também / ver artigo principal" (lista de links, não conteúdo)
_RE_VER_TAMBM = re.compile(r"^\s*(?:ver(?: também| artigo principal| artigos principais)?|ver também)\s*[:：]", re.I | re.M)
# frases que só existem em chrome de página (não em conteúdo)
_FRASES_LIXO = (
    "mover para a barra lateral", "esconder", "avaliações desse artigo",
    "avaliação desse artigo", "quer deixar a sua avaliação", "siga-nos no",
    "siga a gente no", "curta nossa página", "compartilhe este",
    "todos os direitos reservados", "política de privacidade",
    "privacy policy", "termos de uso", "cadastre-se", "conecte-se conosco",
    "newsletter", "assine agora", "faça login", "clique aqui",
    "conteúdo movido para", "saltar para o conteúdo", "ir para o conteúdo",
    "pular para o conteúdo", "pular para o conteúdo principal",
    "voltar ao topo", "leia também:", "publicidade", "anúncios",
    "cookies", "aceito", "comentários (0)",
)
# início de linha estrutural (não costurar com a linha anterior)
_RE_BLOCO = re.compile(
    r"^(?:#{1,6}\s|>\s?|[-*•]\s+|\d{1,3}[.)]\s+|[a-z]\)\s|\|)", )
_FIM_FRASE = tuple(".!?:;")


def _normaliza(t: str) -> str:
    for de, para in _ESPACOS.items():
        t = t.replace(de, para)
    return unicodedata.normalize("NFC", t)


def _juntar_linhas(t: str) -> str:
    """Reconstroi parágrafos: linhas quebradas no meio da frase voltam a ser
    uma linha só (efeito de cada <a>/<b> do HTML virar linha própria).

    Une a linha anterior com a seguinte quando: a anterior NÃO termina em
    pontuação de fim de frase E a seguinte NÃO começa marcador de bloco
    (título, lista, citação). Listas e títulos preservam a estrutura.
    """
    saida, buffer = [], []
    for linha in t.splitlines():
        s = linha.strip()
        if not s:
            if buffer:
                saida.append(" ".join(buffer)); buffer = []
            saida.append("")
            continue
        if buffer and not buffer[-1].rstrip().endswith(_FIM_FRASE) \
                and not _RE_BLOCO.match(s) and len(s) > 1 \
                and not s.startswith("↑"):
            buffer.append(s)
        else:
            if buffer:
                saida.append(" ".join(buffer))
            buffer = [s]
    if buffer:
        saida.append(" ".join(buffer))
    return "\n".join(saida)


def _remover_marcacoes(t: str) -> str:
    t = _RE_EDITAR.sub(" ", t)
    t = _RE_CITACAO.sub(" ", t)
    t = _RE_REF.sub(" ", t)
    t = _RE_VER_TAMBM.sub(" ", t)
    # restos de colchetes vazios ou só de pontuação ("[ , ]" das citações)
    t = re.sub(r"\[\s*[,;:.·|—–-]*\s*\]", " ", t)
    return t


def _remover_linhas_fracas(t: str) -> str:
    """Derruba linhas que não trazem conteúdo: só símbolos, só números de
    referência ou repetições do próprio título do widget."""
    linhas = []
    for linha in t.splitlines():
        s = linha.strip()
        if not s:
            linhas.append("")
            continue
        sem_pont = re.sub(r"[\W\d_]+", "", s, flags=re.UNICODE)
        if len(sem_pont) <= 1:            # "•", "1", "—", "|"…
            continue
        if re.fullmatch(r"[\d\s.,;:·|()\-—↑^]+", s):
            continue
        if re.fullmatch(r"(?:↑|\^)\s*.*op\.\s*cit\..*", s):
            continue
        baixa = s.lower()
        if any(f in baixa for f in _FRASES_LIXO):
            continue
        linhas.append(s)
    return "\n".join(linhas)


def _preenchimento(t: str) -> str:
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def limpar_texto(texto: str) -> str:
    """Pipeline completo: normaliza, reconstroi frases, remove marcações de
    web/wiki e linhas sem conteúdo. Idempotente (limpar 2× = limpar 1×)."""
    if not texto:
        return ""
    t = _normaliza(texto)
    t = _juntar_linhas(t)
    t = _remover_tabelas_inuteis(t)
    t = _remover_marcacoes(t)
    t = _juntar_linhas(t)                 # citações removidas requebram frases
    t = _remover_linhas_fracas(t)
    return _preenchimento(t)


# ---------- HTML cru → texto legível (resposta direta da base) ----------

# marcadores de PÁGIna inteira — usado junto com DENSIDADE de tags (um
# tutorial em markdown pode CITAR <div>/<meta> num exemplo de código:
# poucas tags ≠ página; página tem dezenas)
_RE_TAG_PAGINA = re.compile(
    r"<(?:meta|body|head|html|link|div|span|p|ul|ol|li|table|tr|td|a|h[1-6])\b",
    re.IGNORECASE)


def parece_pagina_html(texto: str) -> bool:
    """True quando o texto é uma página HTML inteira (não um exemplo de
    código dentro de markdown): começa com doctype/<html> OU tem DENSIDADE
    de tags de marcação alta no texto inteiro (≥6 tags e ≥6 por 1000
    chars — prosa citando um exemplo de código fica abaixo)."""
    t = (texto or "").strip()
    if not t:
        return False
    inicio = t[:500].lower()
    if "<!doctype" in inicio or "<html" in inicio:
        return True
    if len(t) < 400:          # chunk curto: exemplos de código inflam a razão
        return False
    tags = len(_RE_TAG_PAGINA.findall(t))
    return tags >= 6 and tags * 1000 / len(t) >= 6


def html_para_texto(html: str, min_chars: int = 120) -> str:
    """HTML cru → texto legível (BeautifulSoup, mesma extração do seed).
    Remove script/style/nav/footer/cabeçalhos e colapsa linhas vazias.
    Devolve '' quando sobra menos que `min_chars` (página sem conteúdo
    aproveitável — o chamador decide o plan B)."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        sopa = BeautifulSoup(html, "html.parser")
        for tag in sopa(["script", "style", "noscript", "template",
                         "header", "footer", "nav", "aside", "form",
                         "svg"]):
            tag.decompose()
        texto = sopa.get_text("\n")
    except Exception:
        return ""
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    texto = "\n".join(linhas)
    return texto if len(texto) >= min_chars else ""


# ---------- detecção de pedaço sem semântica ----------

_MIN_PALAVRAS = 25       # abaixo disso o pedaço é fragmento demais
_MAX_CAPS = 0.72         # razão de palavras iniciando em maiúscula (menu/índice)
_MIN_PONTUACAO = 0.015   # densidade de fim-de-frase por palavra (prosa tem ~0.1)


def _palavras(t: str) -> list[str]:
    return re.findall(r"[\wÀ-ÿ'-]{2,}", t, flags=re.UNICODE)



def _remover_tabelas_inuteis(t: str) -> str:
    """Remove blocos de tabela markdown (linhas |...|) cujas células são
    majoritariamente VAZIAS ou curtas — infobox de wiki, specs, grids.
    Tabela de DADO real (células preenchidas) sobrevive."""
    linhas = t.split("\n")
    saida, i = [], 0
    while i < len(linhas):
        if linhas[i].lstrip().startswith("|"):
            bloco = []
            while i < len(linhas) and linhas[i].lstrip().startswith("|"):
                bloco.append(linhas[i]); i += 1
            celulas = [c.strip() for l in bloco for c in l.strip("|").split("|")]
            if not celulas:
                continue
            vazias = sum(1 for c in celulas if not c)
            media = sum(len(c) for c in celulas) / len(celulas)
            # INFOBOX: celulas vazias em excesso OU só rótulos curtos
            # (Kingdom:|Plantae) num bloco comprido = ficha, não prosa.
            infobox = (vazias / len(celulas) >= 0.30
                       or (len(bloco) >= 6 and media < 14))
            if not infobox:
                saida.extend(bloco)      # tabela de DADO real: fica
        else:
            saida.append(linhas[i]); i += 1
    return "\n".join(saida)


def e_lixo(texto: str, min_chars: int = 180) -> bool:
    """True quando o pedaço (já limpo) não carrega semântica útil: estrutura
    de página (menu, índice, lista de estados), referências ou fragmento
    curto demais. Conservador: na dúvida, mantém (retorna False).
    """
    t = (texto or "").strip()
    if len(t) < min_chars:
        return True
    palavras = _palavras(t)
    if len(palavras) < _MIN_PALAVRAS:
        return True
    alfa = sum(c.isalnum() for c in t)
    if alfa / max(len(t), 1) < 0.55:      # tabela/símbolos dominando
        return True
    caps = sum(1 for p in palavras if p[0].isupper())
    pontuacao = sum(t.count(c) for c in ".!?;:")
    if caps / len(palavras) > _MAX_CAPS and pontuacao / len(palavras) < 0.05:
        return True                        # "Paraíba Paraná Pernambuco …"
    if pontuacao / len(palavras) < _MIN_PONTUACAO and len(palavras) > 40:
        return True                        # lista longa sem UMA frase
    if t.count("op. cit.") >= 2 or t.count("↑") >= 2:
        return True                        # bloco de referências
    return False


def e_lixo_documento(texto: str, min_chars: int = 600) -> bool:
    """Documento INTEIRO (não chunk): só descarta se não sobrou conteúdo.

    As heurísticas de `e_lixo` (caps, pontuação, 'op. cit.') valem para
    PEDAÇOS — um documento grande com um bloco de referências no fim ainda é
    ouro; o split + o filtro por chunk cuidam das partes ruins.
    """
    t = (texto or "").strip()
    if len(t) < min_chars:
        return True
    alfa = sum(c.isalnum() for c in t)
    return alfa / max(len(t), 1) < 0.45


# ---------- SCORE DE QUALIDADE (padrão de mercado p/ ingestão RAG) ----------
# Cada chunk recebe nota 0-1 na INGESTÃO; abaixo do limiar (SCORE_CHUNK_MIN
# no .env, default 0.55) é rejeitado com o motivo no relatório da Revisão.
# Fatores validados pela prática da comunidade (Qdrant/Apify/Firecrawl):
#   - densidade de links (links dominando = página, não conteúdo)
#   - razão de tokens únicos (baixa = repetição/tabela/lista)
#   - comprimento em palavras (< 15 = stub, ruído na query)
#   - razão alfanumérica (tabela/símbolos dominando)
#   - JSON/estrutura de dados embutida (e-commerce, APIs cruas)
#   - lista de nomes próprios (índice/tabela wiki sem frase)

_RE_LINK = re.compile(r"https?://\S+|www\.\S+")

# pesos dos fatores (somam 1.0)
_PESOS = {"links": 0.30, "unicos": 0.30, "palavras": 0.25, "alfa": 0.15}


def _parece_json(texto: str) -> bool:
    """Bloco com estrutura JSON REAL embutida (payload de e-commerce, API).

    Exige chaves/colchetes BALANCEADOS e o par "chave": valor — aspas de
    CITAÇÃO no meio da prosa ("relationship...") não contam (falso
    positivo real: [Brazilian_Portuguese] com citação de linguista).
    """
    t = texto.strip()
    abre = t.count("{") + t.count("[")
    fecha = t.count("}") + t.count("]")
    if abre < 2 or abre != fecha:
        return False
    import re as _re
    # pelo menos 2 pares "chave":valor/'chave':valor com chaves curtas
    pares = _re.findall(r"[{,]\s*[\"']?[A-Za-z_][\w\-]*[\"']?\s*:\s*[\"'\[\{\d]", t)
    return len(pares) >= 2


def _razao_nomes(palavras: list[str]) -> float:
    """Proporção de palavras capitalizadas (índice de wiki, lista de nomes)."""
    if not palavras:
        return 0.0
    return sum(1 for w in palavras if w[:1].isupper()) / len(palavras)


def score_chunk(texto: str) -> tuple[float, list[str]]:
    """(nota 0-1, motivos das penalidades) para um chunk JÁ limpo.

    Usado pela ingestão (gate default) e pelo Modo Revisão (relatório).
    Puro, sem LLM, sem rede — determinístico. Na dúvida, mantém.
    """
    t = (texto or "").strip()
    motivos: list[str] = []
    if not t:
        return 0.0, ["vazio"]
    palavras = _palavras(t)
    n = len(palavras)
    # 1) links: proporção dos tokens que são URLs
    links = len(_RE_LINK.findall(t))
    d_links = links / n if n else 1.0
    nota_links = max(0.0, 1.0 - d_links * 4)   # >25% de links zera o fator
    if d_links > 0.10:
        motivos.append(f"densidade de links {d_links:.0%}")
    # 2) razão de tokens únicos (repetição/lista)
    unicos = len({w.lower() for w in palavras}) / n if n else 0.0
    nota_unicos = min(1.0, unicos / 0.55)       # 55%+ de únicos = nota cheia
    if unicos < 0.45:
        motivos.append(f"repetição (tokens únicos {unicos:.0%})")
    # 3) palavras: mínimo 15 (stub de tabela/código sem contexto)
    nota_pal = min(1.0, n / 15.0)
    if n < 15:
        motivos.append(f"{n} palavra(s) (<15)")
    # 4) alfanumérico
    alfa = sum(c.isalnum() for c in t) / len(t)
    nota_alfa = min(1.0, max(0.0, (alfa - 0.45) / 0.40))
    if alfa < 0.55:
        motivos.append(f"símbolos dominando (alfa {alfa:.0%})")
    # 5) JSON embutido (evidência real: blocos com displayPrice/priceAmount
    #    ingeridos na extinta coleção psicanalista)
    if _parece_json(t):
        motivos.append("estrutura JSON embutida")
        return 0.05, motivos
    # 6) lista de nomes próprios SEM frase (índice/tabela wiki — evidência
    #    real da culinaria: "Wongi Manilkara kauki Wooly jelly palm...")
    if n >= 8:
        nomes = _razao_nomes(palavras)
        pont = sum(t.count(c) for c in ".!?:;")
        if nomes > 0.5 and pont / max(n, 1) < 0.03:
            motivos.append(f"lista de nomes sem frase (caps {nomes:.0%})")
            return 0.20, motivos
    # 7) TABELA markdown (pipes dominando): "| valor | valor |" é grade de
    #    dados, não prosa — evidência real das tabelas wiki ingeridas
    d_pipes = t.count("|") / max(n, 1)
    if d_pipes > 0.15:
        motivos.append(f"tabela markdown (pipes {d_pipes:.0%})")
        return 0.15, motivos
    nota = (_PESOS["links"] * nota_links + _PESOS["unicos"] * nota_unicos
            + _PESOS["palavras"] * nota_pal + _PESOS["alfa"] * nota_alfa)
    return round(nota, 3), motivos


def titulo_de(texto: str, fallback: str = "") -> str:
    """Título do documento: 1º cabeçalho markdown, 1ª linha curta em
    maiúsculas ou o nome do arquivo como último recurso."""
    for linha in texto.splitlines()[:10]:
        s = linha.strip()
        if s.startswith("#"):
            s = re.sub(r"^#+\s*", "", s).strip()
            if 3 <= len(s) <= 120:
                return s
    for linha in texto.splitlines()[:6]:
        s = linha.strip()
        if 3 <= len(s) <= 90 and not s.startswith((">", "|", "-", "*", "h=", "http")):
            return s.rstrip(".…")
    return fallback or "documento"


def url_de(texto: str) -> str | None:
    """Extrai a linha '> fonte: URL' gravada pelo seed (se houver)."""
    m = re.search(r"^\s*>\s*fonte:\s*(\S+)", texto, flags=re.M | re.I)
    return m.group(1) if m else None
