"""Conjuntos de modelos por tarefa (unificação do estúdio no chat).

Cada tarefa declara o CONJUNTO de motores que precisa estar no ar (e o que
precisa estar FORA — 8 GB de VRAM não cabe tudo). `garantir(tarefa)`:

- compara com o conjunto ATIVO (`saidas/conjunto_ativo.json`);
- SE IGUAL → nada faz (cache da GPU permanece quente — trocar à toa é
  desperdício e joga fora o contexto já carregado);
- SE DIFERENTE → transiciona: derruba o que sai (limpeza de VRAM antes de
  subir o novo — "toda mudança precisa de limpeza do cache da GPU, só em
  caso de mudança"), sobe o que entra e grava o novo ativo.

Famílias: `chat` (llm :8090 + embed :8081), `visao` (VL :8082 + embed —
chat embaixado: 2×7B + embed não cabem), `difusao` (sd-cli sobe por
processo; chat E visão embaixados, embed fica), `whisper` (processo; nada
muda — convive). Em container, a transição é PROXYADA ao agente do host
(`/conjunto/{tarefa}`) — quem tem a GPU é o host.
"""
import json
from pathlib import Path
from threading import Lock

from . import config

ATIVO_ARQ = Path(__file__).resolve().parent.parent / "saidas" / "conjunto_ativo.json"
_lock = Lock()

# tarefa → conjunto (o que deve estar NO AR); embed fica SEMPRE
CONJUNTOS = {
    "chat":    {"familias": ["chat"], "rotulo": "conversa (llm + embed)"},
    "visao":   {"familias": ["visao"], "rotulo": "multimodal (imagem→texto; chat embaixado)"},
    "difusao": {"familias": [], "rotulo": "difusão (sd-cli por processo; chat/multimodal embaixados, embed fica)"},
    "whisper": {"familias": ["chat"], "rotulo": "transcrição (whisper por processo; conversa segue)"},
}
# modalidade → tarefa
MODALIDADE_PARA = {
    "t2i": "difusao", "t2v": "difusao", "i2v": "difusao", "a2v": "difusao",
    "i2t": "visao", "v2t": "visao",
    "a2t": "whisper",
    "chat": "chat", "a2t_chat": "whisper",
}


def ativo() -> str:
    try:
        return str(json.loads(ATIVO_ARQ.read_text(encoding="utf-8"))
                   .get("familia", "chat"))
    except Exception:
        return "chat"  # estado inicial: conversa é o padrão do sistema


def _gravar(familia: str) -> None:
    ATIVO_ARQ.parent.mkdir(parents=True, exist_ok=True)
    ATIVO_ARQ.write_text(json.dumps({"familia": familia}), encoding="utf-8")


def _transicionar(de: str, para: str, log) -> None:
    """Derruba o que sai, sobe o que entra (via modelos — host ou proxy)."""
    from . import modelos
    log(f"🧹 troca de conjunto '{de}' → '{para}': limpando a VRAM "
        "(derruba o que sai ANTES de subir o novo)", "modelo")
    if de == "visao" and para != "visao":
        try:
            modelos.desligar_vl_manual(log=log)
        except Exception as e:
            log(f"⚠️ visão não desceu: {e}", "modelo")
    if de == "chat" and para in ("visao", "difusao"):
        try:
            modelos.desligar_llm_manual(log=log)
        except Exception as e:
            log(f"⚠️ chat não desceu: {e}", "modelo")
    if de == "difusao" and para in ("chat", "visao"):
        # sd-cli morre com o processo (tarefas) — VRAM já liberada ao trocar
        pass
    if para == "visao":
        try:
            r = modelos.ligar_vl_manual()  # remove marker e PRÉ-AQUECE o VL
            log(f"   👁 visão: {str(r)[:120]}", "modelo")
        except Exception as e:
            raise RuntimeError(f"visão não subiu: {e}")
    elif para == "chat" and de in ("visao", "difusao"):
        try:
            modelos.ligar_llm_manual()     # religa a conversa
        except Exception as e:
            log(f"⚠️ chat não voltou: {e} (o próximo uso tenta de novo)", "modelo")
    import time as _t
    _t.sleep(int(getattr(config, "ESTUDIO_VRAM_ASSENTAMENTO_S", 6) or 6))


def garantir(tarefa: str, log=print) -> str:
    """Garante o conjunto da tarefa no ar. Devolve a família ATIVA após a
    chamada. Mesma família → cache quente mantido (nada faz)."""
    familia = MODALIDADE_PARA.get(tarefa, tarefa if tarefa in CONJUNTOS else "chat")
    with _lock:
        atual = ativo()
        if atual == familia:
            log(f"🎛️ conjunto '{CONJUNTOS[familia]['rotulo']}' já ativo — "
                "cache da GPU mantido (sem troca)", "modelo")
            return atual
        if getattr(config, "RAGAROY_CONTAINER", os_env_container()):
            # GPU é do host: a transição é proxyada ao agente (:8010)
            _transicionar_proxy(familia, log)
        else:
            _transicionar(atual, familia, log)
        _gravar(familia)
        log(f"✅ conjunto '{CONJUNTOS[familia]['rotulo']}' garantido", "modelo")
        return familia


def os_env_container() -> bool:
    import os
    return os.getenv("RAGAROY_CONTAINER", "") == "1"


def _transicionar_proxy(familia: str, log) -> None:
    import httpx
    base = str(getattr(config, "AGENTE_HOST_URL", "http://host.docker.internal:8010"))
    r = httpx.post(f"{base.rstrip('/')}/conjunto/{familia}", timeout=600)
    r.raise_for_status()
    for linha in (r.json().get("linhas") or []):
        log(str(linha), "modelo")
