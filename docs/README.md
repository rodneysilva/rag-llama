# Documentação do RagAroy — índice e mapa

> ONDE ESTÁ O QUÊ (uma página, sem caça ao tesouro).

| Quero… | Ler |
|---|---|
| **Usar / instalar / subir** | `../README.md` (instalação, serviços, comandos, endpoints) |
| **Modificar o projeto sem quebrar** | `../AGENTS.md` (memória operacional: stack, armadilhas, decisões) |
| **Entender a arquitetura do código** | `core-analise.md` (módulo a módulo, com `arquivo:linha`) |
| **Entender RAG/conceitos** | `guia-conceitos-rag.md` (dados, busca híbrida, rerank, frameworks) |
| **Ver o roteiro e o estado** | `plano-qualidade-rag.md` (fases A–F, débito, status ✅) |
| **Mudar o COMPORTAMENTO da LLM** | `../core/specs/*.md` (índice abaixo) |
| **Protótipos de UI antigos** | `historico/` (layouts e premissas de fases passadas) |

## Regra das três camadas de documentação

1. **README** = como USAR (instalação, comandos, endpoints).
2. **AGENTS.md** = como MODIFICAR (memória de curto prazo do desenvolvedor;
   atualizada a cada mudança — ver "Última atualização" no rodapé).
3. **docs/** = ANÁLISES e PLANOS (estado em um momento; datados no topo).

Conflito entre documentos? A ordem de verdade é: **código > AGENTS.md > README
> docs** — e ao encontrar divergência, corrija o documento errado na mesma
mudança (regra do projeto).

## Índice das specs (`core/specs/*.md` — comportamento da LLM)

Editar a spec = mudar o comportamento (a regra de ouro do projeto: nada de
prompt no código). Exige restart da API (lru_cache) ou `POST /api/specs/reload`.

| Spec | O que governa |
|---|---|
| `chat.md` | modo RAG: responde SÓ com o contexto (+ regras estritas) |
| `hibrido.md` | modo híbrido: base como referência + tom executivo |
| `geracao.md` / `geracao_codigo.md` | modo livre e geração de código |
| `roteador.md` | modo Auto: decidir base/web/livre |
| `reformulacao.md` | reescrever a pergunta com histórico p/ a busca |
| `ingestao.md` / `categorizacao.md` | pipeline e classificação de arquivos |
| `limpeza_texto.md` | regras de higienização de prosa |
| `seed.md` | criar coleção por assunto (web → base) |
| `base_conhecimento.md` | construir base curada em 5 passos |
| `edicao_documentos.md` / `exibicao.md` | editar chunks / exibir respostas |
| `ferramentas.md` | agente ReAct com MCP (portão de aprovação) |
| `midia_prompt.md` / `midia_critica.md` | pipeline de prompts do estúdio |
| `destrinchar.md` / `analise_colecoes.md` / `agrupamento.md` | manutenção de coleções |
| `modelo_dados.md` | schema dos payloads no Qdrant |
| `varredura.md` | julgamento LLM de lixo (conservadora) |
| `pesquisa_planner.md` / `evidencia.md` / `sintese.md` | pesquisa profunda F4: plano, claims, síntese citada |
| `rotulo_cluster.md` | rótulo de grupos no modo Revisão |
| `pesquisa_web.md` | busca web do modo Auto |

## Por que a documentação ficou difícil (histórico honesto)

Cresceu **append-only por três fontes** (README, AGENTS, docs) que se
sobrepõem sem dono único; os endpoints do README **divergiram do código**
(rotas viraram jobs: `/api/analyze|agrupar|enrich` → `/api/manutencao`);
as specs nunca tiveram índice; e o AGENTS acumulou decisões sem podar as
velhas. Este índice + a correção do README (hoje) atacam exatamente isso.
