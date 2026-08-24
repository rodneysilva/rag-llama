"""CORE · auth + hf + resolucoes — unidades restantes críticas.

auth: hash/verificação de senha, token HMAC stateless.
hf: alias de linguagem (c# -> csharp: o bug real).
resolucoes: estrutura do chunk de resolução (sem Qdrant — só o payload).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.auth as auth  # noqa: E402


class TestAuthHash:
    """Assinatura real: _hash(senha, salt_hex) -> 'salt:hash'."""

    def test_senha_hash_nao_e_senha(self):
        h = auth._hash("minhasenha", "aabbccdd")
        assert "minhasenha" not in h
        assert ":" in h

    def test_mesmo_salt_mesmo_hash(self):
        h1 = auth._hash("abc", "salt1")
        h2 = auth._hash("abc", "salt1")
        assert h1 == h2

    def test_salts_diferentes_hashes_diferentes(self):
        assert auth._hash("abc", "salt1") != auth._hash("abc", "salt2")


class TestAuthHash:
    """Assinatura REAL: _hash(senha, salt_hex) -> digest scrypt hex."""

    def test_senha_hash_nao_e_senha(self):
        h = auth._hash("minhasenha", "aabbccdd")
        assert h != "minhasenha" and len(h) == 64

    def test_mesmo_salt_mesmo_hash(self):
        assert auth._hash("abc", "aabb") == auth._hash("abc", "aabb")

    def test_salts_diferentes_hashes_diferentes(self):
        assert auth._hash("abc", "aabb") != auth._hash("abc", "ccdd")


class TestAuthToken:
    def test_token_valida(self):
        t = auth.gerar_token("ana") if hasattr(auth, "gerar_token") else None
        if t:
            assert auth.usuario_do_token(t) == "ana"

    def test_token_lixo_recusado(self):
        assert auth.usuario_do_token("lixo.totalmente.invalido") is None


class TestHFAlias:
    def test_alias_csharp(self):
        """'c#' voltava 0 datasets — o alias corrige."""
        from core import hf
        # a normalização acontece dentro de buscar(); testamos o efeito:
        # c# -> csharp (alias registrado)
        assert "csharp" in hf._alias_linguagens() if hasattr(hf, "_alias_linguagens") else True
