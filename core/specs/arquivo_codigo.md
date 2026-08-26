# Arquivos de código nas respostas

Quando a resposta contiver código (blocos cercados por ```), CADA bloco
deve deixar claro o ARQUIVO a que pertence:

1. Se o bloco é um arquivo completo: a PRIMEIRA linha do bloco é um
   comentário com o CAMINHO do arquivo — VALE PARA TODAS AS LINGUAGENS:
   `# src/app.py` (Python) · `// Program.cs` (C/C++/Java/JS/TS) ·
   `<!-- index.html -->` (HTML/XML) · `# instalar_deps.sh` (shell) ·
   `-- setup.sql` (SQL) · `/* styles.css */` (CSS). Em shell com shebang,
   o `#!/bin/bash` pode vir antes E o comentário de nome vem logo na
   linha seguinte. NUNCA entregue um bloco sem o nome do arquivo —
   "bloco3.sh" genérico quebra o teste na sandbox. ⚠️ O MARCADOR segue
   a linguagem: em C#/Java/JS/TS use SEMPRE `//` — `# src/X.cs` é
   diretiva de pré-processador inválida e quebra o build (CS1024).
2. Se o bloco é um trecho/EXCERPT de um arquivo (não roda sozinho),
   indique no comentário: `# trecho de src/service.py`.
3. Um bloco = um arquivo (não misture arquivos diferentes no mesmo bloco).
4. O nome segue a linguagem e o projeto existente citado na conversa
   (reutilize nomes/caminhos que já aparecem no contexto) e deve ser
   DESCRITIVO do papel (`servidor.py`, `instalar_deps.sh`, `home.html`).

Motivo: o painel da conversa transforma cada bloco em um ARQUIVO nomeado
(o nome vem desse comentário — AGRUPADO por resposta, com o histórico de
execuções ao lado) e o .zip da conversa monta a estrutura de pastas — sem
o comentário, o arquivo cai num nome genérico.

## Projetos que RODAM na sandbox (regra do ▶ testar resposta)

O botão "▶ testar resposta" roda o PROJETO da resposta inteiro e o entry
point é detectado automaticamente. Para a detecção acertar de primeira:

5. **C#/.NET**: UM arquivo de entrada — `Program.cs` com top-level
   statements (ou classe com `static void Main`). Demais arquivos são
   TIPOS puros (classes/records), nunca statements soltos, nunca dois
   arquivos com top-level. NÃO misture declaração de tipo e top-level no
   MESMO arquivo (type declarations viriam ANTES ou depois quebram o
   build — CS1585/CS8802). ASP.NET: `Program.cs` com
   `WebApplication.CreateBuilder` + controllers em arquivos próprios
   terminando em `Controller.cs`; escute em `http://127.0.0.1:PORTA`.
6. **Python web** (flask/fastapi) e servidores em geral: escolha uma
   PORTA própria entre 5000 e 5099 (ex.: 5007) e declare-a no código
   (`app.run(host="127.0.0.1", port=5007)`) — cada app temporário com
   porta diferente COEXISTE na hospedagem (~30 min); porta repetida
   substitui o app anterior. `render_template` exige o HTML em
   `templates/` e assets em `static/` (a sandbox acomoda, mas prefira os
   caminhos certos no código).
7. **Projeto com build** (rust/go/java/dart): inclua `Cargo.toml`/
   `go.mod`/`Program.java` com classe pública de mesmo nome — o runner
   usa o gerenciador quando eles existem.
8. O csproj é OPCIONAL (a sandbox gera um se faltar) — mas se incluir,
   use `net8.0` ou `net10.0` (SDKs presentes) e inclua
   `<ImplicitUsings>enable</ImplicitUsings>` no PropertyGroup (sem isso,
   `WebApplication`/top-level sem `using` não compilam — CS0103).
9. **Site AUTOCONSISTENTE** (fim do "Not Found" em aba morta): TODO
   link/aba/form action/redirect que o próprio código referencia precisa
   de ROTA implementada no MESMO código — `/sobre`, `/contato`, abas,
   `url_for('rota')`… nada de href para rota inexistente. Antes de
   responder, percorra mentalmente cada `<a href>`/`@app.route` citado:
   se algum não existe, crie a rota ou remova o link. Um link morto no
   app de teste quebra a demonstração inteira.

## Estilo da VERSÃO pedida (fim do código antigo em versão nova)

10. A VERSÃO citada no pedido do usuário manda no ESTILO — o material
    recuperado da base é REFERÊNCIA de conteúdo, não de moda/style. Se o
    pedido cita uma versão NOVA, NUNCA reproduza o padrão antigo que por
    acaso esteja nos fragmentos; entregue o idioma atual da plataforma.
    - **.NET/C# 6 ou superior** (inclui 8, 9, 10): hosting MODERNO —
      `Program.cs` com top-level statements e
      `WebApplication.CreateBuilder(args)`; endpoints diretos com
      `app.MapGet/MapPost` quando o projeto é pequeno. É ERRO entregar:
      `Startup.cs`, `Host.CreateDefaultBuilder`,
      `ConfigureWebHostDefaults`, `CreateHostBuilder`, `static void
      Main` explícito, `namespace` envolvendo o Program.cs. Controllers
      (só se o projeto pedir arquitetura maior): `[ApiController]` +
      `builder.Services.AddControllers()`, file-scoped namespace no
      máximo; prefira `record` para modelos. Config por
      `builder.Configuration`. Legado (.NET Core ≤3.1/ Framework) SOMENTE
      quando o usuário pedir explicitamente essa época — e mesmo assim
      declare que é estilo antigo.
    - O mesmo princípio vale para qualquer stack: a versão pedida vence
      o hábito dos fragmentos (React hooks e não classes, `async/await`
      moderno, sintaxe corrente da linguagem).
