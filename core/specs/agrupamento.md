# Especificação do agrupamento de coleções por objetivo

O agrupamento organiza o catálogo: coleções que servem ao MESMO objetivo
recebem o mesmo grupo, para o operador escolher área no chat em vez de ler
uma lista longa. O grupo aparece no combobox do chat e na aba Coleções.

## Regra fixa de categorização

1. **DESENVOLVIMENTO** (grupo exatamente "Desenvolvimento"): TODO conteúdo
   de programação — linguagens (qualquer uma), frameworks, bibliotecas,
   ferramentas de código, engenharia de software, arquitetura, DevOps,
   documentação técnica de API/SDK. Se o material é sobre construir
   software, é "Desenvolvimento". Sem subdividir (não existe "backend",
   "frontend", "Arquitetura de Software" como grupos separados).
2. **Os demais** recebem grupo pelo DOMÍNIO da inserção — o nome curto da
   área do conhecimento (ex.: "Saúde", "Culinária", "Psicologia", "Mídia",
   "Conhecimento geral"), derivado da descrição da coleção, não do gosto
   do momento.

## Como agrupar

Você receberá a lista de coleções (nome, categoria, descrição, pontos).
Para CADA uma defina o grupo seguindo a regra fixa acima. Grupo CURTO
(1-3 palavras), em português, mesma string exata para o mesmo objetivo.
Na dúvida entre dois grupos, prefira o mais genérico (e se for código,
"Desenvolvimento").

Responda SOMENTE um objeto JSON `{"grupos": {"<colecao>": "<grupo>", ...}}`
cobrindo TODAS as coleções listadas — sem texto em volta.
