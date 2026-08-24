# Especificação da varredura LLM de coleções (limpeza profunda)

A varredura usa a LLM para julgar CADA chunk já gravado no Qdrant contra a
definição da coleção (catálogo) e apagar o que é **lixo claro** — o que não
traz informação aproveitável para uma busca semântica.

## O escopo é o ASSUNTO da coleção, não um subtópico

O escopo de julgamento é o assunto geral da coleção (o nome dela). Qualquer
conteúdo TEÓRICO ou factual DO PRÓPRIO ASSUNTO é VÁLIDO: texto sobre
qualquer subtema, conceito, histórico, figura, receita, comando ou técnica
DO ASSUNTO da coleção faz parte dele — não é lixo.

**NUNCA marque um chunk como lixo por "não estar diretamente relacionado"**,
"ser muito geral" ou "tratar de um subtema" — isso é conteúdo válido. Lixo é
APENAS a estrutura da página em volta do conteúdo, nunca o conteúdo.

## Julgamento por lote

Você receberá: o nome da coleção, a definição dela (descrição do catálogo) e
um lote de trechos numerados `[n] (fonte) texto`. Marque SOMENTE:

**LIXO (marque)** — estritamente uma dessas:
- navegação/estrutura de página: menus, índices, listas de seções, migalhas,
  listas de países/estados/categorias sem conteúdo;
- boilerplate: rodapés, avisos de cookie, CTAs, promoções, assinaturas,
  informações de contato/agendamento, texto de e-mail;
- blocos de referências/citações bibliográficas sem conteúdo próprio;
- fragmento que, sozinho, não comunica nada (frase cortada sem assunto,
  tabela de símbolos, números soltos).

**MANTER (não marque)**:
- qualquer conteúdo informativo do assunto da coleção, ainda que curto,
  geral, "básico" ou em outro idioma;
- trecho com informação verificável;
- **NA DÚVIDA, SEMPRE MANTER** — apagar conhecimento bom é MUITO pior que
  manter um trecho medíocre.

Responda SOMENTE um objeto JSON `{"lixo": [{"i": <n>, "motivo": "..."}]}`
com os números marcados — sem texto em volta. Se nenhum for lixo, responda
`{"lixo": []}`.

