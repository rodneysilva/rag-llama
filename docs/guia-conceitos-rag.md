# Guia de Conceitos — dados, RAG e ferramentas (para decisões no rag-llama)

> Data: 20/08/2026 · Objetivo: vocabulário e fundamentos PARA DECIDIR.
> Não descreve o código (isso é `core-analise.md`) nem o que faremos (`plano-qualidade-rag.md`).

---

## 1. Dados: do arquivo bruto à coleção

### 1.1 Formatos e o que cada um SIGNIFICA para o pipeline

| Formato | O que é | Como entra hoje | Armadilha típica |
|---|---|---|---|
| `.txt` | texto plano, SEM estrutura | TextLoader | sem títulos → chunking fica às cegas |
| `.md` | texto + **estrutura** (títulos `#`, listas, código) | split POR SEÇÃO (header) | markdown "sujo" da web (badges, menus) |
| `.json` | dados estruturados chave→valor | código-fonte no nosso CODE_EXTS | JSON NÃO é prosa: embedding de JSON cru é ruim; o certo é extrair campos → texto |
| `.pdf` | maquetação binária | PyPDFLoader | colunas/tabelas viram sopa; PDF de doc técnica é rico e hoje o seed DESCARTA |
| `.html` | estrutura + ruído (nav/ads/JS) | seed: BeautifulSoup→texto | **perde headings/tabelas** — nossa maior lacuna (Trafilatura resolve) |

**Dataset** (no sentido de ML/HuggingFace) = coleção organizada de exemplos (linhas, pares, documentos) com esquema. No nosso vocabulário: **coleção Qdrant** = índice vetorial de UMA base de documentos; **`datasets/`** = pasta de origem (clones e seeds); **corpus** = o conjunto de tudo.

### 1.2 O caminho de um documento (o "wizard")

```
fonte (pasta/web/HF/pesquisa)
  → EXTRAÇÃO (ler o arquivo/HTML)          ← aqui se perde ou se KEEP estrutura
  → LIMPEZA (frases quebradas, ruído)      ← core/limpeza
  → SPLIT em chunks COM contexto            ← [documento · seção] no cabeçalho
  → EMBEDDING (texto → vetor 1024d, bge-m3) ← o cabeçalho entra no vetor (importante!)
  → QDRANT (ponto = vetor + payload)       ← payload: page_content + metadata
  → CATÁLOGO (descrição da coleção)        ← alimenta roteador/auto/varredura
```

**Chunk** = pedaço recuperável (nosso: ~2000 chars, overlap 400, máx 2 por arquivo na resposta). O chunk é a UNIDADE de recuperação — não o documento. Cabeçalho contextual no chunk é o que faz o vetor "saber" de onde aquele trecho veio.

---

## 2. Busca: densa, lexical e híbrida

### 2.1 Densa (embedding/vetores)
- Pergunta e chunks viram **vetores**; similaridade = cosseno.
- Boa em SIGNIFICADO ("como paralelizar tarefas" acha "executar corrotinas simultaneamente").
- Ruim em TERMO EXATO: "asyncio.gather", "CVE-2021-44228", nomes de função — dilui no espaço semântico.

### 2.2 Lexical (BM25 / full-text)
- Casa PALAVRAS (token a token). O Qdrant tem full-text no payload (`MatchText`) com índice `field_schema="text"`.
- Boa em ID/código exato; ruim em sinonímia e ordem.
- **Detalhe do Qdrant**: `MatchText` casa documentos com TODOS os tokens da query (não é BM25 ranqueado — por isso o fallback "frase → termo mais longo" que usamos).

### 2.3 Híbrida com RRF (o que temos, F2)
Recupera pelas DUAS e funde por **Reciprocal Rank Fusion**:

```
score_rrf(doc) = Σ  1 / (60 + rank_na_lista)
```

Quem aparece bem nas duas listas soma sinais e sobe. Docs achados SÓ por texto entram mesmo sem score denso (match exato de ID é sinal forte). A ordem final é do RRF — não do score bruto.

### 2.4 Rerank (cross-encoder) — o guardrail do retorno
- Diferença fundamental: retriever compara **pergunta↔pergunta** (vetores independentes); cross-encoder lê **(pergunta, trecho) JUNTOS** na mesma rede e devolve relevância real.
- É o filtro de qualidade entre o Qdrant e a LLM: top-15 → rerank → **top-4**. Menos contexto, menos ruído, menos alucinação por "enchimento".
- Custo: latência CPU (medimos ~2-6 s para 15 pares) e RAM ao carregar (ver §2.5). Roda em CPU de propósito — não disputa os 8 GB de VRAM.
- **Onde NÃO atua**: rerank precisa de uma CONSULTA. Não serve para consolidar/categorizar documentos entre si na ingestão (isso é similaridade/agrupamento — ver plano §Fase A).

### 2.5 Modelos de rerank locais (tabela de decisão)

| Modelo | Params | RAM (CPU) | Multilíngue/PT | Licença | Nota |
|---|---|---|---|---|---|
| **BAAI/bge-reranker-base** (atual) | 278M | ~1,1 GB fp32 | ok (XLM-R) | MIT | rápido, bom custo/benefício |
| **BAAI/bge-reranker-v2-m3** | 568M | ~2,3 GB fp32 / ~1,2 fp16 | **forte** (base bge-m3) | MIT | melhor upgrade para PT-BR |
| Qwen3-Reranker-0.6B | ~600M | ~2,4 GB | forte | Apache-2.0 | exige formato "Instruct/Query/Document"; mais pesado |
| jina-reranker-v2-base-multilingual | 278M | ~1,1 GB | forte | **CC-BY-NC** (não-comercial) | flag de licença |
| mixedbread mxbai-rerank-base-v2 | ~156M | ~0,7 GB | multilíngue | permissiva (confirmar) | pequeno; validar em PT antes |

**Para a sua máquina (Ryzen AI 9 HX 370, 32 GB RAM, RTX 4070 8 GB)**: cabe com folga qualquer um até o Qwen3-0.6B — o limite é LATÊNCIA, não memória. Recomendação: `RERANK_MODEL` configurável no .env, default `base`, testar `v2-m3` com benchmark golden (ver plano §Fase C).

### 2.6 A pilha de guardrails do retorno (hoje)
1. `SCORE_MIN` (denso abaixo de 0.35 cai) · 2. máx **2 chunks por arquivo** (diversidade) · 3. teto 4×TOP_K · 4. RRF (ordem fundida) · 5. **rerank 15→4** · 6. spec restritiva ("não possuo dados confiáveis…"). O rerank é o guardrail mais forte — mas nenhum deles conserta AQUISIÇÃO ruim (garbage in, garbage out).

---

## 3. Frameworks: o que usamos, o que ganharíamos

| Framework | Papel | Status aqui | Veredito |
|---|---|---|---|
| **LangChain 1.x** | chains, embeddings, Qdrant store, MCP | em uso | manter — já cobre retrieval/LLM/tools |
| **Haystack 2.x** | pipelines declarativos, retrievers/rankers, evaluators, agents | não usado | **não adotar agora**: ganharia componentes prontos, perderíamos jobs/specs/contadores/sessões (re-plumb total). Adotar as IDEIAS (pipeline explícito, evaluator de relevância) sem trocar de framework |
| **LangGraph** | workflows stateful (grafos/ciclos) | indireto (dep do langchain) | futuro candidato ao orquestrador do F4 (planner→search→evidence→replan); hoje funções + jobs bastam e são mais depuráveis |
| **Trafilatura** | HTML → markdown/texto PRINCIPAL (mata nav/ads, KEEP estrutura) | não usado | **adotar** — é a maior lacuna técnica do seed (Apache-2.0) |
| **Unstructured** | parsing multi-formato (PDF/DOCX/HTML em elementos) | não usado | opcional p/ PDFs ricos do seed; mais pesado que Trafilatura |
| **Qdrant** | vetores + payload + full-text | em uso | não é o problema; futuro: sparse vectors/BM25 nativo se o MatchText ficar curto |

Regra de bolso que a conversa que você trouxe acerta: **"não escolha o framework primeiro"**. Nossa sequência: consertar aquisição → modo revisão → pesquisa com evidência → SÓ ENTÃO agente/workflow.

---

## 4. Proveniência e evidência (o vocabulário do RAG "de verdade")

- **Proveniência**: cada chunk sabe de onde veio (url, autor, data de aquisição, versão/revision, score de curadoria). Hoje: quase nada disso sobrevive (ver matriz em `core-analise.md` §1.3).
- **Claim**: afirmação atômica extraída de um documento COM evidência ("X foi criado em 1981" ← doc 17, confiança 0.9). Síntese a partir de claims é mais segura que síntese a partir de "páginas inteiras".
- **Conflito de fontes**: N fontes dizem A, 1 diz B → registrar divergência em vez de escolher calado.
- **Dry-run/preview**: executar o pipeline INTEIRO de ingestão e PARAR antes do Qdrant, para revisão humana. (Fase A do plano.)
- **Evidence check**: "tenho material suficiente para responder?" — hoje o Auto para por CONTAGEM de fontes, não por qualidade.

## 5. Glossário mínimo PT

- **Embedding**: vetor numérico que representa o SIGNIFICADO de um texto; textos parecidos → vetores próximos (cosseno alto).
- **Similaridade por cosseno**: ângulo entre vetores (1.0 = igual; ~0 = sem relação).
- **Recall**: quanto do relevante você ACHOU. **Precisão**: quanto do achado é relevante. Híbrida melhora recall; rerank melhora precisão.
- **Groundedness**: resposta apoiada nas fontes citadas (o oposto de alucinar).
- **CRAG**: correção de recuperação — se a busca é fraca, refazer/consultar web antes de responder.
- **nDCG@k**: métrica de ranking (posição dos relevantes no top-k) — usar no benchmark do rerank.
