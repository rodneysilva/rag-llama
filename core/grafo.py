# -*- coding: utf-8 -*-
"""🧭 Roteador de perguntas — LANGGRAPH (a inteligência vem ANTES).

Pedido do dono: a decisão do que fazer com a pergunta acontece no INÍCIO,
antes de gastar Qdrant/rerank/cache no caminho errado (bug real: pedido
de CRIAÇÃO em modo rag pagava ~7 s de busca para no fim recusar).

Grafo (StateGraph):
    START → triagem_lexica ─(claro)→ decidir → END
                        └(ambíguo)→ classificar_llm → decidir → END

- `triagem_lexica`: regex pura, custo zero — criação/saudação/mídia/
  pergunta factual evidentes decidem na hora (a maioria dos casos).
- `classificar_llm`: SÓ para ambíguos (1 chamada barata, temperature 0;
  comportamento em `core/specs/roteador.md` — regra de ouro do projeto).
- `decidir`: mapeia tipo×modo em ROTA:
    · "fluxo"           → nada intercepta (pipeline normal)
    · "orientar_criacao"→ criação em modo rag: orientar SEM gastar busca
    · "conversa"        → saudação: resposta direta, sem busca

O grafo NÃO responde — só roteia. O pipeline atual segue intocado para
tudo que cai em "fluxo".
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class Estado(TypedDict, total=False):
    pergunta: str
    modo: str
    historia: list          # últimas trocas (contexto p/ follow-ups)
    tipo: str      # criacao|midia|conversa|factual|"" (indefinido)
    motivo: str
    rota: str      # fluxo|orientar_criacao|conversa


# ── triagem léxica (custo zero — PT/EN cobrem o uso real; OUTROS idiomas
#    e ambíguos caem no nó LLM, que segue a spec multilíngue) ────────────
_RE_CRIAR = re.compile(
    r"\b(quer[oa]|cri[ae]|criar|faç|faz|fazer|mont[ae]|montar|ger[ae]|"
    r"gerar|escrev|desenvolv|implement|constru|cod|code|program|"
    r"create|build|make|write|generate|develop|scaffold|quiero|crea|haz|"
    r"créer|erstelle)\w*", re.I)
_RE_COISA = re.compile(
    r"\b(p[áa]gina|site|c[óo]digo|api|app|aplica|projeto|programa|script|"
    r"componente|aba|tela|formul[áa]rio|banco|tabela|servidor|dashboard|"
    r"crud|servi[çc]o|fun[çc][ãa]|classe|endpoint|website|web\s?app|page|"
    r"form|library|lib)\w*", re.I)
_RE_MIDIA = re.compile(
    r"\b(gif|imagem|v[íi]deo|desenh|foto|ilustra|anime|caricatura|quadro|"
    r"anima|picture|drawing|illustration|render|imagen|vidéo|dibuj)\w*",
    re.I)
_RE_FACTUAL = re.compile(
    r"^(o\s+que|que\s+é|qual|quais|como\s+(funciona|fa[zs]|se|us[oa])|"
    r"quando|onde|quem|por\s*qu[eê]|porqu[eê]|explique|explica|"
    r"diferen[çc]a|compare|liste|mostre|cite|"
    r"what|how|when|where|who|why|which|explain|describe|compare|list|"
    r"tell\s+me|c[óo]mo|qu[ée]|cu[áa]l|cu[áa]ndo|d[óo]nde|pourquoi|"
    r"comment|was\s+ist|wie)\b", re.I)
_TRIVIAIS = {
    "oi", "olá", "ola", "hey", "hello", "hi", "yo", "sup", "hola",
    "salut", "bonjour", "buenos", "buenas", "gracias", "merci", "ciao",
    "bom", "dia", "boa", "tarde", "noite", "morning", "evening", "night",
    "tudo", "bem", "how", "are", "you", "como", "vai", "voce", "você",
    "é", "e", "um", "uma", "robo", "robô", "ia", "quem", "o", "que",
    "faz", "qual", "seu", "nome", "obrigado", "obrigada", "valeu", "vlw",
    "thanks", "thank", "you", "beleza", "com", "está", "esta", "tchau",
    "adeus", "bye", "goodbye", "muy", "gracias",
}


def _triagem_lexica(estado: Estado) -> Estado:
    """Regex pura: decide os casos EVIDENTES e marca ambíguos p/ LLM."""
    q = (estado.get("pergunta") or "").strip()
    # follow-up curtíssimo SEM sinais próprios: o tipo vem da CONVERSA —
    # deixa o nó LLM decidir com o histórico (não adivinha aqui)
    palavras = re.findall(r"[a-zà-ú]+", q.lower())
    tem_historia = bool(estado.get("historia"))
    if (q and not tem_historia and len(palavras) <= 6
            and all(p in _TRIVIAIS for p in palavras)):
        return {**estado, "tipo": "conversa", "motivo": "saudação curta"}
    if tem_historia and len(palavras) <= 3:
        return {**estado, "tipo": "", "motivo": "follow-up curto"}
    if _RE_MIDIA.search(q):
        return {**estado, "tipo": "midia", "motivo": "pedido de mídia"}
    if _RE_CRIAR.search(q) and _RE_COISA.search(q):
        return {**estado, "tipo": "criacao", "motivo": "verbo+artefato"}
    if _RE_FACTUAL.search(q):
        return {**estado, "tipo": "factual", "motivo": "abertura interrogativa"}
    return {**estado, "tipo": "", "motivo": "ambíguo"}


def _classificar_llm(estado: Estado) -> Estado:
    """Nó LLM — ambíguos e outros idiomas (temperature 0, spec roteador:
    multilíngue, decide COM a conversa recente quando há follow-up)."""
    from . import contadores, rag
    from .specs import spec
    try:
        hist = estado.get("historia") or []
        trecho = ""
        if hist:
            partes = []
            for m in hist[-4:]:
                papel = "usuário" if m.get("role") == "user" else "assistente"
                partes.append(f"{papel}: {str(m.get('content', ''))[:200]}")
            trecho = ("CONVERSA RECENTE (contexto):\n" + "\n".join(partes)
                      + "\n\nMENSAGEM ATUAL:\n")
        chain = rag.build_prompt() | rag.llm(temperature=0.0)
        contadores.set_etapa("roteador")   # etiqueta própria: não suja as
        # métricas das etapas de resposta (pedido: "não comprometer a llm")
        saida = chain.invoke({
            "system_text": spec("roteador"),
            "context": "", "history": [],
            "question": trecho + f"{estado['pergunta'][:400]}"})
        contadores.set_etapa(None)
        texto = str(saida.content if hasattr(saida, "content") else saida)
        m = re.search(r"\{.*\}", texto, re.S)
        dados = json.loads(m.group(0)) if m else {}
        tipo = str(dados.get("tipo", "")).lower()
        if tipo in ("criacao", "midia", "conversa", "factual"):
            return {**estado, "tipo": tipo,
                    "motivo": str(dados.get("motivo", "llm"))[:60]}
    except Exception:
        pass  # degrada: sem classificação, segue o fluxo normal
    return {**estado, "tipo": "factual", "motivo": "fallback llm"}


def _decidir(estado: Estado) -> Estado:
    """tipo×modo → rota. O grafo NÃO responde — só evita o caminho errado."""
    tipo, modo = estado.get("tipo", ""), estado.get("modo", "")
    if tipo == "criacao" and modo == "rag":
        return {**estado, "rota": "orientar_criacao"}
    if tipo == "conversa" and not estado.get("pergunta"):
        return {**estado, "rota": "fluxo"}
    if tipo == "conversa" and modo in ("rag", "hibrido", "livre"):
        return {**estado, "rota": "conversa"}
    return {**estado, "rota": "fluxo"}


def _rota_pos_triagem(estado: Estado) -> str:
    return "decidir" if estado.get("tipo") else "classificar_llm"


@lru_cache(maxsize=1)
def _compilar():
    g = StateGraph(Estado)
    g.add_node("triagem_lexica", _triagem_lexica)
    g.add_node("classificar_llm", _classificar_llm)
    g.add_node("decidir", _decidir)
    g.add_edge(START, "triagem_lexica")
    g.add_conditional_edges("triagem_lexica", _rota_pos_triagem,
                            ["decidir", "classificar_llm"])
    g.add_edge("classificar_llm", "decidir")
    g.add_edge("decidir", END)
    return g.compile()


def rotear(pergunta: str, modo: str, log=print, historia: list | None = None) -> dict:
    """Ponto de entrada: {"tipo","rota","motivo"} — ANTES de qualquer
    busca/cache. `historia` (últimas trocas) contextualiza follow-ups.
    Custa ~0 (léxico PT/EN) ou 1 chamada barata (outros idiomas/ambíguos)."""
    app = _compilar()
    est = app.invoke({"pergunta": (pergunta or "").strip(),
                      "modo": modo or "",
                      "historia": historia or []})
    rota = est.get("rota", "fluxo")
    if rota != "fluxo" or est.get("motivo") in ("ambíguo", "follow-up curto"):
        log(f"🧭 roteador: tipo={est.get('tipo') or 'ambíguo'} → {rota} "
            f"({est.get('motivo', '')})", "mensagem")
    return {"tipo": est.get("tipo", ""), "rota": rota,
            "motivo": est.get("motivo", "")}
