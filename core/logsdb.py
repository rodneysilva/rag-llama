"""Logs e execuções em SQLite (logs/logs.db) — BASE PERSISTENTE.

Arquitetura: SQLite é serverless — roda EMBUTIDO no processo da API (sem
container novo na VPS) gravando no volume logs/ (o mesmo que já persiste
jsonl): sobrevive a restart/recreate. WAL para escrita concorrente dos
jobs. O jsonl segue sendo escrito (compat/fallback); o SQLite vira a fonte
de leitura do Dashboard/histórico (consultas por job/tipo com SQL em vez
de varrer arquivos).

Tabelas:
  evento    — cada linha de log de job (job, ts, etapa, msg)
  execucao  — cada execução terminada (job, tipo, titulo, ok, duracao_s, ts)
"""
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "logs" / "logs.db"
_lock = threading.Lock()
_con: sqlite3.Connection | None = None


def _conexao() -> sqlite3.Connection:
    global _con
    if _con is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        _con = sqlite3.connect(str(DB), check_same_thread=False)
        _con.execute("PRAGMA journal_mode=WAL")     # escrita concorrente
        _con.execute("PRAGMA synchronous=NORMAL")   # rápido e seguro p/ log
        _con.executescript("""
        CREATE TABLE IF NOT EXISTS evento(
          id INTEGER PRIMARY KEY,
          job TEXT NOT NULL,
          ts TEXT NOT NULL,
          etapa TEXT,
          msg TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_evento_job ON evento(job, id);
        CREATE TABLE IF NOT EXISTS execucao(
          id INTEGER PRIMARY KEY,
          job TEXT,
          tipo TEXT NOT NULL,
          titulo TEXT,
          ok INTEGER NOT NULL,
          duracao_s REAL,
          ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_exec_ts ON execucao(id DESC);
        """)
        _con.commit()
    return _con


def log_evento(job: str, msg: str, etapa: str | None = None) -> None:
    """Uma linha de log (engolindo erros — telemetria é cortesia)."""
    try:
        with _lock:
            _conexao().execute(
                "INSERT INTO evento(job, ts, etapa, msg) VALUES (?,?,?,?)",
                (job, time.strftime("%H:%M:%S"), etapa, msg))
            _conexao().commit()
    except Exception:
        pass


def log_execucao(job: str, tipo: str, titulo: str, ok: bool,
                 duracao_s: float = 0.0) -> None:
    """Uma execução terminada (a linha do histórico do dashboard)."""
    try:
        with _lock:
            _conexao().execute(
                "INSERT INTO execucao(job, tipo, titulo, ok, duracao_s, ts)"
                " VALUES (?,?,?,?,?,?)",
                (job, tipo, (titulo or "")[:120], 1 if ok else 0,
                 round(duracao_s or 0, 1),
                 time.strftime("%d/%m/%Y %H:%M:%S")))
            _conexao().commit()
    except Exception:
        pass


def eventos_do_job(job: str) -> list[dict]:
    """Todas as linhas de um job (para o /hx/histlog)."""
    try:
        with _lock:
            cur = _conexao().execute(
                "SELECT ts, etapa, msg FROM evento WHERE job = ? ORDER BY id",
                (job,))
            return [{"ts": t, "etapa": e, "msg": m} for t, e, m in cur]
    except Exception:
        return []


def execucoes(limite: int = 40) -> list[dict]:
    """Execuções mais recentes (nova primeiro) — mesmo formato do
    historico.ultimos (o dashboard troca a fonte sem mudar o template)."""
    try:
        with _lock:
            cur = _conexao().execute(
                "SELECT job, tipo, titulo, ok, duracao_s, ts FROM execucao"
                " ORDER BY id DESC LIMIT ?", (limite,))
            return [{"job": j, "tipo": tp, "titulo": ti, "ok": bool(o),
                     "duracao_s": d, "ts": ts}
                    for j, tp, ti, o, d, ts in cur]
    except Exception:
        return []
