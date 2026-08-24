"""
Modalidades do estúdio: os tipos de requisição que o sistema aceita.

Cada modalidade diz: o que ENTRA (texto/arquivo), o que SAI, qual MOTOR
executa, se está disponível na máquina e uma estimativa de tempo para
exibir ANTES de o operador disparar (calibrada com os tempos reais em
Redis pelo core.tarefas).

Motores:
  llama-server :8090  — chat / desenvolvimento (texto↔texto)
  llama-server :8082  — visão (Qwen2.5-VL + mmproj: imagem→texto)
  sd-cli 820          — difusão (Flux texto→imagem; Wan2.2 TI2V texto/imagem→vídeo)
  whisper-cli CUDA    — áudio→texto (e a trilha dos vídeos)
  ffmpeg              — extração de frames/áudio (v2t)
"""
from pathlib import Path

PASTAS = {
    "imagem": Path(r"D:\models\imagem"),
    "video": Path(r"D:\models\video"),
    "visao": Path(r"D:\models\visao"),
    "audio": Path(r"D:\models\audio"),
}

# estimativas BASE (s) — ajustadas com a média real medida (core.tarefas)
_MODALIDADES = {
    "chat": dict(rotulo="Chat", emoji="💬", entra=["texto"], sai="texto",
                 motor="llama-server", etapas=["pergunta", "contexto", "resposta"],
                 estimativa_s=15),
    "dev": dict(rotulo="Desenvolvimento", emoji="⌨️", entra=["texto"], sai="codigo",
                motor="llama-server", etapas=["pergunta", "contexto", "resposta"],
                estimativa_s=25),
    "t2i": dict(rotulo="Texto → Imagem", emoji="🖼️", entra=["texto"], sai="imagem",
                motor="sd-cli", etapas=["prompts", "critica", "pausar", "gerar", "restaurar"],
                estimativa_s=240),
    "t2v": dict(rotulo="Texto → Vídeo", emoji="🎬", entra=["texto"], sai="video",
                motor="sd-cli", etapas=["prompts", "critica", "pausar", "gerar", "restaurar"],
                estimativa_s=600),
    "i2v": dict(rotulo="Imagem → Vídeo", emoji="🎞️", entra=["texto", "imagem"], sai="video",
                motor="sd-cli", etapas=["pausar", "gerar", "restaurar"],
                estimativa_s=700),
    "i2t": dict(rotulo="Imagem → Texto", emoji="🔍", entra=["imagem"], sai="texto",
                motor="llama-server-vl", etapas=["pausar", "subir visão", "analisar", "restaurar"],
                estimativa_s=90),
    "v2t": dict(rotulo="Vídeo → Texto", emoji="📋", entra=["video"], sai="texto",
                motor="ffmpeg+vl+whisper", etapas=["frames", "analisar", "transcrever", "sintetizar"],
                estimativa_s=240),
    "a2t": dict(rotulo="Áudio → Texto", emoji="🎙️", entra=["audio", "video"], sai="texto",
                motor="whisper", etapas=["converter", "transcrever"],
                estimativa_s=60),
    "v2a": dict(rotulo="Vídeo → Áudio", emoji="🔊", entra=["video"], sai="audio",
                motor=None, etapas=[], estimativa_s=None,
                pendente="sem engine local: nada na stack SINTETIZA áudio (whisper só "
                         "escuta, ffmpeg só extrai). Exige modelo tipo MMAudio — "
                         "instalar só com autorização"),
    "a2v": dict(rotulo="Áudio → Vídeo", emoji="🎼", entra=["audio"], sai="video",
                motor="whisper+t2v", etapas=["converter", "transcrever", "gerar", "restaurar"],
                estimativa_s=660),
}


def _arquivos(pasta: Path) -> list[str]:
    return [p.name for p in sorted(pasta.glob("*")) if p.is_file()
            and p.suffix.lower() in (".gguf", ".safetensors", ".bin")
            and not p.name.startswith(".")] if pasta.is_dir() else []


def _disponibilidade(nome: str, d: dict) -> tuple[bool, str]:
    if d.get("pendente"):
        return False, d["pendente"]
    if nome in ("chat", "dev"):
        return True, ""
    if nome == "t2i":
        faltas = [f for f in ("flux1-dev-Q4_K_S.gguf", "flux1-schnell-Q4_K_S.gguf",
                              "t5xxl_fp16.safetensors", "clip_l.safetensors",
                              "ae.safetensors")
                  if not (PASTAS["imagem"] / f).exists()]
        return (not faltas, f"faltam em D:\\models\\imagem: {', '.join(faltas)}" if faltas else "")
    if nome in ("t2v", "i2v"):
        faltas = [f for f in ("wan2.2_vae.safetensors", "umt5-xxl-encoder-Q8_0.gguf")
                  if not (PASTAS["video"] / f).exists()]
        difusor = [f for f in _arquivos(PASTAS["video"])
                   if "ti2v" in f.lower() and f.endswith(".gguf")]
        if not difusor:
            faltas.append("wan2.2-ti2v-*.gguf")
        return (not faltas, f"faltam em D:\\models\\video: {', '.join(faltas)}" if faltas else "")
    if nome in ("i2t", "v2t"):
        arquivos = _arquivos(PASTAS["visao"])
        faltas = []
        if not any("qwen2.5-vl" in f.lower() and "mmproj" not in f.lower()
                   for f in arquivos):
            faltas.append("qwen2.5-vl-*.gguf")
        if not any("mmproj" in f.lower() for f in arquivos):
            faltas.append("mmproj-*.gguf")
        if nome == "v2t" and not (PASTAS["audio"] / "ggml-medium.bin").exists():
            faltas.append("ggml-medium.bin")
        return (not faltas, f"faltam em D:\\models\\visao|audio: {', '.join(faltas)}" if faltas else "")
    if nome == "a2t":
        if not (PASTAS["audio"] / "ggml-medium.bin").exists():
            return False, "falta D:\\models\\audio\\ggml-medium.bin"
        return True, ""
    if nome == "a2v":  # whisper (áudio→texto) + Wan (texto→vídeo)
        faltas = []
        if not (PASTAS["audio"] / "ggml-medium.bin").exists():
            faltas.append("ggml-medium.bin")
        if not (PASTAS["video"] / "wan2.2_vae.safetensors").exists():
            faltas.append("wan2.2_vae.safetensors")
        if not [f for f in _arquivos(PASTAS["video"])
                if "ti2v" in f.lower() and f.endswith(".gguf")]:
            faltas.append("wan2.2-ti2v-*.gguf")
        return (not faltas, f"faltam em D:\\models: {', '.join(faltas)}" if faltas else "")
    return False, "modalidade desconhecida"


def _modelos_de(nome: str) -> list[dict]:
    """Quais ARQUIVOS de modelo esta modalidade usa (nome + tamanho) — o
    que falta para o operador saber O QUE roda em cada geração."""
    def _gb(p: Path) -> str:
        try:
            return f"{p.stat().st_size / 2**30:.1f} GB"
        except OSError:
            return "?"

    por_mod = {
        "t2i": lambda: [PASTAS["imagem"] / f for f in
                        ("flux1-dev-Q4_K_S.gguf", "flux1-schnell-Q4_K_S.gguf",
                         "t5xxl_fp16.safetensors", "clip_l.safetensors",
                         "ae.safetensors")],
        "t2v": lambda: [PASTAS["video"] / f for f in
                        ("wan2.2-ti2v-5b", "wan2.2_vae.safetensors",
                         "umt5-xxl-encoder-Q8_0.gguf")],
        "i2v": lambda: [PASTAS["video"] / f for f in
                        ("wan2.2-ti2v-5b", "wan2.2_vae.safetensors",
                         "umt5-xxl-encoder-Q8_0.gguf")],
        "i2t": lambda: sorted(PASTAS["visao"].glob("*")) if PASTAS["visao"].is_dir() else [],
        "v2t": lambda: sorted(PASTAS["visao"].glob("*")) +
                       [PASTAS["audio"] / "ggml-medium.bin"],
        "a2t": lambda: [PASTAS["audio"] / "ggml-medium.bin"],
        "a2v": lambda: [PASTAS["audio"] / "ggml-medium.bin"] +
                       [PASTAS["video"] / f for f in
                        ("wan2.2_vae.safetensors", "umt5-xxl-encoder-Q8_0.gguf")],
        "chat": lambda: [], "dev": lambda: [],
    }
    out = []
    for p in por_mod.get(nome, lambda: [])():
        if not p.exists():  # "wan2.2-ti2v-5b" é prefixo — resolve o GGUF real
            if p.name.startswith("wan2.2-ti2v"):
                p = next((v for v in sorted(PASTAS["video"].glob("*ti2v*.gguf"))
                          if v.is_file()), p)
            else:
                continue
        if p.is_file() and p.suffix.lower() in (".gguf", ".safetensors", ".bin"):
            out.append({"arquivo": p.name, "tamanho": _gb(p)})
    return out


def listar() -> list[dict]:
    """Todas as modalidades com disponibilidade real e estimativa calibrada."""
    from . import tarefas
    saida = []
    for nome, d in _MODALIDADES.items():
        ok, motivo = _disponibilidade(nome, d)
        saida.append({
            "id": nome, "rotulo": d["rotulo"], "emoji": d["emoji"],
            "entra": d["entra"], "sai": d["sai"], "motor": d["motor"],
            "etapas": d["etapas"], "disponivel": ok, "motivo": motivo,
            "estimativa_s": tarefas.estimativa(nome) if ok else None,
            "modelos": _modelos_de(nome),  # o que roda nesta modalidade
        })
    return saida


def get(nome: str) -> dict | None:
    return next((m for m in listar() if m["id"] == nome), None)
