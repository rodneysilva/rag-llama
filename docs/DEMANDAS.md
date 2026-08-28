# Demandas do projeto — fonte única (unificada em 28/08/2026)

> **O que isto é:** a lista de REQUISITOS ATIVOS do RagAroy, unificada das
> rodadas com o operador (Kilo Code e outros agentes). Toda demanda nova,
> mudança de prioridade ou decisão do dono entra AQUI primeiro — depois o
> código. Nenhuma outra MD pode contradizer este arquivo.
>
> **Hierarquia das MDs** (a mesma do docs/README.md):
> 1. `DEMANDAS.md` (este) — O QUÊ (requisitos e decisões do dono)
> 2. `docs/arquitetura.md` — COMO (contrato SOLID/DDD/Clean)
> 3. `AGENTS.md` — operação diária (comandos, armadilhas, história)
> 4. `README.md` — uso público / comunidade

## 1. Requisitos fixos (não negociáveis)

| # | Requisito | Status | Fonte |
|---|---|---|---|
| R1 | **SOLID + DDD + Clean Architecture** — contrato completo em `docs/arquitetura.md`; violação = bug | ✅ contratado 28/08 (Fase 1 do split entregue) | definição do projeto |
| R2 | GPU SEMPRE na estação do dono; VPS sem llama/GGUF | ✅ vigente | política 27/08 |
| R3 | Nunca derrubar processos Python vivos (docker compose down, kill) | ✅ vigente | premissa |
| R4 | MDs unificadas e atualizadas ao fim de cada rodada que as invalida | ✅ vigente | decisão 28/08 |
| R5 | Refactor = paridade provada (rotas/testes); NUNCA mudar comportamento no caminho | ✅ vigente | decisão 28/08 |
| R6 | Deploy só via CI (push main → GH Actions → VPS) | ✅ vigente | política |
| R7 | Iteração em branch (develop/refactor) — main publica | ⚠️ retomado 28/08 (split em `refactor/split-app`) | gitflow |
| R8 | Comportamento de LLM vive em `core/specs/*.md` — nunca hardcoded | ✅ vigente | regra de ouro |

## 2. Demandas em andamento (28/08)

| # | Demanda | Estado |
|---|---|---|
| D1 | Split do monólito `api/app.py` com SOLID/DDD/Clean | **Fase 1 CONCLUÍDA** em `refactor/split-app` (168/168 paridade, pytest igual ao baseline). Aguardando revisão do dono → merge p/ develop → PR p/ main |
| D2 | Fase 2 do split (débitos §5 do contrato): exceção de domínio no jobs.py, particionar api/base.py, mover lógica cross-router para o domínio | Planejada |
| D3 | Migrar E2E vivos de `tests_manual/` para `tests/` versionado | Planejada |
| D4 | Atualizar `AGENTS.md` com o bloco de arquitetura | **PENDENTE DE APROVAÇÃO** — o arquivo é protegido; ver §4 |

## 3. Decisões históricas relevantes (contexto)

- 27/08: Redis/RabbitMQ removidos — executor asyncio in-process com controle de erro Python.
- 27/08: multimídia conversacional unificada ao ciclo do chat (SOLID).
- 28/08: respostas enxutas (~500 palavras), auto-scroll, stream ao vivo recuperado.
- 28/08: causa-raiz do "arquitetura se perdeu": **a exigência R1 nunca tinha sido codificada em MD** — verbal na definição, não chegou ao AGENTS.md que o agente lê. Corrigido com o contrato + esta unificação.

## 4. Notas operacionais

- Escrita em `AGENTS.md` pelo agente requer aprovação explícita (arquivo protegido no harness). Para não travar rodadas: registrar a mudança aqui (§2) e no `docs/arquitetura.md`, e solicitar a edição do AGENTS.md ao dono em lote.
- `Temp/` não é versionado — os scripts de prova do split (`extract_split.py`, `checa_paridade.py`, `costura_cross.py`) devem ser promovidos a `scripts/` antes do merge (D1 inclui).
