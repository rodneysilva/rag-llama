"""
Higienização de coleções JÁ GRAVADAS no Qdrant.

Aplica core.limpeza em cada ponto da coleção, SEM rebaixar a base:
- texto limpo (frases reconstituídas, citações/menus/widgets fora) → re-embeda
  no mesmo id quando muda;
- pontos que viram só estrutura de página (menu, referências, fragmento
  curto) são APAGADOS — são eles que poluem as pesquisas;
- duplicados exatos (mesma pasta ingerida 2×) são removidos, ficando 1.

Coleções com vetores nomeados (mnemosyne_*) e o catálogo (meta_colecoes) não
são higienizadas — só o vetor denso padrão do app.

CLI (a partir da raiz): python -X utf8 -m core.higieniza culinaria [outra...]
API: higienizar_colecao(colecao, log) -> dict com o resumo
"""
import hashlib
import re
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from . import catalog, config, rag
from .limpeza import e_lixo, limpar_texto

LOTE = 64  # pontos re-embedados por lote


def _todos_pontos(client: QdrantClient, colecao: str, log) -> list:
    """Lê todos os pontos da coleção (payload SEM vetor — quem muda re-embeda)."""
    pontos, offset = [], None
    while True:
        lote, offset = client.scroll(collection_name=colecao, limit=256,
                                     with_payload=True, with_vectors=False,
                                     offset=offset)
        pontos += lote
        if offset is None:
            log(f"   {len(pontos)} ponto(s) lido(s)")
            return pontos


def higienizar_colecao(colecao: str, log=None) -> dict:
    """Limpa uma coleção in-place e devolve o resumo do que foi feito."""
    log = log or print
    if colecao == catalog.CATALOG_COLLECTION:
        raise ValueError("O catálogo (meta_colecoes) não é higienizado")
    if colecao.startswith("mnemosyne_"):
        raise ValueError(f"'{colecao}' usa vetores nomeados (mnemosyne) — fora do escopo")

    client = QdrantClient(url=config.QDRANT_URL, timeout=60,
                          check_compatibility=False)
    if not client.collection_exists(colecao):
        raise ValueError(f"Coleção '{colecao}' não existe")

    pontos = _todos_pontos(client, colecao, log)
    emb = None
    reembedar, apagar_lixo, apagar_dup, intactos = [], [], [], 0
    vistos: dict[str, str] = {}  # hash do texto limpo -> id mantido

    for p in pontos:
        payload = p.payload or {}
        md = dict(payload.get("metadata") or {})
        original = str(payload.get("page_content", ""))
        if md.get("camada") == "codigo":
            # CÓDIGO não passa pela limpeza de prosa (que colapsa indentação
            # e costura linhas) nem pelo e_lixo (heurística de texto corrido)
            # — o ingest isenta por camada, a higienização precisa isentar tb
            intactos += 1
            continue
        # cabeçalho contextual de ingests novos não é ruído: preserva
        cab = ""
        m = re.match(r"^(\[[^\]\n]{3,120}\])\n", original)
        if m:
            cab, original = m.group(1) + "\n", original[m.end():]
        limpo = limpar_texto(original)
        if e_lixo(limpo):
            apagar_lixo.append(str(p.id))
            continue
        chave = hashlib.md5(re.sub(r"\s+", " ", limpo).strip()
                            .encode("utf-8")).hexdigest()
        if chave in vistos:
            apagar_dup.append(str(p.id))
            continue
        vistos[chave] = str(p.id)
        novo_texto = cab + limpo
        if novo_texto == str(payload.get("page_content", "")):
            intactos += 1
            continue
        md.setdefault("arquivo", str(md.get("source", "?")).replace("\\", "/")
                      .rsplit("/", 1)[-1])
        reembedar.append((p.id, novo_texto, md))

    # 1) apagar o que não tem semântica (ou é duplicado)
    for alvo, rotulo in ((apagar_lixo, "ruído"), (apagar_dup, "duplicado")):
        for i in range(0, len(alvo), 256):
            client.delete(collection_name=colecao, points_selector=alvo[i:i + 256])
        if alvo:
            log(f"   🗑️  {len(alvo)} ponto(s) de {rotulo} apagado(s)")

    # 2) re-embedar os que mudaram (lotes, no mesmo id)
    if reembedar:
        emb = emb or rag.embeddings()
        for i in range(0, len(reembedar), LOTE):
            lote = reembedar[i:i + LOTE]
            vetores = emb.embed_documents([texto for _, texto, _ in lote])
            client.upsert(collection_name=colecao, points=[
                PointStruct(id=pid, vector=v,
                            payload={"page_content": texto, "metadata": md})
                for (pid, texto, md), v in zip(lote, vetores)])
            log(f"   🧮 {min(i + LOTE, len(reembedar))}/{len(reembedar)} re-embedado(s)")

    resumo = {
        "collection": colecao,
        "pontos": len(pontos),
        "mantidos": intactos + len(reembedar),
        "reembedados": len(reembedar),
        "apagados_ruido": len(apagar_lixo),
        "apagados_duplicados": len(apagar_dup),
        "total_agora": client.count(colecao, exact=True).count,
    }
    log(f"✨ '{colecao}' higienizada: {resumo['mantidos']} mantidos "
        f"({resumo['reembedados']} re-embedados) · {resumo['apagados_ruido']} "
        f"de ruído + {resumo['apagados_duplicados']} duplicado(s) fora")
    return resumo


def main():
    """Entrada do CLI: uma ou várias coleções."""
    if len(sys.argv) < 2:
        sys.exit("Uso: python -X utf8 -m core.higieniza <colecao> [outra...]")
    for nome in sys.argv[1:]:
        try:
            print(higienizar_colecao(nome))
        except Exception as e:
            print(f"❌ {nome}: {e}")


if __name__ == "__main__":
    main()
