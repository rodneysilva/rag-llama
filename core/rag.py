import os
import threading
"""
Componentes LangChain do RAG: embedding, Qdrant, LLM e geração da resposta.

As instruções de comportamento (chat, categorização, análise) vêm dos
arquivos de spec em core/specs/ — nada de prompt hardcoded aqui.
"""
import json
import re
import time

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore

from . import config
from . import telemetria
from .specs import spec

# llama.cpp não valida chave por padrão, mas com --api-key no llama-server
# ela É obrigatória (401 sem). Local não usa; VPS via túnel usa LLM_API_KEY.
API_KEY = os.getenv("LLM_API_KEY", "sk-no-key")

# Lembrete curto repetido no FIM do prompt (depois do histórico): o modelo dá
# mais peso ao que está perto da geração — sem isso, um histórico com respostas
# repetidas vira "exemplo" e o modelo copia a resposta anterior por inércia.
# Texto vive na SPEC lembrete_final.md (regra de ouro: comportamento em spec,
# não no código) — fallback embutido só se a spec sumir.
def _lembrete() -> str:
    try:
        from .specs import spec as _spec
        return _spec("lembrete_final").strip()
    except Exception:
        return ("Lembrete final: esta pergunta é NOVA — nunca repite uma "
                "resposta anterior; cite o fragmento ([n]) quando usar o "
                "contexto.")


def embeddings():
    """Embedding BGE-M3 via API (/v1/embeddings) — SOBE SOZINHO se estiver
    fora do ar (ciclo on-demand: busca/ingestão o acionam com prioridade).

    Subclass com TELEMETRIA: cada lote embedado vira evento tipo "embed"
    (documentos + duração + tokens de entrada estimados pela usage quando
    o servidor devolve) — o Dashboard mostra o consumo do embedding, que
    antes ficava invisível (só a LLM de chat era contada)."""
    from . import modelos as _m
    from . import telemetria as _tel
    _m.garantir_embedding()

    class _EmbeddingsContados(OpenAIEmbeddings):
        def embed_documents(self, texts, **kw):
            import time as _t
            t0 = _t.time()
            saida = super().embed_documents(texts, **kw)
            try:
                _tel.evento("embed", f"🧬 bge-m3: {len(texts)} texto(s)",
                            docs=len(texts), modelo="bge-m3",
                            duracao_s=round(_t.time() - t0, 2))
            except Exception:
                pass
            return saida

        def embed_query(self, text, **kw):
            import time as _t
            t0 = _t.time()
            saida = super().embed_query(text, **kw)
            try:
                _tel.evento("embed", "🧬 bge-m3: consulta",
                            docs=1, modelo="bge-m3",
                            duracao_s=round(_t.time() - t0, 3))
            except Exception:
                pass
            return saida

    return _EmbeddingsContados(
        api_key=API_KEY,
        base_url=config.EMBED_BASE_URL,
        model=config.EMBED_MODEL,
        check_embedding_ctx_length=False,  # envia texto puro, sem tiktoken
        # SEM timeout o cliente OpenAI espera 600 s: com o túnel/agente
        # fora do ar, TUDO que toca embedding (título da sessão, busca,
        # ingestão) ficava pendurado por minutos — 60 s cobre o lote
        # maior do bge-m3 e falha rápido quando o servidor não existe
        timeout=60,
    )


def vectorstore(client, collection=None):
    """Acesso a uma coleção do Qdrant (a padrão do .env, ou outra informada)."""
    return QdrantVectorStore(
        client=client,
        collection_name=collection or config.COLLECTION,
        embedding=embeddings(),  # langchain >= 1.0 usa "embedding" (singular)
    )


# override de PROVEDOR EXTERNO por execução (thread-local, como os
# contadores): {base_url, api_key, model, provedor} — o chat seta quando a
# conversa escolhe glm/deepseek/openai/anthropic; vazio = llama-server local
_TL = threading.local()


def set_override(prov: dict | None) -> None:
    """Define o provedor da ESTA execução (None volta para o local)."""
    _TL.prov = prov or None


def _override() -> dict | None:
    return getattr(_TL, "prov", None)


def llm(temperature=None):
    """LLM de conversa via API (/v1/chat/completions) do llama-server — OU
    de um PROVEDOR EXTERNO quando a execução tem override (glm, deepseek,
    openai, anthropic… todos falam OpenAI-compatible; a chave vem do .env).

    `temperature` sobrepõe a do .env — o agente de ferramentas usa 0:
    loop ReAct precisa de comportamento estável (mesma pergunta, mesmo
    passo a passo), não de criatividade.

    A classe conta os tokens de CADA chamada (usage devolvido pelo
    servidor) no acumulador global por serviço — tudo que passa pela LLM
    é medido, onde quer que seja chamada (chat, ingestão, estúdio…).
    """
    from . import contadores

    class LLMContada(ChatOpenAI):
        """ChatOpenAI que registra prompt/completion tokens de cada chamada —
        no acumulador, no 'pensando…' (tempo real via thread-local) e na
        telemetria persistente (logs/telemetria.jsonl)."""

        def _registrar(self, entrada, saida, dur):
            try:
                contadores.registrar(entrada, saida)
                # MODELO REAL: config.LLM_MODEL fica VELHO após trocas feitas
                # na estação (o .env da VPS não acompanha) — a telemetria e o
                # dashboard "por modelo" liam sempre o mesmo nome. Quem sabe
                # é o SERVIDOR (cache 10 s): lê /v1/models do llama-server.
                # Com override externo, o nome é o modelo ESCOLHIDO.
                try:
                    ov = _override()
                    if ov:
                        modelo_agora = f"[{ov['provedor']}] {ov['model']}"
                    else:
                        from .modelos import servido, CHAT_PORTA
                        modelo_agora = servido(CHAT_PORTA) or config.LLM_MODEL
                except Exception:
                    modelo_agora = config.LLM_MODEL
                # TEMPO REAL: o job desta thread (se registrou o log) mostra
                # os tokens de CADA chamada enquanto ainda está pensando —
                # com a ETAPA entre colchetes (reformulação/resposta/agente
                # passo N/verificação): o par chamada→retorno fica legível
                log = contadores.log_atual()
                if log:
                    et = contadores.etapa_atual()
                    log(f"🪙 LLM {contadores.servico_atual()}"
                        + (f" [{et}]" if et else "")
                        + f": 🔻{entrada} entrada · 🔺{saida} saída · {dur:.1f}s",
                        "tokens")
                telemetria.evento("llm", f"🧠 {modelo_agora}: "
                                         f"🔻{entrada} · 🔺{saida} · {dur:.1f}s "
                                         f"[{contadores.servico_atual()}]",
                                  entrada=entrada, saida=saida,
                                  duracao_s=round(dur, 2),
                                  modelo=modelo_agora,
                                  servico=contadores.servico_atual())
            except Exception:
                pass  # telemetria nunca derruba a resposta

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            t0 = time.time()
            resultado = super()._generate(messages, stop=stop,
                                          run_manager=run_manager, **kwargs)
            try:
                uso = (resultado.llm_output or {}).get("token_usage") or {}
                self._registrar(int(uso.get("prompt_tokens", 0)),
                                int(uso.get("completion_tokens", 0)),
                                time.time() - t0)
            except Exception:
                pass
            return resultado

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            """STREAM também conta (bug pré-existente: respostas em
            streaming — rag com on_token — passavam POR AQUI e saíam com
            0 chamadas/0 tokens no rodapé e na telemetria). O usage viaja
            no ÚLTIMO chunk (usage_metadata) quando stream_usage=True.
            Também mede a velocidade REAL de geração: do 1º chunk em
            diante é geração pura (o tempo até ele é o pré-processamento
            do prompt) — o rodapé antigo dividia saída pelo total e o
            'tok/s' mentia com prompt grande."""
            t0 = time.time()
            t_primeiro = None
            entrada = saida = 0
            for pedaco in super()._stream(messages, stop=stop,
                                          run_manager=run_manager, **kwargs):
                # o _stream devolve ChatGenerationChunk: o usage mora na
                # MENSAGEM (.message.usage_metadata) — no chunk cru é None
                msg = getattr(pedaco, "message", pedaco)
                uso = getattr(msg, "usage_metadata", None)
                if uso:
                    entrada = int(uso.get("input_tokens") or entrada)
                    saida = int(uso.get("output_tokens") or saida)
                if t_primeiro is None:
                    t_primeiro = time.time()   # 1º token = fim do pré-processamento
                yield pedaco
            dur_total = time.time() - t0
            try:
                if saida and t_primeiro:
                    contadores.set_vel_geracao(
                        round(saida / max(dur_total - (t_primeiro - t0), 1e-3), 1))
            except Exception:
                pass
            self._registrar(entrada, saida, dur_total)

    # TEMPERATURA: uma regra só (pedido do dono) — valor do Sistema/.env
    # vale para TODAS as LLMs (local e provedores externos); default 0.5,
    # alterável na tela Sistema sem restart. O antigo 0.15 para "coder"
    # criava exceção invisível que contradizia o valor configurado.
    if temperature is None:
        temperatura = config.TEMPERATURE
    else:
        temperatura = temperature
    ov = _override()
    return LLMContada(
        api_key=(ov or {}).get("api_key") or API_KEY,
        base_url=(ov or {}).get("base_url") or config.LLM_BASE_URL,
        model=(ov or {}).get("model") or config.LLM_MODEL,
        temperature=temperatura,
        # usage no ÚLTIMO chunk do stream (alimenta o _stream do LLMContada;
        # sem isto o rodapé de respostas em streaming ficava 0/0)
        stream_usage=True,
        # sem timeout o job do chat podia ficar "running" para sempre com os
        # slots do llama-server ocupados (fila em vez de erro) — 15 min cobre
        # as respostas mais longas com contexto grande
        timeout=900,
    )


def _history_messages(history) -> list:
    """Últimas 6 mensagens do chat como objetos LangChain (valores, não template)."""
    msgs = []
    for m in (history or [])[-6:]:
        classe = AIMessage if m.get("role") == "assistant" else HumanMessage
        msgs.append(classe(content=str(m.get("content", ""))))
    return msgs


def _system_text(nome_spec: str) -> str:
    """Texto da spec + extras do operador (entra no prompt como valor, não template)."""
    texto = spec(nome_spec)
    if config.PROMPT_SYSTEM.strip():  # instruções extras opcionais do operador
        texto += "\n\nInstruções adicionais do operador:\n" + config.PROMPT_SYSTEM
    return texto


def build_prompt():
    """Prompt do chat (modo RAG): spec fixa (specs/chat.md) + contexto.

    A spec e o histórico entram como VALORES, então texto com { } não quebra
    o template.
    """
    return ChatPromptTemplate.from_messages([
        ("system", "{system_text}\n\nContexto:\n{context}"),
        MessagesPlaceholder("history"),
        ("system", _lembrete()),
        ("human", "{question}"),
    ])


def format_docs(docs):
    """Junta os documentos recuperados em um texto numerado para o prompt.

    Cada fragmento traz sua origem (coleção · área) para a LLM saber de que
    domínio aquele texto é (tecnologia, medicina, psicologia…).
    """
    partes = []
    for i, d in enumerate(docs, 1):
        colecao = d.metadata.get("colecao", "")
        area = d.metadata.get("area", "")
        origem = " · ".join(x for x in (colecao, area) if x)
        tag = f" ({origem})" if origem else ""
        partes.append(f"[{i}]{tag} {d.page_content}")
    return "\n\n".join(partes)


def _contexto_com_bases(bases: str | None, docs_formatados: str) -> str:
    """Prepends ao contexto o mapa das bases selecionadas (se o chamador enviar).

    `bases` já vem formatado pelo chamador (API), com nome + área/categoria +
    descrição de cada coleção consultada — é o que faz a LLM "compreender" de
    que domínio é o material antes de ler os fragmentos.
    """
    return f"{bases}\n\n{docs_formatados}" if bases else docs_formatados


# ─── CONVERSA NATURAL (guardrail de código — o modelo pequeno ecoa o
# envelope do sistema: "Contexto recuperado da base: … (nada foi
# recuperado…) Resposta: …". A conversa do chat tem que ser uma MENSAGEM
# natural; as fontes vivem no painel da interface, não no texto) ───
_RE_ECO_INICIO = re.compile(
    r"^\s*(?:[#>*\-•\s]*)*(?:\*{0,2})(?:contexto recuperado(?: da base)?"
    r"|fragmentos?(?: recuperados?)?|base consultada|resposta|answer)"
    r"(?:\*{0,2})\s*:?\s*(?:\*{0,2})\s*$", re.I)
_RE_ECO_NADA = re.compile(r"^\s*[\(\*]{0,2}nada foi recuperado", re.I)
_RE_RESPOSTA_LABEL = re.compile(r"^\s*\*{0,2}resposta\*{0,2}\s*:\s*", re.I)
_RE_SECAO_FONTES = re.compile(
    r"\n+\s*(?:[#>*\-•\s]*)*\*{0,2}\s*(?:📚\s*)?fontes?\s*(?:da\s+base)?"
    r"[\s*:#>-]*\n", re.I)


def naturalizar(texto: str) -> str:
    """Remove do INÍCIO/FIM da resposta os artefatos de eco do prompt e a
    seção "Fontes:" final (as fontes seguem no painel — pedido do dono:
    "a conversa no chat deve ser natural").

    Casos cobertos (vistos em produção com modelos 7–8B):
    1. eco completo "Contexto recuperado da base: … Resposta: X" → fica X;
    2. linhas-cabeçalho soltas ("Contexto recuperado da base:", "(nada foi
       recuperado…)", "Resposta:") no início → removidas;
    3. seção "Fontes:"/ "Fontes" no final (≤8 linhas curtas) → removida.
    """
    if not texto:
        return texto
    t = texto.strip()
    # 1) eco completo: o modelo reproduziu o envelope E marcou "Resposta:"
    m = re.search(r"^\s*\**\s*contexto recuperado.*?\n\**\s*resposta\**\s*:", t,
                  re.I | re.S)
    if m:
        t = t[m.end():].lstrip(" \n*#>-")
    # 2) cabeçalhos soltos no início (repete enquanto casar)
    for _ in range(6):
        linhas = t.split("\n")
        while linhas and not linhas[0].strip():
            linhas.pop(0)
        if not linhas:
            return ""
        if (_RE_ECO_INICIO.match(linhas[0]) or _RE_ECO_NADA.match(linhas[0])):
            linhas.pop(0)
            t = "\n".join(linhas)
            continue
        linha = _RE_RESPOSTA_LABEL.sub("", linhas[0], count=1)
        if linha != linhas[0]:
            linhas[0] = linha
            t = "\n".join(linhas)
            continue
        break
    # 3) seção "Fontes:" FINAL — só quando o restante é lista curta de
    #    citações/origens (não arranca conteúdo real do meio da resposta)
    m = _RE_SECAO_FONTES.search(t)
    if m:
        calda = t[m.end():].strip("\n")
        linhas_c = [l for l in calda.split("\n") if l.strip()]
        if (linhas_c and len(linhas_c) <= 8
                and all(len(l.strip()) <= 220 for l in linhas_c)
                and "```" not in calda):
            t = t[:m.start()].rstrip(" \n*#>-")
    return t.strip()


def _gerar(chain, payload: dict, on_token=None) -> str:
    if on_token is None:
        return naturalizar(chain.invoke(payload))
    buf = []
    for pedaco in chain.stream(payload):
        if pedaco:
            buf.append(pedaco)
            try:
                on_token(''.join(buf))
            except Exception:
                pass
    # o AO VIVO mostra o bruto (natural de um stream); o FINAL gravado é
    # naturalizado — eco de envelope/sessão de fontes não fica na conversa
    return naturalizar(''.join(buf))


def answer(question, docs, history=None, bases=None, on_token=None):
    """Resposta completa em uma string (usada pela API/webui)."""
    chain = build_prompt() | llm() | StrOutputParser()
    return _gerar(chain, {"system_text": _system_text("chat"),
                          "context": _contexto_com_bases(bases, format_docs(docs)),
                          "question": question, "history": _history_messages(history)},
                  on_token)


def answer_free(question, history=None, on_token=None):
    """Modo livre: gera a resposta com o conhecimento do modelo, sem busca no Qdrant."""
    chain = (ChatPromptTemplate.from_messages([
        ("system", "{system_text}"),
        MessagesPlaceholder("history"),
        ("system", _lembrete()),
        ("human", "{question}"),
    ]) | llm() | StrOutputParser())
    return _gerar(chain, {"system_text": _system_text("geracao"), "question": question,
                          "history": _history_messages(history)}, on_token)


def _ultimo_codigo(history) -> str:
    """Último bloco de código das respostas da conversa (para o envelope —
    pergunta CURTA tipo 'quero que seja web' quase sempre se refere a ELE;
    sem isto o 7B respondia um Hello World genérico fora do assunto)."""
    for m in reversed(history or []):
        if m.get("role") != "assistant":
            continue
        txt = str(m.get("content", ""))
        if "```" not in txt:
            continue
        partes = txt.split("```")
        blocos = [b for b in partes[1::2] if b.strip()]
        if not blocos:
            continue
        cod = blocos[-1].strip()
        # primeira linha pode ser a linguagem (```python) — descarta
        linhas = cod.splitlines()
        if linhas and linhas[0].strip() and not any(c in linhas[0] for c in "(=.;:"):
            cod = "\n".join(linhas[1:])
        return cod[:2400]
    return ""


def answer_hybrid(question, docs, history=None, bases=None, on_token=None):
    """Modo híbrido: busca na base como referência e a LLM contextualiza/completa."""
    chain = (ChatPromptTemplate.from_messages([
        ("system", "{system_text}\n\nContexto recuperado da base (pode estar vazio ou pouco relevante):\n{context}"),
        MessagesPlaceholder("history"),
        ("system", _lembrete()),
        ("human", "{question}"),
    ]) | llm() | StrOutputParser())
    contexto = format_docs(docs) if docs else "(nada foi recuperado da base para esta pergunta)"
    contexto = _contexto_com_bases(bases, contexto)
    # ENVELOPE (dados, não comportamento): pergunta CURTA de continuação
    # ganha o ÚLTIMO CÓDIGO da conversa colado — o 7B não reconecta sozinho
    # e respondia fora do assunto (Hello World para um pedido de culinária).
    if len(question.split()) <= 8:
        cod = _ultimo_codigo(history)
        if cod:
            contexto += ("\n\n[ÚLTIMO CÓDIGO desta conversa — a pergunta atual é "
                         "curta e provavelmente se REFERE a ele; transforme/ajuste "
                         "ESTE código conforme o pedido, mantendo o assunto]:\n```\n"
                         + cod + "\n```")
    return _gerar(chain, {"system_text": _system_text("hibrido"), "context": contexto,
                         "question": question, "history": _history_messages(history)}, on_token)


# GUARDRAIL de reformulação (código, não só spec — o 7B ignora spec): a
# saída é uma CONSULTA, não uma resposta. Padrões de início de resposta
# ("Claro!", "Vou criar…") ou consulta longa demais = LLM copiou a
# resposta anterior → usa a pergunta original (a busca segue com termos
# reais do usuário, nunca com texto gerado).
_RE_RESPOSTA = re.compile(
    r"^(claro|certo|sure|of course|entendo|pe[çc]o desculpas|desculpe|"
    r"vou |vou criar|para criar|para responder|abaixo|aqui est[áa]|"
    r"i'?ll |i will|let'?s )\b", re.I)


def _sanear_consulta(consulta: str, question: str, log=None) -> str:
    """Consulta válida ou a pergunta original. Regras: 1 linha, ≤ 25
    palavras, sem padrão de resposta, sem código/markdown."""
    c = " ".join((consulta or "").split()).strip().strip('"`')
    if not c:
        return question
    if "\n" in consulta or "```" in consulta:
        c = c.splitlines()[0]
    palavras = len(c.split())
    if palavras > 25 or _RE_RESPOSTA.search(c):
        if log:
            log(f"⚠️ reformulação inválida ({palavras} palavras"
                f"{' , padrão de resposta' if _RE_RESPOSTA.search(c) else ''})"
                " — usando a pergunta original na busca", "busca")
        return question
    return c


def reformula(question: str, history) -> str:
    """Reescreve a pergunta como consulta de busca autossuficiente.

    A busca vetorial não vê o histórico: "por que me falou de crianças?" vira
    embedding de pronomes soltos. Aqui a pragmática é resolvida ANTES — a
    LLM condensa histórico + pergunta numa consulta com os termos concretos
    (spec em specs/reformulacao.md). Sem histórico, devolve a pergunta como
    está (nada a resolver). O embedding do BGE-M3 pondera fortemente os
    termos raros do texto: a consulta boa é a que carrega o termo de domínio
    certo, não a palavra final da frase do usuário.

    GUARDRAILS: histórico entra SÓ com mensagens do USUÁRIO (respostas do
    assistente viravam modelo de saída — o 7B devolvia "Claro! Vou criar…"
    como "consulta"); e a saída passa por _sanear_consulta.
    """
    if not history:
        return question
    hist_user = [m for m in history[-6:] if m.get("role") == "user"]
    if not hist_user:
        return question
    prompt = ChatPromptTemplate.from_messages([
        ("system", spec("reformulacao")),
        MessagesPlaceholder("history"),
        ("human", "{question}"),
    ])
    texto = (prompt | llm(temperature=0) | StrOutputParser()).invoke(
        {"question": question, "history": _history_messages(hist_user)})
    return _sanear_consulta(texto, question)


def _termos_busca(pergunta: str) -> str:
    """Palavras >3 chars da pergunta, em minúsculo — a consulta do filtro
    full-text (MatchText casa TODOS os termos: frases completas afinam,
    IDs/códigos exatos são o alvo)."""
    limpos = [p.strip(".,;:!?\"'()[]{}").lower() for p in pergunta.split()]
    return " ".join(t for t in limpos if len(t) > 3)


# índices full-text já criados (1x por coleção por processo)
_indices_texto: set[str] = set()


def _busca_lexical(client, colecao: str, termos: str, limite: int = 40) -> list:
    """Busca full-text do Qdrant no `page_content` (payload que já existe —
    SEM reingestão). O índice é criado LAZY (tentativa única por coleção;
    MatchText funciona SEM índice por full-scan, o índice é velocidade).
    O scroll NÃO é ranqueado: a ordem entra só como rank da fusão RRF."""
    from langchain_core.documents import Document as _Doc
    from qdrant_client.models import FieldCondition, Filter, MatchText
    if colecao not in _indices_texto:
        try:
            client.create_payload_index(collection_name=colecao,
                                        field_name="page_content",
                                        field_schema="text")
        except Exception:
            pass  # já existe ou versão sem suporte — o full-scan segue valendo
        _indices_texto.add(colecao)
    pontos, _ = client.scroll(
        collection_name=colecao,
        scroll_filter=Filter(must=[FieldCondition(
            key="page_content", match=MatchText(text=termos))]),
        limit=limite, with_payload=True)
    docs = []
    for p in pontos:
        payload = p.payload or {}
        texto = str(payload.get("page_content") or "")
        if not texto:
            continue
        docs.append(_Doc(page_content=texto,
                         metadata=dict(payload.get("metadata") or {})))
    return docs


def _chave_dedupe(texto: str) -> str:
    """md5 do conteúdo normalizado — chave do dedupe global E da fusão RRF
    (o mesmo chunk achado pelas duas buscas soma os dois sinais)."""
    import hashlib
    return hashlib.md5(re.sub(r"\s+", " ", texto).strip()
                       .encode("utf-8")).hexdigest()


def search(client, collections, question: str, k: int | None = None,
           log=None):
    """Busca HÍBRIDA ampla em várias coleções (F2-6): densa (embedding, k*3
    candidatos) + full-text (MatchText, limit 40) fundidas por RRF.

    `log(msg, etapa)` (opcional) narra CADA consulta ao Qdrant e o que
    voltou (densos/lexicais/escolhidos + top resultados) — o raciocínio do
    chat mostra a conversa inteira com a base, linha por linha.

    A fusão RRF (Reciprocal Rank Fusion) soma 1/(60+rank) de cada lista por
    chave de conteúdo: quem aparece bem nas DUAS fica no topo; achados
    SÓ-TEXTO entram mesmo sem score denso — match exato de ID/código é
    sinal forte que o embedding dilui. Cada coleção segue contribuindo com
    até k pedaços DIVERSIFICADOS (máximo 2 por arquivo de origem).

    Retorna ([ (documento, score, colecao_de_origem) ], {colecao: erro}).
    Coleções com problema (inexistentes, dimensão errada) não derrubam a
    busca. O total é limitado a k x 4 (no máximo 4x TOP_K) para caber no
    contexto da LLM. Documentos densos com score abaixo de SCORE_MIN (.env)
    são descartados; só-texto entram com score = SCORE_MIN (não há similaridade
    semântica medida — o sinal deles é lexical).
    """
    k = k or config.TOP_K
    achados, erros = [], {}
    vistos: set[str] = set()  # dedupe GLOBAL: mesmo conteúdo em coleções
    _log = log or (lambda *a, **kwa: None)  # sem log → no-op (CLI/scripts)
    for nome in collections:
        _log(f"🗄️ qdrant → {nome}: consulta densa (top {k * 3}) + "
             "full-text…", "busca")
        try:
            densos = vectorstore(client, nome).similarity_search_with_score(
                question, k=k * 3)
        except Exception as e:
            erros[nome] = str(e)[:120]
            _log(f"🗄️ qdrant ✕ {nome}: {erros[nome]}", "busca")
            continue
        # lexical: mesma pergunta, filtro full-text (termos >3 chars); se a
        # frase completa não acha nada, cai para o termo mais longo
        termos = _termos_busca(question)
        lexicos = []
        if termos:
            try:
                lexicos = _busca_lexical(client, nome, termos)
                if not lexicos and " " in termos:
                    lexicos = _busca_lexical(
                        client, nome, max(termos.split(), key=len))
            except Exception:
                lexicos = []
        # fusão RRF por chave de conteúdo: rank denso (já vem ordenado) +
        # rank textual (ordem do scroll); a soma ordena os candidatos
        rrf: dict[str, float] = {}
        for r, (d, _s) in enumerate(densos):
            c = _chave_dedupe(d.page_content)
            rrf[c] = rrf.get(c, 0.0) + 1.0 / (60 + r + 1)
        for r, d in enumerate(lexicos):
            c = _chave_dedupe(d.page_content)
            rrf[c] = rrf.get(c, 0.0) + 1.0 / (60 + r + 1)
        unificados: dict[str, tuple] = {}
        for d, score in densos:
            unificados.setdefault(_chave_dedupe(d.page_content), (d, float(score)))
        for d in lexicos:  # só-texto: entram mesmo sem score denso
            unificados.setdefault(_chave_dedupe(d.page_content), (d, None))
        ordenados = sorted(unificados.items(),
                           key=lambda kv: rrf.get(kv[0], 0.0), reverse=True)
        escolhidos, por_arquivo = [], {}
        for chave, (d, score) in ordenados:
            if score is not None and score < config.SCORE_MIN:
                continue
            # por NOME do arquivo (não pelo caminho completo): o mesmo
            # arquivo ingerido duas vezes com grafias diferentes de
            # caminho (relativo/absoluto) é o MESMO arquivo
            origem = (str(d.metadata.get("source", "?"))
                      .replace("\\", "/").rsplit("/", 1)[-1])
            if por_arquivo.get(origem, 0) >= 2:  # já pegou 2 deste arquivo
                continue                          # → amplia para outros
            if chave in vistos:
                continue  # duplicado exato (outra coleção): 1ª ocorrência fica
            vistos.add(chave)
            por_arquivo[origem] = por_arquivo.get(origem, 0) + 1
            d.metadata["colecao"] = nome  # origem visível no prompt
            escolhidos.append((d, score if score is not None
                               else round(config.SCORE_MIN, 4), nome,
                              rrf.get(chave, 0.0)))
            if len(escolhidos) >= k:
                break
        # RETORNO do qdrant narrado: quantos vieram de cada lado da busca
        # híbrida, quantos sobreviveram ao filtro de score/dedup e os
        # melhores (score + arquivo) — limpo e legível no raciocínio
        _log(f"🗄️ qdrant ← {nome}: {len(densos)} denso(s) + "
             f"{len(lexicos)} textual(is) → {len(escolhidos)} escolhido(s)"
             + (f" (descartados: {len(ordenados) - len(escolhidos)})"
                if len(ordenados) > len(escolhidos) else ""), "busca")
        for d, s, _c, _r in escolhidos[:3]:
            _arq = (str(d.metadata.get("source")
                         or d.metadata.get("arquivo")
                         or d.metadata.get("titulo") or "?")
                    .replace("\\", "/").rsplit("/", 1)[-1])
            _log(f"   ↳ {s:.3f} · {_arq}"
                 + (f" · {str(d.page_content)[:160]}…" if d.page_content else ""),
                 "busca")
        if not escolhidos:
            _log(f"   ↳ nada acima do corte (score ≥ {config.SCORE_MIN})",
                 "busca")
        achados += escolhidos
    # ordem final = RRF (a fusão manda, não o score bruto de similaridade)
    achados.sort(key=lambda t: t[3], reverse=True)
    achados = achados[: k * min(len(collections), 4)]
    return [(d, s, c) for d, s, c, _ in achados], erros


def answer_stream(question, docs, history=None, bases=None):
    """Resposta em streaming (usada pelo CLI)."""
    chain = build_prompt() | llm() | StrOutputParser()
    return chain.stream({"system_text": _system_text("chat"),
                         "context": _contexto_com_bases(bases, format_docs(docs)),
                         "question": question, "history": _history_messages(history)})


# ---------- Tarefas auxiliares da LLM (categorizar / analisar) ----------

def _extract_json(texto: str) -> dict:
    """Extrai o primeiro objeto JSON da resposta da LLM (tolerante a cercas)."""
    try:
        return json.loads(texto[texto.index("{"): texto.rindex("}") + 1])
    except Exception:
        return {}


def _ask_json(nome_spec: str, conteudo: str) -> dict:
    """Pergunta algo à LLM seguindo uma spec e espera JSON de volta.

    Toda a instrução de formato/comportamento vive na spec — o código só
    entrega o conteúdo (definição do projeto: nada de prompt hardcoded).
    """
    prompt = ChatPromptTemplate.from_messages([
        ("system", "{spec}"),
        ("human", "{conteudo}"),
    ])
    texto = (prompt | llm() | StrOutputParser()).invoke(
        {"spec": spec(nome_spec), "conteudo": conteudo})
    return _extract_json(texto)


def categorize(amostra: str, origem: str = "") -> dict:
    """Categoriza um documento: {area, categoria, descricao} em português (via LLM).

    `area` é o domínio controlado (tecnologia, medicina, psicologia…) definido
    em specs/categorizacao.md; `categoria` é o tema específico.
    """
    conteudo = f"Arquivo: {origem}\n\nAmostra:\n{amostra[:1200]}"
    r = _ask_json("categorizacao", conteudo)
    return {
        "area": str(r.get("area") or "indeterminado"),
        "categoria": str(r.get("categoria") or "sem_categoria"),
        "descricao": str(r.get("descricao") or ""),
    }


def analyze_collection(nome: str, amostras: list[str]) -> dict:
    """Analisa uma coleção: {area, categoria, descricao, resumo} em PT (via LLM)."""
    if amostras:
        conteudo = f"Coleção: {nome}\n\nAmostras:\n" + "\n---\n".join(amostras)
    else:
        conteudo = f"Coleção: {nome}\n\n(sem amostras — coleção vazia)"
    r = _ask_json("analise_colecoes", conteudo)
    return {
        "area": str(r.get("area") or "indeterminado"),
        "categoria": str(r.get("categoria") or "indeterminado"),
        "descricao": str(r.get("descricao") or ""),
        "resumo": str(r.get("resumo") or ""),
    }
