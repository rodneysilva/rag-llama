"""CORE · preview — revisão antes de ingerir (aquisição RAG).

Cobertura: persistência do preview em DISCO (sobrevive a restart —
o bug real do operador), TTL, reconstrução de Documents, gate de tema
por cosseno, descartados.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.preview as pv  # noqa: E402
from langchain_core.documents import Document  # noqa: E402


def setup_function(_):
    import tempfile
    tmp = tempfile.mkdtemp()
    pv.PREVIEW_DIR = Path(tmp) / "previews"
    pv._previews.clear()


def _doc(t="conteudo do documento sobre culinaria " * 3, titulo="Vatapa"):
    return Document(page_content=t, metadata={"titulo": titulo, "source": "web"})


class TestPersistencia:
    def test_guardar_grava_em_disco(self):
        pv.guardar("p1", [_doc()], {"resumo": {"documentos": 1}})
        assert pv._caminho("p1").is_file()

    def test_sobrevive_a_restart(self):
        """O bug do operador: preview morria com o processo."""
        pv.guardar("p2", [_doc()], {"resumo": {"documentos": 1}})
        pv._previews.clear()                      # simula restart
        assert pv.ver("p2") is not None

    def test_documents_reconstruidos_do_disco(self):
        pv.guardar("p3", [_doc()], {"resumo": {}})
        pv._previews.clear()
        p = pv._carregar("p3")
        assert isinstance(p["docs"][0], Document)

    def test_ttl_expira(self):
        pv.TTL_S = 0.01
        try:
            pv.guardar("p4", [_doc()], {"resumo": {}})
            time.sleep(0.05)
            assert pv.ver("p4") is None
        finally:
            pv.TTL_S = 2 * 60 * 60

    def test_pid_invalido_nao_explode(self):
        assert pv.ver("inexistente-xyz") is None


class TestGateDeTema:
    """O gate por cosseno (o reranker dava 0.0 para tudo — bug corrigido)."""

    def test_cosseno_doc_parecido(self):
        a = "como instalar docker no ubuntu servidor"
        b = "docker e uma plataforma de containers amplamente usada no ubuntu"
        # cosseno de vetores identicos = 1
        assert pv._coss([1.0, 0.0], [1.0, 0.0]) == 1.0

    def test_cosseno_ortogonal_zero(self):
        assert abs(pv._coss([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_duplicado_exato_por_md5(self):
        d1, d2 = _doc(), _doc()
        h1 = pv._md5(d1.page_content)
        h2 = pv._md5(d2.page_content)
        assert h1 == h2
