# -*- coding: utf-8 -*-
"""Costura imports cross-router: função-definida-como-rota em um router e
chamada como função por outro (no monólito o namespace global cobria).
Detecta por AST e injeta `from api.routers.X import nome` no topo.
"""
import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / "api" / "routers"

# coleta definições por router
defs = {}   # nome -> router
for f in sorted(PASTA.glob("*.py")):
    if f.name == "__init__.py":
        continue
    tree = ast.parse(f.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = f.stem
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defs[t.id] = f.stem

# nomes disponíveis no base.py (star import cobre)
base_tree = ast.parse((RAIZ / "api" / "base.py").read_text(encoding="utf-8"))
base_names = set()
for node in base_tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        base_names.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                base_names.add(t.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        base_names.add(node.target.id)
    elif isinstance(node, ast.Import):
        for a in node.names:
            base_names.add(a.asname or a.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for a in node.names:
            base_names.add(a.asname or a.name)

imports_std = {"router", "APIRouter"}
mudancas = 0
for f in sorted(PASTA.glob("*.py")):
    if f.name == "__init__.py":
        continue
    src = f.read_text(encoding="utf-8")
    tree = ast.parse(src)
    locais = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            locais.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    locais.add(t.id)
    usados = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            usados.add(node.id)
    faltantes = usados - locais - base_names - imports_std
    # só interessa os que SÃO rotas de outro router
    cross = sorted(n for n in faltantes if n in defs and defs[n] != f.stem)
    if cross:
        por_router = {}
        for n in cross:
            por_router.setdefault(defs[n], []).append(n)
        linhas_imp = []
        for r in sorted(por_router):
            nomes = ", ".join(sorted(set(por_router[r])))
            linhas_imp.append(f"from api.routers.{r} import {nomes}  # noqa: F401 — chamada cross-router (era namespace global do monólito)")
        # injeta depois do bloco de imports inicial
        novo = re.sub(r'(from api\.base import \*[^\n]*\n)',
                      r"\1" + "\n".join(linhas_imp) + "\n", src, count=1)
        if novo != src:
            f.write_text(novo, encoding="utf-8")
            mudancas += 1
            print(f"{f.stem}: {len(cross)} cross-import(s) -> {list(por_router.values())}")

print("routers alterados:", mudancas)
