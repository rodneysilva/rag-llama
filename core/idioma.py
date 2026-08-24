"""Idioma de busca — todo assunto vira tema de busca em INGLÊS neutro.

Pedido do dono: o usuário digita em qualquer idioma (português incluso),
mas a PESQUISA/INGESTÃO roda no idioma mundial — sem viés regional do
país de quem digitou (região só entra quando É o assunto). Vale para
pesquisa profunda na web e busca de datasets no Hub.

Comportamento na spec `busca_neutra.md` (regra de ouro). Falha da LLM
devolve o texto original (a busca segue funcionando).
"""
from . import rag
from .specs import spec
import re

# heurística barata: só paga LLM quando há sinal REAL de outro idioma.
# Não-ASCII (culinária) OU stopword PT/ES (filosofia de schopenhauer é
# ASCII puro!) OU palavra regional → traduz; inglês já passa direto.
_NAO_ASCII = re.compile(r"[^\x00-\x7F]")
_REGIONAIS = re.compile(r"\b(brasil|brazil|brasileir[oa]|portugal|português"
                        r"|portugues|em pt|no brasil|em portugal)\b", re.I)
_STOPWORDS_LATIN = re.compile(
    r"\b(de|da|do|das|dos|para|com|que|como|sobre|uma?|não|nao|em|no|na|nos"
    r"|nas|del|los|las|por|mas|como|é|e|o|a)\b", re.I)


def para_busca_inglesa(texto: str, log=print) -> str:
    """Assunto (qualquer idioma) → tema de busca em inglês neutro."""
    texto = (texto or "").strip()
    if not texto:
        return texto
    # heurística: inglês (sem sinal latino/regional) → não gasta LLM
    if (not _NAO_ASCII.search(texto) and not _REGIONAIS.search(texto)
            and not _STOPWORDS_LATIN.search(texto)):
        return texto
    try:
        chain = rag.build_prompt() | rag.llm(temperature=0.0)
        saida = chain.invoke({
            "system_text": spec("busca_neutra"),
            "context": "", "history": [],
            "question": f"ASSUNTO DIGITADO:\n{texto[:400]}",
        })
        tema = str(saida.content if hasattr(saida, "content") else saida).strip()
        if len(tema) >= 2 and tema[0] == tema[-1] and tema[0] in "\"'`":
            tema = tema[1:-1].strip()
        if tema and len(tema.split()) <= 14:
            if tema.lower() != texto.lower():
                log(f"🌐 busca em inglês: “{texto[:60]}” → “{tema[:60]}”",
                    "busca")
            return tema
        return texto
    except Exception as e:
        log(f"   ⚠️ normalização p/ inglês indisponível ({str(e)[:60]})",
            "busca")
        return texto
