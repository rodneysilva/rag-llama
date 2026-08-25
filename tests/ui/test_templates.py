"""UI · templates — render real com contexto e contratos do DOM.

Valida: Jinja parseia, elementos-chave existem no HTML renderizado,
JS dos blocos <script> tem sintaxe válida (node --check).
"""
import re
import pytest
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RAIZ = Path(__file__).resolve().parents[2]
TEMPLATES = RAIZ / "templates"
ENV_DIR = RAIZ.parent  # para o markdown do app


def _env():
    from jinja2 import Environment, FileSystemLoader
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)


import shutil

def _js_valido(codigo: str) -> bool:
    if not shutil.which('node'):
        pytest.skip('node ausente: rode a suite de UI no host')

    p = RAIZ / "_js_check.js"
    p.write_text(codigo, encoding="utf-8")
    try:
        r = subprocess.run(["node", "--check", str(p)],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    finally:
        p.unlink(missing_ok=True)


class TestTemplatesParseiam:
    def test_todos_os_templates(self):
        env = _env()
        for t in env.list_templates():
            if t.endswith(".html"):
                env.get_template(t)  # explode se não parsear


class TestChatRender:
    CTX = {"aba": "chat", "usuario": "teste", "admin": False,
           "mensagens": [{"role": "user", "content": "oi"}],
           "colecoes": [{"nome": "culinaria", "points": 10}],
           "cache": {"online": True, "entradas": 3},
           "modelos_chat": [{"nome": "qwen", "gb": 4.7, "ativo": True}],
           "modelos_chat_grupos": [],
           # seletor de GERAÇÃO do composer (imagem/vídeo/gif — fluxo F1b):
           # dicionário por tipo; vazio esconde os optgroups
           "modelos_geracao": {"imagem": [], "video": []},
           "mcps": []}

    def test_composer_completo(self):
        html = _env().get_template("chat.html").render(**self.CTX)
        for alvo in ('id="pergunta"', 'id="btn-mic"',
                     'class="primario enviar-btn"', 'name="mode"',
                     'name="model"', 'name="colecoes"'):
            assert alvo in html, f"falta {alvo}"

    def test_colecoes_abertas_por_padrao(self):
        html = _env().get_template("chat.html").render(**self.CTX)
        assert 'id="colecoes-box" open' in html

    def test_mensagens_da_sessao_renderizam(self):
        html = _env().get_template("_palco.html").render(
            mensagens=[{"role": "user", "content": "pergunta_x"},
                       {"role": "assistant", "content": "resposta_y",
                        "html": "<p>resposta_y</p>"}])
        assert "pergunta_x" in html and "resposta_y" in html

    def test_js_do_chat_valido(self):
        html = _env().get_template("chat.html").render(**self.CTX)
        for n, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S), 1):
            assert _js_valido(js), f"JS bloco {n} com erro de sintaxe"


class TestJobCard:
    def test_polling_e_log_ao_vivo(self):
        # o card do job é o LOG AO VIVO (a barra pbar-job saiu com o log
        # em linhas): valida o polling htmx e a zona de log
        html = _env().get_template("_job.html").render(
            job="x", kind="pesquisa", rotulo="teste",
            linhas=[], running=True)
        assert 'hx-get="/hx/job/pesquisa/x"' in html
        assert "aguardando as primeiras linhas" in html

    def test_erro_aparece_no_card(self):
        html = _env().get_template("_job.html").render(
            job="x", kind="pesquisa", rotulo="t",
            linhas=[], running=False, erro="explodiu")
        assert "explodiu" in html and "falhou" in html


class TestJSDeTodosTemplates:
    """O bug do alternarMic undefined não pode voltar."""

    def test_sintaxe_de_todos_os_blocos(self):
        for arq in TEMPLATES.glob("*.html"):
            t = arq.read_text(encoding="utf-8")
            for n, js in enumerate(re.findall(r"<script>(.*?)</script>", t, re.S), 1):
                assert _js_valido(js), f"{arq.name} bloco {n}: sintaxe inválida"
