"""Histórico de JOBS — o registro persistente de cada execução.

Cada ingestão/seed/limpeza/manutenção/tarefa de estúdio vira uma linha em
`logs/historico.jsonl` (volume do container): o que rodou, quanto durou, o
que produziu (chunks/mídias), tokens e erro quando houver. A IngestTab (e o
dashboard) lista por /api/historico — "o que já foi ingerido e quando".
"""
import json
import threading
import time
from pathlib import Path

ARQ = Path(__file__).resolve().parent.parent / "logs" / "historico.jsonl"
_lock = threading.Lock()


def registrar(tipo: str, titulo: str, duracao_s: float, **campos) -> None:
    """Anexa uma execução ao histórico (engolindo qualquer erro — cortesia).
    Grava no jsonl (compat) E no SQLite (logs/logs.db) — a base consultável."""
    try:
        reg = {"ts": time.strftime("%d/%m/%Y %H:%M:%S"), "tipo": tipo,
               "titulo": titulo[:120], "duracao_s": round(duracao_s or 0, 1),
               **campos}
        with _lock:
            ARQ.parent.mkdir(parents=True, exist_ok=True)
            with open(ARQ, "a", encoding="utf-8") as f:
                f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        from . import logsdb
        logsdb.log_execucao(str(campos.get("job") or ""), tipo, titulo,
                            ok=bool(campos.get("ok", True)), duracao_s=duracao_s)
    except Exception:
        pass


def ultimos(tipo: str | None = None, limite: int = 40) -> list[dict]:
    """Execuções mais recentes (nova primeiro). FONTE: SQLite (base
    persistente consultável); fallback jsonl se o banco estiver vazio
    (instalações antigas)."""
    try:
        from . import logsdb
        regs = logsdb.execucoes(limite)
        if tipo:
            regs = [r for r in regs if r.get("tipo") == tipo]
        if regs:
            return regs
    except Exception:
        pass
    try:
        with _lock:
            if not ARQ.exists():
                return []
            with open(ARQ, "rb") as f:
                f.seek(0, 2)
                fim = f.tell()
                f.seek(max(0, fim - 262_144))  # tail 256 KB
                texto = f.read().decode("utf-8", errors="replace")
        regs = []
        for l in texto.splitlines():
            try:
                r = json.loads(l)
            except Exception:
                continue
            if tipo and r.get("tipo") != tipo:
                continue
            regs.append(r)
        return list(reversed(regs[-limite:]))
    except Exception:
        return []
