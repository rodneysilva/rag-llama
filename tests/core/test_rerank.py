"""CORE · rerank — refinamento da busca com cross-encoder local.

Cobertura: SINAL antes de reordenar (notas ínfimas = manter ordem
vetorial), notas relativas corretas, degradação silenciosa.
Requer torch+transformers: pula limpo quando ausente (container dev).
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import rerank as rk  # noqa: E402
from langchain_core.documents import Document  # noqa: E402


def _achado(txt):
    return (Document(page_content=txt, metadata={}), 0.5, "py")


@pytest.mark.skipif(not rk.disponivel(), reason="torch/transformers ausentes")
class TestRerankSinal:
    def test_sinal_claro_ordena(self):
        linhas = []
        rr = rk.rerank("how to create a python api with fastapi",
                       [_achado("feijoada recipe with beans"),
                        _achado("FastAPI is a modern Python web framework "
                                "for building APIs")],
                       log=lambda m, g="b": linhas.append(m))
        assert rr is not None
        ordenados, topo = rr
        assert topo >= rk.SINAL_MIN
        assert "FastAPI" in ordenados[0][0].page_content

    def test_sem_sinal_mantem_ordem_vetorial(self):
        """O bug real: top ~0.00 reordenava por ruído (idiomas mistos)."""
        linhas = []
        rr = rk.rerank("falar sobre",
                       [_achado("import os" + " " * 2500),
                        _achado("def x(): pass" + " " * 2500)],
                       log=lambda m, g="b": linhas.append(m))
        assert rr is None                       # chamador mantém a busca
        assert any("inconclusivo" in l for l in linhas)

    def test_notas_relacionam_bem(self):
        n = rk.notas_de("python fastapi api",
                        ["FastAPI framework for Python APIs",
                         "rice and beans recipe"])
        assert n[0] > n[1]                      # ordem relativa correta


class TestContracto:
    def test_sinal_min_existe_e_e_pequeno(self):
        assert 0 < rk.SINAL_MIN < 0.10
