# -*- coding: utf-8 -*-
"""Extrator AST do split api/app.py -> api/base.py + api/routers/* + core/jobs.py.

Fase 1 (mecânica): zero mudança de comportamento — decoradores @app.X viram
@router.X nos routers; helpers/estado compartilhados vão para api/base.py com
__all__ explícito (compat: `from api.app import X` continua funcionando via
re-export). JobRegistry/_novo_job/_podar_concluidos/PASTA_LOGS_JOBS -> core/jobs.py.
"""
import ast
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SRC = RAIZ / "Temp" / "app_original.py"  # original preservado (git show HEAD)
DEST_BASE = RAIZ / "api" / "base.py"
DEST_CORE = RAIZ / "core" / "jobs.py"
DEST_ROUTERS = RAIZ / "api" / "routers"

src = SRC.read_text(encoding="utf-8")
lines = src.splitlines(keepends=True)
tree = ast.parse(src)

METODOS = {"get", "post", "put", "delete", "patch", "head", "options"}

# ---------- 1) unidades top-level (com comentários contíguos acima) ----------
units = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        start = min(d.lineno for d in node.decorator_list)
    else:
        start = node.lineno
    s = start - 1  # 0-based
    while s - 1 >= 0 and lines[s - 1].lstrip().startswith("#"):
        s -= 1
    units.append({"node": node, "start": s, "end": node.end_lineno})

# ---------- 2) classificação ----------
def decor_info(dec):
    """(attr, arg0) se decorator é @app.<attr>("<path>") ou @app.api_route(...,
    methods=[...]) ; senão None."""
    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
            and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"
            and dec.args and isinstance(dec.args[0], ast.Constant)
            and isinstance(dec.args[0].value, str)):
        return dec.func.attr, dec.args[0].value
    return None


METODOS = METODOS | {"api_route"}


def usa_app(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "app" and isinstance(n.ctx, ast.Load):
            return True
    return False


DOMINIOS = [
    ("auth", ("/api/auth/",)),
    ("paginas", ("/biblioteca", "/dashboard", "/sistema", "/entrar", "/sair", "/revisao")),
    ("chat", ("/hx/chat", "/hx/nova", "/hx/conversa", "/hx/conversas", "/hx/voz", "/hx/tts",
              "/hx/contagem", "/hx/jobsbar", "/hx/prompt-melhorar", "/hx/prompt-midia",
              "/api/query", "/api/visao", "/api/anexo")),
    ("sandbox", ("/api/sandbox", "/sandbox/ver")),
    ("midia", ("/api/midia", "/api/tarefas", "/api/modalidades", "/api/fluxos",
               "/api/estudio", "/api/upload", "/api/zip", "/hx/midia")),
    ("biblioteca", ("/hx/aquisicao", "/hx/colecao", "/hx/histlog", "/hx/resolucao",
                    "/hx/recategorizar", "/hx/enriquecer", "/hx/revisao", "/hx/job/",
                    "/api/ingest", "/api/hf", "/api/collections", "/api/manutencao",
                    "/api/higienizar", "/api/limpeza", "/api/seed", "/api/varredura",
                    "/api/pesquisa", "/api/snapshot", "/api/docs")),
    ("sistema", ("/api/settings", "/hx/settings", "/api/parar_tudo", "/hx/parar-tudo",
                 "/api/llm", "/api/embed", "/api/vl", "/api/gpu", "/api/models",
                 "/api/modelo", "/api/status", "/api/specs")),
    ("provedores", ("/api/provedores",)),
    ("agentico", ("/api/sessions", "/api/mcp")),
    ("telemetria", ("/api/contagem", "/api/historico", "/hx/logs", "/api/telemetria", "/api/logs")),
    ("voz", ("/api/voz",)),
]


def dominio_de(path):
    if path == "/" or path.startswith("/c/"):
        return "paginas"
    if path.startswith("/midia"):
        return "paginas"          # /midia e /midia/{sid} são páginas
    if path.startswith("/sandbox"):
        return "sandbox"          # páginas do subdomínio + rotas de arquivo
    for nome, prefixos in DOMINIOS:
        for p in prefixos:
            if path.startswith(p):
                return nome
    return None


rotas_por_dominio = {n: [] for n, _ in DOMINIOS}
shared, app_units, rotas_status = [], [], []
relatorio, problemas = [], []

for u in units:
    node = u["node"]
    texto = "".join(lines[u["start"]:u["end"]])
    has_dec = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list
    if has_dec:
        infos = [decor_info(d) for d in node.decorator_list]
        rota = [i for i in infos if i and i[0] in METODOS]
        if rota:
            doms = {dominio_de(p) for _, p in rota}
            if len(doms) > 1:
                problemas.append(f"rota multi-dominio: {node.name} {rota} -> {doms}")
            dom = doms.pop()
            if dom is None:
                problemas.append(f"SEM DOMINIO: {node.name} {rota}")
                continue
            rotas_por_dominio[dom].append((u, rota, texto, node.name))
            relatorio.append((node.name, rota[0][0].upper(), rota[0][1], dom))
            continue
        mid = [i for i in infos if i and i[0] == "middleware"]
        if mid:
            u["papel"] = "middleware"
            shared.append(u)
            continue
        if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and d.func.attr == "on_event" for d in node.decorator_list):
            u["papel"] = "startup"
            shared.append(u)
            continue
        problemas.append(f"decorator desconhecido em {node.name}")
        shared.append(u)
        continue
    # Expr de chamada _rota_status(...) -> router jobs
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "_rota_status":
        rotas_status.append((u, texto))
        continue
    # _rota_status (def) vai junto das chamadas no router jobs — não é shared
    if isinstance(node, ast.FunctionDef) and node.name == "_rota_status":
        continue
    # assigns/exprs do objeto app -> descartados (app.py novo é escrito à mão)
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "app" for t in node.targets):
        continue
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
            and isinstance(node.value.func, ast.Attribute) \
            and isinstance(node.value.func.value, ast.Name) \
            and node.value.func.value.id == "app" \
            and node.value.func.attr in ("add_middleware", "mount"):
        continue
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        u["papel"] = "import"
        shared.append(u)
        continue
    # JobRegistry e co -> core/jobs.py
    alvo = None
    if isinstance(node, ast.ClassDef) and node.name == "JobRegistry":
        alvo = "core"
    elif isinstance(node, (ast.Assign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id in ("_podar_concluidos",
                                                "PASTA_LOGS_JOBS",
                                                "TODOS_JOBS") for t in
            (node.targets if isinstance(node, ast.Assign) else [node.target])):
        alvo = "core"
    elif isinstance(node, ast.FunctionDef) and node.name in ("_novo_job", "_podar_concluidos"):
        alvo = "core"
    if alvo == "core":
        u["core"] = True
        shared.append(u)
        continue
    if usa_app(node):
        problemas.append(f"SHARED usa 'app': {getattr(node, 'name', node)} "
                         f"(linhas {u['start']+1}-{u['end']})")
    shared.append(u)

# ---------- 3) emitir core/jobs.py ----------
core_units = [u for u in shared if u.get("core")]
if len(core_units) != 5:
    problemas.append(f"core/jobs.py esperava 5 unidades, achei {len(core_units)}")

core_src = '"""Registry de jobs — domínio de execução (camada core).\n\n' \
    'Extraído de api/app.py no split Fase 1 (28/08): fila/log/estado de job é\n' \
    'domínio, não camada de API. Sem dependência de FastAPI.\n"""\n' \
    'import json\nimport os\nimport re\nimport threading\nimport time\nfrom itertools import count\nfrom pathlib import Path\n\n' \
    'PASTA_LOGS_JOBS = Path("logs/jobs")\n\n\n'
for u in sorted(core_units, key=lambda x: x["start"]):
    core_src += "".join(lines[u["start"]:u["end"]]) + "\n\n"
DEST_CORE.write_text(core_src, encoding="utf-8")

# ---------- 4) emitir api/base.py ----------
imports_core = ("\n# domínio de jobs (extraído no split — re-export p/ compat)\n"
                "from core.jobs import PASTA_LOGS_JOBS, JobRegistry, TODOS_JOBS, _novo_job, _podar_concluidos  # noqa: F401\n")

base_parts = ['"""Infraestrutura compartilhada da API (helpers, templates, estado).\n\n'
              'Gerado no split Fase 1: TUDO que não é rota vive aqui; os routers\n'
              'fazem `from api.base import *` (contrato: __all__ explícito abaixo).\n'
              'Mudança de comportamento NÃO acontece aqui sem prova de paridade\n'
              '(docs/arquitetura.md).\n"""\n']
nomes_all = ["PASTA_LOGS_JOBS", "JobRegistry", "_novo_job", "_podar_concluidos"]
for u in shared:
    if u.get("core"):
        continue
    node = u["node"]
    texto = "".join(lines[u["start"]:u["end"]])
    if u.get("papel") == "import":
        base_parts.append(texto)
        if isinstance(node, ast.Import):
            nomes_all += [(a.asname or a.name.split(".")[0]) for a in node.names]
        else:
            nomes_all += [(a.asname or a.name) for a in node.names]
imports_feitos = False
for u in shared:
    if u.get("core") or u.get("papel") == "import":
        continue
    if not imports_feitos:
        base_parts.append(imports_core)
        imports_feitos = True
    node = u["node"]
    texto = "".join(lines[u["start"]:u["end"]])
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
        # middleware/startup: função em base SEM o decorator (o registro
        # acontece no app.py de composição — mesmo efeito)
        texto = "".join(lines[node.lineno - 1:u["end"]])
        base_parts.append(texto + "\n\n")
        nomes_all.append(node.name)
        continue
    base_parts.append(texto + "\n\n")
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                nomes_all.append(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        nomes_all.append(node.target.id)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        nomes_all.append(node.name)

# __all__ após os imports (inserido na posição certa): reconstruir de forma simples
all_block = "\n__all__ = [\n" + "".join(f'    "{n}",\n' for n in dict.fromkeys(nomes_all)) + "]\n"
base_src = base_parts[0] + "".join(p for p in base_parts[1:] if p.startswith(("import ", "from ")))
resto = "".join(p for p in base_parts[1:] if not p.startswith(("import ", "from ")))
DEST_BASE.write_text(base_src + all_block + "\n\n" + resto, encoding="utf-8")

# ---------- 5) emitir routers ----------
DEST_ROUTERS.mkdir(exist_ok=True)
ordem_primeira_rota = {}
for nome, _ in DOMINIOS:
    itens = rotas_por_dominio[nome]
    if not itens:
        continue
    corpo = [f'"""Rotas de {nome} — extraídas mecanicamente de api/app.py (split Fase 1).\n'
             f'Ordem interna preservada; decorator @app -> @router.\n"""\n'
             "from api.base import *  # noqa: F401,F403 — contrato do split\n\n"
             "from fastapi import APIRouter\n\nrouter = APIRouter()\n"]
    for u, rota, texto, fname in itens:
        novo = re.sub(r"@app\.", "@router.", texto)
        corpo.append(novo + "\n\n")
        gidx = units.index(u)
        ordem_primeira_rota.setdefault(nome, []).append((gidx, rota[0][1]))
    (DEST_ROUTERS / f"{nome}.py").write_text("".join(corpo), encoding="utf-8")

# router jobs: _rota_status + as 12 chamadas
jobs_src = ['"""Rotas de status das famílias de job — extraídas de api/app.py (split Fase 1).\n'
            '_rota_status registra no router local (antes registrava no app; mesmo efeito).\n"""\n'
            "from api.base import *  # noqa: F401,F403 — contrato do split\n\n"
            "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n"]
u_rs = next(u for u in units if isinstance(u["node"], ast.FunctionDef) and u["node"].name == "_rota_status")
jobs_src += "".join(lines[u_rs["start"]:u_rs["end"]]).replace("app.get", "router.get") + "\n\n"
for u, texto in rotas_status:
    jobs_src += texto + "\n"
(DEST_ROUTERS / "jobs.py").write_text("".join(jobs_src), encoding="utf-8")

# __init__ na ordem da primeira rota global
seq = sorted(((min(i for i, _ in v), nome) for nome, v in ordem_primeira_rota.items()))
jobs_first = next((i for i, u in enumerate(units)
                   if isinstance(u["node"], ast.Expr)), None)
init = ['"""Routers da API — ordem de include = ordem da 1ª rota no app.py original\n'
        '(paridade de matching preservada; ver docs/arquitetura.md).\n"""\n']
for _, nome in seq:
    init.append(f"from . import {nome}  # noqa: F401\n")
init.append("from . import jobs  # noqa: F401 — status das famílias (paths únicos)\n\n")
init.append("# ordem de include: 1ª rota de cada router no arquivo original\n")
init.append("ORDENADOS = [" + ", ".join(nome for _, nome in seq) + ", \"jobs\"]\n")
(DEST_ROUTERS / "__init__.py").write_text("".join(init), encoding="utf-8")

# ---------- 6) relatório ----------
rotas = [(m, p, d, n) for n, m, p, d in relatorio]
print(f"ROTAS EXTRAIDAS: {len(rotas)} | _rota_status: {len(rotas_status)} | "
      f"shared units: {len([u for u in shared if not u.get('core')])} | "
      f"core units: {len(core_units)} | app_units(middleware/startup): {len(app_units)}")
for nome, itens in rotas_por_dominio.items():
    if itens:
        print(f"  {nome:12s} {len(itens):3d} rota(s)  1a: {itens[0][1][0][1]}")
print("ORDEM INCLUDE:", [n for _, n in seq])
print("APP_UNITS:", [(u['papel'], u['node'].name) for u in app_units])
if problemas:
    print("\n*** PROBLEMAS ***")
    for p in problemas:
        print(" -", p)
    sys.exit(1)
print("\nOK — arquivos gerados em api/base.py, api/routers/, core/jobs.py")
