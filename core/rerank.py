"""Reranker cross-encoder LOCAL (CPU) — F2/Fase C.

Primitiva: `notas_de(consulta, textos)` devolve a nota de relevância
(0..1, sigmoid do logit) de cada texto contra a consulta — pergunta×texto
lidos JUNTOS na mesma rede. Dois consumidores:

- `rerank()` no chat: reordena os top achados da busca e devolve os N
  melhores para o prompt (menos contexto, mais precisão);
- gate de tema do modo Revisão (`core/preview.py`): nota de cada documento
  contra a definição da coleção-alvo (abaixo do limiar → "revisar").

Modelo vem do .env `RERANK_MODEL` (default BAAI/bge-reranker-base ~1,1 GB;
alternativa testada: BAAI/bge-reranker-v2-m3 ~2,3 GB, PT-BR melhor — compare
com tests_manual/bench_rerank.py). Carregamento LAZY por modelo; DEGRADA em
silêncio (None) sem torch ou com a flag RERANKER desligada — o chamador
sempre tem um caminho sem o rerank.

O modelo fica residente em CPU (não disputa a VRAM dos llama-servers);
"Parar tudo" pode derrubá-lo chamando descarregar().
"""
_modelos: dict[str, tuple] = {}   # id do modelo -> (tokenizer, model)
_aviso_indisponivel = False       # loga a degradação 1x só

# Limiar de SINAL do rerank: topo abaixo disto = notas sem confiança
# (calibrado empiricamente: relevante claro ~0.10+; ruído < 0.03).
SINAL_MIN = 0.03


def disponivel() -> bool:
    """torch+transformers importáveis (sem baixar nada)."""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _carregar(modelo: str, log):
    """Carrega (1x por processo e por modelo) o cross-encoder do cache do HF."""
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    log(f"🎛️ carregando reranker ({modelo}, CPU; 1ª vez baixa para o cache "
        "do HF)…", "busca")
    tok = AutoTokenizer.from_pretrained(modelo)
    mod = AutoModelForSequenceClassification.from_pretrained(modelo)
    mod.eval()
    _modelos[modelo] = (tok, mod)


def notas_de(consulta: str, textos: list[str], log=print,
             modelo: str | None = None) -> list[float] | None:
    """Notas de relevância (sigmoid 0..1) de CADA texto contra a consulta.

    None quando indisponível (flag off / torch ausente / erro): o chamador
    segue sem as notas — o rerank é refinamento, nunca ponto de falha."""
    global _aviso_indisponivel
    from . import config
    if not getattr(config, "RERANKER", True):
        return None
    if not disponivel():
        if not _aviso_indisponivel:
            _aviso_indisponivel = True
            log("🎛️ reranker indisponível (torch não instalado) — seguindo "
                "sem rerank; instale com 'pip install torch transformers'",
                "busca")
        return None
    modelo = modelo or getattr(config, "RERANK_MODEL", "BAAI/bge-reranker-base")
    try:
        if modelo not in _modelos:
            _carregar(modelo, log)
        import torch
        tok, mod = _modelos[modelo]
        pares = [(consulta, str(t)[:4000]) for t in textos]
        with torch.no_grad():
            entradas = tok(pares, padding=True, truncation=True,
                           max_length=512, return_tensors="pt")
            return [float(x) for x in
                    torch.sigmoid(mod(**entradas).logits.view(-1)).tolist()]
    except Exception as e:
        log(f"⚠️ rerank falhou ({str(e)[:120]}) — seguindo sem rerank", "busca")
        return None


def rerank(pergunta: str, achados: list, top_n: int = 4,
           log=print, modelo: str | None = None) -> tuple[list, float] | None:
    """[(doc, score, colecao)] → (top_n ordenados, nota do 1º); None quando
    indisponível OU SEM SINAL (o chamador mantém a ordem vetorial).

    SINAL ANTES DE REORDENAR: com query/base em idiomas mistos o
    cross-encoder dá notas absolutas ínfimas (top ~0.00) mesmo acertando a
    ordem RELATIVA — reordenar por isso é reordenar por ruído. Abaixo do
    limiar devolvemos None (a ordem da BUSCA vetorial é a confiável) e o
    log explica em português claro."""
    notas = notas_de(pergunta, [d.page_content for d, _, _ in achados],
                     log=log, modelo=modelo)
    if notas is None:
        return None
    topo = float(max(notas))
    if topo < SINAL_MIN:
        log(f"🎛️ rerank inconclusivo (top {topo:.3f} < {SINAL_MIN} — notas "
            "sem sinal: query e base em idiomas diferentes?) — mantida a "
            "ordem da busca vetorial", "busca")
        return None
    ordenados = [a for a, _ in
                 sorted(zip(achados, notas), key=lambda t: t[1],
                        reverse=True)][:top_n]
    return ordenados, topo


def descarregar() -> None:
    """Solta o(s) modelo(s) da memória (política do ⏹ Parar tudo)."""
    global _modelos
    _modelos = {}
