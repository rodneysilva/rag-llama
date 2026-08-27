"""
Executor de jobs IN-PROCESS (asyncio) — substitui o RabbitMQ (decisão do
dono 27/08: "para esses tipos de chamadas não tem necessidade do
rabbitmq; quero o controle de erros").

Desenho:
- fila `asyncio.Queue` SERIAL (1 job por vez — a VRAM segue sendo o
  gargalo, igual ao worker único de antes com prefetch=1);
- a fábrica (síncrona, closure runtime) roda em `asyncio.to_thread` —
  o event loop da API continua livre para responder;
- RETRY com backoff para erros TRANSIENTES (rede/timeout) e falha
  IMEDIATA para erro de negócio — controle de erro é Python comum
  (try/except), sem DLQ/NACK para raciocinar;
- telemetria tipo "jobs" (enfileirado/iniciado/retry/falha/fim).

Sobrevivência a restart (decisão: ERRO CLARO): nada persiste — o
polling da webui recebe 404 e mostra "job não encontrado — dispare
novamente" (app.py já trata; as tarefas de estúdio seguem com o seu
_sweep_reinicio em saidas/tarefas_ativas.json).
"""
import asyncio
import threading
import time

from . import telemetria

fila: asyncio.Queue | None = None      # criada no boot da API
_no_ar = False
_lock = threading.Lock()

# erros que VALEM retry (transientes de rede/io)
_TRANSIENTES = ("connectionerror", "timeout", "timed out", "eof",
                "connection reset", "temporarily unavailable",
                "refused", "remoteended", "reset by peer")


def _e_transiente(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(t in s for t in _TRANSIENTES)


def iniciar(log=print):
    """Cria a fila e sobe o worker async (chamado no boot da API)."""
    global fila, _no_ar
    with _lock:
        if _no_ar:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # sem loop (script CLI): jobs seguem em thread direta

        async def _worker():
            while True:
                kind, jid, fabricar, payload = await fila.get()
                telemetria.evento("jobs", f"▶ iniciado {jid} ({kind})",
                                  job=jid, kind=kind)
                t0 = time.time()
                tentativa, ok = 0, False
                while not ok and tentativa <= 2:   # 1 tentativa + 2 retries
                    try:
                        rodar = fabricar(payload)
                        await asyncio.to_thread(rodar)
                        ok = True
                    except Exception as e:   # controle de erro explícito
                        tentativa += 1
                        if tentativa <= 2 and _e_transiente(e):
                            espera = 2 ** tentativa   # 2 s · 4 s
                            log(f"🔁 job {jid} ({kind}) falhou transiente "
                                f"({str(e)[:90]}) — retry em {espera}s "
                                f"[{tentativa}/2]")
                            telemetria.evento(
                                "jobs", f"🔁 retry {jid} ({kind}) após "
                                        f"{str(e)[:80]}", job=jid, kind=kind)
                            await asyncio.sleep(espera)
                            continue
                        log(f"✕ job {jid} ({kind}) falhou: {str(e)[:160]}")
                        telemetria.evento(
                            "jobs",
                            f"✕ falhou {jid} ({kind}): {str(e)[:120]}",
                            job=jid, kind=kind)
                        break
                if ok:
                    telemetria.evento(
                        "jobs", f"✓ fim {jid} ({kind}) em "
                                f"{round(time.time() - t0)}s",
                        job=jid, kind=kind)
                fila.task_done()

        fila = asyncio.Queue()
        loop.create_task(_worker(), name="ragaroy-executor")
        _no_ar = True
    log("⚙️ executor de jobs async no ar (fila serial em memória)")


def despachar(kind: str, jid: str, payload: dict, fabricar) -> bool:
    """Enfileira o job no executor async. Devolve False quando não há
    event loop (scripts CLI) — o chamador roda em thread direta."""
    if fila is None:
        return False
    telemetria.evento("jobs", f"📤 enfileirado {jid} ({kind})",
                      job=jid, kind=kind)
    fila.put_nowait((kind, jid, fabricar, payload))
    return True
