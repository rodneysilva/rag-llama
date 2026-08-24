# Lições de campo — como evoluir a RAG e o motor sem se perder

Anotações práticas de operação (cada item nasceu de um bug real em
produção). Leia junto com `docs/guia-conceitos-rag.md` (fundamentos) e
`docs/plano-qualidade-rag.md` (fases).

## Contratos de código (SOLID aplicado ao dia a dia)

1. **Callback de log tem assinatura FIXA: `log(msg, grupo="")`** — todos
   os cores (`auto`, `midia`, `pesquisa`, `conjuntos`…) chamam assim.
   Quem cria lambdas de job usa `lambda m, g="": …`. O bug
   `takes 1 positional argument but 2 were given` (pesquisa profunda
   quebrada) foi EXATAMENTE um lambda de 1 arg. Novo core? Assine igual.
2. **Uma fonte de verdade por conceito** — "o que está no ar" só é
   respondido por `modelos_ativos()`; a UI (badge, Sistema, dashboard)
   NUNCA lê `config.LLM_MODEL` direto. Novo consumidor? Chama a fonte.
3. **Bind-mount de ARQUIVO ÚNICO não aceita `os.replace`** (users.json,
   .env no compose: "Device or resource busy") — gravar in-place sob
   lock (`set_env_inplace`, `auth._gravar`). Se o arquivo é montado,
   rename não existe para você.
4. **Um `@app.rota` por função** — decorator empilhado faz a rota nova
   "roubar" a antiga (`/api/query` respondendo o catálogo de provedores).
5. **Thread do worker de fila é REUSADA** — estado thread-local
   (`rag.set_override`) exige `finally` nos chamadores.

## Como CONSULTAR melhor (para quem usa o chat)

- **Marque as coleções certas** e deixe o modo `híbrido`: a base entra
  como referência primária e o modelo complementa. `rag` puro é para
  quando você quer SÓ o que está na base (recusa honesta).
- **Pergunta sobre o CONTEÚDO, não sobre a base** — "o que tem aqui?"
  não é busca semântica; use o seletor/dashboard para isso.
- **Follow-up funciona**: o histórico das últimas 4 mensagens viaja
  (respostas truncadas) — "e a dele?" resolve pelo fio da conversa.
- **Score gate é seu amigo**: fragmento forte (≥ SCORE_DIRETO) responde
  direto (0 token); contexto fraco é descartado em vez de alucinar.
- **🌐 pesquisa-web** para informação ATUAL; **modo auto** decide sozinho
  (com crítica CRAG e aprofundamento).

## Como ENRIQUECER a base com qualidade

- **Sempre pelo modo Revisão** — nada grava sem aprovação; o relatório
  (duplicados, clusters, gate de tema) é a curadoria de verdade.
- **Seed por assunto** (pesquisa profunda) para nascer grande; **upload
  de pastas/PDF** para material próprio; **HF** para datasets públicos.
- **Higienize coleções antigas** (✨ na Biblioteca) depois de reingestar
  — dedupe/cosso não acontecem sozinhos.
- **Um domínio por coleção** e deixa o `agrupar()` organizar; coleções
  genéricas contaminam o estilo da resposta (spec por área cuida do tom).
- **Temas em português funcionam** — a busca normaliza para inglês
  (`core/idioma`) e traz fontes internacionais; as respostas continuam
  em pt-BR.

## Motor de busca (o que já é genérico e o que vigiar)

`planner (spec) → motores em cascata → página INTEIRA → claims citadas`
— um modo novo de busca é uma SPEC nova (`pesquisa_*.md`), não código.
Vigiar: (a) fallback sem chaves continua buscando (DDG/Wikipedia/GitHub);
(b) `log` de cada motor com tempo gasto (minutos mudos = aparentam
travamento); (c) budget no código (≤6 consultas, ≤12 fontes) protege o
bolso e o tempo — mudar budget é decisão de dono, não de prompt.
