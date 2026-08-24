# Premissas — Chat Central (documento ANTERIOR ao desenvolvimento)

> **Status: PREMISSA FIXADA** — este documento registra decisões de produto que
> CONDICIONAM todo o desenvolvimento da webui. Nenhum módulo novo deve nascer como
> página/taba isolada sem passar por aqui.
>
> Data: definido pelo operador do RagAroy. Protótipo: `docs/chat-central.html`

---

## P1 — O CHAT É O HUB ÚNICO (tudo nasce na conversa)

Todo módulo do sistema (geração de código, estúdio de mídia, ingestão, MCP, pesquisa)
é **acionado a partir do chat central**. Não existem "abas de funcionalidade" — existem
**respostas orgânicas categorizadas** dentro de uma mesma conversa.

- Pergunta conversacional → resposta de texto normal.
- Pedido de código → resposta normal **+ split** abrindo com o código.
- Pedido de mídia → resposta normal **+ o mesmo split** abrindo com o vídeo/imagem.
- Ferramenta MCP executada → aparece na trilha de execução (P2) e na conversa como ação.

**Consequência:** uma mesma conversa contém, misturadas de forma orgânica, mensagens
de texto, blocos de código, mídias geradas (multimodal) e comentários — na ordem
temporal real em que aconteceram.

## P2 — TRANSPARÊNCIA TOTAL DE EXECUÇÃO (estilo harness)

Tudo que o sistema executa aparece na **trilha de execução** (painel próprio), no
mesmo espírito dos harnesses de agentes: cada passo mostra

| Campo | Significado |
|---|---|
| **Ação** | o que foi executado (busca vetorial, rerank, geração, t2v, MCP…) |
| **Por quê** | a decisão que levou àquele passo (roteador, crítica, fallback) |
| **Como** | o comando/parâmetros reais (query EN, top-k, temperature, pipeline) |
| **Resultado** | o que voltou (nº de fontes, tokens, arquivo, duração) |

A trilha **percorre TUDO que foi executado** — inclusive subprocessos (pipeline de
prompt de mídia, pausa/restauração de VRAM, jobs de fila). Nada executa invisível.

## P3 — LOGS TÉCNICOS TRADUZIDOS PARA PORTUGUÊS (LLM + embeddings)

O log técnico nativo (EN, monoespaçado) **não é a interface**. Cada passo da trilha
oferece a **tradução/explicação em PT gerada pela LLM**, e os embeddings agrupam
passos semanticamente iguais entre conversas (ex.: todas as "busca vetorial" viram
o mesmo conceito explicável, com link para a spec `core/specs/*.md` correspondente).

- Técnico (EN) = fonte da verdade, sempre acessível (toggle).
- PT explicado = camada de leitura, gerada sob demanda e cacheada.

## P4 — SPLIT ÚNICO MULTIMODAL (código E mídia no mesmo painel)

Existe **um só painel lateral** (split). Ele não é "de código" nem "de mídia" —
ele é **o palco do artefato da conversa**. Abas internas alternam entre artefatos
abertos: `main.py`, `endpoint.ts`, `video_0012.mp4`, `img_flux.png`… O chat central
nunca é abandonado: o split desliza POR CIMA e fecha voltando ao ponto da conversa.

## P5 — MENSAGENS ORGÂNICAS CATEGORIZADAS

Cada mensagem carrega **categoria** (badge + cor), e a conversa pode ser filtrada
por categoria sem quebrar o fluxo:

- 💬 **Conversa** — texto puro (pergunta/resposta)
- 🧩 **Código** — artefato gerado (resumo no chat, arquivo no split)
- 🎬 **Mídia** — multimodal gerado (inline no chat + palco no split)
- 🔧 **Ferramenta** — ação executada (MCP, job, comando)
- 💬→ **Comentário** — anotação do operador ancorada em trecho (P6)

## P6 — REFERÊNCIA POR SELEÇÃO (comentar ancorado no artefato)

O operador pode **selecionar um trecho** (linhas de código; quadro/tempo de mídia)
e escrever um comentário **referenciando aquele trecho**. O comentário:

1. nasce ancorado (`main.py:L10-24`, `video_0012 @0:12`);
2. aparece como mensagem orgânica na conversa (categoria Comentário);
3. ao clicar nele, o split abre **no trecho exato**, destacado.

É o modelo de revisão de PR do GitHub aplicado à conversa com IA.

## P7 — LAYOUT UNIFICADO PRIMEIRO (isto aqui é protótipo)

Este documento e o protótipo `docs/chat-central.html` definem **apenas o layout**.
Backend (eventos de execução, traduções, âncoras) vem depois, guiado por estas
premissas. Protótipo = fonte visual da verdade para o desenvolvimento da webui.

---

## Implicações no código atual (quando o desenvolvimento começar)

| Hoje | Passa a ser |
|---|---|
| `ChatTab` com estúdio/ingestão em abas separadas | Chat central + split multimodal |
| "pensando…" (linhas de log do job de chat) | **Trilha de execução** completa (P2) com PT (P3) |
| `CodePanel` (code-friendly no chat) | Artefato abre no **split**; chat mostra resumo |
| Comentários não existem | **Seleção → comentário ancorado** (P6) |
| Mídia só na aba Estúdio | Mídia **orgânica no chat** + palco no split (P4) |

## P8 — MODALIDADE É SELECIONADA, NÃO DETECTADA (correção do operador)

O usuário **seleciona explicitamente** o que quer (💬 texto · 🧩 código · 🎨 imagem · 🎬 vídeo)
e **quais coleções RAG** entram no contexto. **O core NÃO gasta LLM analisando isso** —
a seleção do operador é a fonte da verdade, transmitida como parâmetro.

> Correção histórica: o protótipo v1 deixava o "roteador decidir" a categoria da resposta.
> Regra fixada: roteador LLM só existe no modo **Auto** (que é uma escolha explícita do
> usuário entre os modos rag/híbrido/livre/auto). Fora dele: seleção = clique.

**Consequências:**
- Os seletores (modalidade, coleções, MCPs, modo) são **componentes visíveis de 1º nível** no composer — nunca escondidos atrás de ⚙ ou menus.
- Cada resposta carrega a **seleção que a gerou** (badge), permitindo auditar o que foi usado.
- A trilha de execução (P2) registra os parâmetros SELECIONADOS, não um palpite do modelo.

## P9 — A INFRAESTRUTA EXISTENTE É REFERÊNCIA OBRIGATÓRIA

Os layouts NÃO reinventam conceitos que o projeto já tem. Toda proposta de layout deve
reapresentar (não omitir) a infraestrutura atual:

| Infra existente | Sempre visível no layout como |
|---|---|
| Coleções Qdrant (`/api/collections`) | Seletor multi com nome + nº de pontos |
| Modos (rag · híbrido · livre · auto) | Segmented control no composer |
| MCPs registrados (`mcp_servers.json`) | Seletor com as ferramentas disponíveis |
| Ingestão wizard 7 etapas (core/ingest) | Fluxo de importação com etapas acendendo |
| Seed profundo (core/seed) | "Nova coleção por assunto" com rodadas/scores |
| Jobs com log ao vivo (RabbitMQ) | Progresso com linhas em tempo real |
| Estúdio 9 modalidades (core/midia) | Seletor de modalidade + parâmetros |
| Cache semântico · tokens · DLQ | Badges de estado no header |

## Fora de escopo (por enquanto)

- Implementação backend dos eventos/âncoras/traduções.
- Migração dos componentes React existentes.
- Persistência de comentários (só o modelo visual).
