"""
Agente do modo híbrido com ferramentas (MCP): loop ReAct simples —
a LLM pensa, chama uma ferramenta, observa o resultado e repete até a
resposta final. As regras de comportamento estão em specs/ferramentas.md.
"""
import asyncio
import json
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from . import contadores, rag
from .specs import spec

MAX_PASSOS = 6

# POLÍTICA ANTI-REFÉM: o operador não pode virar gargalo de cliques.
# Ferramentas de RACIOCÍNIO e de LEITURA (sem efeito colateral — não criam,
# alteram nem apagam nada) passam DIRETO, sem portão de aprovação; tudo
# continua registrado em `usos` (visível na resposta). Só operações com
# EFEITO REAL (write/create/delete/update/post/move/exec…) exigem a
# decisão de alguém — informação corporativa precisa de gente, mas gente
# refém de clique é erro de design.
SEM_PORTAO_EXATO = {"sequentialthinking"}
_PADROES_LEITURA = (
    "read", "get", "list", "search", "query", "fetch", "find", "describe",
    "summarize", "analyze", "think", "inspect", "show", "view", "check",
    "health", "status", "stat", "ls", "glob", "grep", "head", "explain",
)
# escrita NUNCA passa direto — lista AMPLA de verbos de efeito real (o
# caso `search_replace` atravessava: "search" é leitura e "replace" não
# estava aqui; a checagem de escrita vem ANTES da de leitura)
_PADROES_ESCRITA = (
    "write", "create", "delete", "update", "post", "put", "patch", "remove",
    "replace", "move", "rename", "copy", "exec", "run", "send", "insert",
    "drop", "commit", "push", "deploy", "install", "edit", "mkdir", "make",
    "truncate", "flush", "purge", "clear", "kill", "terminate", "overwrite",
    "save", "destroy", "upload",
)


def _sem_portao(nome: str) -> bool:
    """True = executa sem pedir aprovação (raciocínio/leitura apenas)."""
    n = (nome or "").lower()
    if n in SEM_PORTAO_EXATO:
        return True
    # 1º por TOKEN (search_replace → ["search","replace"] pega a escrita
    # que a substring de leitura esconderia); 2º substring (camelCase:
    # deleteOldItems não tem separador)
    tokens = [t for t in re.split(r"[^a-z0-9]+", n) if t]
    if any(p in tokens or n.startswith(p) for p in _PADROES_ESCRITA):
        return False
    if any(p in n for p in _PADROES_ESCRITA):  # escrita NUNCA passa direto
        return False
    return any(p in tokens or p in n for p in _PADROES_LEITURA)


def _parse(resposta: str) -> tuple[str | None, str]:
    """Devolve (acao, argumento) ou (None, resposta_final) se já concluiu."""
    if "Final Answer:" in resposta:
        return None, resposta.split("Final Answer:", 1)[1].strip()
    m = re.search(r"Action:\s*(.+?)\s*\n\s*Action Input:\s*(.+)", resposta, re.S)
    if not m:  # não seguiu o formato: considera a resposta como final
        return None, resposta.strip()
    return m.group(1).strip(), m.group(2).strip()


def _argumento_json(argumento: str) -> dict:
    """Primeiro objeto JSON BALANCEADO do Action Input.

    Modelos pequenos às vezes emendam texto depois do JSON — inclusive uma
    Observation inventada com outro objeto dentro. Extrair da primeira `{`
    até a última `}` engole esse lixo e quebra o parse; o scan balanceado
    devolve só o objeto de argumentos real.
    """
    ini = argumento.find("{")
    while ini != -1:
        profundidade = 0
        for i in range(ini, len(argumento)):
            if argumento[i] == "{":
                profundidade += 1
            elif argumento[i] == "}":
                profundidade -= 1
                if profundidade == 0:
                    try:
                        return json.loads(argumento[ini:i + 1])
                    except Exception:
                        break  # desbalanceado/inválido: tenta o próximo `{`
        ini = argumento.find("{", ini + 1)
    return {"input": argumento.strip()}


_MARCAS_ERRO = ("mcp error", "erro", "error", "denied", "does not exist", "não existe",
                "no such file", "enoent", "not found", "permission", "invalid", "failed",
                "falhou", "negou")

# frases que afiram TER EXECUTADO uma ação (não apenas explicar)
_CLAIMS_ACAO = ("foi criado", "criei", "arquivo criado", "foi escrito", "escrevi",
                "a ferramenta confirmou", "successfully wrote", "successfully created",
                "criada com sucesso", "criado com sucesso")


def _falhou(observacao: str) -> bool:
    """Heurística: a Observation indica falha da ferramenta?"""
    o = observacao.lower()
    return any(marca in o for marca in _MARCAS_ERRO)


def _registro(usos: list) -> str:
    """Resumo factual do que foi chamado e do que cada chamada devolveu."""
    return "\n".join(
        f"- {u['ferramenta']} ← {u['entrada'][:80]}\n  → {u['resultado'][:200]}" for u in usos)


def _final(resposta: str, usos: list) -> str:
    """Resposta final do agente — NUNCA vazia: se o modelo encerrou sem texto
    (slot cheio, só emitiu Thought:), devolve o registro real do que fez."""
    if (resposta or "").strip():
        return resposta
    if usos:
        return ("(o agente encerrou sem resposta final — registro real do que "
                "foi executado:)\n" + _registro(usos))
    return "(o agente encerrou sem resposta final e sem nenhuma chamada de ferramenta)"


def _executar(ferr, argumento: str) -> str:
    """Executa a ferramenta (sempre DEPOIS de aprovada) e devolve a Observation.

    Limite GENEROSO (pedido do dono: "o mcp de busca tem que retornar
    informações completas e extensas"): resultados de busca/fetch chegam
    inteiros na íntegra possível — o corte 4k antigo truncava exatamente
    o material que o usuário marcava o servidor para obter."""
    try:
        observacao = str(asyncio.run(ferr.ainvoke(_argumento_json(argumento))))[:8000]
    except Exception as e:
        observacao = f"Erro na ferramenta: {e}"
    if _falhou(observacao):
        observacao += ("\n(FALHOU — você ainda tem passos: corrija os argumentos "
                       "com os nomes exatos do schema, use um caminho DENTRO dos "
                       "diretórios permitidos indicados no erro, ex. crie a pasta "
                       "antes com create_directory, ou conclua ADMITINDO a falha)")
    return observacao


def _verifica_e_anota(chain, system_text: str, contexto: str, historico, pergunta: str,
                      resposta: str, usos: list) -> str:
    """Anti-invenção: confere a resposta contra o REGISTRO REAL das ferramentas.

    Modelo pequeno afirma "arquivo criado" mesmo quando toda chamada falhou
    (ou quando nem chegou a chamar) — e ignora instrução para reescrever.
    Por isso a verificação é BINÁRIA (contra o registro) e a nota final é
    anexada pelo CÓDIGO, sem depender da honestidade do modelo. Roda mesmo
    sem chamadas: "criei o arquivo" com registro vazio também é invenção.
    """
    registro = _registro(usos) if usos else "(nenhuma ferramenta foi chamada)"

    # casos 100% determinísticos (não dependem da LLM verificadora):
    # 1) afirma ter executado sem NENHUMA chamada;
    # 2) afirma execução com alguma falha no registro (meia-verdade é mentira).
    claims = any(c in resposta.lower() for c in _CLAIMS_ACAO)
    if claims and (not usos or any(_falhou(u["resultado"]) for u in usos)):
        return (resposta + "\n\n---\n⚠️ Nota de verificação automática: o registro das "
                "ferramentas NÃO confirma que a tarefa foi concluída. Registro real:\n"
                + registro)
    if not usos:  # sem chamadas e sem claim de execução: resposta comum, nada a verificar
        return resposta
    if any(n in resposta.lower() for n in ("negou", "negada", "negar")):
        return resposta  # o modelo já admitiu a negação do operador: nada a corrigir
    checagem = chain.invoke({
        "system_text": system_text, "context": contexto, "history": historico,
        "pergunta": f"TAREFA PEDIDA AO AGENTE:\n{pergunta}\n\nREGISTRO REAL das chamadas de "
        f"ferramenta (única verdade sobre o que aconteceu):\n{registro}\n\n"
        "A tarefa pedia alguma ação executável por ferramenta (criar/editar arquivos, "
        "consultar serviço, calcular…)? E, se pedia, o registro confirma que foi concluída "
        "com sucesso? Responda em UMA linha, exatamente um destes formatos:\n"
        "'concluida: sim' | 'concluida: não' | 'sem acao'"})
    m = re.search(r"conclu[ií]da:\s*(sim|não|nao)|sem acao|sem ação", checagem, re.I)
    veredicto = (m.group(0).lower() if m else "concluida: sim")  # sem veredicto: não anota
    if "não" in veredicto or "nao" in veredicto:
        return (resposta + "\n\n---\n⚠️ Nota de verificação automática: o registro das "
                "ferramentas NÃO confirma que a tarefa foi concluída. Registro real:\n"
                + registro)
    return resposta


def responde(pergunta: str, docs: list, ferramentas: list, history=None, estado: dict | None = None,
             aprovacao: dict | None = None, aprovacoes_sessao: dict | None = None,
             bases: str | None = None, log=None) -> tuple:
    """Responde usando o contexto da base + ferramentas.

    TODA execução de ferramenta exige confirmação do operador antes:
    quando o agente decide chamar, a função PAUSA e devolve
    `pendente = {ferramenta, argumento, estado}` — o chamador pergunta ao
    usuário (permitir uma vez | permitir na sessão | negar) e chama de novo
    passando `estado` + `aprovacao={"ferramenta", "argumento", "decisao"}`.
    `aprovacoes_sessao` ({ferramenta: "sessao"}) dispensa a pergunta para o
    resto da sessão. → (resposta, usos, pendente|None, aprovacoes_sessao)

    `log(msg)` narra CADA passo no "pensando…" do chat: raciocínio,
    ferramenta chamada (com argumentos) e a observação de retorno."""
    log = log or (lambda m: None)
    aprovacoes_sessao = dict(aprovacoes_sessao or {})
    if not ferramentas:  # sem ferramenta marcada: híbrido comum
        return rag.answer_hybrid(pergunta, docs, history, bases), [], None, aprovacoes_sessao

    lista = "\n".join(
        f"- {f.name}: {f.description}\n"
        f"  argumentos (use EXATAMENTE estes nomes): {json.dumps(f.args, ensure_ascii=False)}"
        for f in ferramentas)
    # spec e lista entram como VALOR (não template): chaves no texto não quebram o prompt
    system_text = spec("ferramentas") + f"\n\nFerramentas disponíveis:\n{lista}"
    chain = (ChatPromptTemplate.from_messages([
        ("system", "{system_text}\n\nContexto recuperado da base (pode estar vazio):\n{context}"),
        rag.MessagesPlaceholder("history"), ("human", "{pergunta}"),
    ]) | rag.llm(temperature=0.3) | StrOutputParser())  # agente: estável, com margem p/ recuperar

    contexto = rag.format_docs(docs) if docs else "(nada foi recuperado da base)"
    if bases:  # cabeçalho com o que cada coleção selecionada contém
        contexto = f"{bases}\n\n{contexto}"
    historico = rag._history_messages(history)
    entrada, usos, continuou = pergunta, [], False
    tentadas: set = set()

    if estado:  # retomada: aplica a decisão do operador sobre a chamada que estava pendente
        pergunta = estado["pergunta"]
        contexto = estado["contexto"]
        entrada = estado["entrada"]
        usos = estado["usos"]
        continuou = estado.get("continuou", False)
        tentadas = {tuple(t) for t in estado.get("tentadas", [])}
        pend = estado["pendente"]
        ferr = next((f for f in ferramentas if f.name == pend["ferramenta"]), None)
        decisao = (aprovacao or {}).get("decisao")
        if ferr is not None and (decisao in ("uma_vez", "sessao")
                                 or aprovacoes_sessao.get(pend["ferramenta"]) == "sessao"):
            if decisao == "sessao":
                aprovacoes_sessao[pend["ferramenta"]] = "sessao"
            tentadas.add((pend["ferramenta"], pend["argumento"]))
            log(f"🔧 {pend['ferramenta']} ← {pend['argumento'][:120]}"
                f"  (aprovada: {decisao})")
            t0 = time.time()
            observacao = _executar(ferr, pend["argumento"])
            log(f"↩️ {pend['ferramenta']}: {observacao[:500]}"
                + (f" … ({time.time() - t0:.1f}s)" if time.time() - t0 > 0.2 else ""))
        else:  # negada (ou sem decisão): não executa e não insiste
            tentadas.add((pend["ferramenta"], pend["argumento"]))
            log(f"🚫 {pend['ferramenta']} NEGADA por você — seguindo sem ela")
            observacao = ("O usuário NEGOU esta chamada. Não a repita; continue com outra "
                          "ferramenta ou conclua admitindo o que não pôde fazer.")
        usos.append({"ferramenta": pend["ferramenta"], "entrada": pend["argumento"][:200],
                     "resultado": observacao[:300], "decisao": decisao or "negada"})
        entrada += f"\nObservation: {observacao}"

    for passo in range(1, MAX_PASSOS + 1):
        # CADA chamada LLM do loop é narrada: o que vai (pergunta+registro)
        # e o que volta (tokens pela thread-local + ação decidida abaixo)
        log(f"🧠 passo {passo}/{MAX_PASSOS}: raciocinando sobre a tarefa…")
        contadores.set_etapa(f"agente passo {passo}")
        resposta = chain.invoke({"system_text": system_text, "context": contexto,
                                 "pergunta": entrada, "history": historico})
        contadores.set_etapa(None)
        acao, argumento = _parse(resposta)
        if acao is None:
            if not usos and any(c in argumento.lower() for c in _CLAIMS_ACAO):
                # mentira no primeiro passo: afirma execução sem nenhuma chamada — rejeita
                entrada += (f"\n\n{resposta.strip()}\n\nATENÇÃO: você afirmou ter executado "
                            "algo sem chamar NENHUMA ferramenta — isso é invenção e foi "
                            "rejeitado. Siga o exemplo: responda agora com Thought:/Action:/"
                            "Action Input: usando uma ferramenta da lista.")
                continue
            if not continuou and any(_falhou(u["resultado"]) for u in usos):
                # segunda chance: tentou encerrar com falha no registro e passos sobrando
                continuou = True
                entrada += (f"\n\n{resposta.strip()}\n\nO registro acima mostra tentativa(s) "
                            "FALHA e você ainda tem passos disponíveis. Se a tarefa NÃO foi "
                            "concluída, continue AGORA com Thought/Action (ex.: crie a pasta "
                            "que falta e escreva o arquivo). Se de fato já está tudo feito, "
                            "responda Final Answer.")
                continue
            log("🕵️ verificação anti-invenção: conferindo a resposta contra o "
                "registro real das ferramentas…")
            contadores.set_etapa("verificação")
            verificada = _verifica_e_anota(chain, system_text, contexto, historico,
                                           pergunta, argumento, usos)
            contadores.set_etapa(None)
            return (rag.naturalizar(_final(verificada, usos)),
                    usos, None, aprovacoes_sessao)
        ferr = next((f for f in ferramentas if f.name == acao), None)
        if ferr is None:
            observacao = f"Erro: ferramenta '{acao}' não existe. Disponíveis: " + \
                ", ".join(f.name for f in ferramentas)
        elif (acao, argumento) in tentadas:
            # mesma chamada idêntica já tentada: não gasta passo nem servidor
            observacao = ("Chamada repetida: estes argumentos já foram tentados. "
                          "Mude os argumentos, troque de ferramenta ou vá para Final Answer.")
        else:
            if _sem_portao(acao):
                # política anti-refém: raciocínio/leitura executa direto — o
                # registro em `usos` mantém tudo transparente na resposta
                log(f"🔧 {acao} ← {argumento[:400]}"
                    + "  (leitura/raciocínio: sem portão)")
            elif aprovacoes_sessao.get(acao) == "sessao":
                log(f"🔧 {acao} ← {argumento[:400]}"
                    + "  (liberada para esta sessão)")
            tentadas.add((acao, argumento))
            if _sem_portao(acao) or aprovacoes_sessao.get(acao) == "sessao":
                t0 = time.time()
                observacao = _executar(ferr, argumento)
                log(f"↩️ {acao}: {observacao[:500]}"
                    + (f" … ({time.time() - t0:.1f}s)" if time.time() - t0 > 0.2 else ""))
            else:
                # PORTÃO: toda execução com efeito real precisa de decisão
                # do operador — narra e PAUSA
                log(f"🔐 {acao} exige sua aprovação — pausando")
                pendente = {
                    "ferramenta": acao, "argumento": argumento,
                    # o card de aprovação mostra O QUE a ferramenta faz e de qual
                    # servidor vem — decidir às cegas é impossível
                    "descricao": (getattr(ferr, "description", "") or "")[:400],
                    "servidor": ((getattr(ferr, "metadata", None) or {})
                                 .get("servidor", "")),
                    "estado": {"pergunta": pergunta, "contexto": contexto,
                               "entrada": entrada + "\n\n" + resposta.strip().split("Observation:")[0],
                               "usos": usos, "tentadas": sorted(tentadas), "continuou": continuou,
                               "pendente": {"ferramenta": acao, "argumento": argumento}},
                }
                return "", usos, pendente, aprovacoes_sessao
        usos.append({"ferramenta": acao, "entrada": argumento[:200],
                     "resultado": observacao[:300]})
        # scratchpad do ReAct: a resposta (sem Observation inventada pelo modelo)
        # + o resultado OBSERVADO de verdade
        entrada = (f"{entrada}\n\n{resposta.strip().split('Observation:')[0]}"
                   f"\nObservation: {observacao}")
    # estourou o limite: uma última rodada SEM ferramentas, obrigando a resposta honesta
    log(f"⚠️ limite de {MAX_PASSOS} passos — exigindo resposta final honesta…")
    contadores.set_etapa("resposta final (limite)")
    final = chain.invoke({"system_text": system_text, "context": contexto,
                          "pergunta": entrada + "\n\nLimite de passos atingido — não há mais "
                          "chamadas disponíveis. Responda AGORA no formato 'Final Answer: ...' "
                          "dizendo o que de fato conseguiu e o que falhou (com o erro).",
                          "history": historico})
    contadores.set_etapa(None)
    acao, argumento = _parse(final)
    resposta = argumento if acao is None else final.strip()
    log("🕵️ verificação anti-invenção: conferindo a resposta contra o "
        "registro real das ferramentas…")
    contadores.set_etapa("verificação")
    verificada = _verifica_e_anota(chain, system_text, contexto, historico,
                                   pergunta, resposta, usos)
    contadores.set_etapa(None)
    return (rag.naturalizar(_final(verificada, usos)), usos, None, aprovacoes_sessao)
