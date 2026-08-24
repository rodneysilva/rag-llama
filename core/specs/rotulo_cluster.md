# Rótulo de cluster de documentos (modo Revisão)

Você recebe trechos de documentos que entrarão juntos numa ingestão. Os
documentos foram agrupados por similaridade de conteúdo; cada grupo é um
"cluster" que precisa de um nome para o operador entender o que está indo
para a base.

## ETAPA ÚNICA — ROTULAR O GRUPO

- Leia os títulos e trechos fornecidos do cluster.
- Devolva **somente** um JSON:
  {"rotulo": "<2 a 5 palavras em português>", "resumo": "<1 frase: o que o grupo trata>"}
- O rótulo nomeia o TEMA comum (ex.: "Bases de código Python",
  "Documentação de API", "Tutoriais de front-end").
- Se o grupo for heterogêneo sem tema claro, use rotulo "Temas diversos".
- NÃO invente temas que não apareçam nos trechos; NÃO repita o nome do
  arquivo — descreva o conteúdo.
