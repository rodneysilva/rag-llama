"""Agente do HOST — o que o container NÃO pode fazer, ele faz.

A API roda em container (compose); os processos de GPU (llama-server, e no
futuro sd-cli) pertencem ao HOST. Este agente é um FastAPI mínimo na :8010
do host que expõe exatamente essas operações, e a API-container chama por
http://host.docker.internal:8010 (config AGENTE_HOST_URL) — na VPS, pelo
TÚNEL público configurado no .env (ex.: https://agente.seu-dominio.com).

No BOOT ele mesmo ergue o chat (alias do .env) e o embedding — não existe
mais "llama-server não subiu": subir o agente é subir tudo.

SEGURANÇA: quando AGENTE_HOST_URL é público (túnel), TODA chamada exige
`Authorization: Bearer <AGENTE_TOKEN>` (mesmo valor no .env da estação E
da VPS). Sem AGENTE_TOKEN definido, aceita sem token (uso local).

Uso (host):  python -X utf8 -m api.agente_host
"""
import os
import threading

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from core import config, modelos

app = FastAPI(title="RagAroy · agente do host")


@app.middleware("http")
async def exigir_token(request: Request, call_next):
    from fastapi.responses import JSONResponse
    token = (os.getenv("AGENTE_TOKEN") or "").strip()
    if token and request.headers.get("authorization") != f"Bearer {token}":
        return JSONResponse({"detail": "token do agente inválido"}, status_code=401)
    return await call_next(request)


class AtivarIn(BaseModel):
    modelo: str


class PortaIn(BaseModel):
    porta: int


class VisaoIn(BaseModel):
    arquivo: str = ""
    pergunta: str | None = None
    # 📦 imagem EMBUTIDA (base64): o upload vive no volume da VPS — a GPU é
    # AQUI na estação; sem o b64 o caminho da VPS não existe no host
    b64: str = ""
    nome: str = ""


class TarefaIn(BaseModel):
    """Mesma forma do TarefaIn da API principal — o container delega a
    geração de mídia para cá (a GPU é do host)."""
    modalidade: str
    texto: str | None = None
    arquivo: str | None = None
    arquivo_b64: str | None = None  # conteúdo da referência (disco da VPS
    # não existe aqui): gravado em saidas/entrada/ antes de executar
    modelo: str | None = None
    params: dict = {}
    sessao: str | None = None


@app.get("/arquivo/{pasta}/{nome}")
def enviar_arquivo(pasta: str, nome: str):
    """Envia um arquivo de saidas/ do HOST para a API-container (a mídia é
    GERADA aqui; /api/midia na VPS faz pull-back por este endpoint)."""
    from fastapi.responses import FileResponse
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.\-]+", nome):
        raise HTTPException(status_code=400, detail="nome inválido")
    base = {"imagens": "imagem", "videos": "video", "audios": "audio",
            "entrada": "entrada"}.get(pasta)
    if not base:
        raise HTTPException(status_code=404, detail=f"pasta '{pasta}' inválida")
    from core import midia as _midia
    caminho = (_midia.ENTRADA if base == "entrada"
               else _midia.SAIDAS[base]) / nome
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    return FileResponse(caminho)


def _rodar_tarefa_host(tid: str, body: TarefaIn):
    """Executor de ESTÚDIO no host — espelho enxuto do _rodar_tarefa da API
    principal (só as modalidades que o chat dispara: t2i/t2v/i2v + gif)."""
    from core import midia, tarefas
    import time as _t
    t0 = _t.time()
    mod, p = body.modalidade, (body.params or {})

    def _log(msg, etapa=None):
        tarefas.log(tid, msg, etapa)

    def _prog(fracao):
        tarefas.progresso(tid, fracao)

    _log("🖥️ gerando NO HOST (agente :8010) — a GPU é daqui")
    # 📖 história da sessão (continuidade narrativa — mesmo _rodar_tarefa)
    _prompt = str(body.texto or "")
    if p.get("historia") and mod in ("t2i", "t2v", "i2v"):
        _prompt = (f"{_prompt}. Continuidade da cena desta conversa "
                   f"(mantenha personagens/ambiente/enredo coerentes): "
                   f"{str(p['historia'])[:400]}")
        _log("📖 história da sessão anexada ao prompt (continuidade narrativa)",
             "contexto")
    if p.get("duracao_s"):
        _log(f"⏱️ duração pedida: {p['duracao_s']}s ({p.get('frames')} frames "
             "a 16 fps — spec midia_duracao)", "gerar")
    estado = None
    try:
        from core import conjuntos as _conjuntos
        _conjuntos.garantir(mod, log=lambda m, g="modelo": _log(m, "modelo"))
        if mod in ("t2i", "t2v", "i2v", "a2v"):
            pesado = mod in ("t2v", "i2v", "a2v")
            _log("⏸️ pausando os servidores de LLM — a GPU é da difusão…", "pausar")
            estado = midia.pausar_servicos(log=_log, pesado=pesado)
        if mod == "t2i":
            # INIT (melhoria i2i do multimídia): o anexo é a BASE da cena —
            # força 0.65 preserva a composição (pedido do dono 28/08)
            _init = body.arquivo if p.get("init") else None
            if _init:
                _log("🎨 i2i: anexo é a BASE da cena (força 0.65)", "gerar")
            r = midia.gerar_imagem(_prompt, p.get("modelo"),
                                   p.get("largura", 1024), p.get("altura", 1024),
                                   p.get("seed"), negativo=p.get("negativo"),
                                   imagem_inicial=_init,
                                   log=_log, progresso=_prog)
        elif mod in ("t2v", "i2v"):
            r = midia.gerar_video(_prompt,
                                  body.arquivo if mod == "i2v" else None,
                                  p.get("frames", 33), p.get("largura", 480),
                                  p.get("altura", 832), p.get("seed"),
                                  gif=bool(p.get("gif")),
                                  modelo=p.get("modelo"),
                                  negativo=p.get("negativo"),
                                  log=_log, progresso=_prog)
        elif mod == "i2t":
            # multimodal (Qwen2.5-VL :8082): descreve/analisa a referência —
            # o conjunto "visao" já foi garantido acima (MODALIDADE_PARA)
            if not body.arquivo:
                raise RuntimeError("i2t precisa de uma imagem de entrada")
            _log("🖼️ analisando a imagem com o multimodal (Qwen2.5-VL)…",
                 "analisar")
            r = {"tipo": "texto",
                 "texto": midia.legendar_imagem(body.arquivo,
                                                body.texto or p.get("pergunta"),
                                                log=_log)}
        else:
            raise RuntimeError(f"modalidade '{mod}' não roda via agente")
        import time as _t
        r.setdefault("segundos", round(_t.time() - t0))   # DURAÇÃO visível
        tarefas.concluir(tid, r)
        if r.get("arquivo"):
            try:
                from core import sessoes as _sess
                _sess.registrar(body.sessao or "s_principal",
                                {**r, "modalidade": mod})
            except Exception as e:
                print(f"⚠️ mídia não registrada na sessão: {e}")
    except Exception as e:
        print(f"⌛ erro na tarefa {tid} ({mod}): {e}")
        tarefas.concluir(tid, erro=str(e))
    finally:
        if estado is not None:
            try:
                midia.restaurar_servicos(estado, log=_log)
            except Exception as e:
                tarefas.log(tid, f"⚠️ falha ao restaurar serviços: {e}", "restaurar")


@app.post("/tarefas")
def tarefas_criar(body: TarefaIn):
    """Cria e executa a tarefa de mídia NO HOST — a API-container proxya
    para cá (quem tem a GPU e o sd-cli é o host)."""
    from core import modalidades, tarefas
    m = modalidades.get(body.modalidade)
    if not m:
        raise HTTPException(status_code=404,
                            detail=f"modalidade '{body.modalidade}' não existe")
    if not m["disponivel"]:
        raise HTTPException(status_code=400, detail=m["motivo"])
    body.arquivo = _caminho_host(body.arquivo) if body.arquivo else None
    # referência em base64 (disco da VPS): grava em saidas/entrada/ e usa
    if body.arquivo_b64:
        import base64 as _b64
        import re as _re
        from pathlib import Path as _Path
        from core import midia as _midia
        nome = _re.sub(r"[^\w.\-]", "_", (_Path(body.arquivo or "ref").name
                                           if body.arquivo else "ref.png"))
        destino = _midia.ENTRADA / nome
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(_b64.b64decode(body.arquivo_b64))
        body.arquivo = str(destino)
        body.arquivo_b64 = None
    if any(e in m["entra"] for e in ("imagem", "video", "audio")) and not body.arquivo:
        raise HTTPException(status_code=400,
                            detail=f"'{m['rotulo']}' precisa de um arquivo de entrada")
    try:
        tid = tarefas.criar(body.modalidade, trava_vram=True,
                            sessao=body.sessao or "s_host")
    except RuntimeError as e:
        raise HTTPException(status_code=423, detail=str(e))
    threading.Thread(target=_rodar_tarefa_host, args=(tid, body),
                     daemon=True).start()
    return {"tarefa": tid, "modalidade": m["id"], "rotulo": m["rotulo"],
            "estimativa_s": m["estimativa_s"], "etapas": m["etapas"],
            "status": f"/tarefas/status/{tid}"}


@app.get("/tarefas/status/{tid}")
def tarefa_status(tid: str, cursor: int = 0):
    """Log/progresso/ETA da tarefa em curso no host (polling do container)."""
    from core import tarefas
    s = tarefas.status(tid, cursor)
    if not s:
        raise HTTPException(status_code=404, detail=f"tarefa '{tid}' não encontrada")
    return s


def _caminho_host(arquivo: str) -> str:
    """Converte o caminho que chega da API-container (/app/saidas/…) no
    caminho EQUIVALENTE no host (raiz do projeto/saidas/…) — os volumes do
    compose espelham as mesmas pastas."""
    from pathlib import Path as P
    raiz = P(__file__).resolve().parent.parent
    bruto = (arquivo or "").strip().replace("\\\\", "\\")
    # caminho do CONTAINER (/app/…): no Windows isto NÃO é is_absolute (sem
    # drive) — trata o prefixo explicitamente ANTES de qualquer outra coisa
    norm = bruto.replace("\\", "/")
    if norm.startswith("/app/"):
        rel = P(*[x for x in norm[len("/app/"):].split("/") if x])
        return str(raiz / rel)
    p = P(bruto)
    if p.is_absolute():  # caminho absoluto do PRÓPRIO host
        return str(p)
    for cand in (raiz / "saidas" / bruto,
                 raiz / "saidas" / "entrada" / bruto,
                 # mídias GERADAS incluídas no contexto do chat (painel):
                 # t2i_x.png vive em saidas/imagens/, vídeos em saidas/videos/
                 raiz / "saidas" / "imagens" / bruto,
                 raiz / "saidas" / "videos" / bruto,
                 raiz / "saidas" / "audios" / bruto,
                 raiz / bruto):
        if cand.exists():
            return str(cand)
    return str(raiz / "saidas" / bruto)


@app.post("/visao")
def visao(body: VisaoIn):
    """Descreve uma imagem com o Qwen2.5-VL (:8082) NO HOST — a API-container
    proxya para cá (o anexo de imagem do chat volta a funcionar).
    `b64`+`nome`: a imagem veio EMBUTIDA (upload da VPS — o caminho deles
    não existe aqui); grava em saidas/entrada/ do host e analisa."""
    from core import midia
    import base64 as _b64
    from pathlib import Path
    caminho = body.arquivo
    try:
        if body.b64:
            destino = Path(__file__).resolve().parent.parent / "saidas" / "entrada"
            destino.mkdir(parents=True, exist_ok=True)
            alvo = destino / (Path(body.nome or "upload.png").name
                              if Path(body.nome or "").suffix
                              else "upload.png")
            alvo.write_bytes(_b64.b64decode(body.b64))
            caminho = str(alvo)
        if not caminho:
            raise RuntimeError("informe 'arquivo' ou 'b64'+'nome'")
        desc = midia.legendar_imagem(_caminho_host(caminho), body.pergunta)
        # usage REAL do llama-server :8082 — a API-container REGRAVA o evento
        # de telemetria na VPS (o multimodal aparece no Dashboard de produção;
        # o evento local da estação não atravessa o túnel)
        try:
            return {"descricao": desc,
                    "usage": getattr(midia, "_ultimo_usage_vl", None)}
        except Exception:
            return {"descricao": desc}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:300])


@app.api_route("/saude", methods=["GET", "POST"])
def saude():
    # RODANDO: tarefas ativas (difusão/whisper) — o Sistema mostra o que
    # ocupa a GPU quando o chat está fora ("gerando imagem…", não "offline")
    try:
        from core import tarefas as _tarefas
        rodando = _tarefas.ativos()
    except Exception:
        rodando = []
    return {"ok": True,
            "chat": modelos.servido(modelos.CHAT_PORTA),
            "embed": modelos.embedding_no_ar(),
            "vram_mi": modelos._vram_uso_mi(),
            "rodando": rodando}


@app.post("/ativar")
def ativar(body: AtivarIn):
    """Troca de modelo ASSÍNCRONA: a carga de um GGUF passa dos ~100 s de
    timeout da borda do Cloudflare (HTTP 524 matava a chamada no meio) —
    responde NA HORA e a troca roda em thread própria; o chamador faz
    polling em /saude até o modelo pedido estar no ar."""
    import threading

    def _rodar():
        try:
            modelos.ativar(body.modelo)
        except Exception as e:
            print(f"❌ troca em background falhou ({body.modelo}): {e}")
    threading.Thread(target=_rodar, daemon=True,
                     name=f"troca-{body.modelo}").start()
    return {"ok": True, "iniciada": body.modelo}


@app.post("/embed/garantir")
def embed_garantir():
    ok = modelos.garantir_embedding()
    return {"ok": ok}


@app.post("/embed/ligar")
def embed_ligar():
    return modelos.ligar_embedding_manual()


@app.post("/embed/desligar")
def embed_desligar():
    return modelos.desligar_embedding_manual()


@app.post("/vl/ligar")
def vl_ligar():
    """Religa a visão (marker fora + pré-aquece o Qwen2.5-VL)."""
    return modelos.ligar_vl_manual()


@app.post("/vl/desligar")
def vl_desligar():
    return modelos.desligar_vl_manual()


@app.post("/conjunto/{familia}")
def conjunto(familia: str):
    """🎛️ Garante o CONJUNTO de motores da família no HOST (transição com
    limpeza de VRAM quando muda; cache quente mantido quando não muda) —
    a API-container proxya para cá (quem tem a GPU é o host)."""
    from core import conjuntos
    linhas: list[str] = []
    log = lambda m, *a, **k: linhas.append(str(m))  # noqa: E731
    conjuntos.garantir(familia, log=log)
    return {"familia": familia, "linhas": linhas}


@app.post("/parar_tudo")
def parar_tudo_motores():
    """⏹ Desmonta TODOS os motores de GPU NO HOST (portas + zombies pelo
    nome do processo) — a API-container proxya para cá."""
    return modelos.derrubar_todos_motores()


@app.post("/porta/derrubar")
def derrubar(body: PortaIn):
    return {"pids": modelos.derrubar_porta(body.porta, "agente-host")}


@app.on_event("startup")
def _boot():
    # ergue o que o .env manda — sem depender de mão humana
    def _subir():
        try:
            if modelos.llm_manual_off():
                print("🧠 llama-server DESLIGADO manualmente — boot NÃO sobe "
                      "(religue no badge 🧠 da webui)")
            elif not modelos.servido(modelos.CHAT_PORTA):
                m = next((x for x in modelos.listar()
                          if x["nome"] == config.LLM_MODEL), None)
                # PADRÃO COMPATÍVEL COM A VRAM (pedido do dono): o modelo do
                # .env que NÃO cabe (ou não existe) é TROCADÌO pelo melhor
                # chat compatível — o boot nunca tenta carregar o que a
                # placa não aguenta (OOM na madrugada)
                if m and not m.get("compativel", True):
                    print(f"⚠️ '{config.LLM_MODEL}' {m.get('motivo', '')} — "
                          "trocando pelo maior modelo COMPATÍVEL")
                    m = None
                if not m:
                    cand = [x for x in modelos.listar()
                            if x.get("categoria") == "chat"
                            and x.get("compativel", True)]
                    if cand:
                        m = max(cand, key=lambda x: x.get("gb") or 0)
                        print(f"🧠 padrão VRAM-compatível: {m['nome']} "
                              f"({m.get('gb')} GB)")
                if m:
                    modelos._subir_chat(m["nome"], m["caminho"])
                    print(f"✅ chat {m['nome']} no ar (boot do agente)")
                else:
                    print("⚠️ nenhum modelo de chat compatível com a VRAM "
                          "encontrado em MODELS_DIR — só o embedding sobe")
        except Exception as e:
            print(f"⚠️ chat não subiu no boot: {e}")
        try:
            if modelos.embed_manual_off():
                print("🧬 embedding DESLIGADO manualmente — boot NÃO sobe "
                      "(religue no badge 🧬 da webui)")
            else:
                modelos.garantir_embedding()
        except Exception as e:
            print(f"⚠️ embedding não subiu no boot: {e}")
    threading.Thread(target=_subir, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8010)
