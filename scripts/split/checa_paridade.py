# -*- coding: utf-8 -*-
"""Prova de paridade do split: para CADA (method, path) da tabela ANTES,
resolve qual endpoint atende no app VELHO e no app NOVO (mesma semântica do
Starlette: primeira rota que dá Match.FULL). Falha se qualquer path resolver
para endpoint diferente.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.routing import _IncludedRouter  # noqa: E402
from starlette.routing import Match  # noqa: E402


def carregar(nome, arquivo):
    spec = importlib.util.spec_from_file_location(nome, arquivo)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[nome] = mod
    spec.loader.exec_module(mod)
    return mod


def escopo(method, path):
    return {"type": "http", "method": method, "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": []}


def achatar(rotas):
    """Descobre rotas dentro de wrappers _IncludedRouter (FastAPI 0.141)."""
    out = []
    for r in rotas:
        if isinstance(r, _IncludedRouter):
            inner = getattr(r, "original_router", None)
            if inner is not None:
                out.extend(achatar(inner.routes))
            continue
        out.append(r)
    return out


def resolver(app, method, path):
    """Nome do endpoint que atende (method, path) — ou None."""
    sc = escopo(method, path)
    parcial = None
    for r in achatar(app.routes):
        try:
            m, _ = r.matches(sc)
        except Exception:
            continue
        if m is Match.FULL:
            ep = getattr(r, "endpoint", None)
            return getattr(ep, "__name__", type(r).__name__)
        if m is Match.PARTIAL and parcial is None:
            ep = getattr(r, "endpoint", None)
            parcial = getattr(ep, "__name__", type(r).__name__)
    return parcial  # 405 se None


velho = carregar("app_velho", "Temp/app_original.py")
import api.app as novo_mod  # noqa: E402

tabela = []
for linha in open("Temp/rotas_antes.txt", encoding="utf-8"):
    m, p = linha.strip().split(" ", 1)
    for metodo in m.split(","):
        tabela.append((metodo, p))

dif = 0
for metodo, path in tabela:
    a = resolver(velho.app, metodo, path)
    b = resolver(novo_mod.app, metodo, path)
    if a != b:
        dif += 1
        print(f"DIVERGE {metodo} {path}: velho={a} novo={b}")

print(f"{len(tabela)} combinações (method,path) testadas | {dif} divergências")
sys.exit(1 if dif else 0)
