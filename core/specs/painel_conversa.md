# Painel da Conversa (spec de produto — UI HTMX)

> Contrato do painel direito do chat. Não é prompt de LLM: documenta o
> comportamento implementado que o time (e o dono) espera ver funcionando.
> Regra do projeto: código > AGENTS > README > docs.

## Princípio

O painel é a **bancada de saídas** da conversa: tudo que a conversa produz
entra nele SOZINHO, sem o usuário caçar na página. Nada substitui nada —
itens ACUMULAM por conversa.

## Abas

| Aba | O que entra | Quando |
|---|---|---|
| 📄 arquivos | cada bloco de código/comando da resposta, como `arquivoN.linguagem`, com botão copiar | resposta CONCLUÍDA (streaming ao vivo não conta — o parcial re-renderiza) |
| 🎬 mídia | imagem/vídeo/GIF gerado (job concluído), com player/animação | job de geração conclui; dedupe por URL |
| 📚 fontes | docs citados (📚 do rodapé) e a resposta completa (▦) | ao clicar no rodapé/botão da mensagem |

- contadores `(N)` por aba; mídia nova CHEGA com a aba mídia visível; o
  clique em fontes/resposta TROCA para a aba 📚 (a filtragem é uma ÚNICA
  função `_irParaAba` — nunca fica inconsistente).
- trocar de conversa LIMPA e RECONSTRÓI as abas a partir das mensagens
  (mídia de sessões passadas não reconstrói — só a da sessão de página).
- **⚠️ serialização**: `data-fontes` usa aspas SIMPLES no HTML — o `tojson`
  do Jinja escapa `'` mas NÃO escapa `"`; com aspas duplas o JSON quebrava
  no primeiro `"` e o painel de fontes falhava em silêncio.

## Fontes: dados do embedding

Cada fonte exibe o que a recuperação realmente mediu:

- **score** de similaridade do embedding (bge-m3) — badge ⚡;
- **coleção** de origem e **categoria** do catálogo;
- **arquivo** de origem (`source`, basename) e **descrição/seção** quando
  existem nos metadados;
- o **conteúdo** do chunk (o texto que alimentou a resposta).

## Mídia: play e ampliação

- vídeo: player nativo (`controls`); GIF: `<img>` animado.
- imagem/GIF: clique = **lightbox** (tela cheia, clique fecha).

## Incluir no contexto (📎)

Toda mídia na aba 🎬 tem **📎 incluir no contexto** — SUBTENDIDO: não há
chip/indicador visível (tudo que está na conversa É contexto); a mídia
anexada simplesmente acompanha a próxima mensagem (feedback só no botão):

- **vídeo/gif** → geração vira **i2v**: a imagem anexa é o quadro inicial.
- **texto (pergunta normal)** → o **multimodal (Qwen2.5-VL)** descreve a
  imagem e a descrição entra na pergunta (`[imagem anexada — conteúdo]: …`);
  multimodal indisponível NÃO derruba a pergunta (segue sem, com log claro).
- a referência é consumida no envio (o hidden field limpa sozinho).

## Nome dos arquivos

O item da aba 📄 usa o NOME REAL quando o bloco dá pistas (comentário com
caminho `# src/app.py`, "arquivo: x", declaração `class/def/function` +
extensão da linguagem); sem pistas cai em `bloco.N.ext` — nunca genérico.

## Expansão

- botão ⤢ alterna largo (`min(46rem, 62vw)`) ↔ padrão (21rem).
- a borda esquerda ARRASTA para redimensionar (280px … 80vw).

## Progresso multimodal

Jobs de mídia mostram **barra de progresso REAL** (% do motor: sd-cli /
whisper, parseado no core) + etapa + ETA (~Ns restantes) quando o motor
reporta; sem %, o log linha a linha continua sendo o progresso.

## Raciocínio transparente

O raciocínio do chat narra TUDO o que o core faz, limpo e claro:

- consultas ao Qdrant (`🗄️ qdrant → coleção: densa + full-text…`) e o que
  voltou (`N denso(s) + M textual(is) → K escolhido(s)` + top resultados
  com score/arquivo);
- modelo: `🧠 modelo X já está no ar — sem recarga` (NUNCA recarregar o
  modelo que já está servindo; troca só quando é OUTRO modelo).

## Combobox inteligente de modelos

- **texto** → modelos de conversa CATEGORIZADOS em optgroups
  (👨‍💻 programação · 💬 conversa);
- **imagem** → só os modelos de geração de imagem (Flux dev/schnell — a
  escolha vai no `params.modelo` da tarefa; match tolerante alias↔arquivo);
- **vídeo/gif** → só o Wan2.2 (único motor de vídeo);
- voltar a "texto" restaura a escolha de conversa anterior.

## Download .zip da aba atual

Botão ⬇ no cabeçalho do painel baixa a ABA ATUAL:

- 📄 arquivos → `POST /api/zip` com `{nome, conteudo}` de cada bloco
  (o caminho do rótulo vira pasta no zip);
- 🎬 mídia → `POST /api/midia/zip` com as refs `pasta\arquivo` (a API
  resolve em `saidas/`; refs com `\` são normalizadas para `/`);
- 📚 fontes → `POST /api/zip` com cada fonte como arquivo de texto.
