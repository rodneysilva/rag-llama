# Especificação de exibição e edição de documentos (webui)

A aba Documentos mostra o payload REAL gravado no Qdrant, por coleção:

- `page_content`: texto indexado
- `metadata.source`: arquivo de origem
- `metadata.categoria` / `metadata.descricao`: preenchidos pela LLM na
  ingestão (ou editados aqui)
- `metadata.page`: página, quando PDF

Regras de edição:

1. Editar **metadados** (categoria, descricao, source): o vetor NÃO muda —
   o texto não mudou, a busca permanece idêntica.
2. Editar **page_content** (o texto): o vetor É atualizado automaticamente —
   o sistema RE-EMBEDA o texto novo com o BGE-M3 e grava o vetor novo no
   mesmo id, para a busca continuar correta.
3. Coleções com vetores nomeados (ex.: mnemosyne_*): só metadados podem ser
   editados; mudança de texto é recusada com mensagem clara.
4. Apagar remove os pontos selecionados; não há desfazer.
5. A listagem é paginada por cursor (scroll do Qdrant), 20 por página.
