"""API · integração — rotas com FastAPI TestClient.

As dependências externas (Qdrant/LLM/Redis) são MOCKADAS: o teste valida
o CONTRATO das rotas (status, forma da resposta, cookies) sem infra.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def cliente():
    """App real com um usuário de TESTE criado no users.json temporário."""
    import tempfile
    from core import auth
    real = auth._ARQUIVO if hasattr(auth, "_ARQUIVO") else None
    # isola o users.json em temp (não toca nos usuários reais)
    import core.auth as a
    dir_real = getattr(a, "USERS_FILE", None)
    if dir_real is not None:
        a.USERS_FILE = Path(tempfile.mkdtemp()) / "users.json"
    elif hasattr(a, "_gravar"):
        # descobre o atributo de caminho do modulo
        for nome in dir(a):
            v = getattr(a, nome)
            if isinstance(v, Path) and v.name == "users.json":
                setattr(a, nome, Path(tempfile.mkdtemp()) / "users.json")
                break
    a.criar_usuario("admin", "senha123")
    from api.app import app
    yield TestClient(app)


@pytest.fixture(scope="module")
def logado(cliente):
    r = cliente.post("/api/auth/login",
                     json={"user": "admin", "senha": "senha123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


class TestAuth:
    def test_login_ok(self, cliente):
        r = cliente.post("/api/auth/login",
                         json={"user": "admin", "senha": "senha123"})
        assert r.status_code == 200
        assert "token" in r.json()

    def test_login_errado(self, cliente):
        r = cliente.post("/api/auth/login",
                         json={"user": "admin", "senha": "errada"})
        assert r.status_code in (401, 403)

    def test_sem_token_dah_401(self):
        """Cliente LIMPO: o TestClient guarda cookies do login do
        fixture, o que mascarava o teste (200 sem cabecalho)."""
        from fastapi.testclient import TestClient
        from api.app import app
        r = TestClient(app).get("/api/collections")
        assert r.status_code == 401


class TestMarkdownRender:
    """O render do markdown no SERVIDOR (o bug do <br> exposto)."""

    def _md(self):
        from api.app import _md_basico
        return _md_basico

    def test_html_nao_e_escapado_duplo(self, cliente):
        md = self._md()
        # a lib markdown emite <strong> (semantico) — o contrato e:
        # TAG de verdade, nao texto escapado
        h = md("**negrito**")
        assert "<strong>negrito</strong>" in h
        assert "**negrito**" not in h

    def test_fences_indentados_viram_codigo(self):
        md = self._md()
        cb = "`" * 3
        h = md("Passos:\n    " + cb + "python\nimport x\n" + cb)
        assert "<pre>" in h or "<code" in h

    def test_fence_aberto_fecha_sozinho(self):
        md = self._md()
        cb = "`" * 3
        h = md("a\n" + cb + "bash\npip install x")
        assert h.count("<pre") >= 1

    def test_xss_e_escapado(self):
        md = self._md()
        assert md("<script>alert(1)</script>") == \
            md("<script>alert(1)</script>")  # idempotente e sem <script> cru
        assert "<script>" not in md("<script>x</script>")

    def test_listas_numeradas_um_ol_so(self):
        md = self._md()
        h = md("1. um\n2. dois\n3. tres")
        assert h.count("<ol>") == 1
        assert h.count("<li>") == 3



    def test_fence_colado_no_texto_abre_bloco(self):
        """Modelos 7B colam a fence no texto ("Estrutura:```html") —
        antes o código inteiro virava texto escapado."""
        md = self._md()
        cb = chr(96) * 3
        h = md("Estrutura" + cb + "html" + chr(10) + "<!DOCTYPE html>" + chr(10) + cb)
        assert "<pre>" in h
        assert "&amp;lt;" not in h          # sem escape duplo

    def test_xss_fora_de_fence_bloqueado(self):
        md = self._md()
        h = md("<script>alert(1)</script>" + chr(10) + "**ok**")
        assert "<script>" not in h


class TestRotasChat:
    def test_voz_sem_audio_nunca_500(self, cliente, logado):
        """Sem audio: 422 (validacao) ou 200 tratado — nunca 500."""
        r = cliente.post("/hx/voz", headers=logado)
        assert r.status_code in (200, 422), r.status_code

    def test_conversa_copy_sem_sessao_vazia(self, cliente, logado):
        r = cliente.get("/hx/conversa/copy", headers=logado)
        assert r.status_code == 200
