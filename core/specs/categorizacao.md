# Especificação de categorização (usada a cada arquivo ingerido)

Tarefa: ler o nome e uma amostra de um arquivo e classificá-lo em dois eixos:
o DOMÍNIO do conhecimento (área) e o TEMA específico (categoria).

Retorne um objeto JSON com exatamente estes campos:

- `area`: o domínio do conhecimento, escolhido OBRIGATORIAMENTE desta lista
  (uma só, em snake_case):
  - `tecnologia` — programação, software, hardware, IA, docs técnicas, how-to
  - `medicina` — saúde, doenças, clínicas, farmacologia, anatomia
  - `psicologia` — psicanálise, comportamento, terapias, cognição
  - `noticia` — fato jornalístico datado, cobertura de evento, reportagem
  - `politica` — eleições, governos, partidos, políticas públicas
  - `administracao` — gestão, empresas, processos, RH, marketing
  - `direito` — leis, normas, jurisprudência, contratos
  - `educacao` — ensino, didática, cursos, aprendizagem
  - `financas` — investimentos, economia, contabilidade, mercado
  - `culinaria` — receitas, gastronomia, alimentos
  - `esporte` — esportes, atletismo, competições
  - `arte_cultura` — música, literatura, cinema, história, religião
  - `ciencia` — biologia, física, química, matemática, pesquisa acadêmica
  - `indeterminado` — só se nada se encaixar (evite; prefira o mais próximo)
- `categoria`: rótulo curto em português, em snake_case, no máximo 3 palavras,
  descrevendo o TEMA específico do arquivo (ex.: "logica_python",
  "dicas_git", "skills_claude", "documentacao_api").
- `descricao`: uma frase em português (até 140 caracteres) dizendo o que o
  arquivo contém/para que serve (ex.: "Exercícios de lógica em Python:
  listas, dicionários e compreensões.").

Regras:
- Baseie a classificação no conteúdo real, não no nome da pasta.
- `area` é o domínio; `categoria` é o recorte específico dentro dele.
  Um artigo sobre "uso de IA no diagnóstico" é `medicina` (foco clínico),
  não `tecnologia`; um tutorial de Python para hospitais é `tecnologia`.
- `noticia` vs. domínio: texto jornalístico sobre um fato datado (com data,
  evento, cobertura) é `noticia`; texto técnico/conceitual do mesmo assunto
  pertence ao domínio (ex.: artigo sobre vacinas = `medicina`, reportagem
  sobre campanha de vacinação = `noticia`).
- Sem aspas, acentos estranhos ou quebras de linha dentro dos valores.
- Se a amostra for insuficiente para entender, use area `indeterminado`
  e categoria `indeterminado`, e descreva o que deu para observar.
