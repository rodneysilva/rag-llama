"""
Estúdio de mídia: todas as conversões multimodais locais.

Motores (todos oficiais, sem binding — progresso parseado do stdout):
  texto→imagem   Flux (sd-cli bin820, backend te=cpu,vae=cpu)
  texto→vídeo    Wan2.2 TI2V-5B GGUF (sd-cli bin820, --offload-to-cpu)
  imagem→vídeo   Wan2.2 TI2V-5B + -i imagem
  imagem→texto   Qwen2.5-VL no llama-server :8082 (mmproj)
  vídeo→texto    ffmpeg (frames) + Qwen2.5-VL + whisper + síntese da LLM
  áudio→texto    whisper-cli CUDA (ggml-medium)

Política de VRAM (o embedding :8081 NUNCA desce — diretriz do operador):
derruba o chat :8090 (e a visão :8082 se estiver solta) antes de gerar e
restaura exatamente o que estava no ar. Se mesmo assim faltar VRAM, o
embedding desce como último recurso e volta em seguida.

O pipeline de prompts (ideia → 3 variações → crítica) é ancorado no RAG:
exemplares parecidos vêm da coleção `prompts_midia` do Qdrant via bge-m3.
Mídia aprovada entra no contexto pela coleção `midia_gerada`.
"""
import base64
import random
import re
import subprocess
import time
from pathlib import Path

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import config, modelos, motor, rag
from .specs import spec

RAIZ = Path(__file__).resolve().parent.parent
SAIDAS = {"imagem": RAIZ / "saidas" / "imagens",
          "video": RAIZ / "saidas" / "videos",
          "audio": RAIZ / "saidas" / "audios"}
ENTRADA = RAIZ / "saidas" / "entrada"  # uploads do operador (i2t/i2v/v2t/a2t)
COLECAO_PROMPTS = "prompts_midia"
COLECAO_MIDIA = "midia_gerada"

IMAGEM_DIR = Path(r"D:\models\imagem")
VIDEO_DIR = Path(r"D:\models\video")
VISAO_DIR = Path(r"D:\models\visao")
AUDIO_DIR = Path(r"D:\models\audio")

FLUX_AUX = {  # compartilhados pelos dois Flux
    "t5xxl": str(IMAGEM_DIR / "t5xxl_fp16.safetensors"),
    "clip_l": str(IMAGEM_DIR / "clip_l.safetensors"),
    "vae": str(IMAGEM_DIR / "ae.safetensors"),
}
PERFIS_FLUX = {"schnell": {"steps": 4, "guidance": 1.0},
               "dev": {"steps": 20, "guidance": 3.5}}

# negativo padrão do Wan (recomendado pelo fabricante — melhora a nitidez)
NEG_WAN = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
           "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
           "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
           "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")

_FASES_T2I = {"carregando": 0.35, "codificando": 0.05, "amostrando": 0.45,
              "decodificando": 0.13, "salvando": 0.02}
_FASES_VIDEO = {"carregando": 0.15, "codificando": 0.05, "amostrando": 0.60,
                "decodificando": 0.18, "salvando": 0.02}


# ---------- pipeline de prompts (fases 1 e 2 — só LLM de conversa) --------

def _exemplares(ideia: str, k: int = 3) -> list[str]:
    """Prompts parecidos da coleção prompts_midia (RAG para escrever melhor)."""
    try:
        cliente = QdrantClient(url=config.QDRANT_URL, timeout=5)
        if not cliente.collection_exists(COLECAO_PROMPTS):
            return []
        from .rag import search
        # search devolve ([ (documento, score, colecao) ], {erros})
        achados, _erros = search(cliente, [COLECAO_PROMPTS], ideia, k=k)
        return [d.page_content for d, _s, _c in achados]
    except Exception:
        return []


def sugerir_prompts(ideia: str, tipo: str = "imagem", n: int = 3) -> dict:
    """N variações de prompt ancoradas em exemplares do RAG (spec midia_prompt)."""
    exemplares = _exemplares(ideia)
    bloco = ("\n\nEXEMPLARES DO ACERVO:\n" +
             "\n".join(f"- {e[:300]}" for e in exemplares)) if exemplares else ""
    r = rag.llm(temperature=0.4).invoke(
        f"{spec('midia_prompt')}{bloco}\n\nTIPO: {tipo}\nN DE VARIAÇÕES: {n}\n\n"
        f"IDEIA DO USUÁRIO: {ideia}\n\nETAPA: escrever as variações.")
    d = rag._extract_json(r.content)
    variacoes = d.get("variacoes") or []
    if not variacoes:
        variacoes = [{"estilo": "direto", "prompt": ideia}]
    return {"variacoes": variacoes[:n], "nota": str(d.get("nota", ""))[:200]}


def criticar_prompts(ideia: str, variacoes: list[dict]) -> dict:
    """A crítica decide a melhor variação e devolve o prompt final."""
    lista = "\n".join(f"[{i}] ({v.get('estilo', '')}): {v.get('prompt', '')}"
                      for i, v in enumerate(variacoes))
    r = rag.llm(temperature=0.2).invoke(
        f"{spec('midia_critica')}\n\nIDEIA ORIGINAL: {ideia}\n\n"
        f"VARIAÇÕES:\n{lista}\n\nETAPA: crítica e prompt final.")
    d = rag._extract_json(r.content)
    melhor = d.get("melhor")
    melhor = melhor if isinstance(melhor, int) and 0 <= melhor < len(variacoes) else 0
    final = str(d.get("prompt_final") or variacoes[melhor].get("prompt", "")).strip()
    return {"melhor": melhor, "prompt_final": final,
            "criticas": [str(c) for c in (d.get("criticas") or [])][:4],
            "motivo": str(d.get("motivo", ""))[:200]}


# ---------- política de VRAM (o embedding fica de pé) ----------------------

def pausar_servicos(vl_tambem: bool = True, log=print, pesado: bool = False) -> dict:
    """Pausa chat (:8090) e visão (:8082) para liberar a VRAM da difusão.

    A GPU é EXCLUSIVA para os PESADOS: enquanto a difusão roda, o chat/vl
    não competem — o app gerencia quem está no ar. O EMBEDDING (:8081)
    fica DE PÉ em qualquer geração (ele popula a base — busca/ingestão
    não podem parar); só sai com ESTUDIO_PAUSAR_EMBED=1 explícito ou
    marker manual do operador. `pesado` hoje só ajusta o log.
    Devolve o estado para `restaurar_servicos`."""
    estado = {"chat": modelos.servido(modelos.CHAT_PORTA),
              "vl": modelos.servido(modelos.VL_PORTA),
              "chat_pausado": False}
    # servidor ainda bootando (restore de outra tarefa): a porta tem dono mas
    # /v1/models não responde — tenta mais um pouco antes de desistir do nome
    if not estado["chat"] and modelos._pids_na_porta(modelos.CHAT_PORTA):
        for _ in range(3):
            time.sleep(2)
            estado["chat"] = modelos.servido(modelos.CHAT_PORTA)
            if estado["chat"]:
                break
        if not estado["chat"]:
            # processo vivo mas sem resposta: usa o alias do .env (último que
            # serviu na :8090 — mantido por _subir_chat/servicos_llm.py)
            estado["chat"] = getattr(config, "LLM_MODEL", None) or None
            if estado["chat"]:
                log(f"⏳ chat na :8090 ainda bootando — assumindo alias do "
                    f".env ({estado['chat']})", "pausar")
    if not config.ESTUDIO_PAUSAR_CHAT:
        if estado["chat"]:
            log("▶️ chat segue no ar durante a geração "
                "(ESTUDIO_PAUSAR_CHAT=0 no .env)", "pausar")
        return estado
    if estado["chat"]:
        log(f"⏸️ derrubando llama-server do chat (:8090, {estado['chat']}) "
            "para liberar a VRAM — volta automático ao fim", "pausar")
    pids = modelos.derrubar_porta(modelos.CHAT_PORTA, "llama-server (chat)")
    # marca pelo que foi MORTO de verdade (servido() pode ler None de um
    # servidor em boot — matar sem marcar deixaria o chat fora do ar)
    estado["chat_pausado"] = bool(pids) or bool(estado["chat"])
    if vl_tambem:
        modelos.derrubar_porta(modelos.VL_PORTA, "llama-server (visão)")
    # EMBEDDING NUNCA SAI na geração (pedido do dono: "o embedding é o que
    # popula meu banco" — busca/ingestão precisam dele a qualquer momento).
    # Só sai se o OPERADOR pedir explicitamente (ESTUDIO_PAUSAR_EMBED=1);
    # o marker manual (badge 🧬) continua valendo acima de tudo.
    estado["embed_pausado"] = False
    if (config.ESTUDIO_PAUSAR_EMBED
            and not modelos.embed_manual_off()
            and modelos.embedding_no_ar()):
        modelos.liberar_embedding(log)
        estado["embed_pausado"] = True
    # espera fixa (parâmetro): a liberação de VRAM é do servidor/OS
    time.sleep(config.ESTUDIO_VRAM_ASSENTAMENTO_S)
    return estado


def restaurar_servicos(estado: dict, log=print) -> None:
    """Sobe de volta o chat que foi pausado (e derruba a visão, se sobrou).
    Tentativas e pausa entre elas vêm de ESTUDIO_RESTORE_TENTATIVAS /
    ESTUDIO_VRAM_ASSENTAMENTO_S (.env)."""
    try:
        modelos.derrubar_porta(modelos.VL_PORTA, "llama-server (visão)")
        # EMBEDDING PRIMEIRO (prioridade): qualquer busca/ingestão que
        # chegou durante a geração precisa dele antes do chat pesado
        if estado.get("embed_pausado") and not modelos.embed_manual_off():
            # religa SÓ se não foi desligado manualmente (decisão do
            # operador vence o ciclo automático)
            modelos.garantir_embedding(log=log)
        if not estado.get("chat_pausado"):
            return
        if modelos.llm_manual_off():
            # desligado à mão durante/depois da geração: a decisão do
            # operador vence o restore automático
            log("🧠 chat desligado manualmente — restore automático pulado", "restaurar")
            return
        alvo = (estado.get("chat") or "").lower()
        m = next((x for x in modelos.listar()
                  # id servido é o alias OU o nome do arquivo GGUF
                  # (instância erguida sem --alias, ex.: via PowerShell) —
                  # comparação EXATA (substring nos dois sentidos subia o
                  # GGUF errado: "qwen3" batia em "qwen3-8b" e em outros)
                  if x["nome"] == estado.get("chat")
                  or (alvo and (x["nome"].lower() == alvo
                                or Path(x["caminho"]).stem.lower() == alvo))), None)
        if not (m and m["compativel"]):
            log(f"⚠️ modelo '{estado.get('chat')}' não reconhecido — "
                "rode servicos_llm.py", "restaurar")
            return
        for tentativa in range(1, max(1, config.ESTUDIO_RESTORE_TENTATIVAS) + 1):
            if modelos._subir_chat(m["nome"], m["caminho"]):
                log(f"▶️ chat {m['nome']} de volta na :8090", "restaurar")
                return
            log(f"   restaurar: tentativa {tentativa} falhou — esperando "
                f"{config.ESTUDIO_VRAM_ASSENTAMENTO_S}s e tentando de novo",
                "restaurar")
            time.sleep(config.ESTUDIO_VRAM_ASSENTAMENTO_S)
        log("⚠️ não consegui reerguer o chat — rode servicos_llm.py", "restaurar")
    except Exception as e:
        log(f"⚠️ Falha ao restaurar serviços: {e} — rode servicos_llm.py",
            "restaurar")


def _sem_vram(linhas: list[str]) -> bool:
    texto = " ".join(linhas[-30:]).lower()
    return any(s in texto for s in ("out of memory", "cumalloc", "cuda error",
                                    "failed to allocate", "not enough memory"))


# ---------- texto → imagem (Flux via sd-cli) -------------------------------

def _confirmar_png(arquivo: Path) -> None:
    """GUARDRAIL de sucesso: o sd-cli pode sair com 0 e MESMO assim não ter
    escrito nada útil (crash da VAE no fim). O arquivo só é 'imagem' se
    existir, tiver magic bytes de PNG e tamanho plausível."""
    try:
        dados = arquivo.read_bytes()[:16]
        tamanho = arquivo.stat().st_size
    except Exception as e:
        raise RuntimeError(f"a imagem não foi gravada em disco ({e}) — "
                           "libere VRAM (⏹ parar tudo) e tente de novo")
    if tamanho < 8 * 1024 or not dados.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(
            f"o sd-cli terminou mas o PNG não é válido ({tamanho} B) — "
            "típico de memória insuficiente na decodificação (VAE): tente "
            "resolução menor (768×768) ou libere a VRAM (⏹ parar tudo) "
            "antes de gerar")


def _erro_limpo(codigo: int, linhas: list[str], limite_s: int) -> str:
    """Mensagem de erro LEGÍVEL: descarta barras de progresso/ANSI do tail e
    resume a causa provável (o operador não precisa ver '138/138 MB/s')."""
    uteis = [l.strip() for l in linhas
             if l.strip() and "|" not in l[:4] and "it]" not in l]
    tail = uteis[-2:] if uteis else []
    causas = []
    if any("out of memory" in l.lower() or "cuda" in l.lower() for l in linhas[-20:]):
        causas.append("memória de vídeo insuficiente — resolução menor ou ⏹ "
                      "parar tudo antes")
    if not causas:
        causas.append("verifique a VRAM livre e tente de novo")
    msg = f"o sd-cli terminou com código {codigo}"
    if tail:
        msg += " — " + " | ".join(t[:120] for t in tail)
    return msg + f" (dica: {'; '.join(causas)}; limite {limite_s // 60} min)"


def gerar_imagem(prompt: str, modelo: str | None = None, largura: int = 1024,
                 altura: int = 1024, seed: int | None = None,
                 negativo: str | None = None,
                 log=print, progresso=None) -> dict:
    """Gera UMA imagem com Flux pelo sd-cli (progresso parseado). O chamador
    cuida de pausar/restaurar os serviços. `negativo` editável (default
    `NEG_WAN` — o usuário pode escrever 'negativo: …' no prompt do chat)."""
    candidatos = sorted(IMAGEM_DIR.glob("flux1-*.gguf"))
    if not candidatos:
        raise RuntimeError("nenhum Flux em D:\\models\\imagem")
    # match tolerante: alias do seletor ("flux1-dev-q4-k-s") x nome do
    # arquivo ("flux1-dev-Q4_K_S.gguf") — comparação só de [a-z0-9]
    def _chave(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    alvo = next((c for c in candidatos
                 if modelo and _chave(modelo) in _chave(c.name)),
                candidatos[0])
    chave = next((k for k in PERFIS_FLUX if k in alvo.name.lower()), "dev")
    perfil = PERFIS_FLUX[chave]
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    SAIDAS["imagem"].mkdir(parents=True, exist_ok=True)
    saida = SAIDAS["imagem"] / f"{chave}_{seed}_{int(time.time())}.png"

    t0 = time.time()
    log(f"🖌️ {alvo.stem} · {largura}x{altura} · {perfil['steps']} passos · seed {seed}", "gerar")
    cmd = [motor.sd_cli(),
           "--diffusion-model", str(alvo),
           "--t5xxl", FLUX_AUX["t5xxl"], "--clip_l", FLUX_AUX["clip_l"],
           "--vae", FLUX_AUX["vae"],
           "--backend", "te=cpu,vae=cpu",
           "-p", prompt, "-n", (negativo or NEG_WAN),
           "-W", str(largura), "-H", str(altura),
           "--steps", str(perfil["steps"]), "--guidance", str(perfil["guidance"]),
           "--seed", str(seed), "-o", str(saida),
           "--fa", "--diffusion-fa", "--vae-tiling"]
    if negativo:
        log(f"🚫 negative prompt do pedido: {negativo[:120]}", "gerar")

    def ao_vivo(linha, fase, fracao):
        if fracao is not None and progresso:
            progresso(fracao)          # barra do card: a LINHA de progresso
        elif linha.strip() and ": " in linha:   # NÃO vira log (só eventos)
            log(f"   {linha[:150]}", "gerar")

    codigo, linhas = motor.rodar(cmd, ao_vivo=ao_vivo, fases=_FASES_T2I, timeout_s=1800)
    if codigo != 0 or not saida.exists():
        raise RuntimeError(_erro_limpo(codigo, linhas, 1800))
    _confirmar_png(saida)   # sucesso = PNG válido (guardrail), não exit 0
    kb = round(saida.stat().st_size / 1024)
    log(f"✅ imagem confirmada em disco ({kb} KB)", "salvar")
    try:
        from . import telemetria as _tel
        _tel.evento("geracao", f"🖼️ {alvo.stem}: imagem em "
                    f"{round(time.time() - t0)}s",
                    modelo=alvo.stem, tipo="imagem",
                    segundos=round(time.time() - t0))
    except Exception:
        pass
    return {"arquivo": saida.name, "pasta": str(SAIDAS["imagem"]), "prompt": prompt,
            "modelo": alvo.stem, "tipo": "imagem", "seed": seed,
            "kb": kb,
            "segundos": round(time.time() - t0),
            "vram_mi": modelos._vram_uso_mi()}


# ---------- texto/imagem → vídeo (Wan2.2 TI2V-5B via sd-cli) ---------------

def _para_mp4(origem: Path, log) -> Path:
    """Converte o vídeo bruto (webm/vp9 do Wan) para MP4/H.264 — o formato
    compatível com qualquer player/plataforma. Sem ffmpeg no sistema, devolve
    o original (a geração não falha por causa da conversão)."""
    from shutil import which
    ffmpeg = which("ffmpeg")
    if not ffmpeg or origem.suffix.lower() == ".mp4":
        return origem
    destino = origem.with_suffix(".mp4")
    log("📦 convertendo para MP4 (H.264 — compatível com qualquer player)…", "salvar")
    r = subprocess.run(
        [ffmpeg, "-y", "-i", str(origem),
         "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", str(destino)],
        capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not destino.exists():
        log(f"   ⚠️ conversão falhou ({r.stderr.strip()[-100:]}) — mantendo {origem.name}")
        return origem
    origem.unlink(missing_ok=True)
    return destino


def _para_gif(mp4: Path, log) -> Path | None:
    """Converte o vídeo FINAL (.mp4) em .gif animado — duas passadas com
    paleta (palettegen/paletteuse, padrão testado do ffmpeg): cores fiéis,
    12 fps e 480 px de largura (gif pesa ~10x o mp4 — mantê-lo pequeno).

    Devolve None se não der (o chamador fica com o mp4 — a geração já
    aconteceu, não se joga fora por causa da conversão)."""
    from shutil import which
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        log("⚠️ sem ffmpeg no sistema — o GIF virou .mp4 (instale ffmpeg p/ converter)")
        return None
    destino = mp4.with_suffix(".gif")
    paleta = mp4.with_name(mp4.stem + "_palette.png")
    log("📦 convertendo para GIF animado (paleta, 12 fps, 480 px)…", "salvar")
    filtros = "fps=12,scale=480:-1:flags=lanczos"
    r1 = subprocess.run(
        [ffmpeg, "-y", "-i", str(mp4), "-vf", f"{filtros},palettegen", str(paleta)],
        capture_output=True, text=True, timeout=600)
    r2 = subprocess.run(
        [ffmpeg, "-y", "-i", str(mp4), "-i", str(paleta), "-lavfi",
         f"{filtros}[x];[x][1:v]paletteuse", str(destino)],
        capture_output=True, text=True, timeout=600)
    paleta.unlink(missing_ok=True)
    if r2.returncode != 0 or not destino.exists():
        log(f"   ⚠️ conversão para GIF falhou ({(r2.stderr or r1.stderr or '').strip()[-100:]}) "
            f"— mantendo {mp4.name}")
        return None
    mp4.unlink(missing_ok=True)
    return destino


def _achar_video(modelo: str | None = None) -> Path | None:
    """GGUF de vídeo em VIDEO_DIR (Wan2.1/2.2/…): casa o ALIAS pedido por
    chave alfanumérica (tipo FLUX — "wan2.1-t2v-1.3b" ⊂ "Wan2.1-T2V-1.3B-
    Q8_0.gguf"); sem pedido, o primeiro por ordem de nome."""
    ggufs = sorted(p for p in VIDEO_DIR.glob("*.gguf")
                   if "t2v" in p.stem.lower() or "ti2v" in p.stem.lower())
    if not ggufs:
        return None
    if modelo:
        chave = re.sub(r"[^a-z0-9]+", "", modelo.lower())
        for g in ggufs:
            if chave and chave in re.sub(r"[^a-z0-9]+", "", g.stem.lower()):
                return g
    return ggufs[0]


def _vae_video(difusor: Path) -> Path:
    """VAE CERTA para o difusor: Wan2.1 usa a VAE 3D original; Wan2.2 é
    OUTRA (não são intercambiáveis — decoder errado = vídeo lixo)."""
    stem = difusor.stem.lower()
    if "2.1" in stem or "2_1" in stem:
        v = VIDEO_DIR / "wan2.1_vae.safetensors"
    else:
        v = VIDEO_DIR / "wan2.2_vae.safetensors"
    if not v.exists():
        raise RuntimeError(f"VAE do difusor ausente: {v.name} em {VIDEO_DIR}")
    return v


def gerar_video(prompt: str, imagem_inicial: str | None = None, frames: int = 33,
                largura: int = 480, altura: int = 832, seed: int | None = None,
                gif: bool = False, modelo: str | None = None,
                negativo: str | None = None,
                log=print, progresso=None) -> dict:
    """Gera UM vídeo com Wan (t2v; i2v quando `imagem_inicial`) — o
    `modelo` (alias do combobox) escolhe a geração: Wan2.1-T2V-1.3B (leve,
    ~1,4 GB Q8 — estável em 8 GB de VRAM) ou Wan2.2-TI2V-5B (mais pesado);
    cada um usa a própria VAE e o CFG recomendado (1.3B: 5.0 · 2.2: 6.0).

    O Wan grava .webm; ao final o arquivo é convertido para .mp4 (H.264) —
    formato universal — e o webm é descartado. Com `gif=True` (F1b-3), o mp4
    final é convertido em .gif animado (12 fps, 480 px, paleta) e o resultado
    volta com tipo="gif" — renderiza como <img> em qualquer lugar.
    """
    difusor = _achar_video(modelo)
    vae = _vae_video(difusor) if difusor else None
    t5 = VIDEO_DIR / "umt5-xxl-encoder-Q8_0.gguf"
    if not (difusor and vae and t5.exists()):
        raise RuntimeError("modelos de vídeo ausentes em D:\\models\\video "
                           "(tests_manual/baixar_multimodal.py)")
    eh_21 = "2.1" in difusor.stem.lower() or "2_1" in difusor.stem.lower()
    cfg = "5.0" if eh_21 else "6.0"
    seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    SAIDAS["video"].mkdir(parents=True, exist_ok=True)
    bruto = SAIDAS["video"] / f"{'i2v' if imagem_inicial else 't2v'}_{seed}_{int(time.time())}.webm"
    if imagem_inicial:
        # i2v: a imagem inicial vira o 1º frame — ela precisa bater EXATA
        # com W×H do vídeo (múltiplos de 16 e cap 832 no lado maior: é o que
        # o Wan treina). Divergência = "diffusion model compute failed" no
        # sd-cli. Redimensionamos aqui (Lanczos) e passamos um arquivo tmp.
        from PIL import Image
        with Image.open(imagem_inicial) as im:
            im = im.convert("RGB")
            lado = max(im.size)
            escala = min(1.0, 832.0 / lado)  # cap do lado maior
            w = max(64, round(im.width * escala / 16) * 16)
            h = max(64, round(im.height * escala / 16) * 16)
            im = im.resize((w, h), Image.LANCZOS)
            tmp = SAIDAS["video"] / f"init_{seed}_{int(time.time())}.png"
            im.save(tmp)
        largura, altura = w, h
        imagem_inicial = str(tmp)

    t0 = time.time()
    log(f"🎬 {difusor.stem} · {largura}x{altura} · {frames} frames · "
        f"{'i2v' if imagem_inicial else 't2v'} · seed {seed}", "gerar")
    if negativo:
        log(f"🚫 negative prompt do pedido: {negativo[:120]}", "gerar")
    cmd = [motor.sd_cli(), "-M", "vid_gen",
           "--diffusion-model", str(difusor), "--vae", str(vae), "--t5xxl", str(t5),
           "-p", prompt, "-n", (negativo or NEG_WAN),
           "--cfg-scale", cfg, "--sampling-method", "euler", "-v",
           "-W", str(largura), "-H", str(altura),
           "--diffusion-fa", "--offload-to-cpu",
           "--video-frames", str(frames), "--flow-shift", "3.0",
           "--vae-tiling", "--vae-tile-size", "16x16", "--temporal-tiling",
           # VAE 3D do Wan pede ~20 GB sem tiling (RTX 8 GB): tile pequeno +
           # corte temporal cabem no ~6 GB livres com o embed residente
            "--seed", str(seed), "-o", str(bruto)]
    if imagem_inicial:
        cmd += ["-i", str(imagem_inicial)]

    def ao_vivo(linha, fase, fracao):
        if fracao is not None and progresso:
            progresso(fracao)
        elif linha.strip():
            log(f"   {linha[:160]}", "gerar")

    codigo, linhas = motor.rodar(cmd, ao_vivo=ao_vivo, fases=_FASES_VIDEO, timeout_s=7200)
    try:
        if codigo != 0 or not bruto.exists():
            raise RuntimeError(f"sd-cli saiu com {codigo}: {linhas[-3:] if linhas else 'sem saída'}")
        saida = _para_mp4(bruto, log)
        # TELEMETRIA de geração (mesma sopa do t2i): o dashboard "por modelo"
        # enxerga wan2.1/wan2.2 com tempo e frames
        try:
            from . import telemetria as _tel
            _tel.evento("geracao", f"🎬 {difusor.stem}: vídeo "
                        f"({frames} frames) em {round(time.time() - t0)}s",
                        modelo=difusor.stem, tipo="video", frames=frames,
                        segundos=round(time.time() - t0))
        except Exception:
            pass
        if gif:
            gif_final = _para_gif(saida, log)
            if gif_final is not None:
                return {"arquivo": gif_final.name, "pasta": str(SAIDAS["video"]),
                        "prompt": prompt, "modelo": difusor.stem, "tipo": "gif",
                        "imagem_inicial": str(imagem_inicial) if imagem_inicial else None,
                        "frames": frames, "seed": seed,
                        "segundos": round(time.time() - t0),
                        "vram_mi": modelos._vram_uso_mi()}
        return {"arquivo": saida.name, "pasta": str(SAIDAS["video"]), "prompt": prompt,
                "modelo": difusor.stem, "tipo": "video",
                "imagem_inicial": str(imagem_inicial) if imagem_inicial else None,
                "frames": frames, "seed": seed,
                "segundos": round(time.time() - t0),
                "vram_mi": modelos._vram_uso_mi()}
    finally:
        # tmp redimensionado do i2v sai SEMPRE (mesmo quando o sd-cli falha)
        if imagem_inicial and Path(imagem_inicial).name.startswith("init_"):
            Path(imagem_inicial).unlink(missing_ok=True)


# ---------- imagem → texto (Qwen2.5-VL na :8082) ---------------------------

def legendar_imagem(arquivo: str, pergunta: str | None = None,
                    log=print, modelo: str = "") -> str:
    """Descreve/analisa UMA imagem (ou responde `pergunta` sobre ela).

    `modelo` com "prov:nome" (ex.: `openai:gpt-4o`, `anthropic:claude-
    sonnet-4-5`) usa um MULTIMODAL EXTERNO via OpenAI-compatible — nem
    passa pela GPU local (não pausa nada, funciona até sem estação).
    Vazio/nome local → Qwen2.5-VL no llama-server :8082 como sempre."""
    prov_ext = None
    if ":" in (modelo or ""):
        pid, nome = modelo.split(":", 1)
        from . import provedores
        prov_ext = provedores.resolver(pid.strip(), nome.strip())
        if prov_ext and not provedores.e_multimodal(nome):
            raise RuntimeError(f"'{nome}' não parece multimodal — escolha "
                               "um modelo de visão (👁) do provedor")
    if prov_ext:
        log(f"🔍 visão EXTERNA [{prov_ext['provedor']}] {prov_ext['model']} — "
            "analisando imagem (GPU local intocada)…", "analisar")
        with open(arquivo, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = Path(arquivo).suffix.lstrip(".") or "png"
        pergunta = pergunta or ("Descreva esta imagem em português de forma "
                                "detalhada: cena, objetos, pessoas, ação, "
                                "estilo, cores.")
        r = httpx.post(f"{prov_ext['base_url']}/chat/completions",
                       headers={"Authorization":
                                f"Bearer {prov_ext['api_key']}",
                                "User-Agent": "ragaroy/1.0"},
                       json={"model": prov_ext["model"],
                             "messages": [{"role": "user", "content": [
                                 {"type": "image_url",
                                  "image_url": {"url": f"data:image/{ext};base64,{b64}"}},
                                 {"type": "text", "text": pergunta}]}]},
                       timeout=300)
        if r.status_code != 200:
            # CORPO do erro na cara: "modelCode: does not exist" da z.ai diz
            # TUDO; sem isto o usuário via só "400 Bad Request … mozilla"
            detalhe = ""
            try:
                detalhe = str((r.json().get("error") or {}).get("message")
                              or r.text)[:200]
            except Exception:
                detalhe = r.text[:200]
            raise RuntimeError(
                f"visão externa {prov_ext['model']} → HTTP {r.status_code}"
                + (f": {detalhe}" if detalhe else ""))
        texto = r.json()["choices"][0]["message"]["content"].strip()
        try:
            from . import telemetria as _tel
            u = (r.json().get("usage") or {})
            _tel.evento("llm", f"🖼️ [{prov_ext['provedor']}] "
                               f"{prov_ext['model']} (multimodal)",
                        entrada=int(u.get("prompt_tokens") or 0),
                        saida=int(u.get("completion_tokens") or 0),
                        duracao_s=None, modelo=prov_ext["model"],
                        servico="multimodal")
        except Exception:
            pass
        return texto
    estado = pausar_servicos(vl_tambem=False, log=log)
    try:
        if not modelos._subir_vl():
            raise RuntimeError("visão (:8082) não subiu — veja logs/llama-vl.log")
        log("🔍 visão no ar, analisando imagem…", "analisar")
        with open(arquivo, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = Path(arquivo).suffix.lstrip(".") or "png"
        pergunta = pergunta or ("Descreva esta imagem em português de forma "
                                "detalhada: cena, objetos, pessoas, ação, estilo, cores.")
        # host RESOLVIDO (container → host.docker.internal; host → 127.0.0.1)
        r = httpx.post(f"http://{modelos._host_de(modelos.VL_PORTA)}:{modelos.VL_PORTA}/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": [
                           {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{b64}"}},
                           {"type": "text", "text": pergunta}]}]},
                       timeout=300)
        r.raise_for_status()
        texto = r.json()["choices"][0]["message"]["content"].strip()
        # TELEMETRIA: o multimodal É uma LLM em uso — sem isto o Dashboard
        # só enxergava o chat (pedido do dono). Usage quando o servidor devolve.
        try:
            from . import telemetria as _tel
            u = (r.json().get("usage") or {})
            _tel.evento("llm", "🖼️ qwen2.5-vl (multimodal)",
                        entrada=int(u.get("prompt_tokens") or 0),
                        saida=int(u.get("completion_tokens") or 0),
                        duracao_s=None, modelo="qwen2.5-vl-7b",
                        servico="multimodal")
        except Exception:
            pass
        return texto
    finally:
        restaurar_servicos(estado, log=log)


# ---------- áudio → texto (whisper-cli CUDA) --------------------------------

def transcrever(arquivo: str, log=print, progresso=None) -> dict:
    """Transcreve áudio OU a trilha de um vídeo (whisper medium, PT).

    O wav 16 kHz e o .txt saem num tmp do SISTEMA (nunca ao lado do arquivo
    de entrada — que pode estar em diretório só-leitura) e são apagados no
    finally, mesmo quando o whisper falha."""
    import tempfile
    modelo_whisper = AUDIO_DIR / "ggml-medium.bin"
    if not modelo_whisper.exists():
        raise RuntimeError("falta D:\\models\\audio\\ggml-medium.bin")
    estado = pausar_servicos(log=log)  # whisper medium pede ~3 GB de VRAM
    wav = Path(tempfile.gettempdir()) / f"ragaroy_{int(time.time()*1000)}.16k.wav"
    txt = wav.with_suffix(".txt")
    try:
        log("🎙️ extraindo áudio 16 kHz…", "converter")
        subprocess.run(["ffmpeg", "-y", "-i", arquivo, "-ar", "16000", "-ac", "1",
                        str(wav)], capture_output=True, timeout=600)
        if not wav.exists():
            raise RuntimeError("ffmpeg não conseguiu extrair áudio deste arquivo")
        log("🎙️ transcrevendo com whisper medium…", "transcrever")
        t0 = time.time()
        codigo, linhas = motor.rodar_whisper(
            [motor.whisper_cli(), "-m", str(modelo_whisper), "-f", str(wav),
             "-l", "pt", "--print-progress", "-otxt", "-of", str(wav.with_suffix(""))],
            ao_vivo=lambda l, f, fr: (progresso(fr) if progresso and fr is not None
                                      else log(f"   {l[:160]}", "transcrever")),
            duracao_s=motor.duracao_midia(arquivo))
        if codigo != 0 or not txt.exists():
            raise RuntimeError(f"whisper saiu com {codigo}: {linhas[-3:]}")
        texto = txt.read_text(encoding="utf-8").strip()
        return {"texto": texto, "segundos": round(time.time() - t0),
                "arquivo": arquivo, "tipo": "texto"}
    finally:
        wav.unlink(missing_ok=True)
        txt.unlink(missing_ok=True)
        restaurar_servicos(estado, log=log)


def audio_para_video(arquivo: str, frames: int = 33, log=print,
                     progresso=None) -> dict:
    """Áudio → vídeo (pipeline composto local): whisper transcreve a fala e a
    transcrição vira o prompt do Wan2.2 — o vídeo "ilustra" o que se ouve.
    Cada estágio reporta a sua fatia do progresso (25% / 75%)."""
    tr = transcrever(arquivo, log=log,
                     progresso=(lambda f: progresso(f * 0.25)) if progresso else None)
    texto = (tr.get("texto") or "").strip()
    if not texto:
        raise RuntimeError("o áudio não tem fala reconhecível — não há o que "
                           "transformar em vídeo (a2v usa a TRANSCRIÇÃO como prompt)")
    prompt = texto[:400]
    log(f"📝 transcrição vira prompt ({len(texto)} caracteres): "
        f"{' '.join(texto.split())[:140]}…", "transcrever")
    r = gerar_video(prompt, None, frames,
                    log=log,
                    progresso=(lambda f: progresso(0.25 + f * 0.75)) if progresso else None)
    r["prompt"] = texto[:200]
    r["transcricao"] = texto[:2000]
    return r


# ---------- vídeo → texto (frames + visão + whisper + síntese) --------------

def video_para_texto(arquivo: str, log=print, progresso=None) -> dict:
    """Describe o vídeo: frames analisados pela visão + transcrição da trilha
    + síntese final da LLM de conversa."""
    import tempfile
    from PIL import Image

    duracao = motor.duracao_midia(arquivo) or 10.0
    estado = pausar_servicos(vl_tambem=False, log=log)
    legendas = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            passo = max(1.0, duracao / 6)  # até 6 frames espaçados
            log(f"📋 extraindo frames (1 a cada {passo:.1f}s)…", "frames")
            subprocess.run(["ffmpeg", "-y", "-i", arquivo, "-vf", f"fps=1/{passo}",
                            str(Path(tmp) / "f%02d.jpg")],
                           capture_output=True, timeout=600)
            frames = sorted(Path(tmp).glob("f*.jpg"))
            if not modelos._subir_vl():
                raise RuntimeError("visão (:8082) não subiu — veja logs/llama-vl.log")
            for i, frame in enumerate(frames, 1):
                if progresso:
                    progresso(0.15 + 0.45 * i / max(1, len(frames)))
                with open(frame, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                r = httpx.post(f"http://{modelos._host_de(modelos.VL_PORTA)}:{modelos.VL_PORTA}/v1/chat/completions",
                               json={"messages": [{"role": "user", "content": [
                                   {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                   {"type": "text", "text": "Descreva este quadro de vídeo em português, uma frase objetiva."}]}]},
                               timeout=300)
                r.raise_for_status()
                legendas.append(r.json()["choices"][0]["message"]["content"].strip())
                log(f"   quadro {i}/{len(frames)}: {legendas[-1][:80]}", "analisar")
    finally:
        restaurar_servicos(estado, log=log)

    trilha = ""
    try:
        log("📋 transcrevendo a trilha sonora…", "transcrever")
        r = transcrever(arquivo, log=log)
        trilha = r["texto"]
    except Exception as e:
        log(f"   (sem trilha transcrita: {e})", "transcrever")

    if progresso:
        progresso(0.85)
    log("📋 sintetizando a descrição final…", "sintetizar")
    resumo = rag.llm(temperature=0.2).invoke(
        f"{spec('midia_sintese')}\n\n"
        "QUADROS DO VÍDEO (em ordem), descritos por um modelo de visão:\n"
        + "\n".join(f"- {l}" for l in legendas)
        + (f"\n\nTRANSCRIÇÃO DA TRILHA:\n{trilha[:4000]}" if trilha else "")
        + "\n\nETAPA: descrição final do vídeo.").content.strip()
    return {"texto": resumo, "quadros": legendas, "trilha": trilha,
            "arquivo": arquivo, "tipo": "texto"}


# ---------- mídia → contexto (RAG com bge-m3, "embedding em tudo") ---------

def incluir_no_contexto(arquivo: str, tipo: str, prompt: str = "",
                        log=print) -> dict:
    """Indexa a mídia no Qdrant: descreve (visão/whisper) → embed → midia_gerada."""
    log("🧠 descrevendo a mídia para o contexto…", "analisar")
    if tipo == "imagem":
        desc = legendar_imagem(arquivo, log=log)
    elif tipo == "video":
        r = video_para_texto(arquivo, log=log)
        desc = r["texto"]
    else:
        r = transcrever(arquivo, log=log)
        desc = r["texto"]
    texto = (f"Prompt original: {prompt}\n\n{desc}" if prompt else desc)

    cliente = QdrantClient(url=config.QDRANT_URL, timeout=10)
    if not cliente.collection_exists(COLECAO_MIDIA):
        # dimensão medida no embedding ATIVO (não assume 1024: trocar de
        # modelo de embedding não pode criar coleção com dimensão errada)
        dim = len(rag.embeddings().embed_query("dimensão"))
        cliente.create_collection(
            collection_name=COLECAO_MIDIA,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    vetor = rag.embeddings().embed_query(texto[:6000])
    pid = int(time.time() * 1000) % 2**62
    # formato que o LangChain lê (igual ao resto do projeto): texto em
    # page_content, resto em metadata — senão a busca devolve vazio
    cliente.upsert(COLECAO_MIDIA, points=[
        PointStruct(id=pid, vector=vetor, payload={
            "page_content": texto,
            "metadata": {"arquivo": str(arquivo), "tipo": tipo, "prompt": prompt,
                         "origem": "estudio", "source": Path(arquivo).name,
                         "categoria": COLECAO_MIDIA},
        })])
    log(f"✅ {tipo} indexado em '{COLECAO_MIDIA}' (ponto {pid})", "fim")
    return {"colecao": COLECAO_MIDIA, "ponto": pid, "descricao": desc[:500],
            "pontos": 1, "tipo": "texto"}
