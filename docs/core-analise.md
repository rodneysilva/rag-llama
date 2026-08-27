# Análise do Core — rag-llama

> Data: 20/08/2026 · Escopo: todos os módulos de `core/` + mapa estrutural de `api/app.py`
> Método: leitura completa módulo a módulo, com referências `arquivo:linha` do código ATUAL.
> Companheiros (sem sobreposição): `guia-conceitos-rag.md` (o que as coisas SIGNIFICAM)
> e `plano-qualidade-rag.md` (o que vamos FAZER a respeito).

Legenda de saúde: 🟢 sólido · 🟡 funciona com ressalvas · 🔴 problema real.

---

## 0. Mapa geral

| Módulo | Papel (1 linha) | Saúde |
|---|---|---|
| `config.py` | .env + reload em runtime, flags (MOCK_LLM/RERANKER/GPU_MODO) | 🟢 |
| `rag.py` | embedding/Qdrant/LLM/chain; `search` híbrida RRF; `reformula` | 🟢 |
| `rerank.py` | cross-encoder bge-reranker-base CPU, lazy, descarregável | 🟢 (novo) |
| `mock.py` / `fluxos.py` | MOCK_LLM; registry de fluxos de geração | 🟢 (novos) |
| `ingest.py` | wizard de ingestão; `ingest_docs` núcleo reaproveitável | 🟢 |
| `seed.py` | aquisição web profunda (rodadas, curadoria, repos) | 🟡 |
| `auto.py` | modo Auto do chat (roteador + web aprofundado) | 🔴 ver §1.2 |
| `hf.py` | cards do HuggingFace como fonte | 🟡 |
| `limpeza.py` | limpeza de texto + `e_lixo` | 🟡 |
| `higieniza.py` | limpeza in-place de coleções | 🔴 em coleções com código |
| `varredura.py` | LLM julga e apaga lixo | 🟡 destrutivo sem dry-run |
| `analyze.py` / `catalog.py` | catálogo de coleções (alimenta roteador) | 🟡 base rasa |
| `enrich.py` | divide coleção em temas (reusa vetores) | 🟡 |
| `unificar_arquiteturas.py` | consolida conceitos de arquitetura | 🟡 vetor≠conteúdo |
| `reembed.py` | reparo de vetores zerados pós-crash | 🟢 (O(n²) tolerável) |
| `agent.py` | ReAct artesanal + portão de aprovação | 🟡 |
| ~~`cache.py`~~ | REMOVIDO 27/08 (bússola no lugar) | — |
| `contadores.py` | tokens por serviço (Redis + fallback) | 🟡 dois métodos vivos |
| `fila.py` | jobs RabbitMQ (DLX/DLQ, replay) | 🟡 poison message |
| `sessions.py` / `sessoes.py` | sessões do chat / do estúdio | 🟡 / 🟢 |
| `auth.py` | scrypt + HMAC + allowlist | 🟡 corridas menores |
| `mcp_registry.py` | servidores MCP + instalação | 🟡 bug no detectar |
| `voz.py` | STT/TTS CPU | 🟢 |
| `main.py` | CLI de consulta | 🟡 divergiu do pipeline |
| `modelos.py` / `motor.py` / `modalidades.py` / `tarefas.py` | estúdio GPU | 🟢 / 🟢 / 🟢 / 🔴 1 bug |
| `telemetria.py` / `historico.py` | logs jsonl | 🟡 90% duplicados |
| `prompts_corpus.py` / `docs_modelos.py` | corpus de prompts; seeds de docs | 🟢 |
| `api/app.py` (2.782 linhas, 83 rotas) | tudo exposto | 🟡 ~300 linhas duplicadas |

---

## 1. Aquisição de conhecimento (o coração do problema de qualidade)

### 1.1 `core/seed.py` — o que ele REALMENTE faz

| Fase | Onde | O que acontece | LLM escreve? |
|---|---|---|---|
| 0. Exploração | `seed.py:74` | 1 query → Serper (8 resultados título+snippet); DDG completa | Não |
| 1. Definição | `seed.py:90` | LLM (spec `seed.md`) escreve escopo/tópicos/**8 queries** | Sim (guia, não vira doc) |
| 2. Rodadas | `seed.py:395` | máx **3 rodadas**: r1 Serper, r2-3 DDG; dedupe por URL | Não |
| 2a. Curadoria | `seed.py:165` | LLM pontua **só os primeiros 40** candidatos/rodada; corte ≥6; 1 por domínio | Sim (score) |
| 3. Download | `seed.py:465` | HTML ≤3 MB (PDF é **descartado**) → `_html_texto` → corte 120k chars | Não |
| 4. Dedupe | `seed.py:454` | md5 dos primeiros 8.000 chars | Não |
| 5. Disco | `seed.py:364` | `datasets/seed/<colecao>/NN_slug.md` com `> fonte: URL` no texto | Não |
| 6. Ingestão | `seed.py:511` | `ingest_folder(rapido=True)` — **zero LLM por arquivo**; categoria = nome da coleção | Não |
| 7. Catálogo | `seed.py:517` | LLM lê **5 amostras × 600 chars** → descrição da coleção | Sim |

**Veredicto**: o seed é **conservador em conteúdo** (a página fala, a LLM só escolhe) — diferente do que se temia. Os problemas são outros:

1. **Proveniência não sobrevive**: score de curadoria, motivo, data, rodada e motor existem só no LOG do job. O ponto no Qdrant não sabe se entrou com score 6 ou 10, nem quando.
2. **Estrutura destruída**: `_html_texto` (`seed.py:227`) = BeautifulSoup remove tags → `get_text`. Headings/tabelas/código colapsam em sopa de texto ANTES do split — que foi desenhado para markdown por seções. O split por header fica inútil (só existe o `# título` artificial).
3. Curadoria cega além do 40º candidato (`seed.py:167` marca `vistos` antes de avaliar).
4. Filtro anti-genérico de queries casa substring da 1ª palavra (`seed.py:217`) — assunto curto casa tudo, assunto PT não casa com query EN.
5. Repo GitHub aprovado que falha no clone NÃO volta a ser baixado como HTML (`seed.py:466`).
6. Dedupe por `texto[:8000]` — topos de site repetidos (hero) podem parecer duplicados.

### 1.2 `core/auto.py` — 🔴 o ponto crítico de qualidade do sistema

`responde_auto` (`auto.py:127`):

1. Roteador LLM decide `base|web|livre` (fallback `base`).
2. `base` → `rag.search` + injeção de `arquitetura_unificada` + **crítica CRAG** (`<2` achados ou top `<0.45` → web).
3. `web` → `_web_aprofundado` (`auto.py:90`): até **5 níveis**; cada nível = 1 busca (DDG 6 resultados; Serper só se vazio) → `Document(page_content="título — snippet")`. **A PÁGINA NUNCA É BAIXADA.** Parada por CONTAGEM (≥6 fontes), não por qualidade.
4. **A resposta é SEMPRE `answer_hybrid`** (`auto.py:178`) — mesmo na rota base. A spec estrita `chat.md` nunca é usada no Auto.

**Consequências diretas** (é isto que você sente como "RAG não está bom"):
- Com DDG o snippet vem **vazio** (`seed.py:140` não extrai) → o "documento" efêmero é literalmente `"título — título"` (`auto.py:59`).
- A LLM então "responde" no modo híbrido = conhecimento paramétrico com decoração de citação web. **Síntese não fundamentada com cara de fonte.**
- Fragmentos web aparecem no `found` com `score 0.0`, sem data, sem snippet arquivado — impossível auditar.

### 1.3 Matriz de proveniência (o que chega ao Qdrant)

| Campo | seed (web) | seed (repo) | HF card | Auto (web) |
|---|---|---|---|---|
| texto limpo | ✅ | ✅ markdown | ✅ | ❌ efêmero |
| `url`/`source` | ✅ (por acaso: linha `> fonte:`) | ❌ | ✅ | efêmero |
| `titulo` | ✅ | ✅ | ✅ | efêmero |
| `secao` | ❌ quase nunca | ✅ | ✅ | — |
| `i`,`n` | ✅ | ✅ | 🔴 corrompidos (§1.4) | — |
| **data de aquisição** | ❌ | ❌ | ❌ | ❌ |
| **score/motivo da curadoria** | ❌ (só log) | — | ❌ | ❌ |
| **confidence** | ❌ | ❌ | ❌ | ❌ |

### 1.4 `core/hf.py`
- Docstring promete "mesma esteira de higienização" — **falso**: docs pulam `_preparar_docs`; `limpar_texto` nunca roda nos cards.
- `metadata.arquivo` nunca é setado → `_dividir` agrupa tudo em `"?"` → **`i/n` globais da coleção** (chunk 37 de N-total).
- Front-matter (licença/tags/task) descartado; `bytes` devolvido é código morto; "papers para F4" não existe.

### 1.5 `core/limpeza.py` / `higieniza.py` / `varredura.py` — o funil de destruição
- `_FRASES_LIXO` (`limpeza.py:34`) casa por **substring**: coleção de web dev/segurança/LGPD perde linhas legítimas que contêm "cookies", "termos de uso"…
- `e_lixo` penaliza listas de endpoints/configs e glossários (baixa pontuação, poucas palavras).
- 🔴 **`higienizar_colecao` não respeita `camada=codigo`** (`higieniza.py:43`): aplica `limpar_texto` (colapsa indentação) e `e_lixo` (heurística de prosa) em CÓDIGO — rodar higienização em coleção com código **destrói ou apaga** o código. O ingest isenta; a higienização não.
- `varredura_colecao` julga por **600 chars** do começo do chunk (`varredura.py:21`) e é **destrutiva sem dry-run/backup**; 1 chamada LLM/10 chunks.
- Dedupe global da higienização mata duplicatas legítimas entre arquivos; `i/n` ficam stale após deletes.

### 1.6 `analyze.py` / `catalog.py` — cadeia LLM→LLM
- 5 amostras × 600 chars (as 5 primeiras do scroll = 1º arquivo ingerido) definem a descrição que alimenta **roteador do Auto, agrupamento e varredura**. Erro de descrição se propaga em cascata.
- `list_meta` faz scroll único `limit=256` (`catalog.py:132`) — acima disso trunca **silenciosamente**.
- `resumo` gerado é descartado; `remove_collection_meta` engole exceção.
- 3 listas hardcoded de linguagens já divergem: `unificar_arquiteturas` (7), `auto._LINGUAGENS` (8, +dotnet), `catalog._DEV_FIXO` (17).

### 1.7 `unificar_arquiteturas.py` / `enrich.py` / `reembed.py`
- **Vetor ≠ conteúdo** na `arquitetura_unificada`: embedding recebe prefixo `[conceito]` (`:83`) mas o `page_content` não (todo chunk já começa com `[`) → um `reembed` futuro gera drift silencioso. `metadata.colecao` aponta para a coleção ORIGEM.
- `enrich` classifica o ARQUIVO INTEIRO pelo **primeiro chunk** (`enrich.py:58`) — que é o título.
- `reembed` re-scrolla a coleção inteira por lote de 64 ids (O(n²)); não pula `mnemosyne_*`.

---

## 2. Runtime do chat

### `agent.py` (ReAct + aprovação)
- 🔴 latente: `_PADROES_LEITURA`/`_PADROES_ESCRITA` casam **substring** (`agent.py:26-45`): `search_replace` contém "search" e "replace" não está na lista de escrita → **passa sem portão**. Default seguro (desconhecido → portão), mas o caso verbo-de-leitura + ação-destrutiva escapa.
- `_MARCAS_ERRO` inclui "error"/"erro" → observação legítima "0 errors" vira "FALHOU".
- Pendência de aprovação **não é persistida** (restart perde o fluxo — jobs de fila são replayáveis; o portão não).
- `asyncio.run()` por ferramenta (quebra se chamado de contexto async).
- `_verifica_e_anoa` = +1 chamada LLM por resposta com ferramentas.

### `cache.py` (cache semântico)
- 🔴 **Bug**: `_r = False` no except (`cache.py:48`) desativa o cache **para sempre** — o comentário diz "até a próxima chamada de teste", mas `_r is None` nunca mais é verdadeiro. Redis fora no boot = cache morto até restart da API.
- `lookup` é O(N) com N round-trips (zrange + hgetall por entrada) — 200 entradas ≈ 201 viagens por consulta.

### `contadores.py`
- Conexão Redis decidida **no import** (`:25-33`): Redis subindo depois nunca é adotado; Redis+arquivo divergem para sempre.
- 🔴 **Inconsistência viva**: o return principal de `_processar_query` usa `contadores.uso_desde(marcador)` (método aposentado, cruza processos — `app.py:2421`) enquanto os retornos antecipados usam `balanco_ler()`. O campo `tokens` muda de método conforme o caminho.

### `fila.py`
- 🔴 **Poison message em loop**: no except do callback (`fila.py:133`) o `json.loads(body)` roda de novo — body inválido lança DENTRO do except → reconecta em 5 s → mensagem reentregue **infinitamente** (nunca vai à DLQ).
- O evento `sistema.parar_tudo` (`fila.py:162`) é publicado num exchange **direct** sem binding → vai para o nada (código morto).
- `reliquidar_dlq` publica sem transação (mitigado pela idempotência `picked`).

### `sessions.py` / `auth.py` / `mcp_registry.py`
- `save_session`: get→merge→write **sem lock e sem tmp+rename** (`sessions.py:66-81`) — corrida perde mensagens; crash corrompe o JSON. `list_sessions` parseia o arquivo inteiro (com `raw`) só para resumir.
- `auth`: `nomes_permitidos()` **reescreve** o arquivo em leitura; corrida de registro duplicado sobrescreve senha; `usuario_do_token` pode gravar .env a cada request (secret não cacheado); scrypt n=2¹⁴ abaixo do OWASP atual (2¹⁷) para exposição pública.
- 🔴 `mcp_registry.detectar` (`:121`): para `npx -y @modelcontextprotocol/server-fetch` o "pacote" detectado é **`-y`** — flags antes do pacote quebram a heurística. JSON store sem lock. `MultiServerMCPClient` usado de forma single (1 `asyncio.run` por servidor, sequencial).

### `main.py` (CLI)
- Divergiu: busca direto no vectorstore (`main.py:44`) — não passa por `rag.search` (híbrida/RRF/SCORE_MIN), nem rerank/reformulação. A CLI mostra um comportamento que a API não tem mais.

---

## 3. Estúdio de mídia

### `tarefas.py`
- 🔴 **BUG de 1 linha**: `cancelar_todas` (`tarefas.py:107`) faz `_vram_ocupada_por = None` **sem `global`** → cria local; o lock de VRAM **não é liberado**. Após ⏹ Parar tudo, `estudio_ocupado()` segue reportando ocupado e novas tarefas de GPU são recusadas (423) até restart. `concluir()` (`:213`) declara `global` corretamente — falta o mesmo ali.

### `modelos.py` / `motor.py` / `modalidades.py`
- Dead code: `VRAM_BASE_MI` (`modelos.py:48`), import duplicado de subprocess (`:166`).
- `EMBED_GGUF`/`VL_GGUF` e `modalidades.PASTAS` hardcodam `D:\models` ignorando `MODELS_DIR` — funciona pelo padrão "GPU sempre no host", mas é a 1ª coisa a quebrar se o layout mudar.
- `rodar_whisper` duplica 80% de `rodar` e faz `wait()` **sem timeout** (job de áudio pode pendurar para sempre).
- `modalidades.get()` executa `listar()` inteiro para achar 1 modalidade.

---

## 4. API — mapa e duplicação

**83 rotas** em áreas: auth (4) · status/settings/infra (24) · ingestão/manutenção (11) · chat (7) · estúdio (13) · docs/coleções (5) · sessões (4) · MCP (8) · webui (2). `_processar_query` (`app.py:2205`) concentra ~215 linhas e 4 responsabilidades.

### Duplicação dominante (o "não quero repetidos" do código)
| Padrão repetido | Onde | Custo |
|---|---|---|
| **8 registradores de job** (dict+lock+seq+`_xxx_log`+`fabricar/rodar`+rota status) | ingest, manutenção, higieniza, limpeza, seed, varredura, query, mcp | ~300 linhas, ~270 de boilerplate; 2 listas manuais em sincronia (`_jobs_ativos`, `parar_tudo`) |
| 3 sluggers divergentes | `seed.py:44`, `enrich.py:17`, `ingest.py:30` | mesmo conceito, 3 comportamentos |
| 4+ extratores de JSON de LLM | `seed.py:50/61`, `rag.py:382`, inline em varredura/catalog | — |
| 5 loops de scroll-completo | higieniza, enrich, varredura, reembed, catalog | shapes distintos |
| 3 listas de linguagens | unificar (7) / auto (8) / catalog (17) | já divergem |
| 2 clientes Redis lazy divergentes | `cache.py:38` (com bug), `contadores.py:25` (sem retry) | — |
| jsonl append/tail ~90% idêntico | `telemetria.py` (rotaciona) vs `historico.py` (cresce p/ sempre) | — |
| `asyncio.run` por chamada | `agent.py:119`, `mcp_registry.py:179/217` | — |

### Rotas/modelos mortos ou sobrepostos
- `EnrichIn` (`app.py:363`): **dead code** — as "rotas antigas /api/analyze|agrupar|enrich" citadas no AGENTS.md **não existem mais**.
- `POST /api/mcp/instalar` (síncrono, `:2755`) sobreposto pelo `instalar-job`.
- `/api/higienizar` e `/api/varredura` são subconjuntos de `/api/limpeza` (3 entradas para o mesmo pipeline).
- `GET /api/models` monta payload legado duplicando `modelos.listar()`.
- `midia_contexto` é o ÚNICO job sem registry no `_despachar` (`:1986`) → reentrega Rabbit executa 2×.
- `parar_tudo` itera 7 registries **sem segurar os locks deles** (`:983-993`) — `KeyError` possível no fallback thread.

---

## 5. Bugs confirmados (fila de correção rápida)

| # | Bug | Onde | Efeito |
|---|---|---|---|
| 1 | `global` faltante em `cancelar_todas` | `tarefas.py:107` | VRAM presa após ⏹ Parar tudo |
| 2 | Cache Redis desliga para sempre | `cache.py:48` | sem cache até restart |
| 3 | Tokens por 2 métodos na mesma resposta | `app.py:2421` vs `:2268/2292/2312` | divergência que o AGENTS diz ter matado |
| 4 | Poison message no worker | `fila.py:133` | loop infinito de reentrega |
| 5 | `detectar` pega `-y` como pacote | `mcp_registry.py:121` | registro MCP quebrado |
| 6 | Higieniza destrói código | `higieniza.py:43` | perda de conteúdo |
| 7 | Portão do agente por substring | `agent.py:26-45` | ferramenta destrutiva sem aprovação |
| 8 | `midia_contexto` sem registry | `app.py:1986` | execução duplicada em reentrega |

---

## 6. Dúvidas para o dono (destiladas)

1. Score/motivo da curadoria e data deveriam virar **metadata do chunk** (`curadoria_score`, `adquirido_em`)? (recomendo: sim)
2. O Auto deve **baixar as 2-3 melhores páginas** por nível (reusando `_baixar_html`) em vez de responder por snippet? A rota base deveria usar a spec estrita?
3. PDFs de documentação oficial foram excluídos do seed de propósito? (hoje `content-type html only`)
4. Higienizar/varredura em coleções com código é uso previsto? Dry-run + jsonl de auditoria para o que é apagado?
5. Os `.md` de `datasets/seed/` são o registro oficial de proveniência, ou o Qdrant deve ser auto-suficiente?
6. `mnemosyne_*` deveria ser ignorada explicitamente no reembed/varredura?

## 7. Sugestões priorizadas

**Quick wins (≤1 dia)**: bugs #1-#5 da tabela; `EnrichIn` fora; unificar sluggers/JSON-extractor num `core/util.py`.
**Médio**: `JobRegistry` (8 jobs → ~60 linhas, mata as 2 listas manuais); proveniência no metadata; preview/dry-run de ingestão (ver plano); Trafilatura no `_html_texto`.
**Longo**: modo Auto com páginas baixadas + crítica de sustentação real; F4 `core/pesquisa` com planner/claims (ver plano).
