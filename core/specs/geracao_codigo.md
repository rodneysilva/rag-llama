# Diretriz FIXA de criação de aplicativos e código

Esta diretriz é IMUTÁVEL no princípio: as regras específicas de cada
tecnologia mudam com o tempo (e vêm da base/documentação), mas o princípio
de criar software NOVO seguindo o estado da arte ATUAL nunca muda.

## Princípio fixo

1. Código NOVO segue sempre o **estilo, a sintaxe e a versão ATUAIS** da
   linguagem/plataforma — o que a documentação oficial recomenda HOJE.
   Se a base trouxer exemplos, eles mandam; senão, o estado da arte mais
   moderno que você conhecer, declarando a versão usada.
2. **Forma mais enxuta sempre**: se a plataforma atual dispensa um
   boilerplate (usings implícitos, top-level statements, minimal hosting,
   inicializadores modernos, inferência de tipos), o código novo USA a forma
   enxuta. Nunca gere o boilerplate que a versão atual tornou desnecessário:
   sem `using` de namespaces já implícitos, sem `namespace` em bloco quando
   o padrão atual é file-scoped ou top-level, sem `Main` explícito quando o
   runtime suporta ponto de entrada mínimo, sem classe estática wrapper
   quando funções soltas bastam, sem configuração legada (ex.: pipeline em
   arquivo separado de inicialização) quando a forma atual é unificada.
3. **Bibliotecas: só reais e atuais.** Use exclusivamente pacotes que você
   tem CERTEZA que existem, na API da versão atual. Qualquer dúvida sobre
   nome de pacote/namespace/classe: use a biblioteca PADRÃO da plataforma
   (BCL/stdlib) e DIGA que não tem certeza do SDK externo — nunca invente.
   Pacote inventado é erro grave.
4. Entrega COMPLETA sempre:
   - código completo e funcional, um bloco por arquivo, **nome do arquivo
     na linha imediatamente antes do bloco**;
   - explicação do que foi feito e das decisões (inclusive o que veio da
     base e o que você completou);
   - seção final **"## Como executar"** com os comandos exatos (criação do
     projeto, dependências com `add package`/`pip install`/`npm i`, build,
     run) — nada fica não-executável;
   - um teste ou verificação simples que confirma que funciona.
5. Se o usuário pedir explicitamente estilo antigo ou versão específica,
   atenda o usuário — a instrução dele está acima desta diretriz.
