# Especificação de análise de coleções (varredura do Qdrant)

Tarefa: analisar o nome e amostras de texto de uma coleção do Qdrant e dizer
o que ela contém e para que serve.

Retorne um objeto JSON com exatamente estes campos:

- `area`: o domínio dominante da coleção, OBRIGATORIAMENTE desta lista:
  `tecnologia`, `medicina`, `psicologia`, `noticia`, `politica`,
  `administracao`, `direito`, `educacao`, `financas`, `culinaria`,
  `esporte`, `arte_cultura`, `ciencia`, `indeterminado`.
- `categoria`: rótulo curto em português, snake_case, máximo 3 palavras,
  do tema dominante da coleção.
- `descricao`: 1 a 2 frases em português (até 240 caracteres) explicando o
  que a coleção guarda e qual sua função no sistema.
- `resumo`: uma linha listando os principais assuntos encontrados nas
  amostras (ex.: "skills de agentes; ferramentas customizadas; memória").

Regras:
- Julgue pelo CONTEÚDO das amostras; use o nome só como pista.
- Coleção vazia (sem amostras): infira pelo nome e termine a descricao com
  "(coleção vazia)".
- Sem aspas ou quebras de linha dentro dos valores.
