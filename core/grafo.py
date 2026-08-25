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
    tipo: str      # criacao|midia|conversa|factual|"" (indefinido)
    motivo: str
    rota: str      # fluxo|orientar_criacao|conversa


# ── triagem léxica (custo zero — cobre os casos evidentes) ──────────────
_RE_CRIAR = re.compile(
    r"\b(quer[oa]|cri[ae]|criar|faç|faz|fazer|mont[ae]|montar|ger[ae]|"
    r"gerar|escrev|desenvolv|implement|constru|cod|code|program)\w*", re.I)
_RE_COISA = re.compile(
    r"\b(p[áa]gina|site|c[óo]digo|api|app|aplica|projeto|programa|script|"
    r"componente|aba|tela|formul[áa]rio|banco|tabela|servidor|site|"
    r"dashboard|crud|servi[çc]o|fun[çc][ãa]|classe|endpoint)\w*", re.I)
_RE_MIDIA = re.compile(
    r"\b(gif|imagem|imagine|imagem|v[íi]deo|desenh|foto|ilustra|anime|"
    r"caricatura|quadro|anima)\w*", re.I)
_RE_FACTUAL = re.compile(
    r"^(o\s+que|que\s+é|qual|quais|como\s+(funciona|fa[zs]|se|us[oa])|"
    r"quando|onde|quem|por\s*qu[eê]|porqu[eê]|explique|explica|diferen[çc]a|"
    r"compare|liste|mostre|cite)\b", re.I)
_TRIVIAIS = {
    "oi", "olá", "ola", "hey", "hello", "bom", "dia", "boa", "tarde",
    "noite", "tudo", "bem", "como", "vai", "voce", "você", "é", "e", "um",
    "uma", "robo", "robô", "ia", "quem", "o", "que", "faz", "qual", "seu",
    "nome", "obrigado", "obrigada", "valeu", "vlw", "thanks", "thank",
    "you", "beleza", "com", "está", "esta", "tchau", "adeus", "e",
}


def _triagem_lexica(estado: Estado) -> Estado:
    """Regex pura: decide os casos EVIDENTES e marca ambíguos p/ LLM."""
    q = (estado.get("pergunta") or "").strip()
    palavras = re.findall(r"[a-zà-ú]+", q.lower())
    if q and len(palavras) <= 6 and all(p in _TRIVIAIS for p in palavras):
        return {**estado, "tipo": "conversa", "motivo": "saudação curta"}
    if _RE_MIDIA.search(q):
        return {**estado, "tipo": "midia", "motivo": "pedido de mídia"}
    if _RE_CRIAR.search(q) and _RE_COISA.search(q):
        return {**estado, "tipo": "criacao", "motivo": "verbo+artefato"}
    if _RE_FACTUAL.search(q):
        return {**estado, "tipo": "factual", "motivo": "abertura interrogativa"}
    return {**estado, "tipo": "", "motivo": "ambíguo"}


def _classificar_llm(estado: Estado) -> Estado:
    """Nó LLM — SÓ chamado para ambíguos (temperature 0, spec roteador)."""
    from . import rag
    from .specs import spec
    try:
        chain = rag.build_prompt() | rag.llm(temperature=0.0)
        saida = chain.invoke({
            "system_text": spec("roteador"),
            "context": "", "history": [],
            "question": f"MENSAGEM DO USUÁRIO:\n{estado['pergunta'][:400]}"})
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


def rotear(pergunta: str, modo: str, log=print) -> dict:
    """Ponto de entrada: {"tipo","rota","motivo"} — ANTES de qualquer
    busca/cache. Custa ~0 (léxico) ou 1 chamada barata (ambíguos)."""
    app = _compilar()
    est = app.invoke({"pergunta": (pergunta or "").strip(), "modo": modo or ""})
    rota = est.get("rota", "fluxo")
    if rota != "fluxo" or est.get("motivo") == "ambíguo":
        log(f"🧭 roteador: tipo={est.get('tipo') or 'ambíguo'} → {rota} "
            f"({est.get('motivo', '')})", "mensagem")
    return {"tipo": est.get("tipo", ""), "rota": rota,
            "motivo": est.get("motivo", "")}
