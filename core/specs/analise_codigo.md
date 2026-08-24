# Especificação da análise de código-fonte para ingestão

Você recebe um arquivo de código-fonte. Analise ANTES de ele virar base de
conhecimento — a análise vira camada de metadata que enriquece a busca.

Responda SOMENTE um objeto JSON:

```json
{"linguagem": "nome da linguagem",
 "versao": "versão da plataforma que o código pressupõe (ou "")",
 "proposito": "para que serve este arquivo, 1 frase em português",
 "padroes": ["padrões/técnicas que o código demonstra, em português"],
 "bibliotecas": ["dependências externas identificadas"],
 "resumo_pt": "resumo em português (2-3 frases) do que este código faz e ensina",
 "qualidade": "moderno | legado | misto — o estilo segue as práticas atuais?"}
```

Se não for código (documento comum), responda `{"linguagem": ""}`.
