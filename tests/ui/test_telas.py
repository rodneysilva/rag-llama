"""UI · telas — testes de browser REAL (Playwright/Chromium headless).

São os testes que pegaram os bugs que validação de código não via
(500 escondido, details fechando, cookie perdido). Requerem a API local
no ar: docker compose up -d api (ou uvicorn).

Rodar:  python -m pytest tests/ui/test_telas.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RAIZ = Path(__file__).resolve().parents[2]
CHROME_CANDIDATOS = list((Path.home() / "AppData" / "Local" / "ms-playwright").glob(
    "chromium-*/chrome-win64/chrome.exe"))


def _env_credenciais():
    env = {}
    for linha in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in linha and not linha.startswith("#"):
            k, v = linha.split("=", 1)
            env[k.strip()] = v
    return env


def _api_viva():
    import httpx
    try:
        return httpx.get("http://127.0.0.1:8000/api/status", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _api_viva() or not CHROME_CANDIDATOS,
    reason="API local fora do ar ou Chromium ausente (docker compose up -d api)")


@pytest.fixture(scope="module")
def pagina():
    from playwright.sync_api import sync_playwright
    env = _env_credenciais()
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(sorted(CHROME_CANDIDATOS)[-1]))
        ctx = b.new_context()
        pg = ctx.new_page()
        pg.goto("http://127.0.0.1:8000/entrar")
        pg.fill('input[name="user"]', env.get("AUTH_ADMIN_USER", "admin").strip())
        pg.fill('input[name="senha"]', env.get("AUTH_ADMIN_PASS", "").strip())
        pg.click('button[type="submit"]')
        pg.wait_for_url("http://127.0.0.1:8000/")
        pg.wait_for_selector("#pergunta")
        yield pg
        b.close()


class TestComposer:
    def test_enter_envia_e_limpa(self, pagina):
        pagina.fill("#pergunta", "teste automatizado enter")
        pagina.press("#pergunta", "Enter")
        pagina.wait_for_timeout(2500)
        assert "teste automatizado enter" in pagina.inner_text("#palco")
        assert pagina.input_value("#pergunta") == ""

    def test_shift_enter_quebra_linha(self, pagina):
        pagina.fill("#pergunta", "l1")
        pagina.press("#pergunta", "Shift+Enter")
        pagina.type("#pergunta", "l2")
        v = pagina.input_value("#pergunta")
        assert "l1" in v and "l2" in v

    def test_colecoes_visiveis(self, pagina):
        assert pagina.eval_on_selector(".colecoes-box", "el => el.open") is True


class TestRaciocinio:
    def test_details_persiste_aberto_entre_swaps(self, pagina):
        """O bug: polling recriava o <details> fechado a cada 600ms."""
        pagina.fill("#pergunta", "ping raciocinio")
        pagina.press("#pergunta", "Enter")
        pagina.wait_for_selector(".raciocinio-vivo", timeout=15000)
        pagina.click(".raciocinio-vivo > summary")
        pagina.wait_for_timeout(2000)          # 3 ciclos de polling
        assert pagina.eval_on_selector(".raciocinio-vivo", "el => el.open") is True


class TestHistorico:
    def test_multiplas_mensagens_mesma_sessao(self, pagina):
        """REGRESSÃO: cada envio criava SESSÃO NOVA (criar=True ignorava o
        cookie válido) — o histórico anterior ficava órfão e o chat 'perdia'
        a conversa. Agora: mesma sessão, tudo acumula, F5 restaura."""
        import json
        from pathlib import Path
        marcadores = [f"reg hist A{__import__('time').strftime('%H%M%S')}",
                      "reg hist B"]
        n0 = pagina.locator("#pensando .msg.assistente").count()
        for n, txt in enumerate(marcadores, 1):
            pagina.fill("#pergunta", txt)
            pagina.press("#pergunta", "Enter")
            for _ in range(70):
                pagina.wait_for_timeout(1000)
                if pagina.locator("#pensando .msg.assistente").count() >= n0 + n:
                    break
        corpo = pagina.inner_text("#palco")
        assert all(m in corpo for m in marcadores)
        sid = next((c["value"] for c in pagina.context.cookies()
                    if c["name"] == "rag_sessao"), None)
        assert sid
        d = json.loads((Path("sessions") / f"{sid}.json").read_text(encoding="utf-8"))
        conteudos = [m["content"] for m in d["raw"]]
        assert all(m in conteudos for m in marcadores), "perguntas fora da sessão"
        pagina.reload()
        pagina.wait_for_selector("#palco")
        c2 = pagina.inner_text("#palco")
        assert all(m in c2 for m in marcadores), "histórico não sobreviveu ao F5"


class TestSemErros:
    def test_zero_erros_de_console(self, pagina):
        erros = []
        pagina.on("console", lambda m: erros.append(m.text) if m.type == "error" else None)
        pagina.reload()
        pagina.wait_for_selector("#pergunta")
        pagina.wait_for_timeout(2000)
        severos = [e for e in erros if "favicon" not in e.lower()]
        assert not severos, f"erros de console: {severos[:3]}"
