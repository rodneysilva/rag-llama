"""Sandbox de execução — testa os códigos exibidos no chat num container
isolado com as principais linguagens (dotnet, java, rust, ruby, php, go,
node, python, dart/flutter).

O agente (sandbox/agent.py) roda no serviço `sandbox` da MESMA rede do
compose e é burro de propósito (grava arquivos + executa um comando); TODA
a orquestração vive aqui: mapear linguagem → comando, montar scaffolds
(csproj p/ C#, detecção de Cargo.toml/go.mod) e levar TODO o contexto
(todos os arquivos da conversa vão juntos — cross-file compila).
"""
import os
import re
import sys
import time

import httpx

from . import gateways


def _base() -> str:
    return gateways.base("sandbox")


def _headers() -> dict:
    return gateways.headers("sandbox")


# ─────────────── dependências Python detectadas no CÓDIGO ───────────────
# o teste do chat morria em "No module named 'flask'" — o código gerado usa
# bibliotecas de terceiros e o container vem limpo. Mapa módulo→pacote PyPI
# (nomes que divergem); o resto instala pelo próprio nome com -→_.
_PIP_MAP = {
    "flask": "flask", "flask_sqlalchemy": "flask-sqlalchemy",
    "sklearn": "scikit-learn", "cv2": "opencv-python-headless",
    "PIL": "pillow", "bs4": "beautifulsoup4", "yaml": "pyyaml",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv",
    "Crypto": "pycryptodome", "attr": "attrs", "skimage": "scikit-image",
}
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def _deps_python(arquivos: list[dict]) -> list[str]:
    """Pacotes PyPI que o código IMPORTA e não são stdlib (dedup, máx 12) —
    instalados no teste com `pip install --break-system-packages`."""
    mods: list[str] = []
    textos = " ".join(str(a.get("conteudo") or "")
                      for a in arquivos
                      if str(a.get("nome") or "").endswith(".py"))
    for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)",
                         textos, re.MULTILINE):
        mod = m.group(1)
        if mod in _STDLIB or mod in mods:
            continue
        mods.append(mod)
    pkgs = []
    for mod in mods[:12]:
        pkg = _PIP_MAP.get(mod, mod.replace("_", "-").lower())
        if pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def disponivel() -> bool:
    try:
        r = httpx.get(f"{_base()}/saude", headers=_headers(), timeout=4)
        return r.status_code == 200 and r.json().get("ok")
    except Exception:
        return False


def linguagens() -> dict:
    try:
        r = httpx.get(f"{_base()}/saude", headers=_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get("linguagens") or {}
    except Exception as e:
        raise RuntimeError(f"sandbox indisponível ({str(e)[:80]}) — "
                           "suba o serviço: docker compose up -d sandbox")


# bibliotecas que ABREM JANELA: sem display o processo morre na hora
# (tkinter/PyQt). O sandbox roda headless — xvfb-run cria um display falso
# para o teste executar até o fim (mainloop com janela virtual).
_IMPORTS_GUI = ("tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
                "wx", "gtk", "matplotlib.pyplot", "pygame", "turtle")


def _precisa_gui(arquivos: list[dict]) -> bool:
    textos = " ".join(str(a.get("conteudo") or "") for a in arquivos)
    return any(f"import {lib}" in textos or f"from {lib}" in textos
               for lib in _IMPORTS_GUI)


def _comando(principal: str, nomes: list[str], gui: bool = False,
             deps: list[str] | None = None):
    """(cmd, arquivos_extras, mover_para) para testar `principal` levando os
    demais arquivos como contexto. Scaffolds mínimos garantem que 'um
    arquivo' rode de verdade em linguagens de projeto (C#). GUI (tkinter…)
    roda sob xvfb-run (display virtual). `deps` = pacotes PyPI detectados
    nos imports (instalados antes de rodar o .py)."""
    ext = os.path.splitext(principal)[1].lower()
    tem = set(nomes)
    py_gui = gui and ext == ".py"
    if ext == ".py":
        # DEPENDÊNCIAS do próprio código (flask, requests…): instala ANTES de
        # rodar (pedido do dono — "não está testando todo o código"): o
        # container é limpo e o import de terceiros morria na hora
        if deps:
            instal = ("pip install --quiet --no-input --break-system-packages "
                      + " ".join(deps))
            cmd = (["bash", "-lc",
                    f"{instal} && xvfb-run -a python3 {principal}" if py_gui
                    else f"{instal} && python3 {principal}"])
        else:
            cmd = (["xvfb-run", "-a", "python3", principal] if py_gui
                   else ["python3", principal])
        return cmd, {}, ""
    if ext in (".sh", ".bash", ".zsh"):
        # `python`/`pip` são LINKS em /usr/local/bin (PATH de qualquer
        # shell) e pip roda com PIP_BREAK_SYSTEM_PACKAGES — scripts de
        # teste do chat instalam dependências neles mesmos
        return ["bash", principal], {}, ""
    if ext == ".js":
        return ["node", principal], {}, ""
    if ext == ".rb":
        return ["ruby", principal], {}, ""
    if ext == ".php":
        return ["php", principal], {}, ""
    if ext == ".java":
        return ["java", principal], {}, ""
    if ext == ".rs":
        if "Cargo.toml" in tem:
            return ["cargo", "run", "--quiet"], {}, ""
        return (["bash", "-lc", f"rustc -O {principal} -o app && ./app"],
                {}, "")
    if ext == ".go":
        if "go.mod" in tem:
            return ["go", "run", "."], {}, ""
        return ["go", "run", principal], {}, ""
    if ext == ".dart":
        return ["dart", "run", principal], {}, ""
    if ext == ".html":
        # HTML não EXECUTA — abre PREVIEW (iframe do modal / sandbox.disroy.org)
        raise _Preview(principal)
    if ext == ".cs":
        # orquestração C# com detecção de ENTRY POINT — ver _preparar_cs
        # (mantido para chamadas diretas: scaffold simples + move p/ Program.cs)
        extras = {"app.csproj": _CSPROJ}
        mover = "" if principal == "Program.cs" else "Program.cs"
        return ["dotnet", "run", "--project", "."], extras, mover
    raise ValueError(f"não sei testar '{ext or principal}' ainda — "
                     "linguagens: py, js, sh, rb, php, java, rs, go, dart, cs")


# scaffold console do .NET (dotnet run exige csproj)
_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    '  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n'
    '    <TargetFramework>net8.0</TargetFramework>\n'
    '    <ImplicitUsings>enable</ImplicitUsings>\n'
    '    <Nullable>disable</Nullable>\n  </PropertyGroup>\n'
    "</Project>\n")
# scaffold WEB (controllers/WebApplication precisam do Sdk.Web + refs do
# AspNetCore — o console dava CS0234 'Microsoft.AspNetCore' não existe)
_CSPROJ_WEB = (
    '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
    '  <PropertyGroup>\n'
    '    <TargetFramework>net8.0</TargetFramework>\n'
    '    <ImplicitUsings>enable</ImplicitUsings>\n'
    '    <Nullable>disable</Nullable>\n  </PropertyGroup>\n'
    "</Project>\n")


def _e_aspnet(arquivos: list[dict]) -> bool:
    """Código usa ASP.NET Core (Controller/WebApplication) → scaffold Web."""
    textos = " ".join(str(a.get("conteudo") or "")
                      for a in arquivos
                      if str(a.get("nome") or "").lower().endswith(".cs"))
    return ("Microsoft.AspNetCore" in textos or "WebApplication" in textos
            or "[ApiController]" in textos)


def _e_toplevel(conteudo: str) -> bool:
    """True se o arquivo C# tem TOP-LEVEL STATEMENTS (arquivo de ENTRY:
    sem declaração de tipos e com código além de usings/comentários)."""
    if re.search(r"\b(?:class|record|struct|enum|interface|namespace)\s+\w",
                 conteudo or ""):
        return False
    uteis = [l.strip() for l in (conteudo or "").splitlines()
             if l.strip()
             and not l.strip().startswith(("using ", "//", "/*", "*", "///"))]
    return bool(uteis)


def _sanear_cs(escrever: dict) -> dict:
    """Comentário de NOME estilo Python (`# src/X.cs`) na 1ª linha de um
    arquivo C# é DIRETIVA DE PREPROCESSADOR inválida → CS1024 mata o build
    inteiro (bug real: a LLM às vezes emite `#` em blocos csharp). Qualquer
    `# texto` no topo de .cs vira comentário `// texto` — mesmo que não
    fosse nome de arquivo, `# ` já é inválido em C# de qualquer forma."""
    for nome, conteudo in escrever.items():
        if not nome.lower().endswith(".cs"):
            continue
        linhas = str(conteudo).splitlines(keepends=True)
        if linhas and re.match(r"\s*#\s+\S", linhas[0]):
            linhas[0] = re.sub(r"^(\s*)#\s+", r"\1// ", linhas[0])
            escrever[nome] = "".join(linhas)
    return escrever


def _preparar_cs(escrever: dict, principal: str, aspnet: bool = False,
                 porta: int = 5000):
    """(cmd, extras, mover) do projeto C# com o ENTRY POINT certo.

    Bug real do dono: arquivo de CLASSE testado era MOVIDO para Program.cs
    como se fosse top-level statements → CS1585/CS5001. A verdade do C#:
    top-level statements funcionam em QUALQUER nome de arquivo (desde que
    seja o único com statements) e Main idem — o move só serve quando o
    principal É top-level. Ordem de decisão:
    1. .csproj da conversa → usa ELE (scaffold não pode conflitar);
    2. arquivo com `static … Main(` → entry clássico, NADA move;
    3. arquivo com top-level statements (qualquer nome) → entry, NADA move;
    4. principal top-level → move para Program.cs (única exigência do SDK);
    5. só arquivos de TIPOS: ASP.NET → BOOTSTRAP de verdade (AddControllers
       + MapControllers + rota convencional + Run na porta — o site SOBE e
       o preview público funciona); console → Program.cs que compila e
       AVISA (o ▶ deve apontar para o arquivo de entrada)."""
    csprojs = [n for n in escrever if n.lower().endswith(".csproj")]
    # ENTRY CHECK primeiro (vale para csproj da conversa E scaffold): sem
    # Main/top-level em NENHUM .cs, o build morre em CS5001 — o caso 5
    # (bootstrap ASP.NET / placeholder console) tem que rodar nos DOIS
    # caminhos (bug real: csproj da conversa retornava cedo e 3 arquivos
    # de classe davam CS5001 na cara do dono).
    tem_main = any(re.search(r"static\s+(?:async\s+)?(?:Task|void|int)\s+Main\s*\(",
                             str(c)) for c in escrever.values())
    # ⚠️ só .CS entram na checagem de top-level: o XML do csproj "parece"
    # top-level p/ a heurística (sem declaração de tipo e com linhas úteis)
    tem_entry = tem_main or any(
        _e_toplevel(str(c)) for n, c in escrever.items()
        if n.lower().endswith(".cs"))
    if not tem_entry:
        if aspnet:
            extras = {"Program.cs": (
                "// Program.cs gerado pela sandbox: a conversa trouxe somente\n"
                "// controllers/tipos - este bootstrap registra e sobe o site.\n"
                "var builder = WebApplication.CreateBuilder(args);\n"
                "builder.Services.AddControllersWithViews();\n"
                "var app = builder.Build();\n"
                "app.MapControllers();\n"
                "app.MapControllerRoute(\"default\", \"{controller}/{action=Index}/{id?}\");\n"
                "app.MapGet(\"/\", () => \"app no ar - endpoints: /{controller}/{acao}\");\n"
                f"app.Run(\"http://127.0.0.1:{porta or 5000}\");\n")}
            projeto = csprojs[0] if csprojs else None
            return (["dotnet", "run", "--project", projeto or "."],
                    extras, "")
        # console sem entry: compila como VERIFICAÇÃO + aviso
        extras = {"Program.cs": (
            "// gerado pela sandbox: nenhum arquivo tem Main/top-level —\n"
            "// aponte o ▶ para o arquivo de entrada quando ele existir\n"
            f"System.Console.WriteLine(\"projeto compilou \\u2713 ({len(escrever)} \"\n"
            "    + \"arquivo(s)); sem ponto de entrada — o teste valida a \"\n"
            "    + \"compilação; use o \\u25B6 no arquivo com Main ou top-level\");")}
        return (["dotnet", "run", "--project",
                 csprojs[0] if csprojs else "."], extras, "")
    if csprojs:
        # csproj da conversa tem prioridade — MAS completamos o que falta
        # para RODAR: sem ImplicitUsings, `WebApplication`/`Console` não
        # resolvem sem using explícito (bug real: CS0103 com todas as
        # references corretas — o modelo gera Program.cs sem usings)
        csproj = str(escrever[csprojs[0]])
        if "ImplicitUsings" not in csproj:
            csproj = csproj.replace(
                "<PropertyGroup>", "<PropertyGroup>"
                "\n    <ImplicitUsings>enable</ImplicitUsings>", 1)
            if "ImplicitUsings" not in csproj:  # sem PropertyGroup: cria
                csproj = csproj.replace(
                    "</Project>",
                    "  <PropertyGroup>\n"
                    "    <ImplicitUsings>enable</ImplicitUsings>\n"
                    "  </PropertyGroup>\n</Project>")
            escrever[csprojs[0]] = csproj
        return ["dotnet", "run", "--project", csprojs[0]], {}, ""
    extras = {"app.csproj": _CSPROJ_WEB if aspnet else _CSPROJ}
    if tem_main:
        return ["dotnet", "run", "--project", "."], extras, ""
    # principal top-level → move para Program.cs (única exigência do SDK
    # quando há OUTRO arquivo com statements; sozinho funciona em qualquer
    # nome — coberto pelo tem_entry acima)
    mover = "" if principal == "Program.cs" else "Program.cs"
    return ["dotnet", "run", "--project", "."], extras, mover


class _Preview(Exception):
    """HTML (e afins) não executa: o 'teste' é EXIBIR — o modal abre iframe."""

    def __init__(self, arquivo: str):
        super().__init__(arquivo)
        self.arquivo = arquivo


def limpar() -> bool:
    """Remove os ARQUIVOS dos testes (o container continua de pé — rápido).

    As PASTAS dos APPS VIVOS (preview público temporário) são PRESERVADAS
    via `exceto` — o servidor delas lê templates/static do disco; o resto
    (subdirs de testes concluídos) sai."""
    _podar_apps()
    exceto = [d.get("dir") for d in APPS_VIVOS.values() if d.get("dir")]
    try:
        r = httpx.post(f"{_base()}/limpar",
                       json={"exceto": exceto},
                       headers=_headers(), timeout=15)
        return r.status_code == 200 and r.json().get("ok")
    except Exception:
        return False


def token_preview(arquivo: str, ttl_s: int = 900) -> str:
    """Token curto (HMAC do AUTH_SECRET) que autentica o PREVIEW no
    sandbox.disroy.org — cookie não atravessa subdomínios; expira sozinho."""
    import hmac
    import hashlib
    from . import config as _cfg
    exp = int(time.time()) + ttl_s
    msg = f"{arquivo}|{exp}".encode()
    sig = hmac.new((_cfg.AUTH_SECRET or "ragaroy").encode(), msg,
                   hashlib.sha256).hexdigest()[:32]
    return f"{exp}:{sig}"


def token_preview_ok(arquivo: str, token: str) -> bool:
    import hmac
    import hashlib
    from . import config as _cfg
    try:
        exp, sig = token.split(":", 1)
        if int(exp) < time.time():
            return False
        msg = f"{arquivo}|{exp}".encode()
        calc = hmac.new((_cfg.AUTH_SECRET or "ragaroy").encode(), msg,
                        hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(calc, sig)
    except Exception:
        return False


# bibliotecas que ABREM SERVIDOR: rodar "e ver nada" não prova nada — o
# teste contextualizado SOBE o site, espera a porta e CAPTURA a resposta
# (curl /) antes de encerrar o processo
_IMPORTS_SITE = ("flask", "fastapi", "uvicorn", "http.server", "bottle",
                 "tornado")


def _e_site(arquivos: list[dict]) -> str | None:
    """Framework detectado nos .py (o teste vira subir+capturar)."""
    textos = " ".join(str(a.get("conteudo") or "")
                      for a in arquivos
                      if str(a.get("nome") or "").endswith(".py"))
    for fw in _IMPORTS_SITE:
        if f"import {fw}" in textos or f"from {fw}" in textos:
            return fw
    return None


def _reorganizar_web(escrever: dict, principal: str, textos_py: str,
                     textos_todos: str = "") -> dict:
    """Acomoda os arquivos ONDE o framework procura (bug real do dono:
    `render_template('schopenhauer.html')` + o .html gravado NA RAIZ =
    TemplateNotFound — o Flask só olha templates/).

    - render_template/Jinja2Templates/TemplateResponse no .py → os .html
      soltos (que não são o principal) vão para `templates/`;
    - url_for('static…')/"/static/" em QUALQUER arquivo (o caminho pode
      vir no TEMPLATE html, não só no .py) → os .css/.js soltos vão para
      `static/`.
    Arquivos que JÁ vêm com caminho ("templates/x.html") ficam como estão."""
    usa_templates = any(s in textos_py for s in
                        ("render_template(", "Jinja2Templates", "TemplateResponse"))
    if usa_templates:
        for n in [n for n in escrever
                  if n.endswith((".html", ".htm")) and "/" not in n
                  and n != principal]:
            escrever[f"templates/{n}"] = escrever.pop(n)
    usa_static = any(s in (textos_todos or textos_py) for s in
                     ("url_for('static", 'url_for("static', "/static/"))
    if usa_static:
        for n in [n for n in escrever
                  if n.endswith((".css", ".js")) and "/" not in n
                  and n != principal]:
            escrever[f"static/{n}"] = escrever.pop(n)
    return escrever


def escolher_principal(arquivos: list[dict]) -> str:
    """Entry point da RESPOSTA (botão "▶ testar resposta" do grupo — pedido
    do dono: "pode ser para a resposta completa"). Ordem: Program.cs
    top-level/Main → qualquer arquivo com Main → top-level em qualquer
    nome → site Python (flask/fastapi) → nomes convencionais → primeiro
    executável."""
    por_nome = {str(a.get("nome") or "").strip(): str(a.get("conteudo") or "")
                for a in arquivos if a.get("nome")}
    texto = "\n".join(por_nome.values())
    tem_main = lambda c: bool(re.search(
        r"static\s+(?:async\s+)?(?:Task|void|int)\s+Main\s*\(", c or ""))
    # 1. Program.cs válido (top-level ou Main)
    prog = por_nome.get("Program.cs")
    if prog and (_e_toplevel(prog) or tem_main(prog) or "WebApplication" in prog):
        return "Program.cs"
    # 2. Main clássico em qualquer arquivo
    for n, c in por_nome.items():
        if n.endswith(".cs") and tem_main(c):
            return n
    # 3. top-level em qualquer nome / site ASP.NET em qualquer entrada .cs
    for n, c in por_nome.items():
        if n.endswith(".cs") and (_e_toplevel(c) or "WebApplication" in c):
            return n
    # 4. site Python
    for n, c in por_nome.items():
        if n.endswith(".py") and any(
                f"import {fw}" in c or f"from {fw}" in c
                for fw in _IMPORTS_SITE):
            return n
    # 5. nomes convencionais e primeiro executável
    EXECS = (".py", ".js", ".ts", ".sh", ".rb", ".php", ".java", ".rs",
             ".go", ".dart", ".cs")
    for alvo in ("app.py", "main.py", "servidor.py", "server.py",
                 "index.js", "server.js", "index.sh", "main.go", "main.rs"):
        if alvo in por_nome:
            return alvo
    for n in por_nome:
        if n.lower().endswith(EXECS):
            return n
    # 6. nada executável: front-end puro — o HTML vira PREVIEW
    for alvo in ("index.html", "app.html", "index.htm"):
        if alvo in por_nome:
            return alvo
    for n in por_nome:
        if n.lower().endswith((".html", ".htm")):
            return n
    return sorted(por_nome)[0] if por_nome else ""


def _porta_do_codigo(textos: str, fw: str) -> int:
    """Porta que o CÓDIGO DO USUÁRIO declara — `port=5050` (flask/uvicorn/
    node), `http://127.0.0.1:5001` (ASP.NET Run), `--port 8080`. Padrão
    5000 (8000 p/ fastapi).

    POR QUÊ (bug real do dono): a porta era FIXA por framework — todo teste
    novo fazia `fuser -k 5000` e MATAVA o app temporário anterior (o link
    público de ~30 min morria "sozinho"). Com a porta LIDA DO CÓDIGO, cada
    app com porta declarada diferente coexiste; só códigos SEM porta
    explícita caem no default compartilhado (e aí o novo substitui o velho
    — página amigável na API explica)."""
    for pat in (r"\b[Pp]ort\b\s*=\s*(\d{3,5})",
                r"://127\.0\.0\.1:(\d{3,5})",
                r"://localhost:(\d{3,5})",
                r"--port\s+(\d{3,5})"):
        m = re.search(pat, textos or "")
        if m:
            p = int(m.group(1))
            if 1024 <= p <= 65535:
                return p
    return 8000 if fw in ("fastapi", "uvicorn") else 5000


def _cmd_site(principal: str, fw: str, deps: list[str],
              runner: str = "", porta: int = 0) -> list[str]:
    """Sobe o servidor, espera a porta, captura a home e o log — e DEIXA O
    APP VIVO (pedido do dono: "página temporária sandbox.disroy.org onde
    ficará hospedado o aplicativo por um tempo"): o preview público faz
    proxy até a porta dele. A limpeza da porta no INÍCIO substitui o app
    anterior da MESMA porta (1 app por porta — apps de portas DIFERENTES
    coexistem; a porta vem do CÓDIGO, ver _porta_do_codigo).

    `runner` = comando que sobe o servidor (padrão `python3 {principal}`;
    ASP.NET usa `dotnet run --project .`). ROBUSTEZ (bug real em produção —
    "site.log: No such file or directory" com resposta em 0,5 s): o script
    antigo fazia `pip install … && python3 app.py > site.log &` — o `&`
    mandava pip+python JUNTOS para o background, o loop de curl acertava
    um servidor ÓRFÃO de teste anterior e site.log nem existia. Agora:
    1. porta limpa ANTES (fuser quando disponível — órfãos legados);
    2. instalação em FOREGROUND (o site.log só nasce depois das deps);
    3. servidor em SESSÃO PRÓPRIA (setsid) — sobrevive ao fim do script;
    4. curl com --max-time e aviso explícito quando a porta não responde;
    5. head tolerante (sem log → mensagem clara no lugar de erro)."""
    porta = porta or (8000 if fw in ("fastapi", "uvicorn") else 5000)
    # ASP.NET: build dentro do runner (restore 1ª vez) — espera maior
    loops = 120 if fw == "aspnet" else 30
    instal = ("pip install --quiet --no-input --break-system-packages "
              + " ".join(deps)) if deps else "true"
    run_cmd = runner or f"python3 {principal}"
    script = (
        # 0) limpa a porta (app anterior da MESMA porta — 1 por porta)
        f"(command -v fuser >/dev/null && fuser -k {porta}/tcp) 2>/dev/null; "
        f"sleep 0.4; "
        # 1) dependências FOREGROUND — o log só existe depois disto
        f"{instal} || true; "
        # 2) servidor em grupo próprio (setsid do util-linux; guardado por
        #    garantia) — fica VIVO após o script
        f"CMD=\"{run_cmd}\"; command -v setsid >/dev/null "
        f"&& CMD=\"setsid $CMD\"; $CMD > site.log 2>&1 & SRV=$!; "
        # 3) espera a porta registrando se respondeu
        f"OK=0; for i in $(seq 1 {loops}); do "
        f"curl -s --max-time 1 -o /dev/null http://127.0.0.1:{porta} "
        f"&& OK=1 && break; sleep 0.5; done; "
        f"[ \"$OK\" = 1 ] || echo '⚠️ a porta {porta} não respondeu — veja "
        f"o log abaixo'; "
        # 4) captura a home
        f"echo '── resposta da home (curl) ──'; "
        f"curl -s --max-time 3 http://127.0.0.1:{porta} | head -c 4000; echo; "
        # 5) log do servidor (tolerante à ausência)
        f"echo '── log do servidor ──'; "
        f"if [ -f site.log ]; then head -c 2000 site.log; "
        f"else echo '(sem site.log — o servidor não chegou a gravar)'; fi; "
        # 6) se a porta respondeu, o app PERMANECE no ar (preview público
        #    temporário) — com build falho NÃO anuncia (mentira do 1º fix)
        f"if [ \"$OK\" = 1 ]; then echo '── app NO AR na porta {porta} — "
        f"preview público temporário'; fi")
    return ["bash", "-lc", script]


# ───────── apps VIVOS (preview público temporário no sandbox.disroy.org) ─────
# {porta: {exp, principal, fw}} — o servidor roda NO container sandbox; a
# URL pública leva CHAVE HMAC porta+expiração (SEM estado na borda; a API
# só precisa deste dicionário para saber que NÃO pode limpar os arquivos).
APPS_VIVOS: dict[int, dict] = {}
APP_TTL_S = 30 * 60   # "por um tempo" = meia hora


def chave_app(porta: int, ttl_s: int = APP_TTL_S) -> tuple[str, int]:
    """Chave pública do app: {porta}.{exp}.{hmac} — quem tem o link acessa."""
    import hashlib
    import hmac as _hmac
    import time as _t
    from . import config as _cfg
    exp = int(_t.time()) + ttl_s
    msg = f"app|{porta}|{exp}".encode()
    sig = _hmac.new((_cfg.AUTH_SECRET or "ragaroy").encode(), msg,
                    hashlib.sha256).hexdigest()[:24]
    return f"{porta}.{exp}.{sig}", exp


def chave_app_ok(chave: str) -> int | None:
    """Valida a chave (HMAC + expiração) e devolve a porta — ou None."""
    import hashlib
    import hmac as _hmac
    import time as _t
    from . import config as _cfg
    try:
        porta_s, exp_s, sig = (chave or "").split(".", 2)
        porta, exp = int(porta_s), int(exp_s)
    except Exception:
        return None
    if exp < _t.time():
        return None
    msg = f"app|{porta}|{exp}".encode()
    calc = _hmac.new((_cfg.AUTH_SECRET or "ragaroy").encode(), msg,
                     hashlib.sha256).hexdigest()[:24]
    return porta if _hmac.compare_digest(calc, sig) else None


def _podar_apps() -> None:
    """Remove do registro os apps cujo TTL venceu (o processo pode até
    seguir vivo no container — o link é que morre)."""
    import time as _t
    agora = _t.time()
    for porta in [p for p, d in APPS_VIVOS.items() if d.get("exp", 0) < agora]:
        APPS_VIVOS.pop(porta, None)


def testar(arquivos: list[dict], principal: str, timeout: int = 300,
           log=print) -> dict:
    """Escreve TODOS os arquivos (contexto completo) e roda `principal`.
    Devolve {ok, comando, saida, segundos}; HTML devolve {preview, url}.
    `log` narra as etapas (o teste é JOB — as linhas aparecem no modal)."""
    if not arquivos or not principal:
        raise ValueError("informe o arquivo principal e o contexto (arquivos)")
    escrever = {str(a.get("nome") or "").strip(): str(a.get("conteudo") or "")
                 for a in arquivos if a.get("nome")}
    escrever = _sanear_cs(escrever)
    # 🧹 SUBDIR PRÓPRIO por teste (bug real: /tmp/work acumulava arquivos
    # de testes ANTERIORES — um controller ASP.NET velho quebrava o build
    # do projeto console novo com CS0260/CS8802). Cada teste nasce limpo;
    # o agent acha previews pelo mais recente.
    import uuid as _uuid
    subdir = _uuid.uuid4().hex[:10]
    log(f"📦 gravando {len(escrever)} arquivo(s) na sandbox…", "sandbox")
    gui = _precisa_gui(arquivos)
    deps = _deps_python(arquivos)
    aspnet = (principal.lower().endswith(".cs") and _e_aspnet(arquivos))
    site_fw = None
    if principal.endswith(".py"):
        site_fw = _e_site(arquivos)
    elif aspnet:
        site_fw = "aspnet"   # WebApplication/controller: sobe e captura
    if site_fw:
        log(f"🌐 site detectado ({site_fw}) — o teste SOBE o servidor, "
            "captura a home e deixa o app NO AR (preview público "
            "temporário)", "sandbox")
        # TEMPLATES/STATIC: render_template('x.html') procura em templates/
        # — .html solto na raiz era TemplateNotFound (bug real do dono)
        textos_py = " ".join(str(a.get("conteudo") or "")
                             for a in arquivos
                             if str(a.get("nome") or "").endswith(".py"))
        textos_todos = " ".join(str(a.get("conteudo") or "") for a in arquivos)
        escrever = _reorganizar_web(escrever, principal, textos_py,
                                    textos_todos)
    try:
        if site_fw:
            # PORTA do código do usuário (coexistência de apps — o default
            # por framework matava o app anterior de outra geração)
            porta_app = _porta_do_codigo(textos_todos, site_fw)
            if site_fw == "aspnet":
                _, extras, mover = _preparar_cs(escrever, principal,
                                                aspnet=True, porta=porta_app)
                if mover:
                    escrever[mover] = escrever.pop(principal)
                if "Program.cs" in extras:
                    log(f"🚀 só controllers/tipos na conversa — bootstrap "
                        f"ASP.NET gerado (Program.cs) subindo na porta "
                        f"{porta_app}", "sandbox")
                escrever.update(extras)
                cmd = _cmd_site(principal, site_fw, [],
                                runner="dotnet run --project .", porta=porta_app)
                extras, mover = {}, ""
            else:
                # ⚡ FASTAPI/UVICORN: `python3 app.py` NÃO sobe servidor
                # nenhum (o arquivo só declara `app = FastAPI()` e rotas —
                # sem uvicorn.run o processo morre na hora e a porta nunca
                # responde). Se o código JÁ se auto-sobe (`uvicorn.run(` ou
                # bloco `__main__` com run), roda direto; senão o runner é
                # `python3 -m uvicorn {stem}:app` (o módulo existe porque o
                # _deps instala fastapi → uvicorn junto).
                runner = ""
                if site_fw in ("fastapi", "uvicorn"):
                    auto_sobe = ("uvicorn.run(" in textos_py
                                 or "__main__" in textos_py)
                    if not auto_sobe:
                        stem = principal[:-3].replace("/", ".") if \
                            principal.endswith(".py") else "app"
                        runner = (f"python3 -m uvicorn {stem}:app "
                                  f"--host 127.0.0.1 --port {porta_app}")
                        log(f"⚡ fastapi sem servidor no código — subindo "
                            f"com uvicorn ({stem}:app) na porta {porta_app}",
                            "sandbox")
                        # `pip install fastapi` NEM SEMPRE traz o uvicorn —
                        # garante o servidor nas deps do teste
                        if "uvicorn" not in deps:
                            deps = list(deps) + ["uvicorn"]
                cmd = _cmd_site(principal, site_fw, deps, runner=runner,
                                porta=porta_app)
                extras, mover = {}, ""
        elif principal.lower().endswith(".cs"):
            # C# com ENTRY POINT de verdade (Program.cs/Main/top-level em
            # qualquer arquivo/csproj da conversa) — ver _preparar_cs
            cmd, extras, mover = _preparar_cs(escrever, principal, aspnet)
        else:
            cmd, extras, mover = _comando(principal, list(escrever), gui=gui,
                                          deps=deps)
    except _Preview as p:
        # grava e devolve a URL do preview (iframe do modal + subdomínio)
        try:
            r = httpx.post(f"{_base()}/arquivos",
                            json={"arquivos": escrever, "dir": subdir},
                            headers=_headers(), timeout=30)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"sandbox fora do ar ({str(e)[:60]})") from e
        return {"ok": True, "comando": f"preview {p.arquivo}",
                "preview": p.arquivo,
                "url": f"/sandbox/ver/{p.arquivo}",
                "url_publica": f"https://sandbox.disroy.org/sandbox/ver/{p.arquivo}"
                               f"?k={token_preview(p.arquivo)}",
                "saida": "arquivo HTML pronto — o preview abre ao lado",
                "segundos": 0}
    if mover:  # C#: o principal vira Program.cs (top-level statements)
        escrever[mover] = escrever.pop(principal)
    escrever.update(extras)
    try:
        r = httpx.post(f"{_base()}/arquivos",
                       json={"arquivos": escrever, "dir": subdir},
                       headers=_headers(), timeout=30)
        r.raise_for_status()
        if not r.json().get("ok"):
            raise RuntimeError("falha ao gravar arquivos: "
                               + "; ".join(r.json().get("erros", [])[:3]))
    except httpx.HTTPError as e:
        raise RuntimeError(f"sandbox fora do ar ({str(e)[:60]}) — "
                           "docker compose up -d sandbox") from e
    if deps and not site_fw:
        log(f"⬇️ instalando dependências do código: {', '.join(deps)} "
            "(primeira vez demora)…", "sandbox")
    log(f"▶️ executando {' '.join(cmd[:2])} …", "sandbox")
    try:
        r = httpx.post(f"{_base()}/exec",
                       json={"cmd": cmd, "timeout": 30 if gui else timeout,
                             "dir": subdir},
                       headers=_headers(), timeout=(30 if gui else timeout) + 60)
        r.raise_for_status()
        out = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"sandbox não respondeu ({str(e)[:60]})") from e
    # GUI headless: mainloop roda PARA SEMPRE — o timeout (30 s) encerra.
    # Sem traceback = a janela abriu e o código rodou limpo o tempo todo.
    if gui and out.get("code") == -9 and "Traceback" not in (out.get("saida") or ""):
        return {"ok": True, "comando": " ".join(cmd),
                "saida": (out.get("saida") or "")
                + "\n\n(GUI headless: a janela rodou 30 s sem erro sob display "
                  "virtual — o timeout apenas a fechou.)",
                "segundos": out.get("segundos")}
    # 🌐 SITE: a porta respondeu → o app está VIVO no container; gera a
    # chave pública (porta+expiração assinadas) e devolve o link temporário
    if site_fw and "não respondeu" not in (out.get("saida") or ""):
        # app de MESMA porta substitui o anterior (fuser); porta nova coexiste
        chave, exp = chave_app(porta_app)
        APPS_VIVOS[porta_app] = {"exp": exp, "principal": principal,
                                 "fw": site_fw, "dir": subdir}
        log(f"🔗 app no ar por {APP_TTL_S // 60} min (porta {porta_app}) — "
            "preview público temporário disponível", "sandbox")
        return {"ok": out.get("code") == 0, "comando": " ".join(cmd),
                "saida": out.get("saida", ""), "segundos": out.get("segundos"),
                "preview": f"aplicativo {site_fw} (porta {porta_app}) — no ar "
                           f"~{APP_TTL_S // 60} min",
                "url": f"/sandbox/app/{chave}/",
                "url_publica": f"https://sandbox.disroy.org/sandbox/app/{chave}/"}
    return {"ok": out.get("code") == 0, "comando": " ".join(cmd),
            "saida": out.get("saida", ""), "segundos": out.get("segundos")}
