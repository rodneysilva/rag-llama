"""
Fila de jobs via RabbitMQ (com DLX/DLQ): ingestão, seed, limpeza e chat
rodam como mensagens — sobrevivem a restart da API, uma por vez (worker
único: a VRAM é o gargalo), e mensagens que falham vão para a fila morta
(DLQ) com o erro registrado, em vez de sumirem.

Topologia:
  exchange  ragaroy.jobs (direct)  → fila  ragaroy.tarefas
  fila ragaroy.tarefas argumentos: x-dead-letter-exchange=ragaroy.dlx
  exchange  ragaroy.dlx  (direct)  → fila  ragaroy.dlq (dead letter queue)

Sem RabbitMQ no ar, o publicador cai para execução DIRETA em thread (o
sistema continua funcionando — fila é infraestrutura, não dependência
dura). Reentrega: mensagem volta à fila se o worker morrer (requeue no
cancel); falha de NEGÓCIO (exception tratada) vai para a DLQ de propósito.
"""
import json
import os
import threading
import time

from . import telemetria

EX_JOBS = "ragaroy.jobs"
EX_DLX = "ragaroy.dlx"
FILA_TAREFAS = "ragaroy.tarefas"
FILA_DLQ = "ragaroy.dlq"

_URL = None
_canal_lock = threading.Lock()
_canal = None  # canal do publicador (reusado; publishers confirm)


def _url() -> str:
    import os
    from . import config
    """URL do Rabbit (RABBIT_URL do .env; default de DEV pelo usuário/senha
    padrão do compose — credenciais reais vivem no .env, nunca no código)."""
    usuario = os.getenv("RABBIT_USER", "ragaroy")
    senha = os.getenv("RABBIT_PASS", "ragaroy")
    return os.getenv("RABBIT_URL", getattr(config, "RABBIT_URL", "")
                     or f"amqp://{usuario}:{senha}@localhost:5672/%2F")


def _conectar(timeout=3, heartbeat=60):
    import pika
    params = pika.URLParameters(_url())
    params.socket_timeout = timeout
    params.heartbeat = heartbeat
    return pika.BlockingConnection(params)


def disponivel() -> bool:
    """RabbitMQ alcançável? (para o /api/fila informar o modo de operação)"""
    try:
        _conectar().close()
        return True
    except Exception:
        return False


def _declarar(ch):
    """Topologia: exchanges, fila de tarefas (com DLX) e a DLQ."""
    ch.exchange_declare(EX_JOBS, exchange_type="direct", durable=True)
    ch.exchange_declare(EX_DLX, exchange_type="direct", durable=True)
    ch.queue_declare(FILA_TAREFAS, durable=True, arguments={
        "x-dead-letter-exchange": "ragaroy.dlx",
        "x-dead-letter-routing-key": "morto",
    })
    ch.queue_declare(FILA_DLQ, durable=True)
    ch.queue_bind(FILA_TAREFAS, EX_JOBS, routing_key="job")
    ch.queue_bind(FILA_DLQ, "ragaroy.dlx", routing_key="morto")


def publicar(kind: str, job_id: str, payload: dict) -> bool:
    """Publica um job. Devolve False (e NÃO bloqueia) se o broker estiver
    fora — o chamador decide executar direto em thread (fallback).

    O canal do publicador é CACHED, mas esfria: o broker corta conexões
    ociosas (heartbeat perdido) e a 1ª publicação após a pausa morre com
    EOF. Por isso UMA retentativa com conexão nova antes de desistir."""
    global _canal
    corpo = json.dumps({"kind": kind, "job": job_id, "payload": payload},
                       ensure_ascii=False).encode("utf-8")
    for tentativa in (1, 2):
        try:
            with _canal_lock:
                if _canal is None or _canal.is_closed:
                    conn = _conectar()
                    _canal = conn.channel()
                    _declarar(_canal)
                _canal.basic_publish(
                    exchange=EX_JOBS, routing_key="job", body=corpo,
                    properties=pika_spec())
            telemetria.evento("rabbit", f"📤 publicado job {job_id} ({kind})",
                              job=job_id, kind=kind)
            return True
        except Exception as e:
            _canal = None  # canal morto: força reconexão na próxima tentativa
            if tentativa == 2:
                print(f"⚠️ RabbitMQ indisponível ({e}) — job {job_id} roda direto em thread")
                telemetria.evento("rabbit", f"⚠️ broker fora — job {job_id} ({kind}) "
                                           "em thread direta", job=job_id, kind=kind)
                return False


def pika_spec():
    import pika
    return pika.BasicProperties(delivery_mode=2,  # persistente
                                content_type="application/json")


def consumir(callback, log=print):
    """Worker ÚNICO (prefetch=1 — VRAM não paraleliza): entrega cada mensagem
    ao `callback(msg_dict)`; exception → DLQ (erro de negócio registrado);
    cancel do consumidor → requeue (worker caiu, mensagem volta).

    HEARTBEAT DESLIGADO (heartbeat=0) de propósito: o job roda INTEIRO
    dentro do callback (minutos de LLM/difusão) e o BlockingConnection não
    processa heartbeats enquanto executa — com heartbeat ligado o broker
    cortava a conexão no meio ("missed heartbeats"), reconectava e
    REENTREGAVA a mensagem: o job executava 2x e zerava o próprio status."""
    import pika
    while True:
        try:
            conn = _conectar(heartbeat=0)
            ch = conn.channel()
            _declarar(ch)
            ch.basic_qos(prefetch_count=1)
            for metodo, props, body in ch.consume(FILA_TAREFAS, inactivity_timeout=1):
                if metodo is None:
                    continue  # timeout: volta ao loop (mantém a conexão viva)
                try:
                    msg = json.loads(body.decode("utf-8"))
                    callback(msg)
                    ch.basic_ack(metodo.delivery_tag)
                except Exception as e:
                    # corpo cru no log: se o body NÃO é JSON válido, o
                    # json.loads AQUI lançaria DENTRO do except → loop
                    # externo → reconexão → reentrega INFINITA (poison
                    # message que nunca chegava à DLQ)
                    try:
                        jid = json.loads(body).get("job", "?")
                    except Exception:
                        jid = repr(body[:120])
                    log(f"☠️ job {jid} falhou → DLQ: {e}")
                    # cabeçalho com o erro fica registrado NA própria mensagem morta
                    ch.basic_reject(metodo.delivery_tag, requeue=False)  # → DLX → DLQ
        except Exception as e:
            log(f"⚠️ worker da fila caiu ({e}) — reconectando em 5 s")
            time.sleep(5)


def iniciar_worker(callback, log=print):
    """Sobe o worker em thread daemon (chamado no boot da API). O loop de
    `consumir` RECONECTA sozinho a cada 5 s — o broker pode subir depois da
    API (Rabbit leva ~20 s no primeiro boot); até lá os jobs caem para
    threads diretas e o worker assume assim que conecta."""
    threading.Thread(target=consumir, args=(callback, log), daemon=True,
                     name="ragaroy-worker").start()


def purgar_tudo() -> dict:
    """Purga a fila de tarefas E a DLQ, e publica evento de cancelamento
    (o ⏹ Parar tudo). Devolve as profundidades antes da purga."""
    try:
        conn = _conectar()
        ch = conn.channel()
        t = ch.queue_declare(FILA_TAREFAS, passive=True).method.message_count
        m = ch.queue_declare(FILA_DLQ, passive=True).method.message_count
        ch.queue_purge(FILA_TAREFAS)
        ch.queue_purge(FILA_DLQ)
        # evento (best-effort): consumidores/observadores sabem que houve
        # um cancelamento geral — não é job, é sinal
        _publicar_canal(ch, "sistema.parar_tudo", {"evento": "parar_tudo"})
        conn.close()
        return {"online": True, "purgados": {"fila": t, "dlq": m}}
    except Exception as e:
        return {"online": False, "erro": str(e)[:120]}


def _publicar_canal(ch, routing: str, corpo: dict) -> None:
    import pika
    ch.basic_publish(
        exchange=EX_JOBS, routing_key=routing,
        body=json.dumps(corpo, ensure_ascii=False).encode("utf-8"),
        properties=pika.BasicProperties(delivery_mode=2,
                                        content_type="application/json"))


def estado() -> dict:
    """Profundidades das filas + DLQ (para a tela de administração).
    Consulta PASSIVA de verdade: nenhum declare/redeclara aqui —
    redeclaração com argumentos divergentes derruba o canal com
    precondition_failed e a tela de admin perde a conexão."""
    try:
        conn = _conectar()
        ch = conn.channel()
        tarefas = ch.queue_declare(FILA_TAREFAS, passive=True)
        dlq = ch.queue_declare(FILA_DLQ, passive=True)
        conn.close()
        return {"online": True, "pendentes": tarefas.method.message_count,
                "mortas": dlq.method.message_count,
                "management": "http://localhost:15672 (usuário/senha do .env — RABBIT_USER/RABBIT_PASS)"}
    except Exception:
        return {"online": False, "pendentes": None, "mortas": None,
                "management": None}


def _mgmt_base() -> str:
    """URL da Management API (porta 15672 do MESMO host do amqp://)."""
    import re as _re
    from urllib.parse import urlsplit
    u = urlsplit(_url())
    host = u.hostname or "localhost"
    return os.getenv("RABBIT_MGMT_URL", f"http://{host}:15672")


def detalhe(peek_dlq: int = 5) -> dict:
    """Estado COMPLETO do Rabbit via Management API (imagem *:management,
    auth RABBIT_USER/RABBIT_PASS): por fila — profundidade, consumidores,
    taxas de publicação/entrega desde o boot do broker — mais PEEK da DLQ
    (mensagens mortas com o erro registrado; ack_requeue_true = SÓ OLHA,
    não consome) e o histórico agregado de jobs da telemetria."""
    import httpx
    d = estado()
    if not d.get("online"):
        return {**d, "filas": [], "dlq_msgs": [], "telemetria": {}}
    try:
        auth = (os.getenv("RABBIT_USER", "ragaroy"), os.getenv("RABBIT_PASS", "ragaroy"))
        r = httpx.get(f"{_mgmt_base()}/api/queues", auth=auth, timeout=5)
        r.raise_for_status()
        filas = []
        for q in r.json():
            if not str(q.get("name", "")).startswith("ragaroy"):
                continue   # só as nossas
            tot = q.get("message_stats") or {}
            filas.append({
                "nome": q.get("name"),
                "mensagens": q.get("messages") or 0,
                "prontas": q.get("messages_ready") or 0,
                "nao_ack": q.get("messages_unacknowledged") or 0,
                "consumidores": q.get("consumers") or 0,
                "publicadas": tot.get("publish") or 0,
                "entregues": tot.get("deliver_get") or 0,
                "taxa_pub": round(q.get("message_stats", {}).get("publish_details",
                                                                 {}).get("rate") or 0, 2),
            })
        filas.sort(key=lambda f: f["nome"])
        d["filas"] = filas
        d["broker"] = None
        # PEEK da DLQ (requeue: a mensagem VOLTA — nada é consumido)
        d["dlq_msgs"] = []
        if peek_dlq:
            try:
                pk = httpx.post(
                    f"{_mgmt_base()}/api/queues/%2F/{FILA_DLQ}/get",
                    auth=auth, timeout=5,
                    json={"count": peek_dlq, "ackmode": "ack_requeue_true",
                          "encoding": "auto", "truncate": 30000})
                pk.raise_for_status()
                for m in pk.json():
                    try:
                        corpo = json.loads(m.get("payload") or "{}")
                    except Exception:
                        corpo = {}
                    cab = (m.get("properties") or {}).get("headers") or {}
                    d["dlq_msgs"].append({
                        "job": corpo.get("job") or cab.get("x-first-death-queue", "?"),
                        "kind": corpo.get("kind", "?"),
                        "erro": str(cab.get("x-death", [{}])[0].get("reason", "")
                                    if cab.get("x-death") else "")[:120],
                    })
            except Exception:
                pass
    except Exception as e:
        d["mgmt_erro"] = str(e)[:100]
        d["filas"] = d.get("filas", [])
        d["dlq_msgs"] = []
    # histórico agregado (telemetria): publicado/pego/reentrega por kind
    tel = {"publicados": 0, "kinds": {}}
    try:
        for ev in telemetria.ultimos("rabbit", 400):
            k = ev.get("kind") or "?"
            msg = str(ev.get("msg") or "")
            if "publicado" in msg:
                tel["publicados"] += 1
                tel["kinds"].setdefault(k, {"publicados": 0, "outros": 0})
                tel["kinds"][k]["publicados"] += 1
            elif "reentrega" in msg or "DLQ" in msg or "broker fora" in msg:
                tel["kinds"].setdefault(k, {"publicados": 0, "outros": 0})
                tel["kinds"][k]["outros"] += 1
    except Exception:
        pass
    d["telemetria"] = tel
    return d


def reliquidar_dlq(maximo: int = 50) -> int:
    """Move mensagens da DLQ de volta para a fila de tarefas (reprocessar)."""
    import pika
    movidas = 0
    try:
        conn = _conectar()
        ch = conn.channel()
        _declarar(ch)
        while movidas < maximo:
            metodo, props, body = ch.basic_get(FILA_DLQ)
            if metodo is None:
                break
            ch.basic_publish(EX_JOBS, "job", body, properties=props)
            ch.basic_ack(metodo.delivery_tag)
            movidas += 1
        conn.close()
    except Exception as e:
        print(f"⚠️ reliquidação da DLQ: {e}")
    return movidas
