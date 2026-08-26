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
   "bloco3.sh" genérico quebra o teste na sandbox.
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
