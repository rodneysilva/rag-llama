# AGENTS.md — rag-llama

RAG local (LangChain + Qdrant + llama.cpp) com chat agêntico MCP e Estúdio de
Mídia. Projeto **completo e funcional**; este arquivo captura o contexto de
como operá-lo e modificá-lo sem quebrar nada.

---

## ⚠️ 0. REGRA INEGOCIÁVEL — COMMIT E PUSH SEMPRE

**O ambiente do operador fica na VPS** (ver `INFRA_PAAS.md (doc privada do operador, FORA do repo)`):
toda alteração de código que NÃO for commitada e enviada **não existe para ele**.
O fluxo é estação local → `git push` → GitHub Actions → VPS puxa/reconstrói.

**Todo trabalho que altera arquivos DEVE terminar com:**

```powershell
# 1. verificar o que mudou
git status --short

# 2. commitar com mensagem descritiva (prefixo convencional)
git add -A
git commit -m "feat|fix|docs|refactor: <o que foi feito e por quê>"

# 3. ENVIAR — sem isto o operador não visualiza nada
git push
```

**Ferramenta de git do projeto: `gh` (GitHub CLI)** — os comandos de rotina
(push, PR, status do deploy, acompanhar CI) usam `gh` na estação do operador:

```powershell
gh repo sync                      # sincroniza com o remote
gh pr create --fill               # PR de develop p/ main (avanço do gitflow)
gh pr merge --squash --delete-branch
gh run list --limit 3             # CI/CD rodando (o deploy aparece aqui)
gh run view <RUN_ID> --log-failed # log do deploy que falhou
gh api repos/{owner}/{repo}/branches --jq '.[].name'   # branches
```

> `gh` autentica com a conta GitHub do operador; push/commit direto via `gh`
> usa as credenciais dele (sem SSH key manual). Se `git push` pedir senha,
> use `gh auth login` e `gh config set git_protocol https`.

Regras do gitflow (detalhe na INFRA_PAAS.md §3):
- Branch de trabalho corrente: **develop** (push roda CI, NÃO publica).
- Publicar em produção = avanço develop → main (merge --ff-only + push).
- **Se estiver em `main`**, o push JÁ PUBLICA na VPS — avisar o operador.
- Se o push falhar (conflito/rewrites), avisar IMEDIATAMENTE — nunca deixar
  trabalho local não versionado ao encerrar uma tarefa.
- Arquivos de estado/dados (logs/, sessions/, saidas/, users.json, qdrant_data/,
  rabbit_data/, hf_cache/, .env) são gitignored — NÃO commitar.

### Nota sobre o harness do agente (DeepSeek)

O agente deste ambiente tem DUAS ferramentas:
- **Editor de arquivos**: escreve/lê DIRETO no disco Windows (<home-do-operador>\...) —
  é aqui que o trabalho acontece.
- **Shell `pwsh`**: roda NATIVO no Windows do operador (PowerShell 5.1) — git/gh
  funcionam e versionam o repo local de verdade. Sintaxe PS 5.1: separar comandos
  com `;` (NÃO usar `&&`); caminhos `C:\...`; variáveis `$env:NAME`.

**Ciclo obrigatório ao terminar qualquer tarefa (regra §0):** o próprio agente roda
`git add` + `git commit` + `git push` via pwsh (sem pedir confirmação) e valida o
deploy com `gh run list`. Se o push falhar, avisar IMEDIATAMENTE — nunca encerrar
tarefa com trabalho não versionado.

---

## 1. Stack e ambiente

### 🏭 ONDE RODA O QUÊ — política fixa do dono (27/08)

**A GPU é SEMPRE a ESTAÇÃO do dono** (a máquina local dele). A VPS **NÃO
hospeda llama**: nenhum llama-server, nenhum GGUF — os modelos de
conversa/embedding/visão/difusão/whisper rodam na estação e a produção os
alcança pelos túneis (`llm.disroy.org` :8090 · `embed.disroy.org` :8081 ·
`agente.disroy.org` :8010 — cloudflared com o config
`~/infra/tunnels/local.yml` da estação). Na VPS vivem SÓ os serviços de
aplicação: `api` (FastAPI+webui), `qdrant`, `rabbitmq`, `redis`,
`sandbox` (teste de código), `traefik`/`cloudflared` e o Portainer —
confira com `docker ps`: **se aparecer llama na VPS, está ERRADO**. O
`MODELS_DIR=/models` montado no container é apenas um ponto de leitura
(para o picker listar); em produção a listagem real vem do REGISTRO e do
agente da estação.

- **OS:** Windows, shell **PowerShell 5.1** (sem `&&` — usar `;`).
- **Python:** venv em `.venv/` (`.venv\Scripts\activate`); requisitos com pins
  em `requirements.txt` (LangChain 1.x — o código já usa a API nova).
- **Node:** só para desenvolver o front (`webui/`); para USAR não precisa — a
  API serve o build pronto em `webui/dist/`.
- **Codificação:** sempre `python -X utf8` ao rodar scripts que imprimem
  acentos/emoji no console.

### Serviços externos

| Serviço | Onde | Porta | Notas |
|---|---|---|---|
| Qdrant | container `mnemosyne-qdrant` | :6333 API / :6334 dashboard | obrigatório |
| llama-server chat | `<pasta-do-usuario>\llama.cpp\bin` | :8090 | 4 slots: `-c 24576 -np 4 -fa on -ctk/-ctv q8_0` |
| llama-server embedding (bge-m3) | idem | :8081 | **SEMPRE ligado** — nada pode derrubá-lo |
| llama-server visão (Qwen2.5-VL) | idem | :8082 | sob demanda (i2t/v2t) |
| sd-cli (difusão) | `<pasta-do-usuario>\sdcpp\bin820\sd-cli.exe` | — | master-820: Wan2.2 + Flux; caminho em `core/motor.py` |
| whisper-cli | `<pasta-do-usuario>\whisper\bin\whisper-cli.exe` | — | modelo `ggml-medium.bin` em `saidas/audio/` |

GGUFs dos modelos ficam em `D:\models` (presets em `core/config.py`:
`MODELOS`/`EMBEDDINGS`).

### VRAM (8 GB)

Um modelo de conversa por vez + o embedding. O Estúdio **pausa** o chat
(:8090) e a visão (:8082) para difusão, e **NUNCA** o embedding (:8081).
`servicos_llm.py` já reinicia tudo ao trocar de modelo.

## 2. Comandos

```powershell
# subir a API — SEMPRE PELO CONTAINER (nunca python direto):
# mudou código → docker compose up -d --build api
# (o estúdio GPU fica bloqueado nesse modo por design — _exigir_host;
# os modelos seguem no host via host.docker.internal)
docker compose up -d --build api

# servir publicamente (<sub>.<dominio> — via Traefik na infra central)
# OBRIGATÓRIO --host 0.0.0.0: o Traefik (Docker) alcança a API por
# host.docker.internal:8000; router em infra/traefik/dynamic/routers-rag-llama.yml
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000

# desenvolver o front (hot-reload :5173, /api proxyado para :8000)
cd webui; npm run dev

# regenerar o build servido pela API
cd webui; npm run build

# subir/gerenciar os modelos (menu; atualiza LLM_MODEL no .env)
python servicos_llm.py

# testes de prontidão dos serviços
python -X utf8 tests_manual\teste_servicos.py

# CLI: ingestão e consulta
python -X utf8 -m core.ingest caminho\da\pasta
python -X utf8 -m core.main

# CLI: coleção nova por assunto (seed profundo: definição → rodadas → scores → internos → repos)
python -X utf8 -m core.seed "assunto" --fontes 12

# E2E completo (login -> cache -> ingestao -> docs -> manutencao -> t2i ->
# i2t -> restauracao -> limpeza): python -X utf8 tests_manual\e2e_final.py
# (documento de teste precisa de CONTEUDO REAL: a limpeza descarta textos
# curtos como ruido — by design)

# varredura LLM das coleções (apaga lixo claro apontado pelo modelo)
python -X utf8 -m core.varredura <colecao> [outra...]

# REPARO: vetores zerados após crash do Docker (buscas score 0.0)
python -X utf8 -m core.reembed [colecao ...]   # sem args: todas

# AGENTE DO HOST (obrigatório junto com o container): ergue o chat+embed
# no BOOT e atende as operações de GPU da API-container (:8010)
python -X utf8 -m api.agente_host

# popular a coleção prompts_midia (34 prompts exemplares)
python -X utf8 -m core.prompts_corpus
```

Outros scripts úteis em `tests_manual/` (teste_api, teste_query,
teste_sessoes_mcp, servidor_mcp_teste…).

## 3. Arquitetura — 3 subsistemas

### Núcleo RAG

| Módulo | Papel |
|---|---|
| `core/config` | `.env` + `reload()` em runtime (a webui edita e aplica sem restart); binários locais (LLAMA_BIN/SD_CLI/WHISPER_CLI) ajustáveis no .env |
| `core/contadores` | 📊 uso de tokens do llama-server (:8090): wrapper em `rag.llm()` conta CADA chamada (usage do servidor) por serviço via thread-local; acumula em `logs/uso_llm.jsonl` (append atômico — API e scripts CLI somam no MESMO total, sem broker); `totais()` agrega com cache 3 s; `/api/contagem` e `tokens` nas respostas |
| `core/auth` | login simples: scrypt+salt em `users.json` (FORA do git), tokens HMAC stateless; bootstrap do admin via AUTH_ADMIN_* do .env; owner isola sessões por conta |
| `core/rag` | embedding/Qdrant/LLM/chain; modos rag/livre/híbrido; `search` multi-coleção com `SCORE_MIN`, máx 2 chunks/arquivo e teto 4×TOP_K; `reformula` a pergunta usando o histórico |
| `core/ingest` | wizard de 7 etapas; `rapido=True` pula LLM (modo lote p/ bases grandes); texto LIMPO (`core/limpeza`), split por seções markdown, chunks com cabeçalho contextual `[documento · seção]` e metadata `arquivo/titulo/secao/url/i/n`; descarta ruído e duplicados |
| `core/limpeza` | limpeza de texto (frases quebradas por links, citações, menus, widgets) + `e_lixo()` (heurística de chunk sem semântica) — usado por ingest, seed e higieniza |
| `core/higieniza` | limpa coleções JÁ GRAVADAS in-place: re-embeda o texto limpo no mesmo id, apaga pontos de ruído e duplicados; CLI `python -X utf8 -m core.higieniza <colecao>` e `POST /api/higienizar` |
| `core/catalog` | metadados das coleções na coleção `meta_colecoes` |
| `core/analyze` | LLM analisa todas as coleções → catálogo |
| `core/enrich` | destrincha coleção em várias por tema (reaproveita vetores) |
| `core/sessions` | sessões do CHAT (JSON em `sessions/`) |
| `core/executor` | ⚙️ executor de JOBS async in-process (substituiu o RabbitMQ 27/08): fila `asyncio.Queue` serial (VRAM é o gargalo), fábricas rodam em `to_thread` (event loop livre), retry+backoff SÓ p/ erros transientes (rede/timeout), falha de negócio registrada no job; `_despachar` mantém a interface das fábricas; restart = jobs somem com erro claro no polling |
| `core/seed` | seed PROFUNDO seguindo a spec global `pesquisa_web.md`: 1ª onda Serper (atual), aprofundamento DuckDuckGo (internas), LLM decide se vale aprofundar; definição da RAG antes de importar → curadoria com scores (≥6) → download + internos (≥7, máx 3/fonte) + repos oficiais (clone esparso em `datasets/seed/*_repo/`, fora do git) → ingestão em lote → catálogo; job com log (`POST /api/seed`) |
| `core/varredura` | varredura LLM: julga cada chunk contra o ASSUNTO da coleção e apaga só lixo claro; spec conservadora por design; CLI e `POST /api/varredura` |
| `core/unificar_arquiteturas` | consolida os melhores chunks por CONCEITO universal (SOLID/DDD/clean arch…) das coleções `arquitetura_*` na `arquitetura_unificada` — coleção de SISTEMA: oculta da webui, entra AUTOMÁTICA como base em qualquer busca que toque coleções `arquitetura_*` (regra no `/api/query` e no modo Auto) |
| `core/catalog` | metadados das coleções em `meta_colecoes` + `agrupar()`: grupo por objetivo (spec `agrupamento.md`, `POST /api/agrupar`) |
| `core/specs` | carrega `core/specs/*.md` com `lru_cache` |

### Chat agêntico

| Módulo | Papel |
|---|---|
| `core/agent` | ReAct artesanal; portão de aprovação (`pendente` + `uma_vez`\|`sessao`\|`negar`); verificação anti-invenção contra o registro real das ferramentas |
| `core/mcp_registry` | registro de servidores MCP (`mcp_servers.json`) + catálogo de conhecidos (`mcp_conhecidos.json`) com instalação automática (`POST /api/mcp/instalar`) |

### Estúdio de Mídia

| Módulo | Papel |
|---|---|
| `core/midia` | Flux t2i, Wan2.2 t2v/i2v (**saída convertida para .mp4/H.264** via ffmpeg — webm é descartado), Qwen2.5-VL i2t/v2t, whisper a2t, a2v; pausa :8090/:8082 (nunca :8081); `incluir_no_contexto` → `midia_gerada`; webui mostra mídia em tamanho real (lightbox) |
| `core/modelos` | troca a quente de GGUF; grava `LLM_MODEL` no .env + `config.reload()` |
| `core/motor` | subprocess com progresso parseado (barras do sd-cli, % do whisper) |
| `core/modalidades` | modalidades declarativas: chat, dev, t2i, t2v, i2v, i2t, v2t, a2t, a2v (**v2a pendente**) |
| `core/tarefas` | jobs em background com lock de VRAM/sessão; ETA aprendido no Redis |
| `core/sessoes` | sessões do ESTÚDIO — **não confundir com `core/sessions`!** |
| `core/auto` | modo Auto: roteador decide base/web/livre; **web-first** (DuckDuckGo primeiro, Serper de respaldo) com **aprofundamento de até 5 níveis** (crítica a cada nível + refinamento LLM da query); crítica CRAG na base também cai no web aprofundado; aceita `log` (job do chat mostra cada nível) |
| `core/prompts_corpus` | 34 prompts exemplares → coleção `prompts_midia` (só via CLI) |

## 4. Regras de comportamento = specs (regra de ouro do projeto)

**Toda comunicação com a LLM é via RAG**: comportamento e formato vivem em
`core/specs/*.md` (21 arquivos) ou no conteúdo do Qdrant; o código só monta o envelope
(dados + `ETAPA: x`) — nada de instrução hardcoded. Coleções são sempre
genéricas: nenhum texto do sistema cita coleção específica (contamina o
modelo — ver o incidente da spec seed antiga). Para mudar comportamento →
editar a spec. `core/specs.py` usa `lru_cache`: **editar spec exige restart
da API**.

## 5. Armadilhas

- PowerShell 5.1: sem `&&` (usar `;`); `curl` pede senha → usar
  `Invoke-RestMethod`.
- **MODO CONTAINER** (`RAGAROY_CONTAINER=1` no compose): endpoints de infra
  (Qdrant/Rabbit/Redis/LLM/Embed) vêm do ENVIRONMENT e o `.env` montado
  NÃO os sobrescreve (`load_dotenv(override=False)`). Estúdio, visão e
  troca de modelo têm `_exigir_host()`/guards (processos/GPU do host);
  `embedding_no_ar()` usa `EMBED_BASE_URL` — NUNCA hardcode 127.0.0.1
  (o container testaria a si mesmo). Rebuild: `docker compose up -d --build`.
- **Coleções de sistema** (`COLECOES_SISTEMA` em `api/app.py`):
  `meta_colecoes` (catálogo), `midia_gerada` (contexto RAG das mídias),
  `prompts_midia` (exemplares de prompt) e `arquitetura_unificada` (base)
  são FUNCIONAIS mas OCULTAS da webui — `_scan_collections` filtra por
  padrão; quem precisa consulta direto pelo nome.
- **Auth**: `users.json` (hash das senhas) e `.env` (AUTH_ADMIN_PASS/AUTH_SECRET)
  NUNCA no git. Tokens são HMAC — AUTH_SECRET novo derruba todos os logins.
- **Jobs da webui** vivem no store global + `<JobsManager/>`: NÃO mova o
  polling de volta para os componentes (saiu da página = perdeu o job).
- Ingerir a mesma pasta **duplica** chunks — a ingestão NOVA dedupa no
  mesmo lote; para coleções antigas, rode a **higienização** (✨ na aba
  Coleções ou `python -X utf8 -m core.higieniza <colecao>`). Para recomeçar
  do zero, apague a coleção no dashboard Qdrant (:6333/:6334).
- Coleções `mnemosyne_*` (vetores nomeados) **não são pesquisáveis** por
  similaridade aqui; só metadados são editáveis.
- Embedding de dimensão diferente de bge-m3 (1024) exige **reingestão total**.
- APIs concorrentes: `/api/query` recusa **409/423** quando o Estúdio trava a
  VRAM ou a sessão está ocupada (por design).
- **Chat é JOB**: a webui chama `/api/query` com `job=true` (resposta
  `{job}` na hora — imune ao **524 do Cloudflare**, que mata conexões
  síncronas >100 s) e faz polling em `/api/query/status/{job}`; cada etapa
  (cache, reformulação, busca, geração, MCP, tokens) vira linha em tempo
  real no "pensando…". CLI/tests podem usar a rota síncrona (sem job).
- `sessions.py` (chat) ≠ `sessoes.py` (estúdio).
- 8 GB VRAM: um modelo de conversa por vez. **Embedding**: vídeo (t2v/i2v/a2v)
  SEMPRE o derruba (Wan + VAE 3D pedem toda a VRAM); geração LEVE (t2i,
  whisper) convive com ele — só sai se `ESTUDIO_PAUSAR_EMBED=1` explícito.
  Tarefa de difusão ESPERA (≤60 s) respostas do chat em andamento antes de
  pausar a LLM (`_esperar_chats` — mata o TOCTOU do erro cru no meio da
  resposta).
- **MCP é admin-only**: registrar/testar/instalar/remover executa processos
  no host e grava no .env → `_exigir_admin` em todas as rotas de escrita;
  chaves de env do instalador passam por allowlist (`_ENV_PROIBIDAS`).
- **Settings valida tipos ANTES de gravar** (422) e **mascara segredos**
  (SERPER_API_KEY volta como `••••••••`; PUT ignora quem devolver a máscara).
- **Sessões**: id validado por regex (`_RE_SID`, anti path-traversal) e
  DELETE/PATCH conferem owner (chat e estúdio). Tarefa do estúdio sem sessão
  cai na "Principal" DO DONO (`sessoes.principal`) — mídia nunca fica invisível.
- **Jobs no EXECUTOR ASYNC (substituiu o RabbitMQ — decisão do dono
  27/08)**: `core/executor.py` — fila `asyncio.Queue` SERIAL (1 job por
  vez: VRAM é o gargalo), fábrica roda em `asyncio.to_thread` (event loop
  da API livre), RETRY com backoff 2s/4s SÓ para erros transientes
  (ConnectionError/timeout/EOF — erro de negócio falha direto, controle
  de erro é Python comum). A entrada de status é criada ANTES do return
  (status nunca dá 404 entre despacho e pickup). `_podar_concluidos(dic)`
  mantém os 10 últimos concluídos — chame SEGURANDO o lock (Lock não é
  reentrante; re-adquirir = deadlock, já aconteceu). Restart da API: jobs
  em curso/pendentes somem e o polling mostra ERRO CLARO "dispare
  novamente" (decisão do dono; o replay do Rabbit era fraco na prática —
  fábrica é closure runtime e mensagem sem fábrica era descartada).
  `to_thread` usa POOL que REUSA threads: thread-local (rag.set_override,
  contadores.set_servico) exige `finally` (lição que segue valendo).
- **Manutenção é JOB**: /api/manutencao {acao: analisar|agrupar|dividir}
  com log ao vivo (as rotas antigas /api/analyze|agrupar|enrich delegam nela).
- **Auth em cookie httpOnly** (`ragaroy_token`) além do Bearer: mídia
  (`<img>/<video>`) autentica sozinha — token SAIU da query string. Login tem
  rate limit (8 falhas/5 min por ip|usuário → 429). users.json e AUTH_SECRET
  sob lock (corrida de registros simultâneos).
- `_scan_collections` tem cache de 30 s (o scan é N+1 no Qdrant e o /api/status
  roda a cada 15 s na webui). `servido()`/binários (LLAMA_BIN/SD_CLI/WHISPER_CLI)
  lidos na HORA — editar no .env vale sem restart. TEMPERATURE do .env é
  respeitada como está (o clamp 0.7 foi removido).
- **Tarefas × restart**: ativas persistem em `saidas/tarefas_ativas.json`;
  o `_sweep_reinicio()` (import de `core/tarefas`) re-registra cada uma como
  ERRO claro ("a API reiniciou durante a geração — dispare novamente") — a
  GPU não retoma a difusão do meio, mas NADA fica `running` pendurado/404.
  O `useTarefa` do frontend também desiste com erro após 5 falhas seguidas
  de polling. Jobs de fila (ingestão/seed/…) são re-executados do zero pela
  fábrica; jobs em thread-fallback (sem Rabbit) se perdem — o popup marca.
- **Cache semântico REMOVIDO** (27/08, decisão do dono): a 🧭 bússola
  (Qdrant, coleção `sessoes_chat`, escopo por owner) cobre perguntas
  repetidas cross-sessão; respostas-diretas por score seguem de praxe.
- **`/api/ingest/upload`**: `colecao`/`rapido` são `Form()` — sem a
  anotação o FastAPI lia da QUERY e o slug digitado na webui era
  silenciosamente ignorado (a coleção caía no nome do arquivo). Escrita
  em `asyncio.to_thread` (I/O fora do event loop).
- **Pensamentos do chat**: as linhas do "pensando…" ficam ANEXADAS à
  mensagem (`pensamentos` no ChatMsg) após a execução — o painel RETRAI
  para um resumo clicável (etapas · linhas · duração) acima da resposta,
  também em erros e cards de aprovação, e persistem na sessão (raw).
  AO VIVO, grupos concluídos retraem sozinhos (só o em curso fica aberto).
  A conexão MCP é NARRADA por servidor (`carregar_ferramentas(log=…)` — o
  npx de um stdio demora dezenas de segundos e o painel não fica mudo).
  A sessão é salva JÁ NO ENVIO da pergunta (`salvarSessao(pergunta)`), e o
  job ativo fica anotado no localStorage (`ragaroy.chatJob`, 30 min) —
  sair da tela/recarregar no meio RETOMA o polling ao voltar: a
  solicitação não some.
- **CodePanel merge entre respostas**: os arquivos são acumulados de TODAS
  as respostas da conversa (última versão vence) — resposta nova que
  re-gera um arquivo atualiza na TELA e no ZIP. `extrairArquivos` captura
  o CAMINHO com pastas (`src/domain/News.cs`) e o `/api/zip` preserva a
  estrutura (sanitizada: sem `..`/drive/absoluto).
- **Telemetria persistente** (`core/telemetria.py` → `logs/telemetria.jsonl`,
  volume no container): cada chamada LLM (tokens/duração/serviço), cada job
  Rabbit (publicado/pego/reentrega ignorada), cada cache hit/miss/store.
  `GET /api/telemetria?tipo=llm|rabbit|redis` faz o tail — os badges
  🐇/⚡ do topo da webui abrem o histórico ao clicar (refetch 5 s).
- **Tokens em tempo real**: o runner do job de chat registra
  `contadores.set_log` (thread-local) e o `LLMContada` loga
  `🪙 🔻entrada · 🔺saída · Xs` a CADA chamada — o header do "pensando…"
  mostra a última (mais o ⚡cache quando há hit).
- **Cache no modo livre** grava o MESMO escopo do lookup (coleções
  resolvidas) — gravar vazio nunca bateria.
- **Qdrant × crash do Docker Desktop**: o crash pode ZERAR os vetores em
  disco (payloads sobrevivem; buscas voltam score 0.0 para tudo). Remédio:
  `python -X utf8 -m core.reembed [coleções]` — re-embeda pontos de norma ~0
  no mesmo id, idempotente. Sintoma de tela: "📚 0 fragmento(s)" para tudo.
- **Operação pela API = CONTAINER** (`docker compose up -d --build api`):
  nunca rodar uvicorn direto — o Traefik/healthcheck/stack esperam o
  container; estúdio GPU segue no modo host (guards avisam na webui).
  JUNTO ao container roda o **AGENTE DO HOST** (`python -X utf8 -m
  api.agente_host`, :8010): ergue chat+embedding no BOOT e recebe da
  API-container as operações de GPU (`modelos.ativar`/`garantir_embedding`
  proxyam via AGENTE_HOST_URL) — "llama-server não subiu" não existe mais.
  `D:/models` é montado read-only (MODELS_DIR=/models) para o picker listar.
- **Voz do chat** (`core/voz.py`, 100% CPU — não disputa a VRAM com
  bge/qwen): STT faster-whisper small int8 + TTS piper pt_BR-faber;
  modelos em `modelos_voz/` (volume). Endpoints `/api/voz/{falar,
  transcrever,disponivel}`; webui: 🎤 dita a pergunta, 🔊 ouve a resposta.
- **Tokens por BALANÇO LOCAL da thread** (`contadores.balanco_reset/ler`):
  o diff de totais do Redis cruzava contadores de outros processos/jobs
  concorrentes — daí a divergência ("ora aumenta ora diminui"). O
  `tokens` das respostas é o balanço local, determinístico por execução.
- **Histórico de jobs** (`core/historico.py` → `logs/historico.jsonl`):
  TODO job de fila registra tipo/duração/ok/resumo ao terminar — o
  embrulho é na FÁBRICA (cobre caminho direto e replay do worker).
  `GET /api/historico`; a IngestTab lista as execuções passadas.
- **Desligar embedding (manual)**: badge 🧬 do topo → `POST /api/embed/{ligar,
  desligar}` (admin; em container, proxy ao agente). O estado é um MARKER
  (`saidas/embed_off.marker`) que `garantir_embedding()` respeita — NADA
  religa sozinho (nem busca/ingestão, nem boot do agente, nem o restore do
  estúdio); buscas falham com 503 "embedding desligado manualmente — religue
  no badge 🧬" (quando TODAS as coleções falham, o erro sobe em vez de
  resposta vazia).
- **Desligar llama-server (manual)**: badge 🧠 → `POST /api/llm/{ligar,
  desligar}` (admin; marker `saidas/llm_off.marker`). O BOOT do agente e o
  restore do estúdio PULAM o chat quando o marker existe — desligado fica
  desligado entre restarts até o ▶ no badge. Validado: off → agente
  reinicia → continua off (VRAM só do embedding).
- **Cada servidor llama é INDEPENDENTE** (por conta de cada um): 🧠 chat
  (:8090), 🧬 embedding (:8081) e 👁 visão (:8082) têm o PRÓPRIO
  liga/desliga persistido (markers `saidas/{llm,embed,vl}_off.marker`) —
  desligar um não afeta os outros (validado: visão ligada/desligada com o
  chat off o tempo todo). Visão: `_subir_vl` respeita o marker (análise
  falha com 503 claro "visão desligada manualmente — religue no badge 👁");
  ▶ ligar remove o marker e PRÉ-AQUECE o Qwen2.5-VL. O badge 👁 mostra
  "livre" (sobe na 1ª análise) ou "off" (bloqueada) — não se o processo
  está no ar (é on-demand).
- **Modo da GPU** (`GPU_MODO` no .env: `todos` | `somente_llms`, badge 🎮 do
  topo, admin): em 'somente_llms', modalidades de difusão/whisper (t2i/t2v/
  i2v/a2v/a2t/v2t e contexto de áudio/vídeo) são recusadas com 403 claro —
  só os llama-servers (chat/embed/visão) usam a GPU. Programas EXTERNOS não
  são controláveis (o popover avisa). A checagem vem ANTES do `_exigir_host`
  (o 403 da política é mais útil que o 400 de container).
- **`set_env_inplace`** (config.py): gravar no .env SEM rename — bind mount
  de ARQUIVO único no Docker rejeita `os.replace` (o dotenv.set_key padrão
  morria com 'Device or resource busy'; atingia settings/GPU_MODO/instalador
  MCP em container). Também atualiza `os.environ` (reload usa override=False
  e não veria o novo valor).
- **Layout OOUX do Estúdio Unificado (F1b)**: 5 telas — `Chat` (HOME = porta
  simples: coleções na LINHA PRINCIPAL, linha de status "📚 N coleções · M
  fontes", chips 🧠/🎨 + modos + MCPs no disclosure "avançado", rail
  "Produção" e BANCADA deslizante a 1 clique), `Estúdio` (bancada COMPLETA:
  modalidades, parâmetros, sessões, galeria + seção FLUXOS), `Pulso`
  (dashboard), `Biblioteca` (base) e `Sistema`. Hashes antigos redirecionam
  (`LEGADO`: inicio→chat, midia→estudio). Contexto só entra SE selecionado
  (D2); intenção de mídia = regras PT/EN (`querMídia`, inclui GIF) +
  CONFIRMAÇÃO de 1 clique antes de gerar. Store: `modeloChat`/
  `modeloCriativo` (migração da chave única antiga por categoria, 1x).
- **GIF no chat (F1b-3)**: "crie um gif de…" → t2v/i2v com `params {gif:
  true, frames: 17}` → `midia.gerar_video(gif=True)` converte o mp4 final
  via ffmpeg palettegen/paletteuse (`fps=12,scale=480:-1`) e devolve
  `tipo="gif"`; serve por `/api/midia/gif/<nome>` (alias da pasta de vídeos
  com MIME image/gif) — renderiza `<img>` em todo lugar (ChatMsg, rail,
  galeria, lightbox, MediaPreview). Conversão falhou → fica o .mp4 (a
  geração não se joga fora).
- **Fluxos de geração (F1b-4)**: `core/fluxos.py` + `GET /api/fluxos` —
  registry de CARDS na aba Estúdio: builtins (sd-cli/Flux, wan2.2 — status
  reflete binário/modelos/GPU_MODO) e EXTERNOS (wan2gp, ComfyUI) com
  health-check GET (timeout 2 s) na URL do .env (`FLUXO_WAN2GP_URL`/
  `FLUXO_COMFY_URL`); status pronto|parado|nao_configurado.
- **🧪 MOCK_LLM (F1b-5)**: `MOCK_LLM=1` no .env (exige restart da API) →
  `core/mock.py` responde NO TOPO do `_processar_query` — sem cache, sem
  Qdrant, sem LLM, sem guards de sessão/estúdio. O JOB roda de verdade
  (log ao vivo com sleeps 0,3 s, docs fake, tokens ~900/~200), `/api/status`
  expõe `mock: true` e a webui libera o chat SEM llama-server + fita "🧪
  MODO MOCK". Voltar a 0 e reiniciar para respostas reais.
- **Busca HÍBRIDA (F2)**: `rag.search` funde densa (k×3) + full-text do
  Qdrant (`MatchText` no `page_content`, scroll limit 40, termos >3 chars
  com fallback para o termo mais longo) por **RRF** (1/(60+rank) por chave
  md5 de conteúdo); achados SÓ-TEXTO entram com score exibido = SCORE_MIN
  (match exato de ID/código é sinal forte). Índice full-text criado LAZY
  (1x por coleção, try/except — MatchText funciona sem índice por
  full-scan). A ordem final é do RRF, não do score bruto.
- **🎛️ Reranker (F2)**: `core/rerank.py` — CrossEncoder
  `BAAI/bge-reranker-base` em CPU, LAZY (carrega 1x; ~1,1 GB no cache do
  HF). Em `_processar_query` (modos rag/hibrido com ≥6 achados): top-15 →
  rerank → 4, log "🎛️ rerank N→4 (top 0.xx)". Flag `RERANKER` no .env
  (default 1); DEGRADA em silêncio (retorna None) se torch ausente — log
  1x. `descarregar()` no ⏹ Parar tudo. requirements += torch,
  transformers; Dockerfile instala torch do índice CPU (o wheel CUDA do
  Linux seriam ~2,5 GB à toa); compose monta `./hf_cache:
  /root/.cache/huggingface` para o download sobreviver a rebuilds.
- **Specs restritivas (F2)**: `specs/chat.md` (modo RAG) e `specs/hibrido.md`
  ganham bloco "Regras estritas" — RAG responde ÚNICA E EXCLUSIVAMENTE com
  o contexto; insuficiente/contradição → "Não possuo dados confiáveis o
  suficiente nos documentos para responder"; híbrido: base vence conflito e
  o conflito é DECLARADO. Editar spec ainda exige restart (lru_cache) — ou
  POST /api/specs/reload.
- **👁️ MODO REVISÃO (Fase A, 20/08)**: dry-run da ingestão — NADA grava sem
  aprovação. `core/preview.py` + `POST /api/ingest/preview` {fonte:
  pasta|hf, query, limite, colecao-alvo} + `dry_run` Form no upload → job
  `preview` → `GET /api/ingest/preview/{pid}` (relatório: como veio/como
  vai entrar, chunks com [doc · seção] e i/n, duplicados md5 + quase-dups
  cosseno ≥0.92, clusters cosseno ≥0.75 com rótulo LLM spec rotulo_cluster.md,
  GATE DE TEMA com rerank.notas_de contra a definição da coleção-alvo
  [<0.10 = "revisar"]) → `POST /api/ingest/preview/aplicar` {preview, ids,
  colecao} ingere SÓ aprovados com `adquirido_em`+`curadoria` (proveniência
  no Qdrant, finalmente). Preview vive 30 min em memória da API. UI:
  `RevisaoIngest.tsx` no topo da Biblioteca (master-detail, padrão shadcn+
  lucide da Fase F); IngestTab tem "revisar antes de ingerir" (default ON)
  para upload e HF. Trafilatura (2.2.0) no `_html_texto` do seed: HTML→
  markdown COM estrutura (fallback BeautifulSoup). Fix hf.card: metadata
  `arquivo` por card (i/n deixaram de ser globais).
- **🎛️ RERANK_MODEL (Fase C)**: modelo configurável no .env (+ ⚙️
  Configurações). Primitiva `rerank.notas_de(consulta, textos)` (sigmoid
  por texto) alimenta o rerank do chat E o gate de tema da Revisão.
  **Benchmark** `tests_manual\bench_rerank.py` (18 perguntas, nDCG@4):
  base **0,882** vs v2-m3 **0,867** (Hit@4 0,88) — v2-m3 NÃO paga; base
  segue default. Ampliar o golden antes de qualquer troca.
- **JobRegistry (Fase D)**: as 8 famílias de job (ingest/manutenção/
  higienização/limpeza/seed/varredura/chat/mcp + preview + contexto de
  mídia) viram UMA classe (`jobs/lock/seq/log/concluir/cancelar/status`)
  + `TODOS_JOBS` (alimenta `_jobs_ativos` e o ⏹ com locks segurados) +
  `_rota_status()` (rotas GET de status idênticas geradas). Replay
  pós-restart SEM dispatch agora cria placeholder já `picked` (reentrega
  não re-executa). Fixes do pacote: `global` em `tarefas.cancelar_todas`
  (VRAM ficava presa após ⏹), cache Redis re-testa a cada 60 s (antes:
  desligado para sempre), tokens SÓ por `balanco_ler()` (último
  `uso_desde` removido), poison message do Rabbit não loopa mais
  (json.loads protegido no except), `detectar` MCP ignora flags `-` (`npx
  -y @scope/server` registrava "-y"), higieniza PULA `camada=codigo`
  (antes destrói código), portão do agente checa tokens + verbos de
  escrita ampliados (`search_replace` passava direto), `EnrichIn` morto
  removido.
- **🔬 PESQUISA PROFUNDA COM EVIDÊNCIAS (Fase B, 20/08)**: `core/pesquisa.py`
  + `POST /api/pesquisa` (job `pesq`) + card 🔬 na Biblioteca. O anti-snippet:
  PLANNER (spec `pesquisa_planner.md`, JSON, fallback) → BUSCA (Wikipedia
  pt→en + Serper→DDG + READMEs GitHub priorizados, dedupe URL) → FETCH da
  PÁGINA INTEIRA (Trafilatura via seed) → CLAIMS com evidência (spec
  `evidencia.md`, ≤5 docs × ≤8 claims) → SÍNTESE com citações [Fn] e CONFLITOS
  declarados (spec `sintese.md`) → MODO REVISÃO (`result.preview`; nada grava
  sem aprovação; doc de síntese com metadata `sintese: true`). Budget no
  código (≤6 consultas, ≤12 fontes, 1 síntese). JobsPopup abre a revisão sozinho
  quando o job termina.
- **Infra (política do dono, 20/08, ATUALIZADA 27/08)**: push na main =
  deploy VPS (GH Actions) — sem paths-ignore, docs puro usa `[skip ci]`;
  domínio em outro servidor (cloudflared+Traefik na infra central); na
  VPS rodam SÓ os serviços de aplicação (api/qdrant/rabbit/redis/
  sandbox/traefik) — **GPU/llama-server/GGUFs NUNCA na VPS (ver "ONDE
  RODA O QUÊ" na seção 1)**. **Validação/retorno SEMPRE pós-push no
  ambiente publicado (https://<sub>.<dominio>) — NUNCA forçar
  localhost.**
- **📦 F5 PILOTO (20/08, noite)**: `core/snapshot.py` — fotografia
  id+vetor+payload em `logs/snapshots/*.jsonl` + `restaurar()` ponto a
  ponto SEM re-embedar; rotas admin `/api/snapshot` (listar/criar/
  restaurar — ⚠️ rota LITERAL antes da path-param!); `reembed` e
  `unificar_arquiteturas` criam snapshot antes de mexer. `pesquisa.pesquisar
  (colecao_alvo=)`: FILTRO INCREMENTAL — página aceita é embedada na hora e
  comparada com o lote (≥0.92) e com o ÍNDICE da alvo (≥0.95); redundante
  não ocupa vaga. **KI** (Knowledge Item) no `resumo.ki` e no metadata do
  doc de síntese (fontes com url/revisado_em/claims + conflitos) — auditoria
  chunk→doc→fonte→data no próprio Qdrant. Wikipedia agora busca EN antes de
  PT (consultas são sempre inglês — pt trazia páginas irrelevantes).
  Generalização do KI para coleções existentes: aguarda aprovação do
  piloto. E2E: `tests_manual\e2e_f5_piloto.py`.
- **🧭 BUSSOLA PRÉ-TOKEN (F3, 20/08)**: `core/bussola.py` — coleção de
  sistema `sessoes_chat` indexa cada (pergunta→resposta) respondida nos
  modos rag/livre sem histórico (embedding bge-m3, id determinístico
  owner+pergunta = upsert, escopo POR OWNER). No `_processar_query`, após o
  MISS do cache Redis: ≥0.95 → resposta DIRETA reaproveitada citando a
  conversa (`cache.tipo "sessão"`, ZERO token — cobre cross-sessão e
  sobrevive a flush do Redis); 0.85–0.95 → sugestão no campo `bussola`
  (webui ainda não explora). ARMADILHA: o modo livre tem EARLY-RETURN
  próprio — o `bussola.registrar` precisa existir LÁ TAMBÉM (bug pego em
  produção pelo e2e_bussola). Desligada no MOCK; degrada em silêncio.
- **Pacote crítico de débito (F3 junto)**: sessions.py salvar ATÔMICO
  (lock + tmp+os.replace — corrida perdia mensagens); auth scrypt 2^14→2^17
  com `maxmem` explícito (hashlib limita ~32MB!) e REHASH TRANSPARENTE ao
  logar em hash antigo + users.json atômico + AUTH_SECRET em cache +
  nomes_permitidos sem reescrever em leitura; varredura COPIA todo ponto
  apagado para `logs/varredura_backup/<colecao>_<ts>.jsonl` antes de
  deletar (exclusão reversível; `resumo.backup`); catalog.list_meta scroll
  PAGINADO (256 único truncava em silêncio); motor.rodar_whisper
  wait(timeout); seed._curadoria em LOTES de 40 (máx 3 — o resto morria
  cego); pesquisa._github_readme tira front-matter Jekyll (adeus
  "{:.no_toc}"); contadores Redis LAZY com re-teste 60s (Redis subindo
  depois da API agora é adotado).
- **Fase B+ (20/08, manhã)**: modo Auto rota WEB com **PÁGINAS REAIS**
  (`auto._web_aprofundado`: baixa via `pesquisa._baixar` — máx 3 páginas ×
  4000 chars, crítica por material baixado; fallback snippets se nada baixar).
  `pesquisa.py`: Wikipedia com `revisado_em` (timestamp da última revisão,
  visível na Revisão) + **conflitos determinísticos** `_conflitos()` (tema
  por cobertura ≥0,5 da lista menor de tokens SEM os valores; anos/versões
  por regex NÃO-capturadora — grupo capturador fazia findall devolver "19");
  conflitos entram no prompt da síntese e no log. **Fase E**:
  `core/linguagens.py` (rodeiro único LINGUAGENS/EH_DEV — antes 3 listas
  divergentes), CLI `main.py` usa `rag.search` híbrida, v2a fora do picker.
  **Fase F onda 1**: lucide em JobsPopup (ícone por kind + CheckCircle2/
  XCircle), toolbar do Chat (Plus/BookOpen/Code/Copy/Settings2) e títulos
  da IngestTab; HF mantém o símbolo 🤗 (marca, não decoração). Onda 2
  (tokens CSS + Header) a planejar com screenshots.
- **LIQUID INTERFACE + VOZ A2A (20/08, noite 3)**: chat é a HOME em
  FULLSCREEN (sempre montado); rail mínimo de ícones (`Rail.tsx`);
  Biblioteca/Estúdio/Pulso/Sistema viram PAINÉIS slide-over POR CIMA da
  conversa (App.tsx; fecha = volta ao ponto exato); paleta **⌘K/Ctrl+K**
  (`CommandK.tsx`, evento global `ragaroy:cmdk`): navegação, nova conversa,
  modo voz, tema. **Composer em PILLS**: [conversas][+][modelo][coleções]
  [voz][fontes][código][ouvir conversa][copiar][avançado] — popovers abrem
  PARA CIMA. **Coleções = autocomplete** (busca em foco no abrir, marcar
  em linha, todas/nenhuma/marcar-filtradas) — ARMADILHA que matou: dropdown
  `absolute` dentro de toolbar `overflow-x-auto` era CORTADO (lista "não
  aparecia"); agora vive no composer. **VOZ** (faster-whisper STT + piper
  TTS pt_BR, opensource/CPU — `core/voz.py`): 🎤 dita a pergunta (a2t),
  🔊 ouve cada resposta (t2a), **MODO VOZ** = a2a (respostas faladas ao
  chegar — `modoVoz` no store, persistido; auto-TTS no `tratar`) e
  **AUDIOLEITURA** da conversa inteira (botão "ouvir conversa", id=-1).
  Modelos de voz copiados para a VPS (524 MB em `modelos_voz/`, bind já
  existia no compose) — STT/TTS OK em produção.
- **UNIFICAÇÃO DO ESTÚDIO NO CHAT (20/08, noite)**: EstudioTab/BancadaEstudio
  DELETADAS da webui (rail/⌘K/Sidebar/App sem "estudio"; hashes antigos →
  chat; badge 🎬 do header volta PARA A CONVERSA). A conversa é o centro:
  mídia é comando de chat com **seletor de modalidade** na confirmação
  (auto/texto→imagem/→vídeo/GIF; i2v quando há referência) e **mídias da
  conversa viram REFERÊNCIA** (botão ↰ no rail Produção anexa o ARQUIVO
  gerado — "anime/descreva esta imagem"; `ChatMsg.midia.arquivo/pasta`).
  O core segue com a inteligência (midia/tarefas/modelos/fluxos/sessões —
  rotas mantidas). **`core/conjuntos.py`**: conjunto de modelos POR TAREFA
  (chat: llm+embed · visao: vl+embed, chat embaixado · difusao: sd-cli por
  processo, chat/vl embaixados · whisper convive) com `garantir(tarefa)` —
  MESMO conjunto = **cache da GPU mantido**; troca = **limpeza de VRAM
  antes de subir** o novo (`saidas/conjunto_ativo.json`; em container,
  proxy ao agente `/conjunto/{familia}`; integrado no `_rodar_tarefa`).
  **COLEÇÕES TRI-ESTADO** (regra do dono, NO BACKEND): `collections=[]` =
  SEM busca (sem reformulação/Qdrant/rerank, log claro; rag responde "não
  possuo dados…"); `None` = todas (compat CLI); `"x"` = uma. Webui sem
  regra de negócio (picker mostra estado; status "SEM coleções — sem busca").
  **MICROFONE — CAUSA RAIZ**: o middleware `security-headers` do TRAEFIK
  enviava `permissions-policy: microphone=()` em produção (bloqueio NA
  ORIGEM — não era frontend nem Chrome; local não tem o header, por isso o
  diagnóstico errava). Corrigido na VPS para `microphone=(self)` (backup
  `middlewares.yml.bak`); dialog do mic ganha linha de DIAGNÓSTICO
  (contexto seguro/iframe/featurePolicy). Validado em produção: header ok,
  tri-estado ok (0 docs com [] e resposta restritiva; docs com 1 coleção).
- **ETAPAS DE 1 LINHA + MIC SEM PRE-CHECK (20/08, tarde 2)**: o
  "pensando…" segue o padrão de UI de agente — cada passo é UMA LINHA
  truncada (`LinhaPasso` em CodePanel.tsx); clique expande o conteúdo
  COMPLETO (IN/OUT de ferramenta com 400/500 chars, escopo do planner…);
  ao vivo só o passo corrente abre; concluído fica "✓ raciocínio · N
  passo(s) · Xs" recolhido. **Microfone**: REMOVIDO o pre-check
  `permissions.query` — dava FALSO "bloqueado" (o estado do Chrome não
  reflete Windows/ocupação); agora `getUserMedia` direto e o ERRO REAL
  classifica o motivo do guia: NotAllowed=site (🔒/chrome:// copiável) ·
  NotReadable/Abort=dispositivo (privacidade do Windows p/ apps desktop,
  ocupação por Meet/Teams, entrada padrão) · NotFound=nenhum. Plano B por
  arquivo de áudio segue garantido. **Dashboard**: histórico para o FIM
  (destaques primeiro: Conhecimento → Uso da LLM → Histórico). **Docs**:
  `docs/README.md` = índice único (mapa README/AGENTS/docs + índice das
  21 specs + regra "código > AGENTS > README > docs"); README corrigido
  (navegação atual, /api/analyze|agrupar|enrich → manutencao, endpoints
  novos pesquisa/preview/historico-log/snapshot/fluxos/voz).
- **CORREÇÕES DO OPERADOR (20/08, tarde)**: **🎤 Microfone** — permissão
  negada abre GUIA DE RECUPERAÇÃO (`MicrofoneDialog`): status AO VIVO
  (`permissions.query().onchange` detecta o desbloqueio pelo 🔒 no ato),
  endereço `chrome://settings/content/microphone` **COPIÁVEL** (páginas
  NÃO abrem `chrome://` — era por isso que o passo a passo antigo "não
  funcionava"), botão testar-agora e **PLANO B GARANTIDO: envio de
  ARQUIVO de áudio** (.wav/.mp3/.webm/.m4a → whisper transcreve igual,
  zero permissão). **Log de job PLANO** no popup: as linhas da pesquisa
  caiam no grupo "geral" RECOLHIDO (emoji 🧭 não estava na lista de
  grupos do `agruparLinhas`) — agora é lista corrida, sem dobras, +
  botão **TELA CHEIA por job** (`JobTelaCheia`) e polling tolerante (5
  erros SEGUIDOS → "conexão perdida"; engasgo de gateway não mata mais o
  job na tela). **Páginas são PÁGINAS** (fim do slide-over horizontal) e
  o rail mostra **ÍCONE + NOME** do módulo (Chat/Dashboard/Biblioteca/
  Estúdio/Sistema + Comandos ⌘K). **TEMPERATURE .5→0.1** (local+VPS com
  restart — o .env estava em 0.5, origem do "delírio") + bloco **"Tom
  executivo"** na spec hibrido.md (técnico/direto/estruturado, zero
  recheio, sem repetir a pergunta).
- **RODAPÉ DE MÉTRICAS + IN/OUT COMPLETOS (20/08, dia)**: cada resposta do
  chat termina com rodapé estilo operador — `N chamada(s) · N passo(s) ·
  N ferramenta(s) · Xs · Y tok/s · 🔻in · 🔺out · total tok` (duração do
  log de pensamentos; tok/s = saída/duração). Agente loga argumentos
  (400 chars) e observações (500) POR INTEIRO — o painel "pensando…"
  mostra o par IN/OUT como numa UI de agente (referência do operador:
  chat DeepSeek). **Anexo de imagem com visão desligada**: aviso com AÇÃO
  ("religue no badge 👁 e anexe de novo; ou descreva por texto") + toast,
  não erro cru (o erro "model does not support image input" era da
  ferramenta de referência — aqui a visão é servidor separado :8082).
- **AQUISIÇÃO UNIFICADA + DASHBOARD (20/08, madrugada)**: IngestTab tem
  UMA entrada — seletor de fonte (arquivos do computador | pasta do
  servidor | HuggingFace | pesquisa web) + coleção-alvo; TODO caminho
  termina no MODO REVISÃO (scores obrigatórios). "Pulso" virou
  **Dashboard** com "Histórico de solicitações": clique na execução abre o
  log COMPLETO (`GET /api/historico/log/{job}` lê `logs/jobs/{job}.jsonl`,
  job sanitizado por regex). **Jobs sobrevivem ao recarregar**: os running
  persistem em `ragaroy.jobs` (localStorage) e o polling do JobsPopup
  retoma no boot — o job NUNCA parou no servidor (executor), o que
  se perdia era só o estado na tela. **Pesquisa sem silêncio**:
  `_candidatos` loga cada motor com tempo gasto (minutos mudos pareciam
  travamento — serper são 6 consultas × até 20 s sequenciais). **Guard de
  saudação** no híbrido: mensagem curta com TODAS as palavras de
  saudação/identidade ("oi tudo bem?") responde sem buscar (antes: 3.199
  tokens de contexto aleatório; agora 459 e log "👋"). ARMADILHA: regex
  única não pega "oi tudo bem?" (duas frases) — tokenização resolveu.
  **Pensando ABERTO por padrão** (vivo e histórico; retrair é opção —
  pedido do operador "logs expostos"). **Microfone sempre visível**
  (indisponível → clique explica, não esconde). Rail Produção rotulado
  "por sessão".
- **Migração Qdrant local→VPS (20/08, noite 2)**: as coleções SEMPRE viveram
  no Qdrant LOCAL — a produção dava "Collection python doesn't exist".
  `tests_manual\migrar_qdrant_vps.py` snapshota todas (menos sessoes_chat)
  → scp → restore na VPS (18 coleções, 84k pontos, python 50.515/dotnet
  28.477). Produção agora é AUTOSUFICIENTE em dados. Pendências: midia_gerada
  e prompts_midia restauraram 0 na VPS (formato de payload rejeitado pelo
  server 1.12 — reingerir lá quando precisar); fantasma de coleção no Qdrant
  exige RESTART do container (rm -rf com ele rodando deixa meta órfão).
  Fonte de verdade de infra: `INFRA_PAAS.md (doc privada do operador, FORA do repo)` (GitFlow
  develop/main, domínios, credenciais, NUNCA compose down).
- **UI (20/08, noite 2)**: PillStatus do header em LINHAS COMPLETAS por
  serviço (ponto+nome+estado+detail com break-all — badges cortavam nomes
  de modelo; toggles liga/desliga preservados nos popovers). ChatTab
  RESTAURA a última conversa ao voltar ao app sem job em curso
  (`ragaroy.ultimaConversa` no localStorage; raw traz msgs+fontes+
  pensamentos; novaConversa limpa) — antes a tela voltava vazia.
- **📜 LOG DE JOB GRAVADO (20/08)**: `JobRegistry.log` escreve CADA linha
  também em `logs/jobs/{job}.jsonl` (append na hora; retenção: 400 mais
  recentes) — o registro completo sobrevive ao prune de memória e a
  restarts; `logs/historico.jsonl` ganha campo `log=` com o caminho.
  **Chat linha a linha**: a linha de tokens de CADA chamada LLM traz a
  ETIQUETA da etapa (`contadores.set_etapa`): `[reformulação]`,
  `[resposta (rag|híbrido|livre|auto)]`, `[roteador (auto)]`,
  `[refinamento da consulta (web)]`, `[agente passo N]`, `[verificação]` —
  o par chamada→retorno fica visível no "pensando…". Cache MISS também é
  narrado ("sem equivalente — seguindo o fluxo completo"). **Agente ReAct
  narrado** (`agent.responde(log=…)`): cada passo (raciocínio), cada
  ferramenta (ação ← argumentos + política: sem portão/aprovada/negada),
  cada observação de retorno (150 chars + duração) e a verificação
  anti-invenção. E2E: `tests_manual/e2e_log_chat.py` (roda contra o
  publicado; provisiona coleção de teste própria via upload e remove ao
  fim — produção não tem as coleções locais).
- **Compose prod da VPS**: WORKDIR/paths internos diferem do dev
  (processo escreve em ~/apps/rag-llama/… via bind) — o que
  importa: `logs/` NO HOST da VPS recebe tudo (validado: logs/jobs/*.jsonl
  persistem entre recreates). Diagnóstico de path no container:
  `ls -l /proc/1/cwd` + `docker inspect --format {{.Config.WorkingDir}}`.
- **⏹ Parar tudo** (`POST /api/parar_tudo`, admin; botão no header e no
  Sistema): mata jobs (registros → "cancelado") e desmonta TODOS os
  motores NO HOST — em
  container PROXYA ao agente (`/parar_tudo`; sem proxy matava nada e
  mentia "derrubado") incluindo ZOMBIES (taskkill por NOME pega
  llama-servers órfãos sem porta que seguram VRAM). Próximo uso recarrega.
- **🤗 HuggingFace como fonte** (`core/hf.py`, `POST /api/ingest/hf` job
  no executor): busca datasets no Hub e ingere os CARDS (README.md) pela
  MESMA esteira de higienização. O `search` do Hub casa o ID (termo
  único): frase → 0; fallback frase → 1º termo → termo mais longo.
  `HF_TOKEN` opcional no .env. Card "🤗 HuggingFace" na Biblioteca.
- **ingest_docs(docs…)**: núcleo do pipeline de ingestão extraído para
  aceitar Documents de QUALQUER fonte (pasta, HF, futura pesquisa) —
  `ingest_folder` lê/limpa e delega.

- **Composer (chat)**: controles na ordem do fluxo — 1º TIPO de resposta
  (📝texto/🖼/🎬/🎞/i2*), 2º MODELO (select RECONSTRUÍDO por tipo: texto
  mostra SÓ modelos de conversa; imagem só Flux; vídeo/gif só Wan2.2;
  i2t só o multimodal — cada modelo só aparece para o que serve), 3º MODO
  (desabilitado em mídia). Tipos i2* (imagem de ENTRADA) exibem **📎 subir
  imagem** (`/api/upload` → `saidas/entrada/` → campo `referencia`; chip
  com o nome, clique remove). Busca de coleções/MCP tematizada (variáveis
  CSS) e itens em estilo pill selecionável (`:has(:checked)` com acento).
- **Telas do projeto**: `docs/telas/` (GIFs reais de uso + capturas de
  todas as telas) — regenerar com Playwright contra o ambiente publicado.
- **Memória privada**: o registro das rodadas de trabalho (conversas com
  o operador) vive em `AGENTS-historico.md` — **gitignored**; o repositório
  público exibe apenas documentação do projeto.

- **Serviços LLM**: `python servicos_llm.py` (multi-OS — Windows/Linux/macOS,
  stdlib): pergunta a pasta dos GGUFs (grava `MODELS_DIR` no .env), lista os
  modelos, sobe bge-m3 :8081 + o chat escolhido :8090 e grava `LLM_MODEL`;
  `--listar` só lista, `--parar` encerra (PIDs em `saidas/servicos_llm.json`).
  Validadado em CI na matrix ubuntu/windows/macos (job `ci-multi-os`).
- **Cache semântico**: TTL configurável (`CACHE_TTL_DIAS`, default 30; era
  hardcoded 7) — HIT é 0 s/0 tokens vs ~0,3 s do Qdrant e 10-60 s da LLM:
  estender o TTL compensa mais que tunar o Qdrant; limpeza manual
  `DELETE /api/cache` (respostas sobrevivem a reingestões até expirar).
- **Criação de mídia no chat**: ao selecionar um tipo de mídia o composer
  fica LIMPO (coleções/MCP ocultos — não se aplicam) e entra o **guia do
  tipo** com conhecimento de bastidor por modelo (Flux: storytelling em
  cena única, sem texto na imagem; Wan2.2: cena única ~5 s, movimento
  suave; gif: 17 frames/12 fps, loop perfeito sem cortes; i2\*: papel da
  imagem anexa). O ✨ de mídia NÃO usa o histórico da conversa (privacidade
  — a cena nasce do rascunho; spec `prompt_melhoria.md`).

- **Página Sistema (configurações)**: form GERADO do registro `config.FIELDS`
  (todas as chaves editáveis do .env, agrupadas por categoria — Serviços,
  Estúdio, GPU, Aplicação); segredos (`config.SECRETOS`) renderizam como
  `type=password` com placeholder "•••• definida" (nunca o valor); o PUT
  genérico valida tipos (422) e vazio/máscara não regrava. Salvamento aplica
  NA HORA (`set_env_inplace` + `reload`) — inclusive TEMPERATURE e os
  parâmetros do cache (o `cache.py` lê TTL/limiar/max por getters ao vivo,
  não por constante de módulo).
- **Diagnóstico de disco (VPS)**: o `du` sem root SOME com `/root` e
  `/var/lib/docker` (permissão) — use um container root com bind `-v /:ro`
  para enxergar tudo (foi assim que 144 GB de cache HuggingFace órfão em
  `/root/.cache` foram achados e limpos).

- **Vídeo (Wan)**: gerações escolhíveis no combobox — **wan2.1-t2v-1.3b**
  (leve/estável em 8 GB: Q8 1,4 GB + VAE 2.1 própria + cfg 5.0) e
  **wan2.2-ti2v-5b** (5 GB + VAE 2.2 + cfg 6.0). ⚠️ **VAEs NÃO são
  intercambiáveis** — `_vae_video()` escolhe pelo stem do difusor;
  `_achar_video(modelo)` casa o alias do combobox por substring
  alfanumérica. GGUFs em `MODELS_DIR/video/` (difusor + umt5 +
  wan2.1_vae/wan2.2_vae).
- **Duração de vídeo**: seletor ⏱ (2/3/5/8 s) no composer → frames=s×16+1
  (spec `core/specs/midia_duracao.md` — tabela + como escalar a AÇÃO do
  prompt por duração; o ✨ recebe a dica "video Ns"). GIF segue loop fixo
  de 17 frames. Teto de frames no `_sanear_params`: 129.
- **Mídia com contexto**: as últimas trocas da SESSÃO viajam como
  `params["historia"]` e entram no prompt da difusão como continuidade
  narrativa ("mantenha personagens/ambiente/enredo") — em `_rodar_tarefa`
  e no espelho do `agente_host`.
- **Telemetria por modelo**: o `LLMContada` registra o modelo **SERVIDO**
  (`modelos.servido()`) — `config.LLM_MODEL` fica velho após trocas;
  gerações rodam na estação (agente), então EM CONTAINER a API regrava o
  evento de geração na VPS (senão o dashboard de produção nunca vê
  wan/flux). Filtro de tipo: "cache" é alias só de "redis".
- **Guia de mídia com chips de prompt**: ao escolher um tipo de mídia o
  guia abre sozinho com dicas CLICÁVEIS que inserem no prompt — 🎨 estilo
  (realismo/cinematográfico/anime/desenho animado/quadrinhos/3D/aquarela/
  pixel art/cyberpunk/minimalista), 🌍 ambientes, 💡 luz·câmera e 🎥
  movimento (vídeo/gif). O ✨ depois transforma o rascunho em storytelling
  denso.

- **Sandbox (deps + nomes)**: o ▶ testar INSTALA as dependências que o
  código importa (`_deps_python`: imports de todos os .py → pip com
  `--break-system-packages` antes de rodar; stdlib fora, mapa
  PIL→pillow/sklearn→scikit-learn/…). O nome do arquivo no teste por card
  usa a DICA da prosa (`_nomesCitados`) — nunca diverge do sugerido no chat.
- **Painel/leitura**: a aba 📚 fica SÓ com fontes (o botão "▪ resposta"
  abre um MODAL de leitura em tela ampla — não injeta mais na aba). O
  badge ⚡ do cache é um POPOVER (`/hx/cachepanel`) com as entradas
  carregadas (pergunta, quando, escopo, resumo) — refresh 30 s.
- **Feedback de ações**: o salvar do Sistema mostra TOAST (sucesso
  recarrega; 422 mostra o detalhe do erro sem recarregar) — `#toast-global`.
- **Dashboard**: seção **Infraestrutura** (Qdrant por coleção, Redis do
  cache com limpar, resumo Rabbit), cards de modelo com **médias por
  chamada** (~🔻/~🔺/🪙) e **📜 logs tail ao vivo** por serviço
  (`/hx/logs/{chat|api|embed|visao}`).

- **Modelos ativos (fonte única)**: `modelos_ativos()` (app.py, cache 10 s)
  lê dos SERVIDORES chat/visão/embed + difusores + VRAM — serve o
  `/api/modelo/ativo`, o cabeçalho do Sistema ("no ar: …") e o badge do
  chat (i2t ⇒ 👁 multimodal). `estatisticas.nome_curto()` agrega telemetria
  sem sufixo de quant (Wan…-Q8_0 ≡ wan…); a união do dashboard rastreia
  `_ja` adicionados.
- **Logs ao vivo (dashboard)**: `/hx/logs/{llm|geracao|eventos|jobs}` lê a
  TELEMETRIA persistente + tail de logs/jobs — fontes que existem em
  qualquer host (as antigas apontavam arquivos do llama-server que só
  vivem na estação com GPU).
- **Sandbox**: ▶ do card testa SÓ os arquivos daquele retorno (painel =
  conversa inteira); preview renderiza a URL pública (subdomínio+token).
  **Negative prompt**: cláusula `negativo: …` no pedido vira o `-n` da
  difusão (imagem/vídeo; spec midia_duracao).
- **Teste de SITE (24/08, fix crítico)**: o script antigo fazia
  `pip install … && python3 app.py > site.log &` — o `&` mandava pip+python
  ao background e o `kill $SRV` matava o SUBSHELL (o python órfão seguia
  segurando a porta). Sintoma real: curl respondia em 0,5 s com o servidor
  de um teste ANTERIOR e `site.log` nem existia. `_cmd_site` agora: porta
  limpa antes (fuser guardado), pip em FOREGROUND, servidor com `setsid`
  (grupo próprio), curl `--max-time` com aviso quando a porta não responde,
  `head` tolerante. Validado em produção: 2 rodadas seguidas, cada uma com
  a própria home e log (órfão anterior morto com restart do container).
- **App TEMPORÁRIO público (24/08 — "sandbox.disroy.org por um tempo")**:
  o teste de site agora NÃO mata o servidor — ele fica hospedado ~30 min
  (`APPS_VIVOS` + `chave_app` = porta+expiração assinadas com HMAC do
  AUTH_SECRET). Fluxo: agent ganhou `GET/POST /app/{porta}/{resto}` (proxy
  urllib p/ 127.0.0.1:porta) ← API expõe `/sandbox/app/{chave}/{path}`
  (GET/POST/HEAD, sem login — quem tem o link acessa; `follow_redirects=
  False`: o Location volta ao browser com a chave) ← middleware `_auth_
  middleware` RESGATA URLs absolutas: 404 + Referer com chave válida →
  proxy (o Flask gera `/static/x.css` e `Location: /login` SEM a chave).
  `Cache-Control: no-store` no proxy (Cloudflare negative-caching segurava
  404 antigo no edge por minutos). Limpar do modal é PULSO com app vivo
  (o servidor lê templates/ de /tmp/work). Novo teste na mesma porta
  substitui o app (fuser). **Templates/static**: `_reorganizar_web` move
  .html soltos p/ `templates/` quando o .py usa `render_template` (bug
  TemplateNotFound do schopenhauer) e .css/.js p/ `static/` quando
  QUALQUER arquivo referencia /static/ (a ref. pode estar no template).
- **Sandbox: SUBDIR por teste + entry C# real (24/08, 12h)**: `/tmp/work`
  acumulava lixo de testes anteriores (o controller ASP.NET velho do dono
  quebrava o build seguinte com CS0260/CS8802 — e o `limpar` pulava com
  app vivo). Agora CADA teste grava/executa em `/tmp/work/{uuid}` (agent
  `/arquivos {dir}` + `/exec {dir}`; `/ver` acha o arquivo pelo subdir
  MAIS RECENTE; `/limpar {exceto:[dirs]}` preserva só as pastas dos apps
  vivos — `APPS_VIVOS[porta]["dir"]`). **C#**: `_preparar_cs` decide o
  entry de verdade — csproj da conversa tem prioridade; `static Main(`
  em qualquer arquivo = entry clássico (nada move); top-level statements
  funcionam em QUALQUER nome de arquivo (o move p/ Program.cs só era
  necessário para o principal top-level — arquivo de CLASSE movido quebrava
  CS1585/CS5001); só tipos/biblilioteca → gera Program.cs que compila e
  AVISA ("projeto compilou ✓… use o ▶ no arquivo com Main"). **ASP.NET**
  (`Microsoft.AspNetCore`/`WebApplication`/[ApiController] nos fontes):
  scaffold `Microsoft.NET.Sdk.Web` (o console dava CS0234) e o teste vira
  SITE (sobe `dotnet run`, captura, link público — loops de espera 120
  p/ o build/restore).
- **Modal do sandbox SAME-ORIGIN + hardening (24/08, 13h)**: o iframe do
  modal usa `d.url` (rota RELATIVA em ai.disroy.org — cookie autentica);
  `url_publica` fica só no link "abrir em outra aba" (o modal dependia do
  subdomínio e um engasgo do túnel virava "conexão recusada" com o app
  abrindo em outra aba). **Hardening do agent** (estudo Piston/Judge0:
  fork não compensa — one-shot, privilegiado, GPLv2; emprestamos as
  defesas): `_exec` com `start_new_session` + `os.killpg(SIGKILL)` no
  timeout (mata filhos E netos — fork-bomb/árvores de servidores; o
  setsid do app vivo fica fora do grupo e sobrevive) + `ulimit -t`
  (CPU 4× timeout). ⚠️ ARMADILHAS PROBADAS NO CONTAINER: `preexec_fn`
  com setrlimit DEADLOCKA (ThreadingHTTPServer + fork); `ulimit -u`
  mata o dotnet nem em --version (user-namespace conta threads errado);
  `ulimit -f` gera SIGBUS nos mmaps do .NET. Só -t e killpg são seguros.
- **▶ testar RESPOSTA (24/08, 14h)**: o botão ▶ vive no CABEÇALHO DO
  GRUPO da resposta (`grp-testar`; ▶ por arquivo REMOVIDO — o teste é do
  PROJETO). `principal` vazio no POST → `sandbox.escolher_principal`:
  Program.cs válido > Main em qualquer .cs > top-level qualquer nome >
  site Python > WebApplication em qualquer .cs > nomes convencionais >
  1º executável > HTML puro (preview). ⚠️ o rótulo do grupo vive num
  `<span>` — `grupo.textContent = …` APAGA o botão filho (aconteceu).
  **.NET 10 na sandbox**: SDK 10.0.400 via dotnet-install + runtime
  aspnetcore 10 + shared frameworks do 8 (apt) LIGADOS no MESMO
  `DOTNET_ROOT=/usr/share/dotnet10` (sem isso: app net8 compilava e não
  rodava; Sdk.Web net10 não compilava). csproj da conversa é COMPLEMENTADO
  com `<ImplicitUsings>enable</ImplicitUsings>` quando falta (CS0103
  'WebApplication' com todas as references corretas).
- **BUG CRÍTICO de nomes (24/08, 14h30 — a raiz do "dotnet não
  funciona")**: o sufixo da regex de comentário era OBRIGATÓRIO
  (`\s*-{2,}>?\s*$` → só `<!-- x -->` casava; `// Program.cs` e
  `# x` NUNCA casaram) E a dica citada por índice não conferia
  linguagem — o bloco de TERMINAL `dotnet new webapi…` ganhava o nome
  "Program.cs" citado na prosa e o Program.cs REAL virava "bloco4.cs":
  o teste compilava o COMANDO como C# (CS1585 "new" na col 8 era o
  `dotnet new`!). Fix: sufixo opcional `(?:\s*-{2,}>?)?` + dica só com
  extensão igual à linguagem do bloco. Com isto a conversa REAL do dono
  (TucupiApi) compilou, subiu e `/api/pratos` respondeu pelo link
  público. Spec arquivo_codigo.md ganhou "Projetos que RODAM" (entry
  único, sem misturar tipo+top-level no mesmo arquivo, ImplicitUsings,
  ports 5000/8000).
- ⚠️ **Deploy VPS é SEMPRE pelo CI**: `docker compose up` MANUAL na VPS
  recria a API SEM o override da infra (`~/infra/services/rag-llama/
  docker-compose.prod.yml`, que a coloca na `traefik_net`) → Traefik
  perde a origem e TUDO fica 502. Se precisar manual:
  `docker compose -f docker-compose.yml -f /home/rodney/infra/services/
  rag-llama/docker-compose.prod.yml up -d --force-recreate --no-deps api`.
- **Apps temporários COEXISTEM + páginas amigáveis (24/08, 15h)**: a
  porta do app é LIDA DO CÓDIGO (`_porta_do_codigo`: `port=NNNN`,
  `://127.0.0.1:NNNN`, `localhost:`, `--port`; default 5000/8000 só sem
  declaração) — antes era FIXA por framework e todo teste novo fazia
  `fuser -k 5000`, MATANDO o app anterior do dono ("o link de 30 min
  morreu sozinho"). Spec `arquivo_codigo.md` pede porta própria
  5000–5099. Erros AMIGÁVEIS no subdomínio (`_sandbox_fora.html`,
  `no-store`): raiz 200 explica o que vive ali · link expirado/
  substituído 410 "⏳ este link expirou" · app caído 502 "🔌 saiu do ar"
  — sempre com "rode o ▶ testar resposta de novo" (antes: 404/JSON cru).
  ⚠️ `_pag_fora` devolve status POR MAPA (raiz=200) — o ternário
  "expirou?410:502" mandava 502 até na raiz (bug da 1ª versão).
  O 502 do Cloudflare em ai.disroy.org durante ~20 s = restart da API
  no deploy (CI `--force-recreate`) — janela normal de publicação; a UI
  aberta se recupera sozinha (polling/toast).
- **🎮 Padrão VRAM-compatível + NPU opcional (25/08)**: o boot do agente
  NÃO tenta mais carregar modelo que não cabe (ou não existe): troca pelo
  MAIOR chat compatível (`GB_MAX_CHAT` 6 GB + embedding em 8 GB). O menu
  do `servicos_llm.py` sugere o 1º compatível (Enter), marca ⚠️ os que
  estouram e exige confirmação explícita; encoders de difusão (umt5/
  t5xxl/clip/mmproj) ganharam categoria própria `encoder` em `PADROES`
  (antes caiam no default "chat" e o umt5 de 6 GB aparecia como opção
  de conversa). **FastFlowLM (NPU) é apenas um PROVEDOR opcional**
  (`PROV_FLM_BASE_URL` — flm serve é OpenAI-compatible; sem NPU nada
  muda): máquina com Ryzen AI pode pôr leitura de documentação longa/
  logs na NPU enquanto a GPU cuida do chat — arquitetura híbrida sem
  código novo (o sistema de provedores já cobre).
- **🤗 HF com SELEÇÃO + TEMPERATURE única (24/08, 20h)**: Biblioteca →
  fonte `huggingface (datasets)` abre VITRINE (`GET /api/hf/datasets` sem
  `q` = mais baixados; com `q` = busca do `hf.buscar`) com CHECKBOXES —
  ids marcados (`hf_ids` vírgula-separado no form) entram INTEIROS na
  revisão (`PreviewIn.ids` → `docs_hf(ids=)` pula a busca); sem marcação
  segue a query. `hf.populares()`/_resumo compartilham o formato. O aviso
  da UI mostra se HF_TOKEN está ativo (ele NÃO está no .env — colar na
  tela Sistema e SALVAR; sem token a API pública serve com rate-limit
  menor). **TEMPERATURE 0.5 para TODAS as LLMs** (default 0.5 no config;
  override 0.15 do coder REMOVIDO — uma regra, editável no Sistema;
  .env da VPS já em 0.5). **Termos de teste NEUTROS**: nunca usar o
  vocabulário de teste do dono (tacacá/vatapá/tucupi…) em
  tests/tests_manual/specs-exemplos — trocado por docker/python/k8s.
  README: framing "sua LLM local com RAG" + tabela local×externo
  (custo/latência/uso). Permissões do Kilo: `permission` allow-all no
  `~/.config/kilo/kilo.json` (vale para code/architect/plan).
- **🤗 HF "MOSTRAR TUDO" + SUA CONTA (26/08)**: `hf.meus()` —
  `whoami-v2` + `datasets?author=me` (limit 100, INCLUSIVE PRIVADOS) com
  o HF_TOKEN; a rota `/api/hf/datasets` ganha `usuario`/`meus` e limite
  clamp 200; Biblioteca mostra "👤 seus datasets (@conta)" no TOPO antes
  da vitrine, que começa em 50 com botão "mostrar mais…" (+50 por clique).
  Sem token: vitrine pública segue e o aviso ensina a colar (Sistema →
  HF_TOKEN aplica NA HORA — quem decide é o .env do ambiente). Token
  inválido degrada em silêncio (log ⚠️, seção some — nunca quebra a
  vitrine). E2E `Temp\kilo\e2e_hf_tudo.py` (WAF da borda BLOQUEIA
  User-Agent "Python-urllib" → 403 sem body; UA custom passa).
- **⚡ ENVIO INSTANTÂNEO + ESTILO DA VERSÃO + BUG top_score (26/08)**:
  (1) **textarea limpa NO ATO do submit** (era no `afterRequest` — o
  texto ficava na caixa até o POST voltar); se o envio FALHA o texto
  VOLTA (`_ultimoEnvio`). (2) **título semântico SAIU do POST** — o
  `titulo_semantico` (embed, guard 3,5 s) pendurava o envio na 1ª
  mensagem; agora a sessão nasce "(sem título)" e o título é calculado
  no POLL de conclusão (embed quente, usuário já lendo). POST medido:
  **0,03–0,05 s** em produção. (3) **spec `arquivo_codigo.md` regra 10**:
  a VERSÃO citada no pedido manda no ESTILO — .NET 6+ (inclui 10) =
  minimal hosting (`WebApplication.CreateBuilder`, top-level, MapGet/
  MapPost; PROIBIDO Startup.cs/CreateHostBuilder/ConfigureWebHostDefaults/
  Main explícito/namespace no Program); o material recuperado é
  referência de CONTEÚDO, não de moda — provado: mesmo pedido que gerou
  Startup.cs agora gera `// src/Program.cs` top-level. (4) **BUG
  UnboundLocalError `top_score`**: resgate por versão com densa VAZIA
  reabastecia `achados` e o rodapé lia `top_score` (só nascia no
  `if achados`) — a resposta MORRIA inteira ("cannot access local
  variable"); nasce 0.0 junto da busca e recalcula com os extras.
- **🔧 RAG LENTO + "ENVIANDO…" ETERNO (26/08, tarde)**: diagnóstico por
  timestamps do job (logs/jobs/*.jsonl): com 14 coleções a busca Qdrant
  é RÁPIDA (~3 s/rodada) — os vilões eram (a) **rerank DUPLICADO** (F2
  top-15 + resgate do gate fraco nos 8 = ~15 s CADA na CPU de 2 vCPU —
  mesma consulta, ~mesmos textos) e (b) LLM de geração (~25 s, GPU
  local via túnel). FIXES: **cache de pares no `rerank.notas_de`**
  (`_NOTAS` (modelo, consulta, hash texto)→nota, FIFO 1024 — a 2ª
  chamada sai a custo ZERO, provado 9,2 s→0,000 s) + **rerank do chat
  em top-8** (era 15; ~2 s/par na VPS). Busca total: 33 s→~20 s.
  **"enviando…" eterno + bolha DUPLICADA** (página aberta atravessou um
  deploy; POST em conexão morta pende para sempre; retry duplicava):
  `hx-request timeout 20000` no form + **guard `_enviando`** com
  `htmx:beforeRequest` preventDefault (2º POST no ar NEM SAI) +
  falha/timeout → bolha sai, texto VOLTA à caixa, toast explica.
  Nota: uvicorn SÓ loga request ao RESPONDER — POST pendurado não
  aparece no access log (não chegou ≠ não respondeu). torch da VPS
  já usa os 2 vCPUs (`torch.get_num_threads()==nproc`).
- **🚨 GUARD CANCELAVA O PRÓPRIO POST (26/08, tarde 2)**: o `_enviando`
  do guard anti-duplicado subia no listener de `submit` — ANTES do
  `htmx:beforeRequest` — que via a flag setada e **preventDefault() no
  PRIMEIRO pedido**: nenhum POST saía mais (chat mudo, "enviando…"
  eterno, jobsbar VAZIO = nenhum job existia — o vazio do jobsbar é o
  estado normal "sem jobs"). REGRA: flag de in-flight só sobe DENTRO do
  `beforeRequest`, DEPOIS do check (o submit NATIVO não sabe se o htmx
  vai emitir). Duplicado bloqueado no beforeRequest → remove a BOLHA
  otimista que o submit ansioso já criou e devolve o texto à caixa.
  Validação de JS do template: extrair `<script>` + `node --check`
  (`Temp\kilo\checa_js.py`) — sintaxe OK não pega ERRO DE LÓGICA de
  ordem de eventos; para guard de envio, pensar na SEQUÊNCIA submit →
  beforeRequest → afterRequest.
- **🚨 CAUSA RAIZ FINAL do chat mudo (26/08, tarde 3 — htmx
  validation:halted SILENCIOSO)**: o textarea tinha `required` E o meu
  listener de submit limpava a caixa "no ato" — listeners rodam na
  ordem de registro (o meu script inline vem ANTES do init do htmx),
  então o htmx validava o form COM A CAIXA JÁ VAZIA →
  `htmx:validation:halted` → **request abortado sem NENHUM erro de
  console/rede** (achado com Playwright: `configRequest` nunca chegava
  a disparar; `form.querySelectorAll(':invalid')` mostrava
  `question|textarea|valueMissing:true`). FIX: `required` REMOVIDO
  (vazio bloqueado com `e.preventDefault()` no submit) e a limpeza da
  caixa mudou para o `htmx:beforeRequest` — os PARÂMETROS do form são
  capturados pelo htmx ENTRE o submit e o configRequest: limpar no
  submit mandava `question=""` no POST (a resposta viria "pergunta
  vazia"). REGRA HTMX: nunca mutar inputs do form no listener de
  submit; mutar só em `beforeRequest`/`configRequest` (parâmetros já
  capturados). DIAGNÓSTICO que provou: Playwright headless + listener
  de `htmx:validation:halted` + patch de
  `XMLHttpRequest.prototype.open` (o evento `response` do Playwright
  engoliu o POST em 1 script — patch de XHR é a captura confiável;
  `Temp\kilo\e2e_browser2.py` é o E2E de referência: login → Enter →
  POST → card → resposta). jobsbar em branco é ESTADO NORMAL: só
  lista jobs de pesquisa/preview/ingest/seed/manutencao — job de CHAT
  vive na conversa (#pensando), nunca no jobsbar.
- **🙈 MODELO SÓ QUANDO A LLM FOI CONSULTADA (26/08, tarde 4)**: pedido
  do dono ("não exibir o qwen quando é somente o rag, nem no log").
  (1) linha "🧠 modelo X já está no ar — sem recarga" REMOVIDA do log
  (ruído em todo modo; trocas/recargas seguem logadas). (2) o runner do
  job de chat zera `res["model"]` quando `tokens.chamadas == 0`
  (cache/resposta direta — sem consulta) OU o modo PEDIDO no composer
  foi `rag` (capturado como `_modo_pedido` ANTES do _processar_query —
  o roteador muta corpo.mode para "hibrido" na escalada de criação) →
  header da mensagem fica só "assistente". Hibrido/livre/auto com LLM
  seguem exibindo (provado: 1 chamada → qwen visível). (3) DEDUPE no
  `JobRegistry.log`: linha idêntica consecutiva no mesmo segundo é eco
  (a linha 🪙 de tokens aparecia 2× no raciocínio — chamadas contavam
  1 mas a linha duplicava). GitHub Actions pode NÃO disparar run no
  push (fila) — `gh workflow run ci-cd --ref main` dispara manual
  (workflow_dispatch existe); conferir o commit na VPS
  (  `git log -1` em ~/apps/rag-llama) vale mais que o status do run.
- **🚀 BOOTSTRAP ASP.NET quando falta o Program.cs (26/08, noite 2)**: a
  conversa às vezes traz SÓ CONTROLLERS/modelos (arquivo de classe sem
  Main/top-level) — o teste "compilava" com o placeholder "sem ponto de
  entrada" e o site NUNCA subia. `_preparar_cs` caso 5 com aspnet=True
  agora GERA o hosting: `AddControllersWithViews` + `MapControllers`
  (rotas de atributo [Route]/[ApiController]) + `MapControllerRoute`
  padrão (convencional `/{controller}/{acao}` para quem só tem
  [HttpGet]) + `MapGet("/")` com dica + `Run` na porta lida do código;
  log "🚀 bootstrap ASP.NET gerado". Console sem entry segue com o
  placeholder. ⚠️ LIÇÃO do CS1003: código C# GERADO pela sandbox é
  ASCII puro e UMA declaração por linha — a 1ª versão com travessão +
  literais adjacentes concatenados em linhas separadas dava
  `CS1003 ',' expected` no container (compilava local?? não testado —
  a simples compila). VALIDAÇÃO: dotnet 10 LOCAL (build limpo, home
  responde, `/api/Culinaria` devolve o JSON) + produção nos DOIS
  formatos (com [Route] e só [HttpGet]) — app NO AR com preview.
  Estação tem dotnet 10.0.301: `gera_boot.py` recria o caso em
  `Temp\kilo\boot_cs` para compilar antes de publicar.
- **🧹 SEM-CSPROJ + HOME 404 NO PREVIEW (26/08, noite 4)**: (1) o
  refactor do bootstrap tinha PERDIDO o scaffold `app.csproj` no ramo
  sem-entry (só gravava o Program.cs → "Couldn't find a project to
  run" com 3 .cs soltos): `extras = {} if csprojs else {"app.csproj":
  _CSPROJ_WEB/_CSPROJ}` ANTES do bootstrap/placeholder — compilado 0
  erros no dotnet 10 local e em produção. (2) **app python sem rota
  `/`** (fastapi só com /api/...): o link público abria na página 🧭
  morta. `_proxy_app_api`: 404 na RAIZ tenta `/docs` (Swagger
  automático do FastAPI) e REDIRECIONA (302) — provado: app sem home
  abre na documentação interativa; sem /docs segue a página amigável.
  Spec regra 9 ganhou "todo app web NASCE com rota / (home) que
  LINKE os endpoints" (a raiz do problema: app sem home).
- **🏠 FLASK SEM HOME: HOME INJETADA (26/08, noite 5)**: o fallback
  /docs era FastAPI-ONLY — app Flask só com /api/... (o
  api_culinaria.py do dono) subia mas o link público abria na 🧭
  morta. `_injetar_home_flask` (chamada no ramo flask do site-test):
  se não existe `@app.route("/")`, injota ANTES do `if __main__`/
  `app.run(` uma home DINÂMICA que lista as rotas via `url_map`
  (exclui /static) — app com home própria é intacto. Prova em
  produção: raiz do link 200 com "app no ar" + "/api/culinaria".
  ⚠️ ARMADILHA de edit: ancorar `oldString` na LINHA DE `def` de uma
  função substitui a assinatura e ÓRFÃA o corpo (aconteceu com
  `_cmd_site` — sempre conferir com rg/compile depois de inserir
  função nova).
- **🔤 MOJIBAKE no sistema.html (26/08, noite 6)**: a tela Sistema exibia
  `ServiÃ§os`, `â€` (travessão) — o arquivo nasceu CORROMPIDO no squash
  do release inicial (utf-8 lido como cp1252 e regravado: 54/80 linhas;
  ÚNICO arquivo do repo — varredura com regex `[ÃÂ][...]` em
  templates/core/api/static/docs/tests/scripts). REVERSÃO byte a byte:
  `encode('cp1252')` char a char com FALLBACK para o BYTE literal nos
  chars de controle U+0080..U+009F (cp1252 não mapeia 0x81/0x8D/0x8F/
  0x90/0x9D — encode direto falha) `.decode('utf-8')`; BOM duplo
  removido (utf-8-sig falha quando há 2 — loop no \ufeff). LIÇÃO:
  depois de qualquer WRITE grande em .html/.md, varrer mojibake antes
  do commit. Validado em produção: `/sistema` sem mojibake.
- **☁️ CADASTRO DE PROVEDORES CLOUD (27/08, manhã)**: pedido do dono
  ("onde seleciono a zai para glm… preciso ter um cadastro de provedores
  cloud"). `POST /api/provedores/cadastrar` (admin, JSON {id, base_url,
  nome?, api_key?, modelos?}) grava `PROV_<ID>_BASE_URL/_API_KEY/_NOME/
  (_MODELOS)` via set_env_inplace + reload e devolve o catálogo JÁ com
  os modelos reais (GET {base}/models). UI: cartão "☁️ Cadastrar
  provedor cloud" no Sistema com PRESETS no datalist (Z.AI
  api.z.ai/api/paas/v4 · Zhipu open.bigmodel.cn/api/paas/v4 · DeepSeek
  · OpenAI · Anthropic · OpenRouter · Groq) — salva e recarrega; o
  grupo 🌐 entra no seletor do chat sozinho (auto-descoberta PROV_*).
  ⚠️ BUGS da rodada: (a) retorno comparava pid MAIÚSCULO no catálogo
  (ids() LOWERIZA — "meu" nunca achava, lista vazia); (b) **WAF da
  borda bloqueia UA de Python** (python-httpx/OpenAI/Python → 403 sem
  body) — `_modelos_do_endpoint` e o `LLMContada` (default_headers) usam
  UA próprio `ragaroy/1.0`; (c) o chat :8090 da estação CAÍRA de novo
  (túnel 502 — "nenhum modelo listado" na UI era ISSO): restart do
  agente host reergue. E2E: `Temp\kilo\e2e_prov.py`.
- **☁️ CONHECIDOS em 1 clique + METADADOS de modelo (27/08, manhã 2)**:
  `provedores.CONHECIDOS` (zai=Z.AI Coding Plan, openai=ChatGPT,
  anthropic=Claude, deepseek, openrouter, gemini, grok, groq, mistral —
  nome/URL/dica/site) viram CARDS no Sistema que preenchem o form
  (`usarProv` — falta só a chave); `GET /api/provedores/conhecidos`.
  **Informações do modelo AUTOMÁTICAS**: `_modelos_do_endpoint` devolve
  (nomes, meta) — meta quando a API entrega (OpenRouter:
  context_length/description/pricing → "US$ x/M in"; input_modalities
  vira `visao_api`); sem metadado → `_ctx_do_nome` heurística por regex
  (glm-4.x 200k, gpt-5 400k, gpt-4.1 1M, claude-3/4 200k, deepseek
  128k, gemini-2.5 1M, grok-4 256k, qwen2.5 32k, qwen3 256k, llama-3
  128k — desconhecido → None, NUNCA inventar). `modelos()` =
  [{nome, visao, ctx, info}]; chips do Sistema e options do chat
  mostram "· 200k" com tooltip (info/preço). Provado: OpenRouter
  público traz 417 modelos TODOS com ctx/descrição/preço; ESTACAO
  qwen2.5-coder ctx 32768 pela heurística. ⚠️ OpenRouter /models é
  público (sem chave) — dá pra VER o catálogo antes da chave.
- **🏷️ MODELOS por CATEGORIA + "para que serve" (27/08, manhã 3)**:
  `provedores.categoria_do_modelo(nome)` → (cat, uso): EXCLUSÕES de
  chat primeiro por regex — `imagem` (glm-image/cogview/dall-e/flux/
  sora… "🎨 GERA imagens — não é chat"), `audio` (tts/whisper/voice),
  `embed` (embedding/rerank) — depois `visao` via `e_multimodal`
  (respeita falsos positivos; `glm-v\d` adicionado p/ família V da
  Z.AI — "GLM-V5-Turbo" é multimodal), `programacao` (coder/codestral),
  `raciocinio` (reasoner/thinking/r1/qwq), default `conversa`.
  `modelos()` ganha `cat`/`uso`; `info` cai no uso quando a API não dá
  descrição. **Chat**: optgroups `🌐 <prov> · <categoria>` (visão →
  programação → raciocínio → conversa; imagem/áudio/embed FORA do
  select — não servem chat). **Sistema**: chips agrupados por categoria
  com o uso no header do grupo e tooltip com uso/ctx/preço.
- **👁 MÓDULO MULTIMÍDIA + decisão SwarmUI (27/08, manhã 4)**: pedido do
  dono ("para o que for multimodal, novo módulo; vale fork do
  SwarmUI?"). **DECISÃO: NÃO forkar** — SwarmUI é aplicação standalone
  C#/.NET focada em GERAÇÃO t2i via backends ComfyUI: não cobre ANÁLISE
  (i2t) de provedores cloud, não conversa com Qdrant/chat/MCP/specs/
  sandbox, e manter um fork C# fora da nossa esteira (Python/FastAPI/
  HTMX) viraria dívida sem ganho (mesma lógica do fork do agent
  sandbox). O que aproveitamos são as IDEIAS (galeria/lightbox — já
  temos). **Implementado com os componentes PRÓPRIOS**: página `/midia`
  (nav "Multimídia") — upload de imagem (/api/upload → saidas/entrada)
  + select de multimodais (local 👁 Qwen2.5-VL + TODOS os cat=visao dos
  provedores, com ctx/uso) + pergunta → `POST /api/midia/analisar`
  (job `_midia` na FILA via _despachar; anti path-traversal: só
  basename dentro de ENTRADA) → `midia.legendar_imagem(arquivo,
  pergunta, modelo=)` (externo `prov:nome` roda NA API sem tocar a GPU;
  local pausa o chat, sobe :8082, restaura) → resultado com copiar e
  **📚 ensinar a base** (web-salvar {colecao, documentos:[{titulo,
  content}]}). Status: `/api/midia/status/{job}`. Áudio/vídeo seguem
  pelo chat (🎤/anexos) — módulo é o atalho da análise avulsa.
  ⚠️ BUGS da rodada (todos no caminho local-em-produção): (a) log do
  job com grupo como 3º POSICIONAL → TypeError → **DLQ** (JobRegistry.log
  é (jid, msg, **extra) — grupo é kwarg `etapa=`); (b) `_subir_vl` no
  CONTAINER procura `D:\models` no Linux → local em container SEMPRE
  proxya ao AGENTE (`/visao`); (c) o upload vive no VOLUME DA VPS e a
  GPU na ESTAÇÃO — caminho não atravessa: a imagem viaja **BASE64**
  (`{b64, nome, pergunta}`; agente grava em saidas/entrada dele e
  analisa) — mesmo fix no `/api/visao` do anexo do chat; (d) marker
  `vl_off` da estação dava "desligado manualmente" — `POST /api/vl/
  ligar` remove e PRÉ-AQUECE (o VL existia em D:\models\visao: 4,36 GB
  + mmproj 0,79). ⚠️ AGENTE da estação roda o CÓDIGO LOCAL: mudou
  agente_host.py → restart do processo (o deploy da VPS não o atualiza).
  E2E `Temp\kilo\e2e_midia.py`: PNG vermelho 96×96 → análise "Vermelha".
- **🎨 SISTEMA em OOUX + TODOS os multimodais (27/08, manhã 5)**: pedido
  do dono ("caixas despadronizadas e funções sem serem utilizadas" +
  "multimídia precisa aparecer todos"). **Sistema** virou 4
  cartões-OBJETO padronizados (h2+descricao, .grade/.kpi, grids
  auto-fit): 🧠 Motor (4 KPIs + chips dos provedores CADASTRADOS com
  nº de modelos + 🔧 manutenção da GPU em `<details>` — ações raras
  fora do caminho) · ☁️ Provedores cloud · 🔌 Serviços · ⚙️
  Configurações. **REMOVIDO** o cartão "LLMs — local + provedores"
  (select+fetch `llmOndeMudou` duplicava o seletor do CHAT — a função
  morta apontada). **Multimídia** lista TODOS os multimodais em
  optgroups: 🖥 local (GGUFs categoria visao de `modelos.listar()`) ·
  🌐 cadastrados (cat=visao; grupo SEMPRE visível com placeholder "—
  nenhum multimodal cadastrado —") · 🔑 conhecidos SEM cadastro
  (CONHECIDOS ganhou `visao: [...]` — glm-4.5v/glm-v5-turbo, gpt-5,
  claude-sonnet, gemini-2.5, grok-4, exemplos OpenRouter); o `info` do
  modelo aparece ABAIXO do select ao escolher; usar um 🔑 sem cadastro
  → erro do job orienta ("cole a chave em Sistema → ☁️"). E2E
  `Temp\kilo\e2e_ooux.py`.
- **🎨🎬 GERAÇÃO no Multimídia + FLUX no REGISTRO (27/08, manhã 6)**:
  pedido do dono ("e a geração de vídeos/imagens? cadê meus modelos
  locais — estão em D:\models, por que não puxa de lá?"). CAUSA: em
  container o `modelos.listar()` usa o **REGISTRO** fixo (D:\models não
  existe no Linux da VPS) e os **Flux NÃO estavam no REGISTRO** (os Wan
  sim — por isso só vídeo aparecia). FIX: `flux1-schnell`/`flux1-dev`
  (D:\models\imagem\…-Q4_K_S.gguf, categoria imagem) no REGISTRO. **/midia
  ganhou "Gerar mídia (GPU local)"**: tipo (🖼 t2i · 🎬 t2v · 🎞 gif) +
  modelo (categorias imagem/video do listar, com GB) + duração (2/3/5/8 s
  → frames=s×16+1) + prompt → `POST /api/tarefas` (o MESMO motor do
  chat; em container proxya ao agente) → poll `/api/tarefas/status/{tid}`
  com log/progresso → render `<img>/<video>` + baixar (pull-back
  automático). ⚠️ CONTRATOS: gerador viaja em **`params.modelo`** (o
  campo `modelo` do TarefaIn é o de CHAT — guard 409 "modelo
  divergente"); POST devolve **`{tarefa: tid}`** (não job); status usa
  **`erro`** (não error); URLs de servir são **SINGULARES**
  (  `/api/midia/{imagem|video|gif|audio|entrada}/<nome>` — plural caía no
  404 "pasta inválida" ANTES do pull-back); jinja NUNCA no JS cru
  (`{{ x | tojson }}` quebra o teste de sintaxe — usar data-attribute +
  JSON.parse). E2E `Temp\kilo\e2e_gen.py`: t2i flux1-schnell REAL
  (308 s na estação, PNG 1,4 MB) servido pela produção (pull-back 200).
- **🔧 4 FIXES pós-chat-só-texto (27/08, manhã 8)**: (1) **i2t sem
  NENHUM modelo** — o multimodal LOCAL (qwen2.5-vl) morava no optgrp
  🎨 geração que saiu do composer; grupo **"👁 visão local"** (categoria
  visao do modelos.listar, visao:true) volta ao seletor do chat — i2t
  sempre tem pelo menos o local mesmo sem provedor cloud. (2) **modo
  "não funcionava"**: com i2t o select ficava DISABLED (parecia
  quebrado) — agora SAI DA VISTA com hint "análise direta — o modo não
  se aplica"; em texto segue normal (hibrido validado). (3) **GLM no
  Multimídia**: SUPORTADO — com a chave ZAI cadastrada os 👁 da API
  entram sozinhos no grupo 🌐 (cat=visao) e no i2t do chat; modelos 🔑
  conhecidos sem chave agora mostram atalho **"cadastrar a chave →"**
  (`/sistema?prov=zai`) que PRÉ-PREENCHE o form (a pessoa só cola a
  apikey — diretriz do dono). (4) **Sistema**: chips de provedores
  REMOVIDOS do Motor (repetição com o cartão ☁️; diretriz: "seleção só
  no chat/multimídia, em sistema eu só incluo a apikey" — a
  inteligência de "para que serve cada modelo" é a categorização
  automática que já roda); inputs do form com width:100% (estavam
  finos). ⚠️ diagnóstico visual SEM screenshot (modelo do ambiente não
  lê imagem): DOM por Playwright (bounding boxes, options contadas,
  dataset lido) resolve. E2E `Temp\kilo\e2e_fixes.py`.
- **🌗 DARKMODE + MODO integrado + i2t EM BOLHA (27/08, manhã 9)**:
  (1) **inputs BRANCOS no dark**: o CSS estiliza por `input[type=text]` —
  `pid/purl/pmodelos` estavam SEM `type` (default do browser ≠ seletor
  CSS) → fundo branco e largura quebrada; sempre declarar `type` (PS
  5.1 `Set-Content -Encoding UTF8` POE BOM — remover). (2) **legenda
  solta "análise direta"**: o hint `<span>` fora do fluxo virou o
  PRÓPRIO select modo assumindo "👁 análise de imagem" no i2t
  (dataset.original guarda/restaura as options — zero elemento solto).
  (3) **i2t como RESPOSTA DE CHAT**: era card de TAREFA cru ("✓
  concluído · análise:…" — "por que o chat perdeu o layout?") — novo
  branch cria job no REGISTRY DO CHAT (kind "i2t") com a análise como
  `answer` (mesmo formato do _processar_query) → partial inicial
  `_chat_inicio` + polling → MENSAGEM em bolha com raciocínio.
  ⚠️ 3 BUGS do caminho: `resp_stub` UnboundLocal (a variável nasce no
  fluxo de TEXTO — cookie vem do `_stub_sid` criado antes do corpo);
  closure capturava o PARÂMETRO `midia: str` do form (sombreia o MÓDULO
  — import local `from core import midia as _midia`); modelo local
  (value "qwen2.5-vl-7b", não vazio) caía no caminho direto no
  container — `EM_CONTAINER and ":" not in modelo` → AGENTE. E2E
  `Temp\kilo\e2e_layout.py` (Playwright dark: `emulate_media`).
- **🚨 MOJIBAKE 2 + ZAI SEM 👁 + estado no Sistema (27/08, manhã 10)**:
  (1) **mojibake VOLTOU**: `Get-Content | Set-Content` do PS 5.1 leu o
  UTF-8 SEM BOM como cp1252 e regravou corrompido (63 casos) — REGRa
  FINAL: NUNCA editar template pelo PowerShell; edits de arquivo em
  PYTHON. (2) **ZAI sem multimodais**: o endpoint do CODING PLAN
  (`api.z.ai/api/coding/paas/v4`) lista SÓ conversa no /models (10 glm,
  zero 👁) — `modelos()` ganhou FALLBACK DA CASA: sem nenhum cat=visao
  na listagem, os TÍPICOS do CONHECIDOS entram apensados (glm-4.5v,
  glm-v5-turbo, glm-4.6v; info avisa "não veio na listagem"). PROVADO
  COM A CHAVE DO DONO: `zai:glm-4.5v` analisou o PNG vermelho →
  "vermelho" (GPU local intocada). (3) **"não manteve o
  preenchimento"**: cartão ☁️ agora mostra chips dos CADASTRADOS
  (✅ nome · N modelo(s) · N 👁) — o estado da chave gravada é visível.
  (4) **KPI chat sempre qwen-coder**: correto — é o servidor de TEXTO
  da conversa; visão é OUTRO servidor (:8082) sob demanda — KPIs
  renomeados ("💬 chat (conversa)", "🖼️ visão (multimodal) · escolha
  um 👁 que ele sobe"). E2E `Temp\kilo\e2e_zai.py` + `e2e_zai_real.py`.
- **📝 CHAT SÓ-TEXTO (27/08, manhã 7)**: pedido do dono ("geração de
  imagem e vídeo fica só em Multimídia, o retorno da tela do chat é
  texto"). Composer perdeu os TIPOS de geração (imagem/video/gif/i2v/
  i2g — sobram 📝 texto e **i2t**, que retorna TEXTO), o ⏱ duração e o
  optgroup 🎨 geração; `midiaMudou` simplificado (📎 só no i2t); rodapé
  linka /midia. **Backend recusa geração pelo chat** com partial de
  aviso apontando o módulo (páginas ANTIGAS abertas que ainda mandem
  `midia=imagem` recebem o caminho certo em vez de gerar). E2E
  `Temp\kilo\e2e_chat_texto.py`. ⚠️ PS 5.1 + python -c com aspas:
  JAMAIS inline — escrever script em arquivo (o PS come as aspas).
- **🧹 CS5001 COM CSPROJ + FASTAPI MUDO (26/08, noite 3)**: (1) conversa
  com `app.csproj` + classes SEM entry voltava `return` cedo no
  `_preparar_cs` — CS5001 na cara (bootstrap/placeholder só rodavam SEM
  csproj). Agora o ENTRY-CHECK vem PRIMEIRO (bootstrap aspnet /
  placeholder console valem nos DOIS caminhos) E a complementação de
  `ImplicitUsings` no csproj da conversa também (o bootstrap gerado
  herdava csproj sem usings → CS0103 WebApplication). ⚠️ heurística
  `_e_toplevel` só em `.cs`: o XML do csproj "parece" top-level (sem
  declaração de tipo, com linhas úteis) e fazia tem_entry=True errado.
  (2) **python fastapi**: `python3 app.py` NÃO sobe servidor NENHUM
  quando o código só declara `app = FastAPI()` (sem uvicorn.run/__main__
  o processo morre na hora, porta nunca responde, site.log vazio) —
  runner agora é `python3 -m uvicorn {stem}:app --host --port` quando o
  código não se auto-sobe (uvicorn garantido nas deps; flask segue
  direto). Provado em produção: csproj+classes → "app no ar";
  fastapi → "Uvicorn running… app NO AR". Página 🧭 "esta rota não
  existe" num preview VIVO é o app GERADO com link morto (spec regra
  9) — não é bug da sandbox.
- **🧹 SANDBOX: `#` estilo Python em .cs (26/08, noite)**: a LLM às vezes
  emite `# src/X.cs` (comentário de nome no marcador errado) na 1ª linha
  de blocos csharp → CS1024 "Preprocessor directive expected" matava o
  build INTEIRO. `_sanear_cs()` no `sandbox.testar` converte 1ª linha
  `# texto` de .cs em `// texto` ANTES de gravar (`#region`/`#if` NÃO
  são tocados — exigem espaço após o `#`; .py idem). Spec
  arquivo_codigo.md regra 1 reforça o marcador por linguagem. Provado
  em produção: o caso exato do dono agora compila e sobe
  ("Now listening on: http://127.0.0.1:5000"). **CI/CD ganha
  `concurrency` (group ci-cd-${{ github.ref }}, cancel-in-progress)**:
  incidente do GitHub deixou um run "queued" ZUMBI por 35+ min (o push
  seguinte nem disparou; dispatch em corrida deu startup_failure) —
  runs superseded agora se cancelam sozinhos e o deploy da ponta é o
  que vale. Zumbi "queued" não pode ser cancelado (API diz "completed")
  nem deletado (403) — expira sozinho e é inofensivo (`git pull
  --ff-only` na VPS). Status do sandbox: rota certa é
  `/api/sandbox/status/{job}` (NÃO /testar/status).
- **🌐 Provedores EXTERNOS de LLM (24/08, 16h)**: `core/provedores.py` —
  qualquer endpoint OpenAI-compatible (glm/deepseek/openai/anthropic…
  e o PRÓPRIO llama-server remoto) vira grupo 🌐 no seletor do chat.
  Config no .env (`PROV_<id>_BASE_URL/_API_KEY/_MODELOS/_NOME`) com
  AUTO-DESCOBERTA por `PROV_*_BASE_URL`; modelos são a lista REAL
  (`GET /models` aceita OpenAI {data} E llama-server {models}; fallback
  manual/sugestões; cache 5 min). Multimodal por heurística de nome
  (`e_multimodal` — 👁 no option + `data-tipo="multimodal"`: serve i2t).
  Fluxo: `body.model="prov:modelo"` → `_processar_query` seta
  `rag.set_override` (thread-local; LLMContada usa e a telemetria grava
  `[prov] modelo`; LIMPEZA nos chamadores finally — worker Rabbit reusa
  thread) → a troca de GGUF local é PULADA (5080) e o cache usa o modelo
  externo na chave. i2t externo: `/api/tarefas` intercepta ANTES do
  guard de host/agente (roda NA API) e `legendar_imagem(modelo=)`
  chama o endpoint com image_url. `/api/provedores` (chaves nunca
  saem) · Sistema edita PROV_* com máscara (`_campos_config()`
  compartilha a lista entre tela e save). ⚠️ decorator EMPILHADO já
  fez /api/query responder o catálogo (rota colada no def roubou o @app
  original) — cada rota com SEU decorator. E2E real: provedor `estacao`
  = túnel llm.disroy.org; chat respondeu com telemetria `[estacao]`.
- **Painel AGRUPADO por resposta (24/08)**: cada resposta com código ganha
  cabeçalho `📦 resposta N · M arquivo(s)`; arquivos ficam juntos sob ele
  (`_inserirNoGrupo` rastreia `grupo._fim`) e as EXECUÇÕES do ▶ testar
  viram registros `▶ nome · ✓/✕ · Xs · saída` NO GRUPO da resposta que as
  originou (`msg.dataset.grupoPainel`; ▶ do painel → último grupo —
  `:last-of-type` NÃO serve, itens são divs após o grupo). `_irParaAba`
  filtra por `[data-aba]` (grupos/execs têm aba); `_contaAbas` continua
  contando só `.painel-item`. **✎ renomear** em cada arquivo (extração
  pode errar — o dono corrige e vale para ▶ testar — que lê o nome NO
  clique — e para o .zip). `_nomeArquivo` pula shebang `#!` e varre 6
  linhas; spec `arquivo_codigo.md` exige comentário de nome em TODA
  linguagem (shell/SQL/CSS inclusos).
- **Conversa NATURAL (23/08)**: `rag.naturalizar()` (guardrail de código,
  aplicado no `_gerar` e na resposta do agente) remove o eco do envelope
  ("Contexto recuperado da base:", "(nada foi recuperado…)", "Resposta:")
  e a seção final "Fontes:" (≤8 linhas curtas — heurística conservadora).
  Specs `chat.md`/`hibrido.md`/`ferramentas.md` agora PROÍBEM rótulos de
  sistema e seção Fontes: citação é `[n]` INLINE; as fontes ficam no
  PAINEL (btn-fontes vem dos docs recuperados, não do texto).
- **🔎 pesquisa-web (NATIVA) selecionável (23/08)**: pseudo-MCP
  `MCP_WEB="pesquisa-web"` aparece em PRIMEIRO no box 🔌 do chat (vem do
  JS, não do mcp_servers.json); marcado → `_web_aprofundado` baixa
  páginas INTEIRAS que entram como fragmentos `[n]` (colecao "🌐 web",
  visíveis no painel de fontes) e desativa resposta-direta. Seleção de
  MCPs PERSISTE no localStorage (`ragaroy.mcps`). `_mcps_reais` filtra o
  pseudo-MCP da conexão de servidores (e da trivialidade de saudação).
- **Apagar conversa = JOB em 2º plano (23/08)**: `_conv` (kind
  `conversa_apagar`) faz mídias+cache+sessão em 2º plano; o DELETE
  responde NA HORA com o item em "⏳ apagando…" (`_APAGANDO` por owner) e
  a lista auto-pola (`every 2s`) até o item sumir — clicáveis em
  sequência sem travar. 502 no ✕ → ⚠️ com retry; toast global
  (`window.toast`) avisa engasque de rede nos swaps (throttle 12 s).
- **Estado de `<details>` UNIFICADO (base.html)**: chave estável =
  `data-mi da mensagem | id do card | doc` + summary (raciocinio-vivo →
  `::raciocinio` fixo); INTENÇÃO no `pointerdown` (capture) para vencer a
  corrida com o polling de 600 ms; afterSwap aplica só chaves tocadas
  (server-render `open` segue valendo como default). Raciocínio ao vivo
  nasce ABERTO; recolher/abrir PERMANECE em qualquer re-render.
- **Tailwind no front (23/08)**: `static/vendor/tailwind.js` (Play v3,
  VENDOR local — funciona offline) carregado DEPOIS do app.css:
  utilitários VENCEM no empate (migração incremental), PREFLIGHT off (o
  reset não quebra o legado) e cores mapeadas nos tokens do tema
  (`bg-card`, `text-suave`, `border-borda`, `text-acento`…). Componentes
  NOVOS usam utilitários; legado migra na touch. Sem Node/build.
- **Composer**: textarea nasce em 1 linha e cresce com o texto até 12rem
  (`ajustarAlturaTA` — cálculo em PX; o antigo min(scrollHeight,12)+"rem"
  travava); `.alta` de fábrica REMOVIDA (pedido do dono).
- **Setup/comunidade**: `scripts/setup.sh` (Linux/macOS: venv+compose+
  modelos via flag `--modelos`) · `scripts/baixar_modelos.py` (catálogo
  multi-tipo: chat embed visao imagem video video2 audio — retomável,
  pula existentes) · `setup.ps1 -Modelos`. README: novo slogan, diagrama
  isométrico C4-N2 (`docs/arquitetura.svg` — regenerado pelo gerador em
  temp; blocos 3D flat + cartões de legenda + cilindros laranja),
  pilares open source + lista completa em <details>.
- **🏭 Política ONDE RODA O QUÊ documentada (27/08, tarde)**: pedido do
  dono ("o servidor local sempre será a minha máquina, a VPS nem deve
  ter llama hospedado"). Estado REAL verificado: VPS já CONFORME —
  `docker ps` só tem api/qdrant/rabbit/redis/sandbox/traefik/
  cloudflared/portainer, nenhum processo llama no host, pasta `models/`
  VAZIA (4 KB — só ponto de montagem do picker). Documentado: AGENTS.md
  seção 1 ganhou o bloco "🏭 ONDE RODA O QUÊ" (GPU sempre na estação
  via túneis llm/embed/agente.disroy.org; VPS = só aplicação; "se
  aparecer llama na VPS, está ERRADO") + item Infra reescrito (a linha
  antiga "na VPS SÓ o Qdrant" estava desatualizada) + README/Arquitetura
  nota   "a GPU é a estação do usuário; o servidor não hospeda modelos".

- **🎨 GERAÇÃO POR PROVEDORES + nomes REAIS da Z.AI (27/08, noite)**:
  pedido do dono ("geração pode usar modelos de provedores; glm dá erro
  400"). SONDA com a chave real contra a API: `glm-v5-turbo` **NÃO
  EXISTE** ("modelCode: does not exist" — era nome INVENTADO do meu
  fallback: removido); visão REAL = `glm-4.5v`/`glm-4.6v` (respondem
  com `image_url` base64 — o 1214 da sonda era PNG 1×1 inválido);
  GERADORES `glm-image`/`cogview-4-250304` EXISTEM no `/images/
  generations` mas **429 "insufficient balance"** (fora do CODING PLAN
  — saldo próprio). Implementado: (1) CONHECIDOS com `visao` REAL e
  `gera` por casa (zai glm-image/cogview · openai gpt-image-1 ·
  openrouter gemini-image) + fallback da casa também para imagem;
  (2) `provedores.gerar_imagem(pid, modelo, prompt)` — POST
  `/images/generations` (b64_json OU url), salva em saidas/imagens,
  devolve formato do t2i; intercept no `POST /api/tarefas` (t2i com
  `params.modelo` "prov:x" roda NA API, sem GPU); /midia lista geradores
  ☁️ no select de gerar (t2i = local + externos; vídeo segue local);
  (3) **erros externos com CORPO**: "400 …mozilla" agora vira a
  mensagem REAL da API ("modelCode: does not exist", "Insufficient
  balance…") — legendar_imagem e gerar_imagem capturam r.text.
  ⚠️ status de TAREFA usa `error` (não `erro`) — scripts de E2E lerem
  os dois. E2E `Temp\kilo\e2e_zai3.py` + sondas `sonda_zai*.py`
  (rodar DENTRO do container: lê /app/.env).

## 6. Estado e decisões históricas

- Origem do Estúdio: `docs/pesquisa-midia.md` — crítica do plano original:
  **Wan2.2** em vez de Hunyuan, GGUFs city96, LTX-Video fora (é diffusers).
- Análise viva do código (20/08/2026): `docs/core-analise.md` (módulo a módulo,
  bugs confirmados, duplicações) · `docs/guia-conceitos-rag.md` (fundamentos de
  dados/RAG/frameworks, tabela de modelos de rerank) · `docs/plano-qualidade-rag.md`
  (diagnóstico da aquisição, fases A-F aprováveis: modo Revisão de ingestão,
  F4 core/pesquisa, bugs, UI). Atualizar esses docs ao mudar o que eles descrevem.
- Datasets: clones esparsos de fontes oficiais (coleções .NET e Python/IA)
  em `datasets/` (fora do git) + `datasets/seed/` (versionado; os seeds da
  web gravam os fontes .md lá).
- Git: `webui/dist/` é versionado de propósito (usar sem Node); `.env` NUNCA
  (contém `SERPER_API_KEY`); `sessions/`, `saidas/`, `logs/` fora.

---

---

## Historico de rodadas

O registro completo das rodadas de trabalho (contexto das conversas com o
operador) vive em AGENTS-historico.md — arquivo PRIVADO, fora do git.

- **⚙️ ARQUITETURA SEM BROKER — Redis/RabbitMQ REMOVIDOS (27/08, tarde)**:
  pedido do dono ("acredito que redis/rabbitmq estejam comprometendo —
  vamos usar apis async, quero o controle de erros; para esses tipos de
  chamadas não tem necessidade do rabbitmq"). Análise de arquiteto
  (inventário: 41 arquivos/~380 ocorrências) + decisões do dono:
  cache REMOVIDO (bússola cobre) · contadores em ARQUIVO
  (`logs/uso_llm.jsonl`, append atômico — total unificado API+CLI) ·
  executor **asyncio + retry/backoff** (fila serial; transientes só) ·
  restart = **erro claro** ("dispare novamente") · containers **removidos
  no deploy** (compose down rabbit redis na VPS) · painéis/telemetria de
  fila **removidos** (badges 🐇/⚡, /hx/fila, /api/fila, /api/cache,
  /hx/cachepanel, _fila/_cachepanel, card Redis do dashboard, KPI cache).
  `core/executor.py` novo (telemetria tipo "jobs"); `core/fila.py` e
  `core/cache.py` DELETADOS; `estatisticas.cache_resumo` fora; ETA das
  tarefas em memória; compose dev/prod sem os services, requirements sem
  pika/redis, CI sobe só qdrant, setups/env.example/gitignore limpos;
  README/AGENTS reescritos (menções a Rabbit/Redis em bullets ANTIGOS
  desta lista são HISTÓRICAS — a arquitetura ATIVA está na §1/§3/§5).
  ⚠️ pós-deploy: `docker compose down rabbit redis` manual na VPS (os
  services saíram do compose, os containers velhos precisam do down) e
  o .env da VPS pode descartar RABBIT_*/REDIS_*.

- **👁 MULTIMÍDIA CONVERSACIONAL + INTERRUPÇÃO (27/08, noite 2)**: pacote
  do dono. **/midia virou CHAT ÚNICO** (pedido: "manter histórico de
  sessões" + "ser apenas um chat único onde posso alternar os modelos"):
  `core/midia_sessoes.py` (sessões com itens {tipo, modelo, prompt,
  referencia, linhas, resultado} em saidas/midia_sessoes/, owner isolado,
  job_ativo anotado) + sidebar + composer ÚNICO onde O MODELO DECIDE:
  👁 visao+anexo = ANÁLISE (markdown formatado no histórico —
  `TEMPLATES.env.globals['_md_basico']`; era texto cru com ### e **);
  🎨 Flux+anexo = **MELHORIA i2i** (`gerar_imagem(imagem_inicial=,
  forca=0.65)` → `--init-img --strength`, resize p/ dims alvo); 🎨 sem
  anexo = t2i; provedores = /images/generations; 🎬 Wan = t2v/gif (2-8 s).
  Formatos de melhor custo-benefício mantidos: PNG (imagem), MP4/H.264
  (vídeo), GIF 17f p/ loop. **Raciocínio SEMPRE RECOLHIDO** (vivo e
  histórico — pedido: "quero que fique sempre minimizado"). **Retomada**:
  mudou de página e voltou → polling volta (job_ativo no SERVIDOR +
  localStorage ragaroy.midiaJob.<sid>; a chamada segue no executor).
  Novo envio CANCELA o anterior (`POST /api/midia/cancel/{job}`).
  **CHAT: interrupção** (pedido: "toda vez que eu mandar mensagem
  estiver pensando, pare o raciocínio e mande a nova incluindo o
  contexto da interrompida"): `JobRegistry.cancelar(jid)` (flag
  `cancelado` — `concluir` DESCARTA resultado tardio) +
  `POST /api/query/cancel/{job}`; o beforeRequest do form acha o card
  em curso, cancela no servidor e remove da tela; a pergunta
  interrompida já está na sessão → entra no history do novo POST
  (PROVADO: "A pergunta anterior era sobre escrever um parágrafo longo
  sobre o oceano"). Bloco "também no chat" REMOVIDO do /midia.
  E2E `Temp\kilo\e2e_midia_conv.py` (Playwright). Nota: o Sistema
  mostra o modelo de CONVERSA (texto) — o multimodal/gerador em uso
  aparece por ITEM no multimídia (badge do modelo em cada envio).

- **🔧 MULTIMÍDIA: ordem + guia de criação + ✨ + fullscreen (27/08,
  noite 3)**: (1) **"texto indo depois da resposta"** — item refeito
  com bolha `👤 você` (prompt+thumb) PRIMEIRO e saída depois; no ENVIO
  a bolha do usuário entra na conversa ANTES do card processando e ao
  concluir o card é TROCADO pelo item formatado NO LUGAR
  (`GET /hx/midia/item/{job}?s=` — sem reload; autoscroll). (2) **GUIA
  DE CRIAÇÃO** no composer (só p/ geradores): 22 chips — 🎨 estilo ·
  🌍 cenário · 💡 luz&câmera · 🎥 movimento (vídeo só) que ANEXAM ao
  campo + dica por modelo (Flux/Wan: cena única, sem texto na
  imagem). (3) **✨ gerar prompt** reusa `/hx/prompt-melhorar`
  (spec prompt_melhoria.md) com o tipo do modelo como dica — substitui
  o campo. (4) **fullscreen**: `.midia-grid` (13,5rem + 1fr; mobile
  empilha) + `.midia-coluna` (`height: calc(100dvh - 10,5rem)`,
  conversa flex, composer no fim) — ⚠️ um `max-height:30rem` INLINE
  antigo travava (CSS manda). (5) **nova sessão** via `HX-Redirect`
  (com `hx-swap=none` o `<script>` inline NÃO roda). (6) **home do
  flask sandbox com ROTAS CLICÁVEIS** (`<a href='{r.rule}'>`, métodos
   != GET marcados — "só mostra a rota, não a aplicação"). ⚠️ PS 5.1
   `Add-Content` grava ANSI — CSS regravado utf-8 via python. E2E
   `Temp\kilo\e2e_midia3.py` (fullscreen 60vh, mobile 1 col, guia 22
   chips, ✨, ordem 👤→🤖).

- **🧩 BLOCO DE SESSÕES PADRONIZADO (27/08, noite 4)**: pedido do dono
  ("padronizar multimídia × chat — prefiro o padrão multimídia"). MESMO
  visual nos dois: botão **primário cheio** no topo (➕ nova
  conversa/sessão), itens `conv-item` com `conv-titulo` truncado +
  `conv-contagem` ("N mensagem(ns)" / "N envio(s)" + "⏳ em curso"),
  largura única **13,5rem**. Chat: `.conv-nova` agora É `.primario`,
  **ALÇA de resize removida** (div + JS do arrasto + CSS), slug fora;
  extras funcionais preservados (sticky, drawer mobile, ⏳ apagando com
  auto-polling — `list_sessions` ganhou `job_ativo`). Multimídia:
  itens viram `conv-item` com **✕ apagar** (`DELETE
  /api/midia/sessao/{id}`, owner conferido; confirma e recarrega).
  E2E `Temp\kilo\e2e_padrao.py` (fundo do botão IDÊNTICO
  rgb(15,98,254) nos dois, contagens, sem alça, apagar 6→5).

- **🩺 VL LOCAL + largura + mobile + ⏱ (27/08, noite 5)**: pacote do
  dono. **"Eu TENHO o modelo, por que não subiu?"** — CAUSA RAIZ (3
  camadas): (a) `/api/midia/enviar` análise LOCAL com modelo NOMEADO
  (value `qwen2.5-vl-7b`, não vazio) caía no caminho DIRETO no
  container → `_subir_vl` no Linux com `D:\models` → "ausente" (o
  mesmo bug do i2t do chat: **`EM_CONTAINER and ":" not in modelo` →
  AGENTE**); (b) `_subir_vl` com nome FIXO → `_vl_arquivos()` GLOB
  tolerante (`*[Vv][Ll]*.gguf` + mmproj — ⚠️ `**` no MEIO do padrão é
  INVÁLIDO no pathlib: "can only be an entire path component"; mensagem
  de erro agora LISTA o que achou); (c) marker `vl_off` persistente da
  estação (dono desligou um dia) → `POST /api/vl/ligar` religa e
  pré-aquece. PROVADO: análise local → **"O vermelho." · 27 s** pela
  estação. **ERROR "does not support image input"** = chamada de imagem
  num llama-server SEM mmproj (o fluxo caía no :8090 texto) — resolvido
  pela (a). **Largura PROPORCIONAL base 1024**: chat e multimídia com
  `clamp(64rem, 100vw, 90rem)` (1024–1440; 4k fica no teto; mobile
  100%) — ⚠️ o `max-width:78rem` INLINE antigo do multimídia
  desigualava (removido; medido IGUAL 1287=1287). **Sidebar do chat no
  mobile EMPILHADA** no fluxo como a do multimídia — overlay e botão 💬
  REMOVIDOS (`abrirDrawer` não existia mais = ReferenceError ao
  clicar). **⏱ tempo de retorno**: multimídia grava `segundos` no
  result (todos os tipos) e o item mostra "HH:MM · ⏱ Xs" (chat já
  tinha no rodapé de métricas). ⚠️ AGENTE da estação = código LOCAL:
  cada fix em modelos.py/agente_host.py exige restart do processo.

- **📊 DASHBOARD ENXUGADO + multimodal VISÍVEL (27/08, noite 6)**:
  pedido do dono. REMOVIDOS: "📜 Logs (tail ao vivo)", "Tokens por
  serviço" e "Histórico de solicitações" (+ rotas /hx/logs/* e
  /hx/histlog mortas, ctx execucoes). **Modelos = SÓ o que RODOU**
  (união "sem uso" e selo fora). **Bug antigo do multimodal**:
  (a) o LOCAL (qwen-vl via agente) gravava telemetria NA ESTAÇÃO (não
  atravessa o túnel) e (b) o EXTERNO gravava com `duracao_s: null`
  (tok/s nunca saía). FIX: agente `/visao` devolve `usage` real do
  llama-server (`midia._ultimo_usage_vl`); os runners do multimídia e
  do i2t do chat REGRAVAM o evento llm NA VPS com duração; legendar
  externo com `t0_ext` → duração real. **2 qwencoder unificados**:
  `nome_curto` tira o prefixo `[prov] ` antes da quant (`[estacao]
  qwen…` ≡ `qwen…`). PROVADO: cards = 1× qwen2.5-coder-7b, glm-4.5v/
  4.6v com 👁; análise local → evento na VPS {entrada 36, saida 5,
  duracao 49.6} → card qwen2.5-vl-7b apareceu. ⚠️ NameError `alvo` no
  runner do midia_enviar (a variável era `payload['referencia']`) —
  runner novo: conferir TODOS os nomes locais do escopo.

- **📐 WIDTH FULL + SLUGS NA URI (27/08, noite 7)**: pedido do dono ("o
  chat ficou com width reduzido — era pro MULTIMÍDIA ficar igual ao
  CHAT, como o topbar… incluir slug da sessão na uri do chat, multimídia
  e sandbox; sandbox é máquina sempre ligada"). (1) **clamp/teto
  REMOVIDO** dos dois (chat volta full como o topbar, multimídia
  igual). (2) **Chat `/c/{sid}`**: rota valida owner → assume o cookie
  → renderiza a home (pagina_chat ganhou `_sid` override); abrir
  conversa faz `history.pushState('/c/{sid}')`; nova → replaceState
  '/'. (3) **Multimídia `/midia/{sid}`** (path; `?s=` compatível;
  HX-Redirect da nova aponta o path; itens da sidebar linkam o path).
  (4) **Sandbox com slug**: app vivo nasce
  `/sandbox/app/{sid-da-conversa}/{chave}/` (cookie da sessão no POST
  do testar → `slug_sessao`); ⚠️ a rota ANTIGA `{chave}/{path:path}`
  ENGOLIA a URL com slug por ORDEM DE DECLARAÇÃO → fallback na própria
  rota: chave sem formato + 1º segmento do path com formato = revalida;
  middleware Referer aceita os 2 formatos. Rotas não-GET na home
  injetada SEM link (clicar POST dava 405/login). Sandbox sempre
  ligada: container `restart: unless-stopped`, sem porta pública (só
  rede interna) — por design. E2E `Temp\kilo\e2e_slugs.py`: /c na URL,
  nova → path, app 200 com slug e POST marcado.

- **⎋ ESC GLOBAL + TEMA CLARO NO TOPO (27/08, noite 8)**: pedido do
  dono ("deixar o esc para sair de modals, css globais padronizados,
  incluir o tema claro clicável no topo"). (1) **ESC fecha modais em
  TODAS as páginas** (handler no base.html, um ESC por camada): dialog
  nativo → lightbox → `.modal-fundo` (classe global, fecha via
  `[data-fechar]`) → `details.modal`. (2) **Classes globais de modal**
  no app.css: `.modal-fundo` (fixed+backdrop, hidden=default) /
  `.modal-caixa` / `.modal-cab` / `.modal-corpo` — o doc-modal da
  Biblioteca migrado (dialog nativo com as classes globais); novos
  modais usam esse padrão. (3) **Tema manual**: botão ☀️/🌙 no topo
  (`alternarTema`) grava `data-tema` no `<html>` + localStorage
  `ragaroy.tema` — CSS: `@media dark :root:not([data-tema=claro])` +
   `:root[data-tema=escuro]` (manual VENCE o sistema; editar um bloco =
   editar o outro); script no `<head>` ANTES do CSS (sem flash);
   ícone reflete o tema ATUAL. E2E `Temp\kilo\e2e_tema_esc.py`: ☀️→🌙
   com bg 246→16, persiste após reload, ESC fecha lightbox.

- **🖥️ PACOTE UI FULL-BLEED (28/08, madrugada)**: pedido do dono
  (layout full width/height como o multimídia no chat; scrollbar só na
  lista que excede; CSS global em camadas; dashboard modelos maiores +
  média de tempo; Infra→Qdrant enriquecido sem espaço em branco; modal
  "documento" fantasma; modal de doc GRANDE; título "RagAroy —
  [módulo]"). (1) **`.app-shell`** (camada LAYOUT do app.css):
  `body.pagina-{chat,midia} main` = `calc(100dvh - var(--altura-topo))`
  com overflow hidden; `.chat-shell` stretch; sidebar full-height
  (scroll SÓ na lista); `#palco` rola; composer estático no fim;
  multimídia no MESMO contrato. ⚠️ 3 ARMADILHAS de CSS que travaram o
  stretch: regra ANTIGA `body.pagina-chat main{display:block}` vinha
  depois e vencia; `align-items:flex-start` antigo idem; `height:100%`
  em filho de flex-item esticado vira AUTO no Chrome (o stretch do pai
  é quem estica — medido 204→880). (2) **dialog FANTASMA**:
  `.modal-caixa{display:flex}` vencia o UA → dialog sem open ficava
  visível ("documento perdida já exibindo") —
  `dialog.modal-caixa:not([open]){display:none}`. (3) **Modal de
  documento LARGO** (78rem) com título no cabeçalho e tipografia de
  leitura (`.doc-texto` pre-wrap). (4) **Dashboard**: media_s por
  chamada nos cards; grade `minmax(260px)` — ⚠️ o STYLE EMBUTIDO do
  dashboard vence o app.css E o `rem` fluido encolhia `16rem`→213px
  cortando letras (min em PX real; medido 322px); **Qdrant
  enriquecido** (KPIs: coleções/pontos/maior/média + grade fluida
  `.qdrant-grid`); ⚠️ a section "Infraestrutura" antiga DUPLICAVA no
  topo (removida) e o `.dash-2col` 1.4fr deixava a coluna direita
  VAZIA (fora — tudo full width). (5) **Títulos** `RagAroy — Chat|
  Multimídia|…` (block titulo por módulo). E2E
  `Temp\kilo\e2e_uipack.py` (chat 898px full, sbFull, palco auto,
  título, midia semScroll, qdrant+media, fantasma fora, card 322px).

- **🩹 REVISÃO DO PEDIDO (28/08, noite)**: dono insatisfeito com a
  1ª rodada ("analisem o que foi feito errado… estou usando sua máxima
  capacidade"). (0) **AUDITORIA DO FIELDS**: script cru `config.FIELDS`
  × referências em api/core — **23/23 USADOS** (PROMPT_SYSTEM em
  rag.py:260, LLM_PROVIDERS em provedores.ids, ESTUDIO_* em
  conjuntos/midia/app) — nada morto p/ remover; a tela segue com os
  23. (1) **ERROR "does not support image input"**: o servidor :8082
  alias "vl" erguido SEM mmproj devolvia o ERRO **como se fosse a
  descrição** (entrava no contexto!) — `legendar_imagem` local ganhou
  AUTO-REPARO (detecta o marker → derruba a porta → `_subir_vl` com
  mmproj → tenta 1x; persiste = erro com orientação) + guard na
  `visao()` (503 claro). ⚠️ agente da estação REINICIADO p/ pegar. (2)
  **Largura chat × multimídia IGUAL de verdade** (medido 1417=1417) e
  **CHAT EM CARTÃO** destacado (`.chat-col` com fundo card/borda/raio
  como o `.midia-coluna`; palco rolando dentro; composer no cartão).
  (3) **Dashboard**: nome do modelo em 1 LINHA com ellipsis (quebrava
  feio — `white-space:nowrap` no mc-topo b) + linhas com wrap. (4)
  **Biblioteca docs**: lista LIMPA (`.doc-linha` = 1 linha: título
  ellipsis + chips; hover acento) e o CONTEÚDO (trechos completos
  `_colecao_doc.html` em `.doc-bloco`s) SÓ no MODAL LARGO — validado
  via API (1600 doc-linhas na coleção de teste). E2E
  `Temp\kilo\e2e_pack2.py`.
