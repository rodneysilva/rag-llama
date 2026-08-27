"""Telemetria de infraestrutura — o histórico PERSISTENTE de cada chamada.

Uma linha JSON por evento em logs/telemetria.jsonl (volume montado no
container — sobrevive a restart). Tipos:
  llm    → cada chamada ao llama-server (modelo, 🔻 entrada, 🔺 saída, duração)
  jobs   → executor async (enfileirado/iniciado/retry/falha/fim)
  geracao → mídia gerada (Flux/Wan/externos)
  (tipos legados "rabbit"/"redis"/"cache" continuam LEGÍVEIS no histórico
   antigo — sem novos emissores desde a remoção do Rabbit/Redis 27/08)

Quem quer ver "como o sistema está trabalhando" consulta
/api/telemetria?tipo=… Telemetria NUNCA derruba a operação: qualquer erro
aqui é engolido.
"""
import json
import threading
import time
from pathlib import Path

ARQ = Path(__file__).resolve().parent.parent / "logs" / "telemetria.jsonl"
_lock = threading.Lock()
_TAM_MAX = 5 * 1024 * 1024  # 5 MB → trunca o começo (mantém o final)
_ultima_truncada = 0.0


def evento(tipo: str, msg: str, **campos) -> None:
    """Registra um evento (llm|jobs|geracao…) no histórico."""
    global _ultima_truncada
    try:
        reg = {"ts": time.strftime("%d/%m %H:%M:%S"), "tipo": tipo,
               "msg": msg, **campos}
        with _lock:
            ARQ.parent.mkdir(parents=True, exist_ok=True)
            # rotação barata: a cada 10 min, se passou de 5 MB, fica só o final
            if ARQ.exists() and ARQ.stat().st_size > _TAM_MAX \
                    and time.time() - _ultima_truncada > 600:
                with open(ARQ, "rb") as f:
                    f.seek(max(0, ARQ.stat().st_size - _TAM_MAX // 2))
                    f.readline()  # descarta meia-linha
                    resto = f.read()
                ARQ.write_bytes(resto)
                _ultima_truncada = time.time()
            with open(ARQ, "a", encoding="utf-8") as f:
                f.write(json.dumps(reg, ensure_ascii=False) + "\n")
    except Exception:
        pass  # telemetria é cortesia: nunca derruba nada


def ultimos(tipo: str | None = None, limite: int = 50) -> list[dict]:
    """Últimos eventos (mais recentes por último), filtrados por tipo."""
    try:
        with _lock:
            if not ARQ.exists():
                return []
            with open(ARQ, "rb") as f:
                f.seek(0, 2)
                fim = f.tell()
                f.seek(max(0, fim - 262_144))  # tail de 256 KB
                texto = f.read().decode("utf-8", errors="replace")
        linhas = [l for l in texto.splitlines() if l.strip()]
        eventos = []
        for l in linhas:
            try:
                e = json.loads(l)
            except Exception:
                continue
            if tipo:
                if e.get("tipo") != tipo:
                    continue
            eventos.append(e)
        return eventos[-limite:]
    except Exception:
        return []
