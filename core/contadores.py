"""
Contagem de tokens: TUDO que passa pelo llama-server de conversa (:8090) é
medido pelo `usage` que o próprio servidor devolve em cada chamada e
acumulado por SERVIÇO (chat, ingestão, seed, limpeza, estúdio, manutenção,
sistema/testes).

Onde acumula (decisão do dono 27/08 — Redis removido): ARQUIVO DO SERVIDOR
`logs/uso_llm.jsonl` — cada chamada é UM append atômico (O_APPEND, linha
curta): API e scripts CLI somam NO MESMO total sem broker e sem lock
entre processos; `totais()` agrega o arquivo (cache 3 s por mtime).
O contexto de serviço é thread-local: rotas/jobs marcam o que estão
fazendo no início e todas as chamadas LLM daquela thread contam para o
serviço certo.
"""
import json
import threading
import time
from pathlib import Path

ARQUIVO = Path(__file__).resolve().parent.parent / "logs" / "uso_llm.jsonl"
_lock = threading.Lock()
_local = threading.local()

SERVICOS = ("chat", "ingestao", "seed", "limpeza", "estudio", "manutencao", "sistema")


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


# ---------- balanço da THREAD (determinístico por job) ----------

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
    GERAR tokens (depois do 1º chunk) — sem o pré-processamento do prompt."""
    _local.vel_geracao = tok_s


def vel_geracao() -> float | None:
    return getattr(_local, "vel_geracao", None)


def ultima_chamada() -> dict:
    """Saída e duração da ÚLTIMA chamada LLM desta thread (fallback do tok/s
    quando não houve streaming — sem 1º token marcado não há taxa pura)."""
    return dict(getattr(_local, "ultima", {}) or {})


def _balanco_somar(entrada: int, saida: int) -> None:
    b = getattr(_local, "balanco", None)
    if b is None:
        b = {"entrada": 0, "saida": 0, "chamadas": 0}
        _local.balanco = b
    b["entrada"] += int(entrada or 0)
    b["saida"] += int(saida or 0)
    b["chamadas"] += 1


# ---------- persistência: append atômico em JSONL (API + CLI no mesmo
# total, sem broker — cada linha é uma chamada LLM) ----------

def registrar(entrada: int, saida: int, duracao_s: float = 0) -> None:
    """Acumula o uso de UMA chamada LLM (prompt/completion tokens) — no
    balanço desta thread E no arquivo do servidor (append). A `duracao_s`
    alimenta o tok/s do rodapé quando não houve streaming (a taxa caía no
    'saída/duração do JOB', que inclui busca web e fila)."""
    _balanco_somar(entrada, saida)
    try:
        _local.ultima = {"saida": int(saida or 0),
                         "duracao_s": round(float(duracao_s or 0), 2)}
    except Exception:
        pass
    try:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        with open(ARQUIVO, "a", encoding="utf-8") as f:   # O_APPEND
            f.write(json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "servico": servico_atual(),
                 "entrada": int(entrada or 0), "saida": int(saida or 0)},
                ensure_ascii=False) + "\n")
    except Exception:
        pass  # contagem é telemetria: nunca derruba a resposta


_AGREGADO: dict = {"mtime": 0.0, "t": 0.0, "dados": None}


def _agregar(forcar: bool = False) -> dict:
    """Soma o JSONL inteiro (cache 3 s por mtime — o /hx/contagem pola
    a cada 5 s). Retorna {por_servico, total, desde}."""
    agora = time.time()
    try:
        mtime = ARQUIVO.stat().st_mtime
    except OSError:
        mtime = 0.0
    if (not forcar and _AGREGADO["dados"] is not None
            and mtime == _AGREGADO["mtime"]
            and agora - _AGREGADO["t"] < 3):
        return _AGREGADO["dados"]
    por: dict[str, dict] = {}
    total = {"entrada": 0, "saida": 0, "chamadas": 0}
    desde = ""
    try:
        with open(ARQUIVO, encoding="utf-8") as f:
            for linha in f:
                try:
                    d = json.loads(linha)
                except ValueError:
                    continue
                desde = desde or str(d.get("ts", ""))
                svc = str(d.get("servico") or "sistema")
                alvo = por.setdefault(
                    svc, {"entrada": 0, "saida": 0, "chamadas": 0})
                for a in (alvo, total):
                    a["entrada"] += int(d.get("entrada") or 0)
                    a["saida"] += int(d.get("saida") or 0)
                    a["chamadas"] += 1
    except OSError:
        pass
    dados = {"por_servico": {k: v for k, v in sorted(por.items())},
             "total": total, "desde": desde, "fonte": "arquivo"}
    _AGREGADO.update(mtime=mtime, t=agora, dados=dados)
    return dados


def marcador() -> dict:
    """Fotografia do total (para medir o consumo de UMA interação por diff)."""
    return dict(_agregar(forcar=True)["total"])


def uso_desde(marcador: dict) -> dict:
    """Quanto foi consumido desde o `marcador` (diff de totais)."""
    t = _agregar(forcar=True)["total"]
    return {"entrada": t["entrada"] - marcador.get("entrada", 0),
            "saida": t["saida"] - marcador.get("saida", 0),
            "chamadas": t["chamadas"] - marcador.get("chamadas", 0)}


def totais() -> dict:
    """Panorama completo (administração): por serviço + total."""
    return _agregar()
