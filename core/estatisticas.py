"""Estatísticas por modelo de linguagem — insumo do Dashboard.

Lê o tail de `logs/telemetria.jsonl` (eventos tipo "llm" carregam
entrada/saida/duracao_s/modelo/servico) e agrega por modelo: chamadas,
tokens, throughput médio (tokens de SAÍDA por segundo — o que o usuário
espera) e tempo total. Cache por mtime: o dashboard bate a cada acesso e
o arquivo cresce a cada chamada — reler tudo sempre seria desperdício.

 memória: o tamanho do GGUF (GB de VRAM que o modelo pede) vem do
`modelos.listar()` no chamador — aqui só o que é telemetria.
"""
import json
from pathlib import Path

ARQ = Path(__file__).resolve().parent.parent / "logs" / "telemetria.jsonl"
_CACHE: dict = {"mtime": 0, "tamanho": 0, "dados": {}}


_RE_QUANT = None


def nome_curto(nome: str) -> str:
    """Nome de modelo CANÔNICO para agregar: minúsculo e SEM o sufixo de
    quantização (Wan2.1-T2V-1.3B-Q8_0 e wan2.1-t2v-1.3b são O MESMO modelo —
    sem isto o dashboard criava um card por sufixo)."""
    global _RE_QUANT
    import re as _re
    if _RE_QUANT is None:
        _RE_QUANT = _re.compile(r"[-_. ](q\d+[_ ]?\w*|bf16|fp16|f16)$",
                                _re.IGNORECASE)
    n = _RE_QUANT.sub("", str(nome or "").strip()).strip("-_ .")
    return n.lower()


def por_modelo(tail: int = 6000) -> dict[str, dict]:
    """{modelo: {chamadas, entrada, saida, segundos, tok_s, ultima}} —
    cobertura: LLMs de CONVERSA (tipo llm, servico chat/*), MULTIMODAL
    (tipo llm, servico multimodal) e GERAÇÃO (tipo geracao: flux/wan —
    chamadas e tempo, sem tokens). Throughput ponderado pelo tempo
    (sum(saida)/sum(duracao)), não a média das médias. Falhas → {}."""
    try:
        st = ARQ.stat()
        if st.st_mtime == _CACHE["mtime"] and st.st_size == _CACHE["tamanho"]:
            return _CACHE["dados"]
    except OSError:
        return {}

    agg: dict[str, dict] = {}
    try:
        linhas = ARQ.read_text(encoding="utf-8", errors="replace").splitlines()
        for linha in linhas[-tail:]:
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            tipo = ev.get("tipo")
            if tipo not in ("llm", "geracao"):
                continue
            nome = (ev.get("modelo") or ev.get("servico")
                    or ("multimodal" if ev.get("servico") == "multimodal" else "?"))
            nome = nome_curto(nome)   # agrega por modelo, não por quantização
            try:
                ent = int(ev.get("entrada") or 0)
                sai = int(ev.get("saida") or 0)
                dur = float(ev.get("duracao_s") or 0)
            except (TypeError, ValueError):
                continue
            m = agg.setdefault(nome, {"chamadas": 0, "entrada": 0, "saida": 0,
                                      "segundos": 0.0, "ultima": "",
                                      "multimodal": ev.get("servico") == "multimodal"})
            m["chamadas"] += 1
            m["entrada"] += ent
            m["saida"] += sai
            m["segundos"] += dur or (float(ev.get("segundos") or 0)
                                     if tipo == "geracao" else 0.0)
            m["ultima"] = max(m["ultima"], str(ev.get("ts", "")))
    except OSError:
        return {}

    for m in agg.values():
        m["segundos"] = round(m["segundos"], 1)
        m["tok_s"] = round(m["saida"] / m["segundos"], 1) if m["segundos"] > 1 and m["saida"] else None
    _CACHE.update(mtime=st.st_mtime if ARQ.exists() else 0,
                  tamanho=st.st_size if ARQ.exists() else 0, dados=agg)
    return agg


_EMB_CACHE: dict = {"mtime": 0, "tamanho": 0, "dados": {}}


def embedding_resumo(tail: int = 6000) -> dict:
    """Consumo do EMBEDDING (bge-m3): chamadas, documentos embedados e
    tempo total — eventos tipo "embed" da telemetria."""
    try:
        st = ARQ.stat()
        if st.st_mtime == _EMB_CACHE["mtime"] and st.st_size == _EMB_CACHE["tamanho"]:
            return _EMB_CACHE["dados"]
    except OSError:
        return {}
    r = {"chamadas": 0, "documentos": 0, "segundos": 0.0, "ultima": ""}
    try:
        for linha in ARQ.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]:
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            if ev.get("tipo") != "embed":
                continue
            r["chamadas"] += 1
            r["documentos"] += int(ev.get("docs") or 0)
            r["segundos"] += float(ev.get("duracao_s") or 0)
            r["ultima"] = max(r["ultima"], str(ev.get("ts", "")))
    except OSError:
        pass
    r["segundos"] = round(r["segundos"], 1)
    _EMB_CACHE.update(mtime=st.st_mtime if ARQ.exists() else 0,
                      tamanho=st.st_size if ARQ.exists() else 0, dados=r)
    return r


def cache_resumo(tail: int = 4000) -> dict:
    """Cache semântico: hits/stores (eventos tipo redis/cache) para o KPI
    do dashboard — 'quantos tokens o cache poupou' ≈ hits."""
    hits = stores = misses = 0
    try:
        for linha in ARQ.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]:
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            if ev.get("tipo") not in ("redis", "cache"):
                continue
            msg = str(ev.get("msg", "")).lower()
            if "hit" in msg or ev.get("hit"):
                hits += 1
            elif "store" in msg or "gravad" in msg:
                stores += 1
            elif "miss" in msg:
                misses += 1
    except OSError:
        pass
    return {"hits": hits, "stores": stores, "misses": misses}
