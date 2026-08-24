"""
Contagem de tokens: TUDO que passa pelo llama-server de conversa (:8090) é
medido pelo `usage` que o próprio servidor devolve em cada chamada e
acumulado por SERVIÇO (chat, ingestão, seed, limpeza, estúdio, manutenção,
sistema/testes).

Onde acumula: **Redis** (compartilhado entre processos — a API e os scripts
CLI de seed/varredura contam no MESMO total via INCR atômico) e, sem Redis,
arquivo `saidas/uso_llm.json` por processo. O contexto de serviço é
thread-local: rotas/jobs marcam o que estão fazendo no início e todas as
chamadas LLM daquela thread contam para o serviço certo.
"""
import json
import threading
import time
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent.parent / "saidas" / "uso_llm.json"
_lock = threading.Lock()
_local = threading.local()

SERVICOS = ("chat", "ingestao", "seed", "limpeza", "estudio", "manutencao", "sistema")

# Redis compartilhado (mesmo do cache/ETA) — INCR atômico entre processos.
# Conexão LAZY com re-teste (60 s): Redis subindo DEPOIS da API agora é
# adotado (antes a decisão era no import e caía no arquivo para sempre).
_r = None
_r_off_desde = 0.0
_RETRY_S = 60.0


def _redis_client():
    global _r, _r_off_desde
    if _r is not None:
        return _r
    import time as _t
    if _r is None and _t.time() - _r_off_desde < _RETRY_S:
        return None
    try:
        import os
        import redis as _redis
        cliente = _redis.Redis(host=os.getenv("REDIS_HOST", "127.0.0.1"),
                               port=int(os.getenv("REDIS_PORT", "6379")),
                               socket_connect_timeout=0.3, decode_responses=True)
        cliente.ping()
        _r = cliente
    except Exception:
        _r_off_desde = _t.time()
        return None
    return _r


_r = _redis_client()  # best-effort no import (o lazy cobre o resto)


def set_servico(nome: str) -> None:
    """Marca a thread corrente (rota/job) — as chamadas LLM daqui contam
    para este serviço (padrão: sistema)."""
    _local.servico = nome


def servico_atual() -> str:
    return getattr(_local, "servico", "sistema")


def set_etapa(etapa: str | None) -> None:
    """Etapa corrente desta thread (reformulação / resposta / agente passo 2 /
    verificação…) — a linha de tokens de CADA chamada LLM mostra o que
    aquela chamada ERA, dando o par chamada→retorno no 'pensando…'."""
    _local.etapa = etapa or ""


def etapa_atual() -> str:
    return getattr(_local, "etapa", "") or ""


def set_log(log) -> None:
    """Registra o `log(msg, grupo)` do JOB desta thread: cada chamada LLM
    loga 🔻/🔺 NA HORA no 'pensando…' do chat (tokens em tempo real, não
    só no final). Jobs que não registram ficam silenciosos como antes."""
    _local.log = log


def log_atual():
    return getattr(_local, "log", None)


# ---------- balanço da THREAD (fim da divergência de tokens) -------------
# O diff de totais do Redis (`uso_desde`) cruzava contadores de OUTROS
# processos/serviços rodando junto (API + CLI + jobs concorrentes) — daí os
# números "ora aumentam ora diminuem". O balanço LOCAL da thread soma SÓ o
# que esta execução consumiu de verdade: determinístico por job.

def balanco_reset() -> None:
    """Zera o acumulador desta thread (início de um job/resposta)."""
    _local.balanco = {"entrada": 0, "saida": 0, "chamadas": 0}
    _local.vel_geracao = None


def balanco_ler() -> dict:
    """O que ESTA thread consumiu desde o balanco_reset()."""
    b = getattr(_local, "balanco", None)
    return dict(b) if b else {"entrada": 0, "saida": 0, "chamadas": 0}


def set_vel_geracao(tok_s: float | None) -> None:
    """Velocidade REAL de geração (tokens/s) desta thread: só o trecho de
    GERAR tokens (depois do 1º chunk) — sem o pré-processamento do prompt.
    O rodapé antigo dividia saída pelo tempo TOTAL e o 'tok/s' caía com o
    prompt grande sem a GPU estar mais lenta."""
    _local.vel_geracao = tok_s


def vel_geracao() -> float | None:
    return getattr(_local, "vel_geracao", None)


def _balanco_somar(entrada: int, saida: int) -> None:
    b = getattr(_local, "balanco", None)
    if b is None:
        b = {"entrada": 0, "saida": 0, "chamadas": 0}
        _local.balanco = b
    b["entrada"] += int(entrada or 0)
    b["saida"] += int(saida or 0)
    b["chamadas"] += 1


# ---------- modo Redis (compartilhado: API + scripts CLI) ----------

def _registrar_redis(cliente, entrada: int, saida: int) -> None:
    svc = servico_atual()
    pipe = cliente.pipeline()
    for alvo in (f"rag:uso:{svc}", "rag:uso:total"):
        pipe.incrby(f"{alvo}:entrada", int(entrada or 0))
        pipe.incrby(f"{alvo}:saida", int(saida or 0))
        pipe.incr(f"{alvo}:chamadas")
    pipe.execute()


def _ler_redis(cliente, chave: str) -> dict:
    return {"entrada": int(cliente.get(f"{chave}:entrada") or 0),
            "saida": int(cliente.get(f"{chave}:saida") or 0),
            "chamadas": int(cliente.get(f"{chave}:chamadas") or 0)}


# ---------- modo arquivo (sem Redis — por processo) ----------

def _vazio() -> dict:
    return {"entrada": 0, "saida": 0, "chamadas": 0}


def _carregar() -> dict:
    try:
        dados = json.loads(ARQUIVO.read_text(encoding="utf-8"))
        if isinstance(dados.get("por_servico"), dict):
            dados.setdefault("total", _vazio())
            return dados
    except Exception:
        pass
    return {"por_servico": {}, "total": _vazio(),
            "desde": time.strftime("%Y-%m-%dT%H:%M:%S")}


_mem = _carregar()
_ultima_gravacao = 0.0
_chamadas_desde_gravacao = 0


def _gravar() -> None:
    try:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        ARQUIVO.write_text(json.dumps(_mem, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    except Exception:
        pass  # contagem é telemetria: nunca derruba a resposta


# ---------- API pública (mesma interface nos dois modos) ----------

def registrar(entrada: int, saida: int) -> None:
    """Acumula o uso de UMA chamada LLM (prompt/completion tokens) — no
    total global E no balanço desta thread (ver balanco_reset)."""
    _balanco_somar(entrada, saida)
    cliente = _redis_client()
    if cliente is not None:
        try:
            _registrar_redis(cliente, entrada, saida)
            return
        except Exception:
            pass  # Redis caiu no meio: conta no arquivo
    global _ultima_gravacao, _chamadas_desde_gravacao
    with _lock:
        svc = _mem["por_servico"].setdefault(servico_atual(), _vazio())
        for alvo in (svc, _mem["total"]):
            alvo["entrada"] += int(entrada or 0)
            alvo["saida"] += int(saida or 0)
            alvo["chamadas"] += 1
        _chamadas_desde_gravacao += 1
        agora = time.time()
        if agora - _ultima_gravacao > 5 or _chamadas_desde_gravacao >= 25:
            _gravar()
            _ultima_gravacao = agora
            _chamadas_desde_gravacao = 0


def marcador() -> dict:
    """Fotografia do total (para medir o consumo de UMA interação por diff)."""
    cliente = _redis_client()
    if cliente is not None:
        try:
            return _ler_redis(cliente, "rag:uso:total")
        except Exception:
            pass
    with _lock:
        return dict(_mem["total"])


def uso_desde(marcador: dict) -> dict:
    """Quanto foi consumido desde o `marcador` (diff de totais)."""
    cliente = _redis_client()
    if cliente is not None:
        try:
            t = _ler_redis(cliente, "rag:uso:total")
            return {"entrada": t["entrada"] - marcador.get("entrada", 0),
                    "saida": t["saida"] - marcador.get("saida", 0),
                    "chamadas": t["chamadas"] - marcador.get("chamadas", 0)}
        except Exception:
            pass
    with _lock:
        t = _mem["total"]
        return {"entrada": t["entrada"] - marcador.get("entrada", 0),
                "saida": t["saida"] - marcador.get("saida", 0),
                "chamadas": t["chamadas"] - marcador.get("chamadas", 0)}


def totais() -> dict:
    """Panorama completo (administração): por serviço + total."""
    cliente = _redis_client()
    if cliente is not None:
        try:
            por = {s: _ler_redis(cliente, f"rag:uso:{s}") for s in SERVICOS
                   if cliente.exists(f"rag:uso:{s}:chamadas")}
            return {"por_servico": por, "total": _ler_redis(cliente, "rag:uso:total"),
                    "desde": "", "fonte": "redis"}
        except Exception:
            pass
    with _lock:
        _gravar()
        por = {k: dict(v) for k, v in sorted(_mem["por_servico"].items())}
        return {"por_servico": por, "total": dict(_mem["total"]),
                "desde": _mem.get("desde", ""), "fonte": "arquivo"}
