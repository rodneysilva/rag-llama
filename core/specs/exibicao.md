# Especificação da exibição das respostas no chat (code-friendly)

As respostas do assistente são exibidas de acordo com o que contêm:

1. **Bloco de código cercado** (``` linguagem … ```) vira um cartão de código:
   - barra superior com o nome da linguagem (ou "código") e botão
     **📋 copiar** (copia para a área de transferência e confirma "✅ copiado");
   - fonte monoespaçada, fundo escuro, rolagem horizontal quando a linha é longa;
   - **destaque de sintaxe** (highlight.js, tema github-dark) quando a linguagem
     é reconhecida — python, bash, csharp, json, sql etc.
2. **`código` em linha** (crases simples) fica destacado dentro do parágrafo.
3. O resto do texto é prosa normal: **negrito** preservado, títulos `#` em
   negrito, listas com marcador • e quebras de linha respeitadas.
4. A mesma renderização vale para mensagens de sessões salvas reabertas —
   a conversa volta idêntica ao que foi visto.
5. Se o CDN do highlight.js não carregar, os blocos continuam funcionando
   (cartão, cópia e monoespaçado), apenas sem cores de sintaxe.
6. Mensagens do usuário nunca interpretam markdown — são exibidas literais.

A exibição não altera o texto salvo na sessão nem o histórico enviado ao
modelo: a renderização é só camada visual sobre a resposta original.
