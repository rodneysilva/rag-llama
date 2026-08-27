"""
Tarefas: o framework de jobs em segundo plano com progresso, ETA e locks.

Uma tarefa = um fluxo (chat com contexto, geração de mídia, análise…)
rodando numa thread própria. A webui acompanha por /api/tarefas/status com
cursor (como a ingestão fazia) e recebe:

  lines      — log ao vivo (msg + etapa)
  progresso  — 0..1 (quando o motor consegue reportar)
  etapa      — nome da etapa atual do fluxo
  eta_s      — estimativa restante (média móvel real + fração atual)

Regras de ocupação (pedidas pelo operador):
  - Enquanto UMA tarefa de VRAM roda, nenhuma outra pode começar.
  - A sessão que disparou fica "ocupada": navega nas abas, mas novos
    comandos NAQUELA sessão são recusados até terminar.
  - Outras sessões continuam livres para tarefas que não usam VRAM pesada.

RESTART da API: a GPU não retoma uma difusão no meio — mas a tarefa também
NÃO pode ficar pendurada como "running" para sempre. As ativas são
persistidas (saidas/tarefas_ativas.json) e, no boot seguinte, o sweep
re-registra cada uma como ERRO claro ("a API reiniciou durante a geração"):
o polling do frontend recebe o erro, a sessão desbloqueia e o operador
sabe que pode disparar de novo.

As médias de tempo por modalidade vão para o Redis (se disponível) e
degradam para em-memória quando ele está fora.
"""
import json
import threading
import time
from itertools import count
from pathlib import Path

_seq = count(1)
_lock = threading.Lock()
_tarefas: dict = {}          # id -> estado
_sessao_ocupada: dict = {}   # sid -> id da tarefa que a travou
_vram_ocupada_por: str | None = None

_ATIVAS = Path(__file__).resolve().parent.parent / "saidas" / "tarefas_ativas.json"

# média de duração por modalidade (s) — MEMÓRIA (Redis removido 27/08; as
# bases declarativas das modalidades cobrem o 1º uso de cada restart)
_medias: dict = {}


# ---------- média de tempo por modalidade (para o ETA exibido antes) ------

def estimativa(modalidade: str) -> int | None:
    """Estimativa (s) da modalidade: média real medida, senão a base."""
    from . import modalidades
    base = _MOD_BASE(modalidade)
    med = _medias.get(modalidade)
    return med or base


def _MOD_BASE(modalidade: str):
    from .modalidades import _MODALIDADES
    return _MODALIDADES.get(modalidade, {}).get("estimativa_s")


def registrar_duracao(modalidade: str, segundos: float) -> None:
    media = (_medias.get(modalidade) or _MOD_BASE(modalidade) or segundos)
    _medias[modalidade] = round(0.7 * media + 0.3 * segundos)  # média móvel


# ---------- persistência das ativas (sweep pós-restart) ---------------------

def cancelar_todas(log=print) -> list[str]:
    """⏹ Parar tudo: marca TODA tarefa ativa como cancelada (erro claro),
    libera locks de sessão/VRAM e limpa o registro de ativas. O processo
    de GPU em si morre com o motor (derrubar_todos_motores) ou termina
    sozinho órfão — o estado fica CONSISTENTE para a webui na hora."""
    global _vram_ocupada_por  # sem isto, a linha abaixo vira LOCAL e o lock
    # de VRAM NUNCA era liberado (estúdio ficava "ocupado" até reiniciar)
    with _lock:
        canceladas = []
        for tid, j in _tarefas.items():
            if j["running"]:
                j["running"] = False
                j["error"] = "cancelado (⏹ parar tudo)"
                j["lines"].append({"msg": "⚠️ cancelado pelo operador (⏹ parar tudo)",
                                   "etapa": "fim"})
                canceladas.append(tid)
        _sessao_ocupada.clear()
        _vram_ocupada_por = None
        return canceladas


def _persistir_ativas() -> None:
    """Grava as tarefas RUNNING no disco — o sweep do próximo boot usa isto
    para marcá-las como interrompidas (erro claro) em vez de 404 pendurado."""
    try:
        ativas = [{"id": tid, "modalidade": j["modalidade"], "rotulo": j["rotulo"],
                   "sessao": j.get("sessao"), "t0": j["t0"]}
                  for tid, j in _tarefas.items() if j["running"]]
        _ATIVAS.parent.mkdir(parents=True, exist_ok=True)
        _ATIVAS.write_text(json.dumps(ativas, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # persistência é cortesia: nunca derruba a tarefa


def _sweep_reinicio() -> None:
    """No boot: tarefas que estavam rodando quando a API caiu voltam como ERRO
    claro. A GPU não retoma a difusão do meio — mas o polling do frontend
    recebe o motivo, a sessão desbloqueia e nada fica 'running' eterno."""
    try:
        interrompidas = json.loads(_ATIVAS.read_text(encoding="utf-8"))
    except Exception:
        return
    for t in interrompidas if isinstance(interrompidas, list) else []:
        tid = t.get("id")
        if not tid:
            continue
        minutos = round((time.time() - float(t.get("t0") or 0)) / 60, 1)
        _tarefas[tid] = {
            "modalidade": t.get("modalidade", "?"), "rotulo": t.get("rotulo", "?"),
            "lines": [{"msg": "⚠️ a API reiniciou durante esta geração — o processo "
                              "de GPU foi embora com ela. Dispare novamente.",
                       "etapa": "fim"}],
            "progresso": None, "etapa": "fim", "eta_s": 0,
            "running": False, "result": None,
            "error": f"a API reiniciou durante a geração (~{minutos} min em curso) — "
                     "dispare novamente",
            "t0": t.get("t0", time.time()), "trava_vram": False,
            "sessao": t.get("sessao"), "segundos": None}
        print(f"🧹 tarefa {tid} ({t.get('rotulo')}) marcada como interrompida "
              "(restart da API)")
    _ATIVAS.write_text("[]", encoding="utf-8")


_sweep_reinicio()  # roda no import: API de pé já nasce com o estado honesto


# ---------- ciclo de vida das tarefas --------------------------------------

def criar(modalidade: str, trava_vram: bool = True, sessao: str | None = None) -> str:
    """Registra a tarefa; recusa (RuntimeError) se a VRAM/estúdio está ocupado
    OU a sessão já tem tarefa em curso (cheque ATÔMICO com a reserva — sem
    janela de corrida entre o caller consultar e reservar)."""
    global _vram_ocupada_por
    with _lock:
        if trava_vram and _vram_ocupada_por:
            raise RuntimeError(
                f"o estúdio está ocupado com a tarefa {_vram_ocupada_por} "
                f"({_tarefas[_vram_ocupada_por]['rotulo']}) — aguarde concluir")
        if sessao and sessao in _sessao_ocupada and _sessao_ocupada[sessao] in _tarefas:
            tid_ocup = _sessao_ocupada[sessao]
            raise RuntimeError(
                f"a sessão está ocupada com a tarefa {tid_ocup} "
                f"({_tarefas[tid_ocup]['rotulo']}) — aguarde concluir ou crie outra sessão")
        tid = f"t{next(_seq)}"
        rotulo = (modalidade or "?").upper()
        _tarefas[tid] = {"modalidade": modalidade, "rotulo": rotulo, "lines": [],
                         "progresso": None, "etapa": "iniciando", "eta_s": None,
                         "running": True, "result": None, "error": None,
                         "t0": time.time(), "trava_vram": trava_vram, "sessao": sessao}
        if trava_vram:
            _vram_ocupada_por = tid
        if sessao:
            _sessao_ocupada[sessao] = tid
        _persistir_ativas()
        return tid


def ativos() -> list[dict]:
    """Tarefas rodando AGORA (para o /saude do agente/Sistema): id, rótulo
    e modalidade — mostra O QUE ocupa a GPU (ex.: difusão) em vez de um
    "offline" enganoso quando o chat está pausado."""
    with _lock:
        return [{"job": tid, "rotulo": str(j.get("rotulo")
                                           or j.get("modalidade") or tid)[:80],
                 "modalidade": j.get("modalidade")}
                for tid, j in _tarefas.items() if j.get("running")]


def log(tid: str, msg: str, etapa: str | None = None) -> None:
    with _lock:
        j = _tarefas.get(tid)
        if not j:
            return
        j["lines"].append({"msg": msg, "etapa": etapa or j["etapa"]})
        if etapa:
            j["etapa"] = etapa


def progresso(tid: str, fracao: float | None, etapa: str | None = None) -> None:
    with _lock:
        j = _tarefas.get(tid)
        if not j:
            return
        if fracao is not None:
            j["progresso"] = max(j["progresso"] or 0.0, min(1.0, fracao))
            decorrido = time.time() - j["t0"]
            if j["progresso"] > 0.05:
                total_est = decorrido / j["progresso"]
                j["eta_s"] = max(0, round(total_est - decorrido))
        if etapa:
            j["etapa"] = etapa


def concluir(tid: str, result: dict | None = None, erro: str | None = None) -> None:
    global _vram_ocupada_por
    with _lock:
        j = _tarefas.get(tid)
        if not j:
            return
        j["running"] = False
        j["result"] = result
        j["error"] = erro
        j["progresso"] = 1.0 if not erro else j["progresso"]
        j["eta_s"] = 0
        segundos = time.time() - j["t0"]
        if not erro:
            registrar_duracao(j["modalidade"], segundos)
        j["segundos"] = round(segundos)
        if _vram_ocupada_por == tid:
            _vram_ocupada_por = None
        if _sessao_ocupada.get(j["sessao"]) == tid:
            _sessao_ocupada.pop(j["sessao"], None)
        # mantém só as 20 últimas concluídas na memória
        velhas = [k for k, v in _tarefas.items() if not v["running"]][:-20]
        for k in velhas:
            _tarefas.pop(k, None)
        _persistir_ativas()


def status(tid: str, cursor: int = 0) -> dict | None:
    with _lock:
        j = _tarefas.get(tid)
        if not j:
            return None
        return {"running": j["running"], "total": len(j["lines"]),
                "lines": j["lines"][cursor:], "progresso": j["progresso"],
                "etapa": j["etapa"], "eta_s": j["eta_s"],
                "result": j["result"], "error": j["error"],
                "modalidade": j["modalidade"], "segundos": j.get("segundos")}


# ---------- ocupação --------------------------------------------------------

def estudio_ocupado() -> dict | None:
    """Info da tarefa que trava a VRAM (None se livre) — para recusar novas."""
    with _lock:
        tid = _vram_ocupada_por
        return {"id": tid, "rotulo": _tarefas[tid]["rotulo"],
                "etapa": _tarefas[tid]["etapa"]} if tid else None


def sessao_ocupada(sid: str | None) -> dict | None:
    """Info da tarefa que trava a sessão (None se livre)."""
    if not sid:
        return None
    with _lock:
        tid = _sessao_ocupada.get(sid)
        if not tid or tid not in _tarefas:
            return None
        j = _tarefas[tid]
        return {"id": tid, "rotulo": j["rotulo"], "etapa": j["etapa"],
                "progresso": j["progresso"], "eta_s": j["eta_s"]}


def limpar_sessao(sid: str | None) -> None:
    """Solta o lock da sessão (usado se a sessão for apagada)."""
    if sid:
        with _lock:
            _sessao_ocupada.pop(sid, None)
