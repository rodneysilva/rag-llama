"""
Varredura LLM de coleções — APOSENTADA (29/08, decisão do dono).

Prova definitiva de 28/08: o modelo de 7B local não executa o julgamento
estruturado (N trechos → JSON seletivo) de forma confiável — 77% de
falso-positivo com motivos AUTO-CONTRADITÓRIOS ("Conteúdo específico
sobre X, não é lixo" marcando como lixo), mesmo com spec corrigida E
strip do cabeçalho contextual. Instrução negativa e strip não resolvem.

A limpeza vive nas camadas DETERMINÍSTICAS: gate `score_chunk` na
ingestão, higienização (core/higieniza) e cura por score na Revisão.

O módulo segue importável APENAS para restauro pontual de pontos
apagados (logs/varredura_backup) por operador experiente — rota
POST /api/varredura responde 410 Gone, CLI recusa com orientação.
"""
import json
import sys
import time
from pathlib import Path

from qdrant_client import QdrantClient

from . import catalog, config, rag
from .specs import spec

LOTE = 10  # chunks julgados por chamada
BACKUP_DIR = Path(__file__).resolve().parent.parent / "logs" / "varredura_backup"


def _sem_cabecalho(texto: str) -> str:
    """Remove o cabeçalho contextual '[título · seção]' da 1ª linha do chunk.

    A LLM de varredura lia o cabeçalho como 'migalhas/navegação' e marcava
    conteúdo valioso como lixo (incidente 28/08: erros_comuns 26→0,
    restaurado do snapshot). Com o strip, o julgamento recai SÓ no conteúdo
    — determinístico, não depende de obediência a instrução negativa.
    """
    t = (texto or "").lstrip()
    if t.startswith("[") and "\n" in t:
        primeira, resto = t.split("\n", 1)
        if "]" in primeira and len(primeira) <= 220:
            return resto.strip()
    return t


def _julgar_lote(colecao: str, descricao: str, lote: list) -> list[dict]:
    """Pede à LLM os índices que são lixo claro; devolve [{id, motivo}]."""
    partes = []
    for n, p in enumerate(lote, 1):
        payload = p.payload or {}
        md = payload.get("metadata") or {}
        fonte = md.get("source") or md.get("arquivo") or "?"
        partes.append(f"[{n}] ({str(fonte)[-60:]}) "
                      f"{_sem_cabecalho(str(payload.get('page_content', '')))[:600]}")
    conteudo = (f"Coleção: {colecao}\n"
                f"Definição (catálogo): {descricao or '—'}\n\n"
                + "\n---\n".join(partes)
                + "\n\nETAPA: julgamento do lote.")
    r = rag.llm(temperature=0).invoke(f"{spec('varredura')}\n\n{conteudo}")
    ini, fim = r.content.find("{"), r.content.rfind("}")
    if ini < 0 or fim <= ini:
        return []
    try:
        d = json.loads(r.content[ini:fim + 1])
    except Exception:
        return []
    out = []
    for item in (d.get("lixo") or []):
        try:
            i = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(lote):
            out.append({"id": str(lote[i - 1].id),
                        "motivo": str(item.get("motivo", ""))[:80]})
    return out


def varredura_colecao(colecao: str, log=None) -> dict:
    """Varre a coleção inteira e apaga o lixo apontado pela LLM."""
    log = log or print
    if colecao == catalog.CATALOG_COLLECTION or colecao.startswith("mnemosyne_"):
        raise ValueError(f"'{colecao}' fora do escopo da varredura")
    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    if not client.collection_exists(colecao):
        raise ValueError(f"Coleção '{colecao}' não existe")

    pontos, offset = [], None
    while True:
        lote, offset = client.scroll(collection_name=colecao, limit=256,
                                     with_payload=True, offset=offset)
        pontos += lote
        if offset is None:
            break
    log(f"   🔎 {len(pontos)} chunk(s) para julgar na coleção '{colecao}'")
    try:
        descricao = catalog.list_meta(client).get(colecao, {}).get("descricao", "")
    except Exception:
        descricao = ""

    lixo = []
    for i in range(0, len(pontos), LOTE):
        lote = pontos[i:i + LOTE]
        try:
            marcados = _julgar_lote(colecao, descricao, lote)
        except Exception as e:
            log(f"   ⚠️ lote {i // LOTE + 1}: LLM falhou ({str(e)[:60]}) — mantido")
            continue
        for m in marcados:
            lixo.append(m)
            log(f"   🗑️  [{m['id'][:8]}] {m['motivo']}")
        if (i // LOTE) % 5 == 0:
            log(f"   … {min(i + LOTE, len(pontos))}/{len(pontos)} julgados")

    arq_backup = None
    if lixo:
        # BACKUP primeiro (payload completo): exclusão reversível por design
        por_id = {str(p.id): p for p in pontos}
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        arq_backup = BACKUP_DIR / f"{colecao}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        with open(arq_backup, "a", encoding="utf-8") as f:
            for m in lixo:
                p = por_id.get(m["id"])
                f.write(json.dumps({
                    "id": m["id"], "motivo": m["motivo"],
                    "payload": (p.payload if p else None) or {},
                }, ensure_ascii=False) + "\n")
        log(f"💾 backup dos {len(lixo)} ponto(s): {arq_backup.name}")

    for j in range(0, len(lixo), 256):
        client.delete(collection_name=colecao,
                      points_selector=[m["id"] for m in lixo[j:j + 256]])
    resumo = {"collection": colecao, "pontos": len(pontos),
              "lixo_apagado": len(lixo),
              "total_agora": client.count(colecao, exact=True).count,
              "backup": str(arq_backup) if arq_backup else None}
    log(f"🧹 '{colecao}': {resumo['lixo_apagado']} de {resumo['pontos']} "
        f"apagado(s) — restam {resumo['total_agora']}")
    return resumo


def main():
    """Entrada do CLI: APOSENTADA (29/08, decisão do dono).

    O 7B local não executa o julgamento estruturado de forma confiável
    (77% falso-positivo com motivos auto-contraditórios — ver
    /api/varredura 410). A limpeza vive nas camadas determinísticas:
    higienização (`python -X utf8 -m core.higieniza <colecao>`) e o gate
    score_chunk da ingestão. O módulo segue importável para restauros
    pontuais (logs/varredura_backup) por operador experiente."""
    sys.exit(
        "Varredura LLM APOSENTADA (29/08): o modelo local de 7B julga mal e\n"
        "apagava conteúdo valioso. Use a higienização determinística:\n"
        "  python -X utf8 -m core.higieniza <colecao>\n"
        "Restaurar pontos apagados: logs/varredura_backup/<colecao>_<ts>.jsonl")


if __name__ == "__main__":
    main()
