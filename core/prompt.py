"""Melhoria de prompt — reescreve o rascunho do usuário na melhor forma.

Universal (pedido do dono): entende SOMENTE o que está digitado — pergunta,
pedido de código, instrução ou (quando o seletor de mídia está ativo, como
DICA extra) geração de mídia — e propõe a forma melhor de escrever.
Comportamento na spec `prompt_melhoria.md` (regra de ouro: nada hardcoded).
Uma ÚNICA chamada à LLM de conversa; usado pelo botão ✨ do composer.
"""
from . import rag
from .specs import spec


def melhorar(ideia: str, tipo: str = "", contexto: str = "") -> str:
    """Rascunho → texto reescrito (mesmo idioma, mesma intenção, spec).
    `tipo` é DICA opcional (imagem/vídeo/gif quando o seletor está ativo).
    `contexto` = mensagens do usuário + referência selecionada.
    SEM CACHE (pedido do dono: o ✨ deve respeitar O QUE ESTÁ ESCRITO — o
    cache semântico casava rascunhos parecidos e devolvia a resposta de
    OUTRO rascunho). Falha da LLM devolve o próprio rascunho."""
    ideia = (ideia or "").strip()[:600]
    if not ideia:
        return ""
    tipo = (tipo or "").strip().lower()
    pergunta = f"RASCUNHO DO USUÁRIO:\n{ideia}"
    if tipo:
        pergunta = f"DICA DE TIPO (selecionada no composer): {tipo}\n{pergunta}"
    if contexto.strip():
        pergunta = (f"CONTEXTO DA CONVERSA (mensagens do usuário, mais recente "
                    f"por último):\n{contexto.strip()[-1200:]}\n\n{pergunta}")
    try:
        chain = rag.build_prompt() | rag.llm(temperature=0.3)
        saida = chain.invoke({
            "system_text": spec("prompt_melhoria"),
            "context": "",
            "history": [],
            "question": pergunta,
        })
        texto = str(saida.content if hasattr(saida, "content") else saida).strip()
        # a spec manda responder SÓ o texto; cercou de aspas → limpa 1 camada
        if len(texto) >= 2 and texto[0] == texto[-1] and texto[0] in "\"'`":
            texto = texto[1:-1].strip()
        return texto[:900] or ideia
    except Exception:
        return ideia
