# Especificação de destrinchar (enriquecimento de coleções)

Processo que pega uma coleção grande/mista e a divide em várias coleções
menores, por tema:

1. Ler todos os pontos da coleção de origem (com payload e vetores).
2. Agrupar os pontos por arquivo de origem (`metadata.source`).
3. Para cada arquivo, a LLM gera `categoria` e `descricao` (regras em
   `categorizacao.md`).
4. O nome da coleção de destino é a categoria em slug válido
   (a-z, 0-9 e _; sem acentos; até 48 caracteres).
5. Os pontos são copiados para a coleção de destino mantendo o vetor
   original (o embedding não muda) e enriquecendo o payload:
   `metadata.categoria` = tema e `metadata.descricao` = descrição do arquivo.
6. Cada coleção criada é registrada no catálogo (`meta_colecoes`).
7. Se pedido, a coleção de origem é apagada no final (e saí do catálogo).

Comportamento esperado:
- Rodar de novo é seguro: mesmos IDs => mesmos pontos (atualiza, não duplica).
- Arquivos sem `metadata.source` viram um grupo por ponto.
- A coleção `meta_colecoes` (catálogo) nunca é destrinchada.
- A LLM fora do ar: os pontos vão para `sem_categoria` (o processo continua).
