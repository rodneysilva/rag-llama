# Especificação global de pesquisa na web

Como o sistema pesquisa na internet para construir e atualizar bases de
conhecimento (seeds e modo Auto). Válida para QUALQUER assunto — nada aqui
é específico de um tema ou coleção.

## Idioma (regra global)

O input do usuário pode vir em qualquer idioma, mas TODA busca é feita em
**inglês** (queries, avaliação, refinamento) — melhores resultados; o
conteúdo baixado fica no idioma original.

## Estratégia de motores

1. **Primeira onda — Serper (Google)**: informação ATUAL e ampla do assunto.
   É a única etapa que exige chave (SERPER_API_KEY); sem chave, começa direto
   no DuckDuckGo.
2. **Ondas de aprofundamento — DuckDuckGo**: buscas internas e específicas
   (recursos dentro dos sites aprovados, ângulos estreitos, termões de
   domínio). Não usa chave — pode rodar à vontade.
3. **Serper como respaldo** do DuckDuckGo quando este não trouxer candidatos
   suficientes.

## Decisão de aprofundar (a LLM avalia, sempre)

Depois de cada onda, a LLM compara o que JÁ FOI aprovado com a DEFINIÇÃO da
base e decide se vale aprofundar:

- há tópicos da definição sem cobertura? → gera queries novas e MAIS
  ESPECÍFICAS para esses ângulos e a pesquisa continua (DuckDuckGo);
- cobertura suficiente, ou as novas rodadas só repetem o que já entrou →
  para. Ir a fundo NÃO é encher a base: é cobrir a definição com fontes de
  qualidade — score alto, não volume.

## Qualidade (score de relevância)

Cada candidato recebe score 0-10 CONTRA A DEFINIÇÃO da base (nunca contra o
gosto do buscador): entra só o que pontua alto; páginas de venda, notícias
fugazes, redes sociais e fóruns rasos pontuam 0. Páginas internas de uma
fonte aprovada são mais exigentes que a própria fonte (a página principal já
entrou — o interno precisa somar).

## No modo Auto (chat)

O fallback web do roteador segue a mesma estratégia: Serper para a pergunta
atual, DuckDuckGo quando o Serper estiver indisponível; a crítica CRAG
decide se o recuperado sustenta a resposta — se não, aprofunda a busca antes
de responder.
