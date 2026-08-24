"""Respostas FALSAS para validar a UI sem GPU/LLM (MOCK_LLM=1 no .env).

Nada aqui toca Qdrant, Redis ou llama-server: o objetivo é exercitar o JOB
real do chat (/api/query com job=true), o painel "pensando…", as fontes e os
tokens com a máquina desligada. As etapas têm `sleep` curto para o log ao
vivo aparecer progressivamente, como na execução real.

Ativar: MOCK_LLM=1 no .env (reinicie a API) — validar a UI; depois voltar a 0.
"""
import random
import time


def resposta(pergunta: str) -> str:
    """Resposta genérica em PT-BR com uma pitada de detecção: pedido de
    código vira um bloco de código; pergunta de definição vira definição."""
    p = " " + pergunta.lower() + " "
    if any(s in p for s in ("código", "codigo", "code", "script", "função",
                            "funcao", "function", "programa", "classe",
                            "endpoint", "query")):
        return (
            f"(modo mock — resposta genérica para “{pergunta.strip()[:80]}”)\n\n"
            "Aqui vai um esqueleto do que você pediu:\n\n"
            "```python\n"
            "def resolver(entrada: str) -> str:\n"
            "    \"\"\"Substitua pela lógica real.\"\"\"\n"
            "    if not entrada:\n"
            "        raise ValueError(\"entrada vazia\")\n"
            "    return entrada.strip().lower()\n"
            "```\n\n"
            "Com a LLM real, este bloco seria escrito para o SEU problema, "
            "ancorado no contexto da base selecionada."
        )
    if p.strip().startswith(("o que é", "o que e", "defina", "qual a diferença",
                             "qual a diferenca", "explique", "explique o que")):
        return (
            f"(modo mock — resposta genérica para “{pergunta.strip()[:80]}”)\n\n"
            "**Definição curta:** o termo pedido tem um significado técnico "
            "específico que a LLM real explicaria em 2-3 parágrafos, citando "
            "os fragmentos `[n]` recuperados da base quando existirem.\n\n"
            "Com o modo RAG, a resposta viria ÚNICA E EXCLUSIVAMENTE dos "
            "blocos de contexto; sem contexto suficiente, a resposta seria "
            "“Não possuo dados confiáveis o suficiente nos documentos para "
            "responder”."
        )
    return (
        f"(modo mock — resposta genérica para “{pergunta.strip()[:80]}”)\n\n"
        "Entendi o pedido. Com a LLM real, esta resposta seria gerada pelo "
        "modelo de conversa, usando o contexto selecionado (coleções/MCPs) e "
        "seguindo a spec do modo atual. Fontes: nenhuma (base mock).\n\n"
        "- Para validar o fluxo de mídia: peça “crie um GIF de…” ou “crie uma "
        "imagem de…” e use a confirmação de 1 clique.\n"
        "- Para validar fontes/tokens: veja os fragmentos e o contador 🪙 "
        "abaixo desta resposta."
    )


def docs_fake() -> list[dict]:
    """Fragmentos de mentira no formato DocFound — exercita o painel de
    Fontes, o rail Produção e a contagem “📚 N fontes”."""
    return [
        {
            "score": 0.9123,
            "source": "mock://fragmentos/guia.md",
            "titulo": "Guia do modo mock",
            "secao": "Como validar a UI",
            "categoria": "mock",
            "descricao": "fragmento de exemplo (não veio do Qdrant)",
            "resumo_pt": "MOCK_LLM=1 faz a API responder sem LLM nem Qdrant: "
                         "o job roda de verdade, com log e tokens falsos.",
            "linguagem": None,
            "qualidade": None,
            "colecao": "mock_base",
            "content": "[Guia do modo mock · Como validar a UI]\n"
                       "Com MOCK_LLM=1 no .env, /api/query executa o job real "
                       "(log ao vivo, painel pensando…, tokens) mas devolve "
                       "respostas genéricas — nenhuma GPU é usada.",
        },
        {
            "score": 0.8741,
            "source": "mock://fragmentos/fluxo.md",
            "titulo": "Fluxo do job de chat",
            "secao": "Etapas",
            "categoria": "mock",
            "descricao": "fragmento de exemplo (não veio do Qdrant)",
            "resumo_pt": "Etapas do job de chat: mensagem → cache → busca → "
                         "geração → tokens.",
            "linguagem": None,
            "qualidade": None,
            "colecao": "mock_base",
            "content": "[Fluxo do job de chat · Etapas]\n"
                       "mensagem recebida → (cache semântico) → reforma da "
                       "pergunta → busca na base → geração → tokens. No modo "
                       "mock, busca e geração são simuladas com sleeps de 0,3 s.",
        },
        {
            "score": 0.7902,
            "source": "mock://fragmentos/fontes.md",
            "titulo": "Painel de fontes",
            "secao": "Interface",
            "categoria": "mock",
            "descricao": "fragmento de exemplo (não veio do Qdrant)",
            "resumo_pt": "Cada resposta lista os fragmentos usados; o rail "
                         "Produção reúne mídias, código e fontes da conversa.",
            "linguagem": None,
            "qualidade": None,
            "colecao": "mock_base",
            "content": "[Painel de fontes · Interface]\n"
                       "O botão 📖 fontes abre o painel lateral com os "
                       "fragmentos da última resposta; o rail Produção agrupa "
                       "mídias geradas, arquivos de código e fontes.",
        },
    ]


def responder(body, log) -> dict:
    """Ponto único do mock: loga as etapas como o fluxo real (com sleeps
    curtos) e devolve uma QueryResp completa — SEM cache, SEM LLM."""
    docs = docs_fake()
    log(f"mensagem recebida (modo {body.mode}) — 🧪 MOCK_LLM ativo", "mensagem")
    time.sleep(0.3)
    log("🔎 (mock) consultando a base…", "busca")
    time.sleep(0.3)
    log(f"📚 {len(docs)} fragmento(s): mock_base ({len(docs)}) — docs FALSOS "
        "para exercitar o painel de fontes", "busca")
    time.sleep(0.3)
    log("✍️ gerando resposta (mock — nenhuma LLM foi acionada)…", "geração")
    time.sleep(0.3)
    tokens = {"entrada": random.randint(850, 950),
              "saida": random.randint(180, 220),
              "chamadas": 1}
    # (a linha "🪙 pedido completo" do wrapper do job fecha o painel sozinha)
    return {
        "question": body.question,
        "mode": body.mode,
        "collections": list(body.collections or []),
        "docs": docs,
        "answer": resposta(body.question),
        "erros": {},
        "ferramentas": [],
        "mcp_erros": {},
        "pendente": None,
        "aprovacoes_sessao": body.aprovacoes_sessao or {},
        "pergunta_busca": body.question,
        "decisao": None,
        "cache": {"usado": False, "similaridade": 0.0, "pergunta_original": ""},
        "model": "(mock)",
        "provider": "mock",
        "tokens": tokens,
    }
