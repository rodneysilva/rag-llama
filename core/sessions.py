"""
Sessões do chat: cada conversa é um arquivo JSON na pasta sessions/.
Simples e sem banco — listar, salvar (upsert), carregar e apagar.

Escrita ATÔMICA (tmp+replace) sob lock: duas respostas terminando juntas
faziam ler→merge→gravar em corrida e uma sobrescrevia a outra; crash no
meio da gravação deixava meio JSON (agora o .tmp morre sozinho).
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"
_io = threading.Lock()  # salvar é ler→merge→gravar: precisa ser atômico


def _arquivo(sid: str) -> Path:
    return SESSIONS_DIR / f"{sid}.json"


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


# cache de RESUMOS por mtime: o list_sessions parseava TODOS os JSONs de
# sessões a cada carregamento do chat (197 arquivos ~3 s no bind mount
# NTFS do Docker). stat() é barato; o parse só roda quando o arquivo muda.
_resumo_cache: dict = {}   # {sid: (mtime_ns, resumo_dict)}


def list_sessions(owner: str = "") -> list[dict]:
    """Resumo das sessões (do `owner` quando informado), mais recente primeiro."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    saida = []
    for arq in SESSIONS_DIR.glob("*.json"):
        try:
            mt = arq.stat().st_mtime_ns
            cacheado = _resumo_cache.get(arq.stem)
            if cacheado and cacheado[0] == mt:
                dados = cacheado[1]
            else:
                dados = json.loads(arq.read_text(encoding="utf-8"))
                _resumo_cache[arq.stem] = (mt, dados)
            if owner and dados.get("owner") != owner:
                continue  # sessão de outra conta: invisível
            saida.append({
                "id": dados.get("id", arq.stem),
                "slug": dados.get("slug", ""),   # referência legível
                "titulo": dados.get("titulo", "(sem título)"),
                "atualizada": dados.get("atualizada", ""),
                "mensagens": len(dados.get("messages", [])),
                "modo": dados.get("modo", ""),
                "colecoes": dados.get("colecoes", []),
            })
        except Exception:
            continue  # arquivo corrompido: ignora
    return sorted(saida, key=lambda s: s["atualizada"], reverse=True)


def get_session(sid: str) -> dict | None:
    """Sessão completa (com as mensagens) ou None se não existir."""
    arq = _arquivo(sid)
    if not arq.exists():
        return None
    return json.loads(arq.read_text(encoding="utf-8"))


_UNSET = object()


# slugs de conversa (pedido do dono: "identificar e referenciar"):
# palavra-palavra-número — legível, único e curto; nasce na CRIAÇÃO da
# sessão (server-side, custo zero — o textarea nunca espera por isso)
_SLUG_PALAVRAS = (
    "gaivota", "tucuma", "garrafao", "vitoria", "manguezal", "boto", "acai",
    "jabuti", "caravela", "farofa", "tucupi", "castanhal", "igarape",
    "marajo", "pacoca", "seringueira", "bodega", "quartinha", "moqueca",
    "culinaria", "panela", "tempero", "cheiro", "tacho", "gordura")


def slug_novo() -> str:
    import random
    a, b = random.sample(_SLUG_PALAVRAS, 2)
    return f"{a}-{b}-{random.randint(10, 99)}"


def slug_de(sid_or_sess) -> str:
    """Slug da sessão (gera e PERSISTE se ainda não tem — idempotente)."""
    dados = (get_session(sid_or_sess) if isinstance(sid_or_sess, str)
             else (sid_or_sess or {}))
    if dados.get("slug"):
        return dados["slug"]
    if not dados.get("id"):
        return ""
    slug = slug_novo()
    try:  # grava sem tocar nas mensagens (merge cirúrgico)
        arq = _arquivo(dados["id"])
        if arq.is_file():
            with _io:
                corpo = json.loads(arq.read_text(encoding="utf-8"))
                corpo.setdefault("slug", slug)
                tmp = arq.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(corpo, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(tmp, arq)
    except Exception:
        pass
    return slug


def save_session(mensagens: list[dict], titulo: str = "", sid: str | None = None,
                 modo: str = "", colecoes: list | None = None,
                 aprovacoes: dict | None = None, owner: str = "",
                 raw: list | None = None,
                 job_ativo: dict | None | object = _UNSET) -> dict:
    """Cria/atualiza a sessão (upsert pelo id) e devolve o resumo dela.

    `raw` guarda as mensagens COMPLETAS (docs/fontes, tokens, mídia) — sem
    isso, reabrir uma sessão antiga perde as fontes da resposta.
    `owner` marca de quem é a sessão (isolamento entre contas).
    `job_ativo` = {kind, job, rotulo} do job em curso (chat/tarefa): o palco
    RE-RENDERIZA o polling ao recarregar a página (nada se perde no
    refresh); None LIMPA o campo (job concluído); omitir preserva.
    """
    SESSIONS_DIR.mkdir(exist_ok=True)
    sid = sid or str(uuid.uuid4())
    with _io:  # ler→merge→gravar ATÔMICO entre threads concorrentes
        anterior = get_session(sid) or {}
        dados = {
            "id": sid,
            "owner": owner or anterior.get("owner", ""),
            # SLUG de referência (nasce com a sessão, imutável, legível)
            "slug": anterior.get("slug") or slug_novo(),
            # título ÉVEL: calculado 1x (semântico/1ª pergunta) e preservado
            # nas gravações seguintes — não reescreve a cada mensagem
            "titulo": (titulo or anterior.get("titulo")
                       or (next((m["content"] for m in mensagens
                                 if m.get("role") == "user"), "")[:80])),
            "modo": modo,
            "colecoes": colecoes or [],
            "aprovacoes": aprovacoes or {},
            "raw": raw or anterior.get("raw", []),
            "job_ativo": (anterior.get("job_ativo")
                          if job_ativo is _UNSET else job_ativo),
            "atualizada": _agora(),
            "messages": mensagens,
        }
        if not dados["owner"]:  # sessão legada/CLI sem dono → admin do .env
            from . import config
            dados["owner"] = getattr(config, "AUTH_ADMIN_USER", "") or ""
        destino = _arquivo(sid)
        tmp = destino.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, destino)  # atômico: nunca existe meio arquivo
    return {"id": sid, "titulo": dados["titulo"], "atualizada": dados["atualizada"],
            "mensagens": len(mensagens)}


def titulo_semantico(texto: str, colecoes: list | None = None) -> str:
    """Título da conversa via EMBEDDING (pedido do dono): embeda a 1ª
    pergunta (bge-m3) e acha o TEMA mais próximo nas coleções — o título
    vira "tema · pergunta-curta" (ex.: "culinaria · como fazer slow food")
    em vez do corte cru da frase. Degrada para a pergunta pura em qualquer
    falha (embedding fora = título simples, nunca erro)."""
    texto = (texto or "").strip()
    if not texto:
        return "(sem título)"
    try:
        from . import config, rag
        from qdrant_client import QdrantClient
        # ⏱️ O embed (e o `garantir_embedding` que ele aciona — em container
        # PROXYA ao agente pelo túnel) pode ficar PENDURADO quando a estação
        # está fora: o POST /hx/chat chamava isto SINCRONAMENTE e o envio da
        # mensagem travava por minutos SEM erro (form morto). Guard de 3,5 s
        # em thread daemon — não voltou a tempo = pergunta pura, o envio
        # NUNCA espera pelo título.
        caixa: list = []

        def _embed():
            try:
                caixa.append(rag.embeddings().embed_query(texto[:400]))
            except Exception:
                pass
        t = threading.Thread(target=_embed, daemon=True)
        t.start()
        t.join(3.5)
        if not caixa:
            return texto[:60] or "(sem título)"
        emb = caixa[0]
        client = QdrantClient(url=config.QDRANT_URL, timeout=8,
                              check_compatibility=False)
        # TEMA SÓ do ESCOPO ESCOLHIDO (pedido: "selecionei culinária e
        # apareceu psicanalista — deve ser real"): adivinhar entre TODAS
        # as coleções cruzava idiomas (pergunta PT casava com psicanalista
        # PT enquanto culinária é EN — o título mentia sobre a conversa).
        # Sem coleção marcada = sem tema: a pergunta pura é o título.
        alvos = [c for c in (colecoes or []) if c][:6]
        tema, melhor = None, 0.30   # piso: tema precisa ser plausível
        for col in alvos:
            try:
                r = client.query_points(collection_name=col, query=emb,
                                        limit=1, with_payload=False)
                pts = r.points if hasattr(r, "points") else (r or [])
                if pts and pts[0].score > melhor:
                    tema, melhor = col, pts[0].score
            except Exception:
                continue
        curto = texto[:52].rstrip()
        return f"{tema} · {curto}" if tema else curto
    except Exception:
        return texto[:60] or "(sem título)"


def delete_session(sid: str) -> bool:
    """Apaga a sessão; devolve True se existia."""
    arq = _arquivo(sid)
    if arq.exists():
        arq.unlink()
        return True
    return False


def main():
    sessoes = list_sessions()
    print(f"{len(sessoes)} sessão(ões) em {SESSIONS_DIR}")
    for s in sessoes:
        print(f"  {s['atualizada']}  {s['mensagens']:>3} msg  {s['titulo'][:60]}  ({s['id']})")


if __name__ == "__main__":
    sys.exit(main())
