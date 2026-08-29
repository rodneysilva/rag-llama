"""Registry de jobs — domínio de execução (camada core).

Extraído de api/app.py no split Fase 1 (28/08): fila/log/estado de job é
domínio, não camada de API. Sem dependência de FastAPI.
"""
import json
import os
import re
import threading
import time
from itertools import count
from pathlib import Path

PASTA_LOGS_JOBS = Path("logs/jobs")


class JobNaoEncontrado(Exception):
    """Job ausente do registro — exceção de DOMÍNIO (Fase 2 do split).

    Nasceu na Fase 1 como HTTPException (paridade com o monólito); a borda
    da API (`api/routers/jobs.py`) converte em HTTP 404 — o core segue sem
    depender de FastAPI, como manda o contrato de camadas.
    """


def _podar_concluidos(dic: dict, manter: int = 10) -> None:
    """Deixa só os N últimos jobs CONCLUÍDOS no registry — os logs das
    execuções antigas acumulam memória para sempre sem isto (o ingest já
    podava; estende o mesmo comportamento a todos os tipos de job).

    ATENÇÃO: chame SEGURANDO o lock do registry (o threading.Lock não é
    reentrante — adquirir de novo aqui dentro seria deadlock, já aconteceu)."""
    for velho in [k for k, v in dic.items() if not v.get("running")][:-manter]:
        dic.pop(velho, None)


def _novo_job(dic: dict, lock: threading.Lock, job: str) -> None:
    """Cria (ou RE-criA, no replay pós-restart) a entrada de status do job.
    Segura o lock e poda os concluídos antigos. `picked=False` = aguardando
    o worker pegar da fila (o `_no_worker` marca True ao EXECUTAR)."""
    with lock:
        _podar_concluidos(dic)
        dic[job] = {"lines": [], "running": True, "result": None,
                    "error": None, "picked": False}


TODOS_JOBS: list = []  # alimenta _jobs_ativos e o ⏹ Parar tudo (1 lista só)


# TODO log de job é GRAVADO (logs/jobs/{job}.jsonl, um por execução — a
# linha vira registro permanente: "registrar tudo e deixar gravado")
PASTA_LOGS_JOBS = Path("logs/jobs")


class JobRegistry:
    # sufixo ÚNICO por processo: o _seq reinicia a cada boot e o sbx_4 de
    # hoje colidia com o sbx_4 de ontem (jsonl em append misturava
    # execuções de dias diferentes no mesmo arquivo — visto em produção)
    _BOOT = os.urandom(2).hex()

    def __init__(self, prefixo: str, rotulo: str):
        self.prefixo, self.rotulo = prefixo, rotulo
        self.jobs: dict = {}
        self.lock = threading.Lock()
        self._seq = count(1)
        TODOS_JOBS.append(self)

    def novo_id(self) -> str:
        return f"{self.prefixo}_{next(self._seq)}-{self._BOOT}"

    def iniciar(self, jid: str) -> None:
        """Cria/recria a entrada de status (placeholder pré-pickup) e poda
        os arquivos de log antigos (retenção por contagem)."""
        _novo_job(self.jobs, self.lock, jid)
        try:  # fire-and-forget: retenção nunca atrasa o job
            PASTA_LOGS_JOBS.mkdir(parents=True, exist_ok=True)
            arquivos = sorted(PASTA_LOGS_JOBS.glob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            for velho in arquivos[400:]:  # mantém os 400 logs mais recentes
                velho.unlink(missing_ok=True)
        except Exception:
            pass

    def parcial(self, jid: str, texto: str) -> None:
        """TEXTO AO VIVO da geração (streaming): o polling mostra a
        resposta sendo escrita antes do job concluir."""
        with self.lock:
            st = self.jobs.get(jid)
            if st is not None:
                st["parcial"] = texto

    def log(self, jid: str, msg: str, **extra) -> None:
        """Anexa linha ao log ao vivo, grava no jsonl (compat) E no SQLite
        BASE PERSISTENTE (logs/logs.db) — o dashboard consulta por SQL;
        tudo sobrevive a restart/recreate da API."""
        with self.lock:
            if jid not in self.jobs:
                return
            # 🧹 DEDUPE: linha IDÊNTICA consecutiva no mesmo segundo é eco de
            # callback duplo (bug real: a linha 🪙 de tokens aparecia 2× no
            # raciocínio) — o jsonl/SQLite já não registra duplicata nenhuma
            linhas = self.jobs[jid]["lines"]
            agora = time.strftime("%H:%M:%S")
            if (linhas and linhas[-1].get("msg") == msg
                    and linhas[-1].get("ts") == agora):
                return
            linhas.append({"ts": agora, "msg": msg, **extra})
        try:
            from core import logsdb
            logsdb.log_evento(jid, str(msg), extra.get("etapa"))
        except Exception:
            pass
        try:
            caminho = PASTA_LOGS_JOBS / f"{jid}.jsonl"
            with open(caminho, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": agora, "msg": msg, **extra},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass  # disco cheio/permissão: o log ao vivo segue valendo

    def concluir(self, jid: str, result=None, error=None) -> None:
        """Fecha o job (running=False) gravando result e/ou error. Job
        CANCELADO pelo usuário: o resultado tardio é DESCARTADO (o cancelou
        porque mandou outra — sobrescrever ressuscitaria a resposta morta)."""
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j.get("cancelado"):
                return
            if result is not None:
                j["result"] = result
            if error is not None:
                j["error"] = error
            j["running"] = False

    def cancelar(self, jid: str, motivo: str = "cancelado pelo usuário") -> bool:
        """Cancela UM job (pedido do dono: nova mensagem enquanto pensa →
        interrompe). A thread segue até o fim (não se mata LLM no meio), mas
        o resultado é descartado e o polling vê 'cancelado' na hora."""
        with self.lock:
            j = self.jobs.get(jid)
            if not j or not j.get("running"):
                return False
            j["cancelado"] = True
            j["running"], j["error"] = False, motivo
            j["lines"].append({"msg": f"⚠️ {motivo}"})
            return True

    def ativos(self) -> int:
        return sum(1 for j in self.jobs.values() if j.get("running"))

    def cancelar_todos(self, motivo: str) -> list[str]:
        """Marca toda tarefa ativa como cancelada (⏹ Parar tudo) — SEGURANDO
        o lock (antes o parar_tudo iterava 7 registries sem lock nenhum)."""
        with self.lock:
            cancelados = []
            for jid, j in self.jobs.items():
                if j.get("running"):
                    j["running"], j["error"] = False, motivo
                    j["lines"].append({"msg": f"⚠️ {motivo}"})
                    cancelados.append(jid)
            return cancelados

    def status(self, jid: str, cursor: int, msg404: str) -> dict:
        """Snapshot do job a partir do `cursor` (polling da webui).

        Levanta JobNaoEncontrado (domínio) se o job não está no registro —
        a borda da API converte em HTTP 404 com a mensagem da família."""
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                raise JobNaoEncontrado(msg404)
            return {"running": j["running"], "total": len(j["lines"]),
                    "lines": j["lines"][cursor:], "result": j["result"],
                    "error": j["error"],
                    "parcial": j.get("parcial") or ""}


