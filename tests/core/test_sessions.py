"""CORE · sessions — persistência das conversas (o bug do histórico).

Cobertura: salvar/carregar/listar/apagar, isolamento por owner, upsert
idempotente, raw completo (tokens/raciocínio) na reabertura.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.sessions as sessions  # noqa: E402


def setup_function(_):
    """Cada teste roda numa pasta de sessões temporária isolada."""
    import tempfile
    sessions._TMP = tempfile.mkdtemp()
    sessions.SESSIONS_DIR = Path(sessions._TMP)


class TestSalvarCarregar:
    def test_salvar_cria_e_carregar_devolve(self):
        r = sessions.save_session(
            [], owner="ana", raw=[{"role": "user", "content": "oi"},
                              {"role": "assistant", "content": "ola!"}])
        sid = r["id"]
        d = sessions.get_session(sid)
        assert d["raw"][0]["content"] == "oi"
        assert d["owner"] == "ana"

    def test_titulo_deriva_da_primeira_pergunta(self):
        r = sessions.save_session(
            [{"role": "user", "content": "como fazer feijoada completa"}],
            owner="ana")
        assert "feijoada" in sessions.get_session(r["id"])["titulo"]

    def test_upsert_mesmo_sid_nao_duplica(self):
        r1 = sessions.save_session([], owner="ana", raw=[{"role": "user", "content": "a"}])
        sid = r1["id"]
        sessions.save_session(
            [{"role": "user", "content": "a"},
             {"role": "assistant", "content": "b"}],
            sid=sid, owner="ana",
            raw=[{"role": "user", "content": "a"},
                 {"role": "assistant", "content": "b"}])
        d = sessions.get_session(sid)
        assert len(d["raw"]) == 2

    def test_raw_completo_persiste(self):
        """Tokens e raciocínio voltam ao reabrir (o requisito do operador)."""
        r = sessions.save_session([], owner="ana", raw=[
            {"role": "assistant", "content": "resp",
             "tokens": {"entrada": 10, "saida": 5, "chamadas": 1},
             "pensamentos": [{"ts": "10:00", "msg": "busca"}]}])
        m = sessions.get_session(r["id"])["raw"][-1]
        assert m["tokens"]["saida"] == 5
        assert m["pensamentos"][0]["msg"] == "busca"


class TestListar:
    def test_owner_isola(self):
        a = sessions.save_session([], owner="ana", raw=[{"role": "user", "content": "x"}])
        b = sessions.save_session([], owner="bruno", raw=[{"role": "user", "content": "y"}])
        lista_ana = sessions.list_sessions(owner="ana")
        assert a["id"] in [s["id"] for s in lista_ana]
        assert b["id"] not in [s["id"] for s in lista_ana]

    def test_mais_recente_primeiro(self):
        sessions.save_session([], owner="ana", raw=[{"role": "user", "content": "velha"}])
        import time
        time.sleep(1.1)   # _agora() tem precisao de SEGUNDOS
        nova = sessions.save_session([], owner="ana",
                                     raw=[{"role": "user", "content": "nova"}])
        primeira = sessions.list_sessions(owner="ana")[0]
        assert primeira["id"] == nova["id"]


class TestApagar:
    def test_apagar_remove(self):
        r = sessions.save_session([], owner="ana", raw=[{"role": "user", "content": "x"}])
        assert sessions.delete_session(r["id"]) is True
        assert sessions.get_session(r["id"]) is None

    def test_apagar_inexistente(self):
        assert sessions.delete_session("nao-existe-123") is False
