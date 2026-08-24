"""Síntese do raciocínio — o log do "pensando…" em PASSOS semanticamente
coerentes (pedido do dono: "está sendo cortado, precisa ser sintetizado,
usa o embedding").

Como funciona:
  1. emba as linhas em UM lote (bge-m3 — o :8081 está sempre no ar);
  2. agrupa linhas CONSECUTIVAS com cosseno ≥ LIMIAR (0.70) num mesmo
     passo — linhas de busca ficam juntas, a reformulação separa, a
     geração separa, os tokens juntos;
  3. o TÍTULO do passo é a linha mais CENTRAL dele (maior soma de
     similaridade com as demais) — a linha que melhor representa o bloco;
  4. NADA se perde: as linhas completas ficam a um clique (details).

Fallback determinístico (embedding fora/erro — a estação pode estar
reiniciando): agrupa por `grupo` (mensagem/busca/geração/tokens/…),
título = 1ª linha do grupo. Sem sintese, sem quebrar.
"""
from __future__ import annotations

import math

LIMIAR = 0.58          # cosseno mínimo entre CONSECUTIVAS para o mesmo passo
MAX_EMB_CHARS = 300    # emba o começo da linha (o resto é ruído de score/url)

# emojis/números/pontuação DOURAM o sinal do embedding (números distintos
# derrubam o cosseno de linhas semanticamente iguais): o embedding vê o
# TEXTO puro — "qdrant culinaria consulta densa" ↔ "qdrant culinaria
# denso textual escolhido" ficam juros de vez)
_RE_NAO_SEMANTICO = None


def _txt_semantico(msg: str) -> str:
    global _RE_NAO_SEMANTICO
    import re
    if _RE_NAO_SEMANTICO is None:
        _RE_NAO_SEMANTICO = re.compile(
            "[^a-zà-ÿ0-9 ]+", re.IGNORECASE)
    t = _RE_NAO_SEMANTICO.sub(" ", str(msg).lower())
    return " ".join(p for p in t.split() if len(p) > 2
                    and not p.isdigit())[:MAX_EMB_CHARS]


def _cosseno(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (na * nb)


def _emb_lote(textos: list[str]) -> list[list[float]] | None:
    try:
        from . import rag
        semantico = [_txt_semantico(t) or str(t)[:40] for t in textos]
        return rag.embeddings().embed_documents(semantico)
    except Exception as e:
        print(f"⚠️ sintese: embedding indisponível ({e}) — fallback por grupo")
        return None


def _titulo_central(linhas: list[dict], vetores) -> int:
    """Índice da linha mais CENTRAL do passo (maior soma de cosseno com as
    demais do próprio passo) — a que melhor representa o bloco."""
    if not vetores:
        return 0
    n = len(linhas)
    if n == 1:
        return 0
    melhor, melhor_soma = 0, -1.0
    for i in range(n):
        soma = sum(_cosseno(vetores[i], vetores[j])
                   for j in range(n) if j != i)
        if soma > melhor_soma:
            melhor, melhor_soma = i, soma
    return melhor


def sintetizar(linhas: list[dict]) -> list[dict]:
    """[{ts, msg, grupo?}] → [{titulo, ts, n, linhas}] — passos prontos
    para render; o título resume, as linhas completas acompanham."""
    if not linhas:
        return []
    vetores = _emb_lote([str(l.get("msg") or "") for l in linhas]) or None

    # FASES: linhas consecutivas do MESMO grupo formam um passo-base
    # (mensagem → cache → busca → geração → tokens…), e DENTRO de uma fase
    # longa o EMBEDING sub-agrupa consecutivas parecidas (cosseno ≥ LIMIAR
    # no texto semântico) — ex.: busca em 3 coleções vira 3 sub-passos.
    fases: list[list[int]] = [[0]]
    for i in range(1, len(linhas)):
        g_ant, g_agora = linhas[i - 1].get("grupo"), linhas[i].get("grupo")
        if g_ant is not None and g_agora is not None and g_ant != g_agora:
            fases.append([i])            # mudou a fase: novo passo
        else:
            fases[-1].append(i)

    grupos_idx: list[list[int]] = []
    for fase in fases:
        if vetores is None or len(fase) <= 2:
            grupos_idx.append(fase)      # fase curta: um passo só
            continue
        sub = [fase[0]]                  # sub-agrupa por embedding
        for k in range(1, len(fase)):
            if _cosseno(vetores[fase[k]], vetores[fase[k - 1]]) >= LIMIAR:
                sub.append(fase[k])
            else:
                grupos_idx.append(sub)
                sub = [fase[k]]
        grupos_idx.append(sub)

    passos = []
    for idxs in grupos_idx:
        sub = [linhas[i] for i in idxs]
        vs = [vetores[i] for i in idxs] if vetores is not None else None
        t = _titulo_central(sub, vs)
        passos.append({
            "titulo": sub[t].get("msg") or "",
            "ts": sub[0].get("ts") or sub[t].get("ts") or "",
            "n": len(sub),
            "linhas": sub,
        })
    return passos
