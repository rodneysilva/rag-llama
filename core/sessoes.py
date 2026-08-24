"""
Sessões do estúdio: agrupam as mídias geradas (imagens E vídeos) por
trabalho, persistidas em saidas/estudio_sessoes.json — sobrevivem a
reinícios da API e podem ser importadas como entrada de novas tarefas.
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO = RAIZ / "saidas" / "estudio_sessoes.json"
_trava = threading.Lock()


def _carregar() -> dict:
    try:
        dados = json.loads(ARQUIVO.read_text(encoding="utf-8"))
        if isinstance(dados.get("sessoes"), list) and dados["sessoes"]:
            return dados
    except Exception:
        pass
    return {"sessoes": [{"id": "s_principal", "nome": "Principal",
                         "criada": datetime.now().strftime("%d/%m %H:%M"),
                         "midias": []}]}


def _salvar(dados: dict) -> None:
    ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=1),
                       encoding="utf-8")


def listar(owner: str = "") -> list[dict]:
    """Sessões com suas mídias (do `owner` quando informado), recentes primeiro."""
    with _trava:
        return list(reversed([s for s in _carregar()["sessoes"]
                              if not owner or s.get("owner") == owner]))


def criar(nome: str, owner: str = "") -> dict:
    with _trava:
        dados = _carregar()
        s = {"id": "s_" + uuid.uuid4().hex[:8],
             "nome": nome.strip()[:60] or "sessão",
             "owner": owner,
             "criada": datetime.now().strftime("%d/%m %H:%M"), "midias": []}
        dados["sessoes"].append(s)
        _salvar(dados)
        return s


def obter(sessao: str) -> dict | None:
    with _trava:
        return next((s for s in _carregar()["sessoes"] if s["id"] == sessao), None)


def principal(owner: str) -> str:
    """Id da sessão 'Principal' DO DONO (cria se não existir). Usada quando
    uma tarefa vem sem sessão: antes caía na s_principal global (sem owner)
    e a mídia ficava INVISÍVEL para o usuário na listagem."""
    with _trava:
        dados = _carregar()
        s = next((x for x in dados["sessoes"]
                  if x.get("owner") == owner and x["nome"].lower() == "principal"), None)
        if s is None:
            s = {"id": "s_" + uuid.uuid4().hex[:8], "nome": "Principal",
                 "owner": owner,
                 "criada": datetime.now().strftime("%d/%m %H:%M"), "midias": []}
            dados["sessoes"].append(s)
            _salvar(dados)
        return s["id"]


def registrar(sessao: str, midia: dict) -> None:
    """Anexa a mídia gerada (dict com arquivo/pasta/tipo/prompt…) à sessão.

    A ref 'pasta\\arquivo' é o formato que o _resolver_arquivo da API aceita
    como entrada de novas tarefas (importação entre sessões)."""
    if not midia or not midia.get("arquivo"):
        return
    pasta = (Path(midia.get("pasta", "")).name or "imagens").lower()
    if pasta.startswith("video"):
        pasta = "videos"
    elif pasta.startswith("audio"):
        pasta = "audios"
    else:
        pasta = "imagens"
    item = {"ref": f"{pasta}\\{midia['arquivo']}",
            "tipo": midia.get("tipo", "imagem"),
            "modalidade": midia.get("modalidade", ""),
            "prompt": (midia.get("prompt") or "")[:200],
            "segundos": midia.get("segundos"),
            "quando": datetime.now().strftime("%d/%m %H:%M")}
    with _trava:
        dados = _carregar()
        s = next((x for x in dados["sessoes"] if x["id"] == sessao), None)
        if s is None:  # sessão avulsa (ex.: id de teste) → cria nominal
            s = {"id": sessao if sessao.startswith("s_") else "s_" + uuid.uuid4().hex[:8],
                 "nome": (sessao.capitalize()[:30] if sessao else "Sessão"),
                 "criada": item["quando"], "midias": []}
            dados["sessoes"].append(s)
        s["midias"].append(item)
        _salvar(dados)


def renomear(sessao: str, nome: str) -> dict | None:
    with _trava:
        dados = _carregar()
        s = next((x for x in dados["sessoes"] if x["id"] == sessao), None)
        if not s:
            return None
        s["nome"] = nome.strip()[:60] or s["nome"]
        _salvar(dados)
        return s


def apagar(sessao: str) -> bool:
    """Remove a SESSÃO do registro (as mídias em saidas/ continuam no disco)."""
    with _trava:
        dados = _carregar()
        antes = len(dados["sessoes"])
        dados["sessoes"] = [s for s in dados["sessoes"] if s["id"] != sessao]
        if len(dados["sessoes"]) == antes:
            return False
        if not dados["sessoes"]:  # nunca deixa vazio
            dados["sessoes"].append(
                {"id": "s_principal", "nome": "Principal",
                 "criada": datetime.now().strftime("%d/%m %H:%M"), "midias": []})
        _salvar(dados)
        return True
