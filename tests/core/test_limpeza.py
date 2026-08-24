"""CORE · limpeza — o pipeline de texto (usado por ingest/seed/preview).

Cobertura: normalização, remoção de infobox de wiki, detecção de lixo.
Determinístico: nenhuma rede, nenhuma LLM.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.limpeza import e_lixo, limpar_texto, _remover_tabelas_inuteis  # noqa: E402


class TestRemoverTabelasInuteis:
    def test_infobox_wiki_sai(self):
        """Infobox com células vazias/rótulos curtos é removida."""
        infobox = (
            "| *Theobroma cacao* | |\n"
            "| Kingdom: | Plantae |\n"
            "| *Clade* : | Embryophytes |\n"
            "| Order: | Malvales |\n"
            "| Family: | Malvaceae |\n"
            "| Genus: | *Theobroma* |\n"
            "| Binomial name | |\n")
        assert _remover_tabelas_inuteis(infobox).strip() == ""

    def test_tabela_de_dado_fica(self):
        """Tabela com conteúdo real (docs/comandos) sobrevive."""
        dado = ("| comando | o que faz |\n"
                "| dotnet build | compila a solucao completa |\n"
                "| dotnet run | executa o projeto |")
        assert _remover_tabelas_inuteis(dado) == dado

    def test_texto_sem_tabela_intacto(self):
        t = "paragrafo comum\nsem tabela nenhuma"
        assert _remover_tabelas_inuteis(t) == t


class TestLimparTexto:
    def test_idempotente(self):
        """Limpar 2x == limpar 1x (contrato do pipeline)."""
        t = "texto comum de documento.\n" * 5
        assert limpar_texto(limpar_texto(t)) == limpar_texto(t)

    def test_vazio(self):
        assert limpar_texto("") == ""

    def test_remove_marcacoes_web(self):
        t = "[editar] Conteúdo real do documento " + "com palavras suficientes " * 8
        limpo = limpar_texto(t)
        assert "[editar]" not in limpo


class TestELixo:
    def test_curto_demais_e_lixo(self):
        assert e_lixo("muito curto") is True

    def test_prosa_real_nao_e_lixo(self):
        prosa = ("A feijoada e um prato tipico da culinaria brasileira feito com "
                 "feijao preto e varias carnes de porco, servida com arroz, "
                 "couve, laranja e farofa. Existem varias receitas regionais "
                 "com ingredientes diferentes ao longo do pais, cada estado "
                 "com sua variacao tradicional passada de geracao em geracao.")
        assert e_lixo(prosa) is False

    def test_menu_indice_e_lixo(self):
        menu = " ".join(f"Item Menu {i}" for i in range(40))
        assert e_lixo(menu) is True
