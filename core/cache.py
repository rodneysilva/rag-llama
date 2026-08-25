"""
Cache semântico das consultas: pergunta igual OU PARECIDA → resposta na hora.

O Redis (docker rag-redis :6379) guarda pares (pergunta → resposta) com o
vetor bge-m3 da pergunta. Na consulta:

  1. embeda a pergunta (o :8081 está SEMPRE no ar — ~10 ms)
  2. compara com os vetores em cache (cosseno)
  3. similaridade ≥ LIMIAR (.env CACHE_LIMIAR, padrão 0.97) → devolve a
     resposta guardada SEM consultar Qdrant/LLM
  4. senão o fluxo normal roda; a resposta vai para o cache

Por que vetor e não chave exata: "como configurar o Qdrant?" e "como eu
configuro o Qdrant" são a mesma pergunta para o bge-m3, mas chaves Redis
diferentes — o cache semântico acerta as duas.

Limites: MAX entradas (LRU pelas mais recentes), TTL de 7 dias. Se o Redis
estiver fora, tudo vira no-op — a consulta nunca derruba por causa do cache.
"""
import os
import time

import numpy as np
import redis as _redis

from . import telemetria

LIMIAR = float(os.getenv("CACHE_LIMIAR", "0.97"))
MAX_ENTRADAS = int(os.getenv("CACHE_MAX", "1000"))   # app-wide: 200 era pouco
# TTL configurável (.env CACHE_TTL_DIAS; padrão 30 — HIT do cache é 0 s +
# 0 tokens vs ~0,3 s do Qdrant e 10-60 s da LLM: manter respostas por mais
# tempo compensa MUITO mais que tunar o Qdrant; limpeza manual: DELETE /api/cache
TTL_S = int(os.getenv("CACHE_TTL_DIAS", "30")) * 24 * 3600


def _ttl_s() -> int:
    """TTL AO VIVO (config.CACHE_TTL_DIAS — editável na tela de Sistema sem
    restart; o módulo é recarregado por config.reload())."""
    from . import config as _cfg
    return max(1, int(getattr(_cfg, "CACHE_TTL_DIAS", 30))) * 86400


def _maxentradas() -> int:
    from . import config as _cfg
    return max(10, int(getattr(_cfg, "CACHE_MAX", 1000)))


def _limiar() -> float:
    from . import config as _cfg
    return min(1.0, max(0.5, float(getattr(_cfg, "CACHE_LIMIAR", 0.97))))

_IDX = "rag:cache:idx"      # zset: score=timestamp, member=id da entrada
_PREFIXO = "rag:cache:e:"   # hash por entrada: pergunta/resposta/vetor/meta

_r: _redis.Redis | None = None
_r_off_desde: float = 0.0        # quando o Redis ficou fora (retry com TTL)
_RETRY_S = 60.0                  # re-testa o Redis 1x/min (bug: antes era p/ sempre)


def _redis_client() -> _redis.Redis | None:
    global _r, _r_off_desde
    if _r is False:  # fora do ar: re-testa após o TTL (Redis pode subir depois)
        import time as _t
        if _t.time() - _r_off_desde < _RETRY_S:
            return None
        _r = None
    if _r is None:
        try:
            cliente = _redis.Redis(host=os.getenv("REDIS_HOST", "127.0.0.1"),
                                   port=int(os.getenv("REDIS_PORT", "6379")),
                                   socket_connect_timeout=0.3, decode_responses=False)
            cliente.ping()
            _r = cliente
        except Exception:
            import time as _t
            _r = False
            _r_off_desde = _t.time()
    return _r or None


def _embed(pergunta: str) -> np.ndarray:
    from . import rag  # import tardio: rag puxa langchain na carga
    return np.array(rag.embeddings().embed_query(pergunta), dtype=np.float32)


def _cosseno(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na and nb else 0.0


def _escopo(colecoes: list[str] | None, owner: str = "") -> str:
    """Chave de CONTEXTO do cache: coleções resolvidas (sorted) + DONO.
    GUARDRAIL de isolamento (pedido do dono): o cache é contextualizado —
    responde dentro do MESMO escopo (usuário + coleções + modelo); sessões
    de outro usuário (ou outro escopo de coleções) NUNCA veem a resposta.
    Sessions do mesmo dono compartilham: perguntas autossuficientes (sem
    histórico) têm a mesma resposta — sem "assunto de outra sessão"
    dirigindo a conversa, porque a resposta guardada foi gerada SEM
    histórico (autocontida)."""
    return f"{(owner or '')[:60]}|{'|'.join(sorted(colecoes or []))}"


def _int(b) -> int:
    """bytes do Redis -> int (0 se ausente/lixo)."""
    try:
        return int(b)
    except Exception:
        return 0


def limpar_sid(sid: str) -> int:
    """Apaga as entradas de cache GERADAS por uma sessão (o pedido do dono:
    apagar a conversa apaga o que ela deixou — sem tocar as outras)."""
    cliente = _redis_client()
    if cliente is None or not sid:
        return 0
    n = 0
    try:
        for eid in cliente.zrange(_IDX, 0, -1):
            d = cliente.hgetall(_PREFIXO + eid.decode())
            if d and d.get(b"sid", b"").decode() == sid:
                cliente.delete(_PREFIXO + eid.decode())
                cliente.zrem(_IDX, eid)
                n += 1
    except Exception:
        pass
    return n


def limpar_colecoes(colecoes: list[str]) -> int:
    """Apaga as entradas cujo ESCOPO toca qualquer das coleções ingeridas
    (invalidação por REINGESTÃO — bug real: a base ganhou o doc do frango
    e o cache devolvia a recusa ANTIGA com 0.979; com "ensinar a base" no
    chat, a resposta obsoleta na hora errada virou mentira).

    Escopos "todas" (sem coleções na chave) também caem: a ingestão
    afeta quem consulta sem filtro."""
    cliente = _redis_client()
    if cliente is None or not colecoes:
        return 0
    alvo = {str(c) for c in colecoes if c}
    n = 0
    try:
        for eid in cliente.zrange(_IDX, 0, -1):
            d = cliente.hgetall(_PREFIXO + eid.decode())
            if not d:
                continue
            escopo = d.get(b"colecoes", b"").decode()
            # escopo = "owner|colA|colB" (sorted no store) — vazio = "todas"
            cols = {x for x in escopo.split("|")[1:] if x}
            if not cols or (cols & alvo):
                cliente.delete(_PREFIXO + eid.decode())
                cliente.zrem(_IDX, eid)
                n += 1
    except Exception:
        pass
    return n


def lookup(pergunta: str, colecoes: list[str] | None = None,
           modelo: str = "", owner: str = "") -> dict | None:
    """Resposta em cache para a pergunta (ou parecida), ou None.

    GUARDRAIS (todos):
    1. escopo = DONO + coleções resolvidas (sorted) — sem vazamento
       cross-usuário/cross-escopo;
    2. `modelo`: resposta de outro modelo não serve;
    3. só consultas SEM histórico chamam o lookup (o chamador garante) —
       follow-up nunca vem do cache;
    4. a resposta guardada foi GERADA sem histórico: autocontida, não
       referencia assunto de sessão alguma."""
    cliente = _redis_client()
    if cliente is None:
        return None
    alvo_colecoes = _escopo(colecoes, owner)
    # MATCH EXATO: o llama-server tem não-determinismo numérico no
    # embedding (mesmo texto → cosseno 0.97-0.99, às vezes ABAIXO do
    # limiar) — pergunta REPETIDA palavra por palavra bate direto pela
    # STRING (normalizada), sem depender do vetor
    alvo_txt = " ".join((pergunta or "").lower().split())
    try:
        ids = cliente.zrange(_IDX, 0, -1)  # mais recentes por último
        if not ids:
            return None
        alvo = _embed(pergunta)
        melhor, nota, exata = None, 0.0, None
        for eid in ids[-_maxentradas():]:
            dados = cliente.hgetall(_PREFIXO + eid.decode())
            if not dados:
                continue
            if dados.get(b"colecoes", b"").decode() != alvo_colecoes:
                continue  # escopo/dono diferente: resposta não serve
            m_guardado = dados.get(b"modelo", b"").decode()
            if modelo and m_guardado and m_guardado != modelo:
                continue  # outro modelo respondeu: cache não serve
            if exata is None and " ".join(
                    dados.get(b"pergunta", b"").decode(
                        "utf-8", "replace").lower().split()) == alvo_txt:
                exata = dados
            vetor = np.frombuffer(dados[b"vetor"], dtype=np.float32)
            if vetor.shape != alvo.shape:
                continue  # outra dimensão de embedding: cache velho
            s = _cosseno(alvo, vetor)
            if s > nota:
                melhor, nota = dados, s
        if exata is not None:
            cliente.zadd(_IDX, {exata[b"id"]: time.time()})  # renova o LRU
            telemetria.evento("cache", f"⚡ HIT (exata) para: {pergunta[:60]}")
            return {"pergunta": exata[b"pergunta"].decode("utf-8", "replace"),
                    "resposta": exata[b"resposta"].decode("utf-8", "replace"),
                    "modo": exata.get(b"modo", b"?").decode(),
                    "modelo": exata.get(b"modelo", b"").decode(),
                    "criado_em": _int(exata.get(b"criado_em")),
                    "colecoes": [c for c in exata.get(b"colecoes", b"")
                                             .decode().split("|")[1:] if c],
                    "similaridade": 1.0}
        if melhor is not None and nota >= _limiar():
            cliente.zadd(_IDX, {melhor[b"id"]: time.time()})  # renova o LRU
            telemetria.evento("cache", f"⚡ HIT (sim={round(nota, 4)}) "
                                       f"para: {pergunta[:60]}",
                              similaridade=round(nota, 4))
            return {"pergunta": melhor[b"pergunta"].decode("utf-8", "replace"),
                    "resposta": melhor[b"resposta"].decode("utf-8", "replace"),
                    "modo": melhor.get(b"modo", b"?").decode(),
                    "modelo": melhor.get(b"modelo", b"").decode(),
                    "criado_em": _int(melhor.get(b"criado_em")),
                    "colecoes": [c for c in melhor.get(b"colecoes", b"")
                                              .decode().split("|")[1:] if c],
                    "similaridade": round(nota, 4)}
    except Exception as e:
        print(f"⚠️  Cache semântico (lookup): {e}")
    return None


def store(pergunta: str, resposta: str, modo: str = "rag",
          colecoes: list[str] | None = None, modelo: str = "",
          owner: str = "", sid: str = "") -> bool:
    """Guarda a resposta para a pergunta (dedup: parecida substitui).

    Resposta vazia/sem conteúdo NÃO entra no cache — um miss momentâneo
    (Qdrant fora, LLM pausada) não pode ficar colado por 7 dias. `sid`
    (sessão do chat) permite limpar SÓ o que aquela sessão gerou quando
    ela é apagada."""
    cliente = _redis_client()
    if cliente is None or not (resposta or "").strip():
        return False
    try:
        alvo = _embed(pergunta)
        # substitui entrada parecida em vez de acumular duplicatas
        for eid in cliente.zrange(_IDX, 0, -1)[-_maxentradas():]:
            dados = cliente.hgetall(_PREFIXO + eid.decode())
            if not dados:
                continue
            vetor = np.frombuffer(dados[b"vetor"], dtype=np.float32)
            if vetor.shape == alvo.shape and _cosseno(alvo, vetor) >= _limiar():
                cliente.delete(_PREFIXO + eid.decode())
                cliente.zrem(_IDX, eid)
        eid = str(int(time.time() * 1000))
        pipe = cliente.pipeline()
        pipe.hset(_PREFIXO + eid, mapping={
            "id": eid, "pergunta": pergunta, "resposta": resposta,
            "modo": modo, "modelo": modelo or "",
            # ESCOPO contextualizado (dono+coleções sorted) — o lookup
            # compara com o MESMO formato; sem isto entradas cruzadas
            "colecoes": _escopo(colecoes, owner),
            "vetor": alvo.tobytes(), "criado_em": int(time.time()),
            **({"sid": sid} if sid else {})})
        pipe.expire(_PREFIXO + eid, _ttl_s())
        pipe.zadd(_IDX, {eid: time.time()})
        pipe.expire(_IDX, _ttl_s())
        # LRU: mantém só as MAX_ENTRADAS mais recentes
        excedentes = cliente.zcard(_IDX) - _maxentradas()
        if excedentes > 0:
            for velho in cliente.zrange(_IDX, 0, excedentes - 1):
                pipe.delete(_PREFIXO + velho.decode())
                pipe.zrem(_IDX, velho)
        pipe.execute()
        telemetria.evento("cache", f"💾 STORE ({len(colecoes or [])} coleção(ões)) "
                                   f"para: {pergunta[:60]}")
        return True
    except Exception as e:
        print(f"⚠️  Cache semântico (store): {e}")
        return False


def limpar() -> int:
    """Apaga todo o cache (botão da webui / manutenção)."""
    cliente = _redis_client()
    if cliente is None:
        return 0
    try:
        ids = cliente.zrange(_IDX, 0, -1)
        if ids:
            cliente.delete(*[_PREFIXO + i.decode() for i in ids])
        cliente.delete(_IDX)
        return len(ids)
    except Exception:
        return 0


def info() -> dict:
    """Estado do cache para o painel da webui — com as ENTRADAS recentes
    (pergunta, modo, coleções, trecho da resposta) para o log estruturado."""
    cliente = _redis_client()
    if cliente is None:
        return {"online": False, "entradas": 0, "limiar": LIMIAR, "max": MAX_ENTRADAS,
                "lista": []}
    try:
        ids = cliente.zrange(_IDX, 0, -1)
        lista = []
        for eid in reversed(ids[-20:]):  # mais recentes primeiro, até 20
            d = cliente.hgetall(_PREFIXO + eid.decode())
            if not d:
                continue
            lista.append({
                "pergunta": d.get(b"pergunta", b"").decode("utf-8", "replace")[:120],
                "modo": d.get(b"modo", b"?").decode(),
                "modelo": d.get(b"modelo", b"").decode(),
                "colecoes": [c for c in d.get(b"colecoes", b"").decode().split("|") if c],
                "resumo": d.get(b"resposta", b"").decode("utf-8", "replace")[:160],
                "criado_em": _int(d.get(b"criado_em")),
            })
        return {"online": True, "entradas": cliente.zcard(_IDX),
                "limiar": LIMIAR, "max": MAX_ENTRADAS, "lista": lista}
    except Exception:
        return {"online": False, "entradas": 0, "limiar": LIMIAR, "max": MAX_ENTRADAS,
                "lista": []}
