# Modelo de dados (payloads no Qdrant)

## Pedaço de documento (coleções de conteúdo)

Cada ponto tem:

- vetor: embedding BGE-M3 (1024 dimensões, distância COSINE)
- payload:
  - `page_content`: texto do pedaço
  - `metadata.source`: caminho do arquivo de origem
  - `metadata.page`: página (só PDF)
  - `metadata.area`: domínio do conhecimento (lista controlada: tecnologia,
    medicina, psicologia, noticia, politica, administracao, direito,
    educacao, financas, culinaria, esporte, arte_cultura, ciencia,
    indeterminado), gerado pela LLM na ingestão
  - `metadata.categoria`: tema específico do arquivo, gerado pela LLM na
    ingestão
  - `metadata.descricao`: frase curta em português sobre o arquivo, gerada
    pela LLM na ingestão

## Catálogo (coleção `meta_colecoes`)

Ponto por coleção e por spec do sistema:

- vetor: embedding do texto resumido
- payload:
  - `tipo`: "colecao" | "spec"
  - `nome`: nome da coleção ou da spec
  - `area`: domínio da coleção (mesma lista controlada da ingestão)
  - `categoria`: tema (coleções)
  - `descricao`: o que a coleção faz, em português (coleções)
  - `texto`: conteúdo indexado

IDs são determinísticos (UUID v5 da chave), então re-analisar atualiza o
ponto em vez de duplicar.
