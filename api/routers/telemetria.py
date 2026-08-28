"""Rotas de telemetria — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.get("/api/contagem")
def contagem_tokens():
    """📊 Uso da LLM local (llama-server :8090) — TUDO é contado pelo usage
    que o servidor devolve: por serviço (chat, ingestão, seed, limpeza,
    estúdio, manutenção, sistema/testes) e total geral."""
    return contadores.totais()


@router.get("/api/historico")
def get_historico(tipo: str = "", limit: int = 40):
    """Histórico de execuções (ingestão/seed/limpeza/manutenção/tarefas):
    o que rodou, quando, quanto durou e o que produziu."""
    return {"execucoes": historico.ultimos(tipo or None, max(1, min(limit, 200)))}


@router.get("/api/historico/log/{job}")
def historico_log(job: str):
    """Log COMPLETO gravado de um job (logs/jobs/{job}.jsonl) — linha por
    linha com ts/grupo, exatamente como rodou. `job` sanitizado (anti
    path-traversal)."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", job):
        raise HTTPException(status_code=400, detail="id de job inválido")
    arq = PASTA_LOGS_JOBS / f"{job}.jsonl"
    if not arq.is_file():
        raise HTTPException(status_code=404, detail=f"não há log gravado para '{job}'")
    linhas = []
    for l in arq.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(l)
            linhas.append({"ts": str(d.get("ts", "")), "msg": str(d.get("msg", "")),
                           "grupo": str(d.get("grupo", d.get("etapa", "")))})
        except Exception:
            continue
    return {"job": job, "total": len(linhas), "lines": linhas}


@router.get("/hx/logs/{fonte}")
def hx_logs(fonte: str, request: Request):
    """TAIL AO VIVO com fonte QUE EXISTE NO SERVIDOR (pedido do dono: "os
    logs não aparecem em tempo real" — as fontes antigas apontavam arquivos
    do llama-server que vivem SÓ na estação com GPU). Agora:
      llm      → telemetria filtrada (cada chamada LLM: modelo/tokens/s)
      eventos  → telemetria completa (llm + jobs + cache + gerações)
      jobs     → últimas linhas dos logs/jobs/*.jsonl (o que rodou)
    """
    _usuario(request)
    import html as _h
    linhas: list[str] = []
    if fonte in ("llm", "eventos", "geracao"):
        evs = telemetria.ultimos(None if fonte == "eventos"
                                 else ("llm" if fonte == "llm" else "geracao"),
                                 40)
        linhas = [f"{e.get('ts','')} · {e.get('msg','')}" for e in evs]
        linhas.reverse()   # mais recente POR ÚLTIMO (como um log)
    elif fonte == "jobs":
        try:
            arqs = sorted(PASTA_LOGS_JOBS.glob("*.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:4]
            for arq in arqs:
                with open(arq, "rb") as f:
                    f.seek(max(0, (lambda s: s)(arq.stat().st_size) - 8192))
                    txt = f.read().decode("utf-8", errors="replace")
                for l in txt.splitlines()[-6:]:
                    try:
                        d = json.loads(l)
                        linhas.append(f"{d.get('ts','')} [{arq.stem}] "
                                      f"{str(d.get('msg',''))[:150]}")
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        raise HTTPException(status_code=404, detail="fonte desconhecida")
    html = "".join(f"<div>{_h.escape(l)}</div>" for l in linhas) \
        or '<div class="mini">sem linhas ainda…</div>'
    return HTMLResponse(f'<div class="log">{html}</div>')


@router.get("/api/telemetria")
def get_telemetria(tipo: str = "", limit: int = 60):
    """Histórico PERSISTENTE dos eventos de infraestrutura (tail do
    logs/telemetria.jsonl): `tipo` = llm|jobs (vazio = tudo). É o
    "como está trabalhando" de cada peça — cada chamada LLM
    (tokens/duração), cada job no executor async."""
    return {"eventos": telemetria.ultimos(tipo or None, max(1, min(limit, 300)))}


@router.get("/api/logs")
def logs_servico(fonte: str = "chat"):
    """Últimas linhas do log de um serviço (chat | embed | visao | api) —
    o topbar faz polling e mostra o tail ao vivo. Lê só o FINAL do arquivo
    (seek 64 KB), não o arquivo inteiro na memória."""
    alvo = _LOGS_FONTES.get(fonte)
    arquivo = alvo() if alvo else None
    if not arquivo or not arquivo.exists():
        return {"fonte": fonte, "arquivo": None, "linhas": []}
    try:  # logs são append binário do servidor: utf-8 com tolerate
        with open(arquivo, "rb") as f:
            f.seek(0, 2)  # fim
            fim = f.tell()
            f.seek(max(0, fim - 65536))
            texto = f.read().decode("utf-8", errors="replace")
        linhas = texto.splitlines()[-LOG_TAIL_LINHAS:]
        return {"fonte": fonte, "arquivo": arquivo.name, "linhas": linhas}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


