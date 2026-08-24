"""
Especificações do sistema em arquivos fixos (core/specs/*.md).

O comportamento da ingestão, do chat, da categorização e da análise NÃO fica
hardcoded no código: as instruções vêm destes arquivos. Eles também são
registrados no catálogo (coleção meta_colecoes do Qdrant) como documentos
RAG — dá para perguntar no chat como o sistema funciona.
"""
from functools import lru_cache
from pathlib import Path

SPECS_DIR = Path(__file__).resolve().parent / "specs"


@lru_cache
def spec(name: str) -> str:
    """Conteúdo de uma spec (ex.: spec('chat')). Leitura tolerante: byte
    inválido é substituído — uma spec salva em encoding errado NUNCA derruba
    o chat (já aconteceu: 'utf-8 codec can't decode 0xf3' virou resposta)."""
    return (SPECS_DIR / f"{name}.md").read_text(encoding="utf-8",
                                                errors="replace")


def recarregar() -> int:
    """Derruba o cache das specs (editar spec passa a valer SEM restart da
    API). Devolve o nº de specs que estavam em cache."""
    n = spec.cache_info().currsize
    spec.cache_clear()
    return n


def all_specs() -> dict[str, str]:
    """Todas as specs: {nome_do_arquivo: conteúdo}."""
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(SPECS_DIR.glob("*.md"))}
