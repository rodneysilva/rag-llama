# Especificação do seed de coleções por assunto (web → base)

O seed cria uma coleção nova sobre **qualquer assunto informado no fim do
prompt** (o input pode vir em QUALQUER idioma — todo o trabalho interno é em
INGLÊS, que dá os melhores resultados de pesquisa): escreve a DEFINIÇÃO da
RAG antes de importar qualquer coisa (apoiado numa busca contextual
exploratória), pesquisa em RODADAS (Serper e, como respaldo, DuckDuckGo),
faz curadoria com SCORES, baixa as páginas aprovadas E os recursos internos
relevantes, clona repos oficiais quando aparecem, limpa o texto e ingere no
mesmo modelo de dados da ingestão comum — depois cataloga para confirmar que
a coleção faz sentido.

TODAS as etapas abaixo se referem SEMPRE ao assunto informado no fim do
prompt. Nomes citados nesta especificação são genéricos — nunca use-os como
assunto.

## IDIOMA (regra global)

As QUERIES de busca, a definição da RAG e a curadoria são SEMPRE em INGLÊS —
traduza o assunto se ele vier em outro idioma. As páginas baixadas ficam no
idioma original (inglês na maioria — melhor para o embedding).

## 1. Definição da RAG (ANTES de importar — informada pelo contexto)

Você pode receber RESULTADOS DE UMA BUSCA EXPLORATÓRIA (títulos + resumos)
sobre o assunto: use-os para escrever uma definição INFORMADA — os temas que
realmente existem na literatura/documentação, não um palpite.

Responda SOMENTE um objeto JSON:

```json
{"escopo": "o que a base deve conter, 1-2 frases (em inglês)",
 "topicos": ["temas concretos que a base deve cobrir (em inglês)"],
 "tipos_fonte": ["documentation", "tutorials", ...],
 "queries": ["8 search queries IN ENGLISH"]}
```

As queries DEVEM ser EXCLUSIVAMENTE sobre o assunto informado (nunca sobre
exemplos genéricos desta especificação), priorizando documentação oficial,
guias consolidados, sociedades/instituições reconhecidas, manuais, livros
abertos, wikis estáveis e repos oficiais de documentação.

## 2. Curadoria com SCORES — análise ANTES de ingerir

Você receberá o assunto, a definição da RAG, a lista de resultados da busca
(título, URL, resumo) e um máximo de fontes. Para cada resultado, atribua um
**score de relevância de 0 a 10** contra a definição:

- **descarte imediatamente** (score < 6) qualquer resultado que não trate do
  assunto informado, por melhor que pareça — assuntos vizinhos ou exemplos
  genéricos não entram;
- **descarte** páginas de venda, notícias fugazes, redes sociais, fóruns
  rasos, páginas de login/assinatura e resultados genéricos;
- **mantenha** (score ≥ 6) fontes autoritativas, conteúdo denso e específico
  do assunto; duplicidades do mesmo domínio ficam com a melhor.

Responda SOMENTE um array JSON de objetos
`{"url": "...", "motivo": "...", "score": 0-10}` — sem texto em volta.

## 3. Refinamento das buscas (rodadas)

Se as fontes aprovadas ainda forem menos que o alvo, você receberá as fontes
já aprovadas e deverá gerar **queries novas, mais específicas E EM INGLÊS**
— ângulos que as fontes aprovadas AINDA não cobrem (ex.: se já entrou o
guia geral, busque API reference, exemplos práticos, arquitetura, migração
de versão). Ir a fundo NÃO é volume: é cobrir a definição com fontes de
score alto. Responda SOMENTE um array JSON de 6 strings.

## 4. Recursos internos das fontes

Para cada fonte aprovada você receberá a lista de páginas internas do mesmo
site (texto do link + URL). Escolha as que valem entrar na base contra a
definição da RAG — capítulos de conteúdo, referência, guias — e atribua
score 0-10. Só entram as de score ≥ 7 (mais exigente que a fonte: a página
principal já entrou). Responda SOMENTE um array JSON de
`{"url": "...", "score": 0-10}`, no máximo 3 por fonte.

## 5. Download e conversão

O HTML vira texto puro e LIMPO (frases reconstituídas, scripts/menus/rodapés
fora), com corte de tamanho; páginas com menos de 500 caracteres úteis são
descartadas. Cada fonte vira um arquivo `.md` com `# título`, a linha
`> fonte: URL` e o texto — a URL fica preservada no conteúdo. Repos oficiais
do GitHub (donde a documentação vive) são clonados de forma esparso (só as
pastas de docs) e ingeridos como base grande, no mesmo modelo das bases
.NET/Python.

## 6. Ingestão e catalogação

Os arquivos entram no Qdrant em **modo lote** (`--rapido`): categoria = nome
da coleção, descrição = nome do arquivo. Depois a LLM lê amostras e grava
categoria + descrição em português no catálogo (`meta_colecoes`) — é a
análise final que verifica se a coleção faz sentido.

## Atualização

Rodar o seed de novo no mesmo assunto traz fontes novas (duplica as iguais).
Para recomeçar, apague a coleção no dashboard do Qdrant e rode o seed de novo.
