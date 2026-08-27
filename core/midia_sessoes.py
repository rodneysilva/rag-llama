"""
Sessões do módulo Multimídia (pedido do dono 27/08: "manter histórico de
sessões também no módulo multimídia" + "ser apenas um chat único onde posso
alternar os modelos").

Cada sessão é uma CONVERSA multimodal: itens {análise | imagem | vídeo |
gif | melhoria} com prompt, modelo usado, raciocínio (linhas do job) e o
resultado — persistidos em `saidas/midia_sessoes/<id>.json`, isolados por
owner (mesmo guardrail do chat). O job ativo fica anotado na sessão para o
polling RETOMAR ao voltar para a página (a chamada continua no servidor).
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path

PASTA = Path(__file__).resolve().parent.parent / "saidas" / "midia_sessoes"
_lock = threading.Lock()
_RE_ID = re.compile(r"^[\w-]{6,64}$")


def _caminho(sid: str) -> Path:
    if not _RE_ID.match(sid or ""):
        raise ValueError("id de sessão inválido")
    return PASTA / f"{sid}.json"


def _novo_id() -> str:
    return "m" + uuid.uuid4().hex[:12]


def _carregar(sid: str) -> dict | None:
    try:
        return json.loads(_caminho(sid).read_text(encoding="utf-8"))
    except Exception:
        return None


def _salvar(d: dict) -> None:
    PASTA.mkdir(parents=True, exist_ok=True)
    alvo = _caminho(d["id"])
    tmp = alvo.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(alvo)   # atômico: leitura nunca vê meio-arquivo


def listar(owner: str, limit: int = 40) -> list[dict]:
    """Sessões do owner (mais recentes primeiro) — resumo p/ a sidebar."""
    saida = []
    try:
        arqs = sorted(PASTA.glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for a in arqs[:limit * 2]:
        try:
            d = json.loads(a.read_text(encoding="utf-8"))
        except Exception:
            continue
        if owner and (d.get("owner") or "") != owner:
            continue
        saida.append({"id": d["id"], "titulo": d.get("titulo") or "(sem título)",
                      "itens": len(d.get("itens") or []),
                      "job_ativo": bool(d.get("job_ativo")),
                      "ts": d.get("criado_em", "")})
        if len(saida) >= limit:
            break
    return saida


def criar(owner: str, titulo: str = "") -> dict:
    d = {"id": _novo_id(), "owner": owner or "", "titulo": titulo or "",
         "criado_em": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "itens": [], "job_ativo": None}
    with _lock:
        _salvar(d)
    return d


def abrir(sid: str, owner: str) -> dict | None:
    d = _carregar(sid)
    if d and owner and (d.get("owner") or "") != owner:
        return None
    return d


def anexar_item(sid: str, item: dict, titulo: str | None = None) -> None:
    """Adiciona o item CONCLUÍDO à sessão (entrada+raciocínio+resultado);
    `titulo` só vale para o 1º item (a sessão nasce com o prompt dele)."""
    with _lock:
        d = _carregar(sid)
        if not d:
            return
        d["itens"].append(item)
        if titulo and not (d.get("titulo") or "").strip():
            d["titulo"] = titulo.strip()[:80]
        d["job_ativo"] = None
        _salvar(d)


def marcar_job(sid: str, job: str, tipo: str, modelo: str) -> None:
    """Anota o job em curso na sessão — quem voltar para a página RETOMA o
    polling (a chamada segue rodando no executor do servidor)."""
    with _lock:
        d = _carregar(sid)
        if not d:
            return
        d["job_ativo"] = {"job": job, "tipo": tipo, "modelo": modelo,
                          "inicio": time.strftime("%H:%M:%S")}
        _salvar(d)


def limpar_job(sid: str) -> None:
    with _lock:
        d = _carregar(sid)
        if d and d.get("job_ativo"):
            d["job_ativo"] = None
            _salvar(d)


def apagar(sid: str, owner: str) -> bool:
    with _lock:
        d = _carregar(sid)
        if not d or (owner and (d.get("owner") or "") != owner):
            return False
        try:
            _caminho(sid).unlink(missing_ok=True)
            return True
        except OSError:
            return False
