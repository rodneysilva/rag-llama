"""UI · telas · CONTEXTO — o histórico precisa chegar à LLM.

Regressão do bug real: a webui HTMX nunca enviava `history` (o React
antigo mandava) — a LLM respondia sem contexto. O fix lê o histórico da
SESSÃO salva no servidor. O teste confere o EFEITO (a 2ª resposta cita
dado da 1ª troca) — não só o envio.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.ui.test_telas import pagina  # noqa: F401  (fixture)


class TestContextoLLM:
    def test_segunda_resposta_usa_a_primeira(self, pagina):
        """O dado dito na troca 1 tem que aparecer na resposta da troca 2."""
        marca = f"contexto{time.strftime('%H%M%S')}"
        pg = pagina

        def salvo(n):
            ck = [c["value"] for c in pg.context.cookies()
                  if c["name"] == "rag_sessao"]
            f = Path("sessions") / f"{ck[0]}.json" if ck else None
            if not f or not f.exists():
                return False
            return len(json.loads(
                f.read_text(encoding="utf-8")).get("raw") or []) >= n

        pg.fill("#pergunta", f"Anote com atenção: minha senha teste é {marca}. "
                             "Confirme apenas: anotado.")
        pg.press("#pergunta", "Enter")
        for _ in range(90):
            pg.wait_for_timeout(1000)
            if salvo(2):
                break
        assert salvo(2), "1ª troca não foi salva (sem resposta)"

        pg.fill("#pergunta", "Repita qual é a minha senha teste.")
        pg.press("#pergunta", "Enter")
        for _ in range(90):
            pg.wait_for_timeout(1000)
            if salvo(4):
                break
        ultima = [m for m in json.loads(
            (Path("sessions") / f"{pg.context.cookies()[-1]['value']}.json"
             ).read_text(encoding="utf-8"))["raw"]
            if m["role"] == "assistant"][-1]["content"].lower()
        assert marca in ultima, (
            f"a 2ª resposta não citou '{marca}' — a LLM respondeu sem "
            "o contexto da sessão (histórico não chegou)")
