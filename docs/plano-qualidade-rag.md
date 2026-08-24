# Plano de Qualidade do RAG — diagnóstico, decisões e fases

> Data: 20/08/2026 · Leitura prévia: `core-analise.md` (estado do código) e `guia-conceitos-rag.md` (conceitos).
> **STATUS 20/08/2026 (fim do dia): Fase A ✅ · Fase B ✅ (validada EM PRODUÇÃO) · Fase C ✅ (bench: manter `base`) ·
> Fase D ✅ (10/10) · Fase F: direção aprovada (shadcn+lucide), aplicada à tela da Revisão.**
> **Loop de validação: pós-push em https://ai.disroy.org** (nunca localhost).
> Infra de produção corrigida no processo: `.env` da VPS agora aponta
> LLM/EMBED para os túneis zero-trust + QDRANT_URL/RABBIT_URL internos do
> stack (feito via SSH, backup `.env.bak.*` na VPS); health checks do core
> (`embedding_no_ar`/`servido`) honram URLs https completas.

---

## 0. Política de infra (definida pelo dono em 20/08/2026)

- Todo projeto (inclusive MVP) sobe ao GitHub; **push na main = deploy** (GH
  Actions → VPS). Sem `paths-ignore` — push de docs puro usa `[skip ci]`.
- Domínio público (cloudflared+Traefik) vive na infra central (outro servidor).
- Dev local = Docker local. Na VPS, **apenas o Qdrant fica online**; os demais
  serviços ficam offline lá (jobs têm fallback thread sem Rabbit; cache degrada
  sem Redis; rerank/torch não rodam lá por enquanto).
- Consequências para este plano: erros_comuns/coleções vivem no Qdrant LOCAL;
  o VPS não executa a API — anotações sobre hf_cache/torch no VPS caducaram.

---

## 1. Diagnóstico: por que "o RAG não está bom"

A suspeita que você trouxe (resultado de busca ≠ conhecimento) está **confirmada no código**, com três causas-raiz:

1. **O modo Auto responde por snippet** (`auto.py:50-75`): a página nunca é baixada; com DuckDuckGo o "documento" é o próprio título duplicado; a parada é por contagem, não por qualidade; e a resposta usa a spec híbrida (permite o modelo completar). → respostas com cara de fonte web e corpo de conhecimento paramétrico.
2. **O seed destrói a estrutura** (`seed.py:227`): HTML → texto plano; headings/tabelas/código colapsam ANTES do split que foi desenhado para markdown. → chunks sem seção, sem contexto, "sopa".
3. **Proveniência inexistente** (matriz em `core-analise.md` §1.3): nenhum pipeline grava data/score/motor/confiança no ponto. → base não auditável; impossível saber o que é confiável ou atualizar seletivamente.

**E o que NÃO é o problema**: Qdrant, bge-m3 e o retrieval em si (híbrida+RRF+rerank F2 já estão entre os bons). A consulta está razoável; **a aquisição é o elo fraco**.

## 2. Posicionamento contra a proposta que você trouxe (adoções e críticas)

| Ideia da proposta | Decisão | Motivo |
|---|---|---|
| Busca → fetch → extrair → limpar → estruturar → evidência → síntese → Qdrant | **ADOTAR** (Fase B) | é exatamente o anti-snippet |
| Wikipedia como fonte estruturada própria (sections/revision) | **ADOTAR** (Fase B) | API REST pública, markdown por seção — maior salto de qualidade por esforço |
| Query Planner (JSON: subqueries/fontes/freshness/budget) | **ADOTAR** (Fase B) | já temos spec+JSON extractor; LLM pequena basta |
| Claims + detecção de conflito de fontes | **ADOTAR versão 1 simples** (Fase B) | conflito = mesma claim, valores distintos, contagem por fonte |
| Dois RAGs (aquisição × recuperação) | **JÁ SOMOS assim** (seed/hf × query) — faltava QUALIDADE na aquisição | — |
| Reescrever em Haystack | **RECUSAR por ora** | re-plumb de jobs/specs/contadores/sessões; as IDEIAS (pipeline explícito, evaluator) entram sem trocar de framework |
| LangGraph como orquestrador | **ADIAR** | funções + jobs Rabbit são depuráveis e replayáveis; avaliar só quando o F4 ganhar ciclos (replan) |
| Trafilatura na extração | **ADOTAR** (Fase A.1) | mata nossa maior lacuna (HTML→markdown estruturado), Apache-2.0 |
| Reranker como guardrail do retorno | **FEITO** (F2) — manter e evoluir (Fase C) | — |
| **Reranker para consolidar/categorizar na ingestão** | **CRÍTICA: ferramenta errada** | cross-encoder precisa de UMA consulta e mede relevância (query↔doc), não equivalência (doc↔doc). Consolidar/categorizar = cosseno bge-m3 + cluster + rótulo LLM. O rerank CABE na ingestão como **gate de aderência ao tema** (doc vs definição da coleção) |
| "Ver documentos bonitos antes do Qdrant" | **ADOTAR** (Fase A — modo Revisão) | barato: `ingest_docs` já é o núcleo; falta dry-run + UI |
| Knowledge Item (claims/sections/chunks aninhados) | **ADIAR** (F5) | exige reingestão total; primeiro provar aquisição com proveniência plana |

## 3. Fase A — Modo Revisão de Ingestão ✅ IMPLEMENTADO (20/08/2026)

`core/preview.py` + `POST /api/ingest/preview` (fonte pasta|hf) + `dry_run`
no upload + `GET /api/ingest/preview/{pid}` + `POST .../aplicar` + UI
`RevisaoIngest.tsx` (master-detail, shadcn+lucide) no topo
da Biblioteca. O que roda: pipeline idêntico (limpeza/_dividir com cabeçalho
[i/n]) e PARA antes do Qdrant; duplicados exatos (md5) e quase-duplicados
(cosseno ≥0.92); clusters por cosseno ≥0.75 com rótulo LLM (spec
`rotulo_cluster.md`); **gate de tema com o reranker** (nota de cada doc contra
a definição da coleção-alvo, `<0.10` → "revisar"); aplicar só aprovados com
proveniência (`adquirido_em`, `curadoria`). Preview vive 30 min (memória da
API). Validado E2E (`tests_manual/e2e_revisao.py`): dry-run HF → clusters
rotulados → gate correto → aplicar seletivo → proveniência no Qdrant.
A.1 Trafilatura ✅ no `_html_texto` (markdown estruturado, fallback BS4).

## 4. Fase B — F4 `core/pesquisa` ✅ IMPLEMENTADO (20/08/2026)

`core/pesquisa.py` + `POST /api/pesquisa` (job "pesquisa") + card 🔬 na
Biblioteca. Pipeline: **planner** (spec `pesquisa_planner.md` → JSON com
escopo/consultas/frescor; fallback determinístico) → **busca** (Wikipedia
API pt→en + Serper → DuckDuckGo + READMEs de repos GitHub priorizados; dedupe
por URL) → **fetch da página INTEIRA** (`seed._baixar_html` + Trafilatura;
GitHub → README cru) → **claims com evidência** (spec `evidencia.md`, ≤5
docs, ≤8 claims/doc, confiança) → **síntese com citações [Fn] e seção de
conflitos declarados** (spec `sintese.md`) → **MODO REVISÃO**: `result.preview`
abre o painel e NADA entra no Qdrant sem aprovação. Budget no CÓDIGO (≤6
consultas, ≤12 fontes, claims dos 5 principais, 1 síntese) — a LLM não decide
sozinha. O documento de síntese entra na revisão junto com as fontes
(metadata `sintese: true`). Validado E2E pós-push em produção
(`tests_manual/e2e_pesquisa.py https://<sub>.<dominio>`).

### Futuro (Fase B+) — ✅ feita em 20/08 (manhã)
- ✅ Rota `web` do modo Auto usa páginas inteiras (`auto._web_aprofundado`
  baixa 3 páginas × 4 kB via `pesquisa._baixar`; crítica por material
  baixado; fallback snippet). Validado EM PRODUÇÃO: pergunta "versão
  estável do Python" → whatsnew 3.14/python.org baixados → resposta
  fundamentada com data.
- ✅ Conflitos determinísticos (`pesquisa._conflitos`): tema por cobertura
  de tokens sem os valores + anos/versões distintas → entram na síntese e
  no log. Armadilha corrigida: regex com grupo capturador fazia findall
  devolver "19" em vez de "2013".
- ✅ Wikipedia com `revisado_em` (última revisão; aparece na Revisão).
- Futuro: claims reutilizadas entre pesquisa e chat (F3).

## 5. Fase C — evolução do rerank ✅ CONCLUÍDA (20/08/2026)

1. ✅ `RERANK_MODEL` no .env (default `BAAI/bge-reranker-base`; exposto em
   ⚙️ Configurações) + primitiva `rerank.notas_de()` (usada pelo gate de tema).
2. ✅ Benchmark golden (`tests_manual/bench_rerank.py`, 18 perguntas, nDCG@4):
   **base 0,882 · v2-m3 0,867** (Hit@4 0,88 nos dois) — **o v2-m3 NÃO paga
   nos nossos dados** (mesma asserção, −0,016 nDCG, 2× RAM). Decisão:
   manter `base`; trocar só se um golden maior mostrar diferença real.
3. Futuro: ampliar o golden (30+ perguntas por coleção) antes de qualquer
   troca; fp16 se adotar um modelo grande.

## 6. Fase D — correções rápidas ✅ 10/10 APLICADAS (20/08/2026)

| # | Correção | Onde | Status |
|---|---|---|---|
| 1 | `global` em `cancelar_todas` (VRAM presa após ⏹) | `tarefas.py` | ✅ testado unitariamente |
| 2 | Cache Redis: TTL de 60 s para re-testar | `cache.py` | ✅ |
| 3 | Tokens: `balanco_ler()` no return principal | `api/app.py` | ✅ método único |
| 4 | Poison message: try/except no log do body | `fila.py` | ✅ |
| 5 | `detectar` MCP: descarta flags `-` antes do pacote | `mcp_registry.py` | ✅ |
| 6 | Higieniza pula `camada=codigo` | `higieniza.py` | ✅ |
| 7 | Portão do agente: tokens + lista de escrita ampliada (`replace` etc.) | `agent.py` | ✅ |
| 8 | `midia_contexto` com registry (picked contra reentrega) | `api/app.py` | ✅ |
| 9 | `EnrichIn` morto removido (payload legado de /api/models mantido — webui usa) | `api/app.py` | ✅ |
| 10 | `JobRegistry` — 8 famílias → classe única + `TODOS_JOBS` + `_rota_status` (rotas de status idênticas geradas); replay pós-restart agora marca `picked` | `api/app.py` | ✅ E2E query+preview+aplicar |

## 7. Fase E — pontas soltas: remover / reduzir / melhorar

| Ponta solta | Decisão | Status |
|---|---|---|
| CORS + `servicos_llm.py --cors` | commit do dono (`0fef8a6`) — túnel CF com API key | ✅ FECHADA |
| `datasets/` e `tests_manual/` fora do git | política mantida (dados NUNCA no git); erros_comuns segue só no host + Qdrant local | ✅ ACEITA |
| Deploy GH Actions a cada push | **é a POLÍTICA do dono** (push = deploy; docs puro usa `[skip ci]`) — sem `paths-ignore` | ✅ FECHADA |
| VPS: apenas Qdrant online | os demais serviços offline lá por design (fallbacks já existem) | ✅ FECHADA |
| Modo `v2a` pendente | **REMOVER da UI** até existir | aberta |
| `/api/mcp/instalar` síncrono + `/api/higienizar` + `/api/varredura` sobrepostos | **REDUZIR** a 1 entrada por pipeline (manter compat 1 ciclo) | aberta |
| `main.py` (CLI) divergido | **MELHORAR**: usar `rag.search` | aberta |
| `unificar_arquiteturas` vetor≠conteúdo + listas de linguagens 3× | **MELHORAR** (rodeiro único `core/linguagens.py`) | aberta |
| MOCK_LLM | voltou a 0 | ✅ FECHADA |
| F3 (bússola) e F5 (reforma de coleções) | **EM ESPERA** — depois de A+B, F3 fica quase grátis | aberta |

## 8. Fase F — UI/OOUX (direção APROVADA: shadcn + lucide)

Diagnóstico: OOUX bom; **UI funcional-feia** (emoji como ícone, sem tokens,
shadcn parcial). Direção aprovada em 20/08:
1. **shadcn/ui + lucide-react em TODA a UI** (fim dos emojis-funcionais).
2. Tokens no `index.css` (espaçamento/tipografia/raios unificados).
3. Telas de dados em master-detail + tabela.
4. Auditoria 375→1920 como critério de aceite.
**Começou pela tela da Revisão** (`RevisaoIngest.tsx` — Badge/Button/Card/
lucide, zero emoji-funcional); a onda completa das demais telas é trabalho
próprio a planejar.

## 10. F3 — Bússola pré-token ✅ IMPLEMENTADA (20/08, tarde)

`core/bussola.py` + integração no `_processar_query`: coleção de sistema
`sessoes_chat` indexa (pergunta→resposta) por embedding, escopo por owner;
≥0.95 → resposta DIRETA reaproveitada citando a conversa (ZERO token);
0.85–0.95 → campo `bussola` (sugestão; UI do 1-clique pendente). Validação
EM PRODUÇÃO (`tests_manual/e2e_bussola.py`): mesma pergunta com Redis
limpo → hit 1.00, 0 chamadas LLM. Bug pego pelo E2E: early-return do modo
livre não registrava (corrigido). Limpeza pós-teste via /api/docs.

## 11. Débito técnico — estado (20/08, tarde)

**Fechados agora (críticos)**: sessões atômicas sob lock (corrida), scrypt
2^17 + rehash transparente + users.json atômico + secret em cache,
varredura com backup reversível, catálogo paginado (fim do truncamento
silencioso), whisper com timeout, curadoria do seed em lotes, front-matter
Jekyll fora do README, contadores com Redis lazy.

**Restantes (não-críticos, por área)**:
- `mcp_registry`: JSON sem lock; conexões MCP sequenciais (1 asyncio.run
  por servidor).
- `agent`: aprovação pendente não sobrevive a restart; asyncio.run por
  ferramenta.
- `cache.lookup`: O(N) round-trips (migração para índice Redis pendente).
- `telemetria`/`historico`: ~90% duplicados (unificar em core/jobs_log).
- `limpeza._FRASES_LIXO`: substring casa linhas legítimas em coleções
  dev/segurança (tornar por-tipo: código isento já existe via camada).
- `enrich`: classifica o arquivo inteiro pelo 1º chunk (título).
- `unificar_arquiteturas`: vetor≠conteúdo (risco de drift no reembed) e
  metadata.colecao apontando a origem.
- `reembed`: scroll O(n²); não pula mnemosyne_* explicitamente.
- `modelos/modalidades`: dead code (VRAM_BASE_MI, subprocess duplicado);
  modalidades.get() roda listar() inteiro.
- `api/app.py`: /api/models payload legado; /api/mcp/instalar síncrono
  sobreposto ao job; /api/higienizar + /api/varredura sobrepostos a
  /api/limpeza.
- Infra: Wikipedia retorna 0 da VPS (egress/DNS — investigar na infra
  central); scrypt 2^17 usa ~128 MB por login (ok com rate limit).
- UI: faixa 0.85–0.95 da bússola sem botão "usar a anterior/gerar nova".

## 13. F5 — piloto ✅ IMPLEMENTADO (20/08, noite)

- **Snapshot restaurável** (`core/snapshot.py` + rotas admin `/api/snapshot`):
  fotografia id+vetor+payload → `logs/snapshots/*.jsonl`; `restaurar` recria
  a coleção ponto a ponto SEM re-embedar. `reembed` e `unificar_arquiteturas`
  criam snapshot antes de mexer (reversibilidade). ARMADILHA FastAPI: rota
  literal `/restaurar` precisa vir ANTES da `/{colecao}` (path-param engole —
  bug pego em produção).
- **Filtro incremental** (`pesquisa.pesquisar(colecao_alvo=)`): cada página
  aceita é embedada NA HORA e comparada com o lote (≥0.92) e com o ÍNDICE
  da coleção-alvo (≥0.95) — redundante não ocupa vaga de fonte; processo
  interrompível com o aceito aproveitado ("embeda a cada iteração, não no
  final").
- **Knowledge Item (KI)**: `resumo.ki` = fontes (url/revisado_em/claims) +
  conflitos + consultas; gravado no metadata do doc de SÍNTESE → auditoria
  chunk→doc→fonte→data direto no Qdrant (/api/docs mostra).
- **Piloto validado EM PRODUÇÃO** (`tests_manual/e2e_f5_piloto.py`):
  coleção python → snapshot → pesquisa com alvo (fontes relevantes da
  Wikipedia EN — fix: en antes de pt) → aplicar síntese+fonte pela Revisão
  → KI no payload → restauração exata.
- **Generalização** (reformatar coleções existentes como KI) fica pendente
  de aprovação do piloto pelo dono — como manda o plano original.

## 12. Ordem (atualizada)

```
✅ D (bugs+JobRegistry) ─┐
✅ A (Revisão+Trafilatura) ├─► ✅ B (F4 pesquisa) + B+ (Auto páginas) ─► ✅ F5 piloto (KI+snapshot+filtro incremental)
✅ C (RERANK_MODEL+bench) ─┘                    └─► ✅ F3 bússola          F5 geral: aguarda aprovação do piloto
F (UI shadcn) — onda 1 feita; onda 2 (tokens+Header) a planejar
Débito não-crítico — lista do §11, atacar por opport unidade
```

**Critério de sucesso mensurável**: benchmark golden (nDCG@4 — baseline
0,882 do base) + "auditoria de resposta": qualquer resposta do modo RAG
aponta chunk→doc→fonte→data. Fase A fechou o "data"; Fase B fecha o "fonte".
