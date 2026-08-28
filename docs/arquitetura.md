# Arquitetura do RagAroy — Contrato Técnico

> **Natureza deste documento.** Requisito normativo do projeto, não sugestão.
> A adoção de SOLID, DDD e Clean Architecture foi estabelecida na definição
> do produto; qualquer código que viole este contrato constitui defeito de
> arquitetura e deve ser tratado com a mesma severidade de um defeito
> funcional. A aderência funcional de uma alteração não a legitima se a
> estrutura estiver em desacordo.

## 1. Visão geral

O RagAroy é um **monólito modular em camadas** com fronteira HTTP única.
As camadas protegem o domínio — recuperação augmentada (RAG), execução de
jobs, mídia, sessões — das bordas tecnológicas (HTTP, Jinja2, variáveis de
ambiente, binários do hospedeiro). O vetor da dependência é unidirecional
e aponta sempre para o interior:

```
api/ (borda HTTP)  →  core/ (domínio)  →  (stdlib + bibliotecas puras)
```

A inversão desta direção — `core` importando `api` — é a violação mais
grave possível do contrato. Verificação automatizada do invariantes:

```bash
grep -rn "from api\|import api" core/ --include="*.py"   # deve devolver vazio
wc -l api/app.py                                         # deve permanecer < 150
```

*Exceção documentada (dívida da Fase 1): `core/jobs.py` importa
`HTTPException` por exigência de paridade comportamental; a Fase 2 a
substitui por exceção de domínio convertida em HTTP 404 na borda.*

## 2. Composição das camadas

| Camada | Localidade | Responsabilidade | Restrições |
|---|---|---|---|
| Composição | `api/app.py` (~90 linhas) | Instancia `FastAPI`, CORS, `/static`, registro de middleware e handlers de ciclo de vida, inclusão dos routers | Não conter lógica de rota ou domínio |
| Interface HTTP | `api/routers/*.py` (12 routers) | Uma rota por função enxuta: validar entrada, delegar ao domínio, formatar resposta | Proibido lógica de negócio, acesso direto a Qdrant ou leitura de `.env` |
| Infraestrutura compartilhada | `api/base.py` | Helpers de template, modelos Pydantic, instâncias de registro de jobs, `_despachar` | Código de borda aceito no estágio atual; migração para o domínio prevista na Fase 2 |
| Domínio | `core/*.py` (~55 módulos) | Regras de negócio: `rag`, `jobs`, `midia`, `sessions`, `modelos`, `provedores`, `ingest`, `limpeza` | Proibido importar FastAPI ou `api`, conhecer Jinja2 ou manipular `Request` |
| Especificações | `core/specs/*.md` | Comportamento dos modelos de linguagem | Código não embute instruções de prompt |

### 2.1 Routers e limites de domínio

| Router | Rotas | Contexto delimitado |
|---|---:|---|
| `auth` | 4 | Autenticação e sessão de identidade |
| `chat` | 18 | Conversa, jobs de consulta, visão e anexos |
| `paginas` | 11 | Renderização de páginas (`/`, `/c/{sid}`, biblioteca, dashboard, sistema, midia) |
| `sandbox` | 8 | Execução de código e aplicativos temporários |
| `biblioteca` | 34 | Aquisição, coleções, pesquisa, revisão e curadoria |
| `sistema` | 19 | Configurações, modelos, LLM/embedding/visão, GPU |
| `midia` | 22 | Multimídia conversacional, tarefas, upload, empacotamento |
| `telemetria` | 6 | Contadores, histórico, logs |
| `voz` | 3 | STT/TTS |
| `provedores` | 3 | provedores em nuvem |
| `agentico` | 11 | Sessões MCP |
| `jobs` | 12 | Rotas de status das famílias de job (`_rota_status`) |

**A ordem de inclusão dos routers é parte do contrato.** Rotas ambíguas
resolvem pela primeira declarada: `jobs` precede os demais para que
`/api/*/status/{job}` prevaleça sobre os padrões paramétricos (caso
concreto: `/api/midia/status/{job}` contra `/api/midia/{pasta}/{nome}`).
Reordenação sem execução prévia de `scripts/split/checa_paridade.py`
constitui violação procedural.

## 3. Ambientes e implantação

Dois ambientes completos coexistem na VPS sob isolamento total de estado:

| Atributo | Produção | Desenvolvimento |
|---|---|---|
| Domínio | `ai.disroy.org` (alias `raga`/`ia`) | `dev.disroy.org` |
| Branch | `main` | `develop` |
| Diretório | `~/apps/rag-llama` | `~/apps/rag-llama-dev` |
| Containers | `ragaroy-*` | `ragaroy-dev-*` |
| Gatilho de deploy | push em `main` (job `cd`) | push em `develop` (job `cd-dev`) |
| Reranker | Ativo | Inativo (`RERANKER=0`) |

A GPU — inferência de chat, embedding, visão e difusão — reside
exclusivamente na estação do usuário, exposta pelos túneis
`llm`, `embed` e `agente` (`.disroy.org`). O servidor jamais hospeda
modelos; a ausência de GPU local é coberta pelos provedores em nuvem,
mantendo a base vetorial no Qdrant do servidor.

## 4. Pipeline de dados e qualidade

O pipeline de ingestão aplica, em ordem: extração → limpeza textual
(`core/limpeza.limpar_texto`) → particionamento semântico com cabeçalho
contextualizado → **gate de qualidade** → embedding (bge-m3, 1024 d) →
persistência no Qdrant com metadados de proveniência (`arquivo`,
`titulo`, `secao`, `url`, `i`, `n`) → catalogação.

O **gate de qualidade** (`core/limpeza.score_chunk`) atribui nota 0–1 a
cada chunk segundo fatores consolidados pela prática da comunidade:
densidade de links, razão de tokens únicos, comprimento mínimo (15
palavras), razão alfanumérica, presença de JSON embutido, tabelas
markdown e listas de nomes sem estrutura sentencial. Chunks abaixo de
`SCORE_CHUNK_MIN` (`.env`, padrão 0,55) são rejeitados com registro do
motivo. A camada de código (`camada=codigo`) é isenta do gate —
heurísticas de prosa não se aplicam a código.

A recuperação (`core/rag.search`) combina busca densa (k×3 candidatos)
e busca lexical full-text, fundidas por Reciprocal Rank Fusion, com
diversificação (máx. 2 chunks por documento), deduplicação global por
hash de conteúdo e corte por `SCORE_MIN`. Um reranker cross-encoder
(`bge-reranker-base`, CPU) reordena o top-8 quando habilitado.

## 5. SOLID aplicado

- **Responsabilidade única.** Uma rota, um propósito HTTP; um módulo
  `core`, um domínio. `JobRegistry` gerencia jobs; `rerank`, re-scoring;
  `bussola`, cache semântico. Não adicionar responsabilidade a módulo
  cujo nome já declara a sua.
- **Aberto/fechado.** Nova família de job: registrar em `TODOS_JOBS`
  (`core/jobs.py`) — o ⏹ e `/api/status` absorvem sem alteração. Novo
  provedor em nuvem: entrada em `CONHECIDOS` — o seletor do chat absorve.
- **Substituição de Liskov.** Os gateways de binários do hospedeiro
  mantêm assinatura uniforme `log(msg, grupo="")`; lambdas de job
  assinam de forma idêntica (`docs/licoes-de-campo.md`).
- **Segregação de interface.** A webui consome ~30 endpoints REST
  pequenos, nunca um mega-endpoint.
- **Inversão de dependência.** A API depende de abstrações do core
  (`modelos.servido()`, `executor.despachar`); caminhos de binários e
  portas vêm de configuração, nunca hardcoded — em container, a regra
  `EM_CONTAINER and ":" not in modelo → AGENTE` impede auto-referência.

## 6. Linguagem ubíqua e contextos delimitados

- **Conversa**: `sessions`, `bussola`, jobs de consulta, visão/anexos.
- **Aquisição**: `ingest`, `seed`, `pesquisa`, revisão, `hf`, `varredura`.
- **Mídia**: `midia`, `midia_sessoes`, `tarefas`, `conjuntos`, `fluxos`.
- **Motor**: `modelos`, `provedores`, `conjuntos`, `contadores`, `telemetria`.
- **Sandbox**: `sandbox`, aplicativos temporários.

Módulos de domínio não consultam uns aos outros horizontalmente; a
orquestração pertence à rota, exceto nos fluxos documentados
(`_processar_query` coordena `rag`, `bussola` e `rerank`). Estado de
domínio persiste no formato do domínio — Qdrant, JSONL, `sessions/` —
nunca em variáveis de template.

## 7. Especificações como fonte de comportamento

Toda comunicação com os modelos de linguagem é mediada por RAG: o
comportamento vive em `core/specs/*.md` ou no conteúdo do Qdrant; o
código apenas monta o envelope (dados + `ETAPA: x`). Alteração de
comportamento exige editar a spec e recarregar (`POST /api/specs/reload`
ou reinício — o cache `lru_cache` não observa o disco).

## 8. Dívidas registradas (Fase 2)

1. `core/jobs.py` importa `HTTPException` — substituir por exceção de
   domínio (`JobNaoEncontrado`) convertida em 404 na borda.
2. `api/base.py` concentra 3.000+ linhas de infraestrutura heterogênea —
   particionar em `api/infra/` e migrar modelos Pydantic para
   `core/*/schemas`.
3. Rotas invocam funções de outros routers como funções (`status()`,
   `collections()`, `query()` — costuras `# noqa: F401 cross-router`);
   o destino é extrair a operação para o domínio.
4. `core/auto.py` exporta `_web_aprofundado` — promover a nome público.
5. E2E críticos vivem em `tests_manual/` (não versionado) — migrar para
   `tests/`.
6. Ordens de rota sensíveis (fallback do sandbox, `/hx/conversa/copy`
   duplicada) são permanentes por herança do monólito original —
   `scripts/split/checa_paridade.py` é o guardião.

## 9. Procedimento obrigatório de alteração

1. Nova rota → router do domínio competente; função enxuta; lógica no
   core. Nova família de job → `JobRegistry` + `TODOS_JOBS`.
2. Alteração de comportamento de LLM → spec, nunca código.
3. Refatoração estrutural → prova de paridade prévia
   (`scripts/split/checa_paridade.py`) + `pytest` + smoke uvicorn em
   porta de teste.
4. Vedações: `core` importando `api`; commit de segredos; reordenação
   de routers sem prova.
5. Rodadas que alterem arquitetura atualizam este documento e o
   `AGENTS.md` no mesmo commit.

## 10. Registros de validação

- **Fase 1 (28/08/2026)** — partição do monólito `api/app.py` (7.062
  linhas) em composição + `api/base.py` + 12 routers + `core/jobs.py`,
  com 168/168 combinações método×caminho resolvendo para o mesmo
  endpoint antes e depois; `pytest` 55 aprovados / 1 erro pré-existente
  (idêntico ao baseline); smoke uvicorn com login e páginas 200.
- **Ambiente dev (28/08/2026)** — stack `ragaroy-dev-*` em
  `dev.disroy.org`, deploy automático por push em `develop`, isolamento
  de volumes, reranker inativo.
- **Gate de qualidade (28/08/2026)** — `score_chunk` calibrado: prosa
  0,95+; código 0,95; tabela wiki 0,15; JSON de e-commerce 0,05;
  limiar `SCORE_CHUNK_MIN` configurável.
