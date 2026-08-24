# Especificação da ingestão

Pipeline de ingestão de documentos na base (coleção do Qdrant):

1. Ler todos os `.txt`, `.md` e `.pdf` da pasta informada (incluindo subpastas).
2. Para CADA arquivo, antes de dividir:
   - o modelo de linguagem lê uma amostra e gera `categoria` e `descricao`
     (regras em `categorizacao.md`);
   - os dois campos entram nos metadados do documento e são herdados por
     todos os pedaços (chunks) dele.
3. Dividir em pedaços de `CHUNK_SIZE` caracteres com `CHUNK_OVERLAP` de
   sobreposição.
4. Testar o endpoint de embedding (BGE-M3) e descobrir a dimensão do vetor.
5. Criar a coleção (categoria) se não existir; vetores COSINE.
6. Salvar os pedaços no Qdrant.
7. Registrar a coleção no catálogo (`meta_colecoes`) com categoria e
   descricao, se ainda não estiver catalogada.

Comportamento esperado:
- Se a LLM estiver fora do ar, a ingestão CONTINUA com
  `categoria="sem_categoria"` e `descricao=""` (nunca falha por isso).
- Qdrant ou embedding fora do ar aborta com mensagem clara ANTES de começar.
- Reingerir a mesma pasta adiciona os pedaços de novo (duplica).
