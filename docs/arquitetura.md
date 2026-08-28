# Arquitetura do RagAroy — contrato SOLID / DDD / Clean Architecture

> **Este documento é REQUISITO, não sugestão.** Exigido desde a definição do
> projeto. Qualquer código que o viole é bug de arquitetura — tratar com a
> mesma seriedade de um bug funcional. "Funciona" não substitui "está no
> lugar certo".

## 0. Princípio inegociável

O RagAroy é um **monólito modular com camadas** — não microserviços. As
camadas protegem o DOMÍNIO (RAG, jobs, mídia, sessões) das bordas
(HTTP, Jinja, .env, binários do host). A regra de dependência aponta SEMPRE
para dentro:

```
api/ (borda HTTP)  →  core/ (domínio)  →  (nada além de stdlib+pura)
        ↑ NUNCA o contrário: core/ NÃO importa api/
```

## 1. Camadas (estado real, pós-split 28/08)

| Camada | Onde | Responsabilidade | PROIBIDO |
|---|---|---|---|
| **Composição** | `api/app.py` (~90 linhas) | cria `FastAPI`, CORS, `/static`, middleware, `include_router` | conter lógica de rota/domínio |
| **Interface/HTTP** | `api/routers/*.py` (11 routers) | uma rota = uma função fina; validar entrada, chamar domínio, formatar resposta | lógica de negócio, SQL/Qdrant direto, acesso a .env |
| **Infra compartilhada** | `api/base.py` | helpers de templates, auth de borda, Pydantic models, registries de job INSTANCIADOS | — (é código de BORDA aceito no estágio atual; migra para domínio na Fase 2) |
| **Domínio** | `core/*.py` (~55 módulos) | regras: rag, jobs (core/jobs.py), midia, sessions, modelos, provedores | importar fastapi/api, conhecer Jinja, ler request |
| **Especificações** | `core/specs/*.md` | comportamento da LLM (ver §4) | código com instrução de prompt hardcoded |

Verificação rápida da regra de dependência:

```bash
grep -rn "from api\|import api" core/ --include="*.py"   # deve ser VAZIO
wc -l api/app.py                                          # deve ficar < ~150
```
*Exceção documentada (débito Fase 1): `core/jobs.py` importa `HTTPException`
do FastAPI por paridade — ver §5.*


### Routers e seus domínios

| Router | Rotas | Domínio de negócio |
|---|---|---|
| `auth` | 4 | login/register/logout/me |
| `chat` | 18 | conversa, jobs de query, visão/anexo |
| `paginas` | 11 | `/`, `/c/{sid}`, biblioteca, dashboard, sistema, midia |
| `sandbox` | 8 | testar código, apps vivos |
| `biblioteca` | 34 | ingestão, coleções, pesquisa, revisão |
| `sistema` | 19 | settings, modelos, LLM/embed/VL, GPU |
| `midia` | 22 | multimídia conversacional, tarefas, upload, zip |
| `telemetria` | 6 | contadores, histórico, logs |
| `voz` | 3 | STT/TTS |
| `provedores` | 3 | provedores cloud |
| `agentico` | 11 | sessões MCP |
| `jobs` | 12 | rotas de status `_rota_status` |

**Ordem de include é CONTRATO** (rotas ambíguas resolvem pela primeira
declarada): `jobs` primeiro (paths literais `/api/*/status/{job}` vencem os
paramétricos), depois auth, chat, paginas, sandbox, biblioteca, sistema,
midia, telemetria, voz, provedores, agentico. Não reordenar sem rodar
`Temp/checa_paridade.py` (a prova de 168 combinações method×path).


## 2. SOLID aplicado a este código

- **S — Single Responsibility**: 1 rota = 1 propósito HTTP; 1 módulo core = 1
  domínio. `JobRegistry` cuida de jobs; `rerank` de re-scoring; `bussola`
  de cache semântico. Não adicionar responsabilidade a módulo que já tem nome.
- **O — Open/Closed**: nova família de job = registrar em `TODOS_JOBS`
  (core/jobs.py) — não editar o ⏹ nem o `/api/status`. Novo provedor cloud =
  entry em `CONHECIDOS` — não tocar o chat.
- **L** (substituição): os "gateways padronizados" (chamadas a binários do
  host) mantêm a MESMA assinatura `log(msg, grupo="")` — lambdas de job
  assinam igual (`docs/licoes-de-campo.md` §1).
- **I — Interface Segregation**: a webui conversa com ~30 endpoints REST
  pequenos, não com um mega-endpoint. Manter assim.
- **D — Dependency Inversion**: a API depende das ABSTRAÇßes do core
  (`modelos.servido()`, `executor.despachar`), nunca de caminho de binário
  ou porta hardcoded — endpoints vêm do .env/compose (o container NUNCA
  testa a si mesmo: `EM_CONTAINER and ":" not in modelo → AGENTE`).


## 3. DDD — linguagem e fronteiras

Bounded Contexts existem de fato no código; use os nomes:
- **Conversa** (chat): sessions, bussola, jobs de query, visão/anexo
- **Aquisição** (biblioteca): ingest, seed, pesquisa, revisão, hf, varredura
- **Mídia** (estúdio/multimídia): midia, midia_sessoes, tarefas, conjuntos,
  fluxos
- **Motor** (sistema): modelos, provedores, conjuntos, contadores, telemetria
- **Sandbox**: sandbox, apps vivos

Regras DDD:
- Nomes de código segem o ubíqua: "job", "sessão", "coleção", "provider",
  "tarefa" — não traduções criativas.
- Um módulo core NÃO consulta o outro por dentro (acoplamento horizontal);
  quem orquestra é a ROTA (borda) — exceto orquestrações explícitas
  documentadas (ex.: `_processar_query` chama rag/bussola/rerank).
- Estado de domínio persiste no formato do domínio (Qdrant/jsonl/sessions/),
  nunca em variável de template.


## 4. Specs são a fonte do comportamento (regra de ouro)

"Toda comunicação com a LLM é via RAG": comportamento/formato vivem em
`core/specs/*.md` ou no Qdrant; o código monta o envelope. **Mudou
comportamento → editar spec + `POST /api/specs/reload` (ou restart)** —
`lru_cache` não vê edição no disco sozinha.

## 5. Débitos conhecidos (Fase 2 — não bloqueiam, mas são cobrados)

1. `core/jobs.py` importa `HTTPException` (fastapi) — trocar por exceção de
   domínio (`JobNaoEncontrado`) convertida em 404 na borda.
2. `api/base.py` concentra 3.041 linhas de infra mista (helpers Jinja +
   Pydantic models + registries de job + middlewares). Fase 2: partir em
   `api/infra/` (templates/auth de borda) + mover models Pydantic para
   `core/*/schemas` e registries para domínio puro.
3. Rotas ainda chamam funções de OUTRO router como função (`status()`,
   `collections()`, `query()` — costuras `# noqa: F401 cross-router`): no
   monólito era namespace global; o destino é extrair a função para o DOMÍNIO
   e ambas as rotas chamarem o domínio.
4. `core/auto.py` exporta `_web_aprofundado` (underscore) — sinal de que a
   função é de domínio público dentro do módulo.
5. `tests_manual/` (não versionado) guarda E2E críticos — migrar os vivos
   para `tests/` versionado (roadmap).
6. Ordem de rotas sensível (sandbox fallback, `/hx/conva/copy` duplicada):
   permanente por design do original — `checa_paridade.py` é o guardião.

## 6. Como evoluir sem quebrar (checklist do agente de codificação)

1. **Nova rota** → router do domínio certo; função fina; lógica no core.
   Nova família de job → `JobRegistry` + `TODOS_JOBS`.
2. **Mudança de comportamento de LLM** → spec, nunca código.
3. **Antes de publicar refactor** → rodar paridade (`Temp/checa_paridade.py`)
   + `pytest tests/` + smoke uvicorn em porta de teste.
4. **Nunca** commitar `core` importando `api` (a regra §0).
5. Split adicional? Só com prova de paridade idêntica a esta.
6. Depois de cada rodada que muda arquitetura: atualizar ESTE arquivo +
   `AGENTS.md` (§3 mapa de módulos) no MESMO commit.

## 7. Prova da Fase 1 (28/08/2026)

Split mecânico por AST (`Temp/extract_split.py`) do monólito `api/app.py`
(7.062 linhas) em composição + base + 12 routers + `core/jobs.py`, com:
- **168/168 combinações (method,path)** resolvendo para o MESMO endpoint
  antes e depois (`Temp/checa_paridade.py`);
- pytest: 55 passed / 6 skipped / 1 error PRÉ-EXISTENTE (igual ao baseline);
- smoke uvicorn: login, `/`, `/biblioteca`, `/dashboard`, `/sistema`,
  `/midia` 200; openapi autenticado 140 paths, nenhuma faltando;
- comportamento de auth (303→/entrar sem token) preservado;
- 503 do `/api/collections` = Qdrant fora do ar no momento do teste
  (ambiente, não código).
