"""Voz para o CHAT — STT e TTS leves, 100% CPU (concorrem com bge/qwen em
NADA: nada de VRAM).

STT: faster-whisper small int8 (CPU) — transcricao do microfone do chat.
TTS: piper (pt_BR-faber-medium, ONNX CPU) — botao "ouvir resposta".
Modelos em modelos_voz/ (volume do container; baixados uma vez).
"""
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "modelos_voz"
PIPER_ONNX = RAIZ / "piper" / "pt_BR-faber-medium.onnx"
WHISPER_DIR = RAIZ / "whisper" / "small"

_stt = None
_stt_lock = threading.Lock()
_tts = None
_tts_lock = threading.Lock()


# ---------- helpers de BYTES (UI server-rendered: form/upload/áudio tag) ----------

def transcrever_bytes(dados: bytes, log=print) -> str:
    """Bytes de áudio (upload/webm do microfone) → texto (whisper CPU).

    O MediaRecorder do navegador grava WEBM/OPUS: detecta pela assinatura
    (EBML 0x1A45DFA3), grava com a extensão REAL e converte para WAV 16k
    mono com ffmpeg antes do whisper. Era o bug do microfone: conteúdo
    webm salvo como .wav → whisper não decodificava → vazio/silêncio."""
    import tempfile
    import subprocess
    import shutil
    import os
    e_webm = dados[:4] == b"\x1a\x45\xdf\xa3"
    with tempfile.NamedTemporaryFile(suffix=".webm" if e_webm else ".wav",
                                     delete=False) as tmp:
        tmp.write(dados)
        cam = tmp.name
    try:
        if e_webm:
            if not shutil.which("ffmpeg"):
                raise RuntimeError("webm do microfone precisa do ffmpeg")
            wav = cam + ".wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", cam, "-ar", "16000", "-ac", "1", wav],
                check=True, capture_output=True, timeout=60)
            os.unlink(cam)
            cam = wav
        return transcrever(cam, log=log)
    finally:
        try:
            os.unlink(cam)
        except Exception:
            pass


def falar_bytes(texto: str, log=print) -> bytes:
    """Texto → bytes WAV (piper CPU) — para servir direto no <audio>."""
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        cam = tmp.name
    try:
        falar(texto, cam, log=log)
        return Path(cam).read_bytes()
    finally:
        try:
            os.unlink(cam)
        except Exception:
            pass


# ---------- STT (fala → texto) ----------

def transcrever(arquivo: str, log=print) -> str:
    """Transcreve um áudio do CHAT (webm/wav/mp3 do microfone) no CPU.
    Modelo small int8: ~500 MB de RAM, zero VRAM."""
    global _stt
    with _stt_lock:
        if _stt is None:
            from faster_whisper import WhisperModel
            log("🎙️ carregando whisper small (CPU, 1ª vez demora um pouco)…")
            _stt = WhisperModel(str(WHISPER_DIR), device="cpu",
                                compute_type="int8", cpu_threads=4)
        segs, _info = _stt.transcribe(arquivo, language="pt", beam_size=1)
        texto = " ".join(s.text.strip() for s in segs).strip()
        return texto


# ---------- TTS (texto → fala) ----------

def falar(texto: str, saida_wav: str, log=print) -> str:
    """Sintetiza o texto em PT-BR (piper, CPU) e devolve o caminho .wav."""
    global _tts
    with _tts_lock:
        if _tts is None:
            import piper
            log("🔊 carregando piper pt_BR (CPU, 1ª vez demora um pouco)…")
            _tts = piper.PiperVoice.load(str(PIPER_ONNX))
        synth = _tts
    texto = (texto or "").strip()
    if not texto:
        raise RuntimeError("nada para sintetizar")
    # limita o tamanho: resposta longa demais vira áudio de minutos
    if len(texto) > 4000:
        texto = texto[:4000] + "…"
    wav = Path(saida_wav)
    wav.parent.mkdir(parents=True, exist_ok=True)
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(synth.config.sample_rate)
        for quadro in synth.synthesize(texto):
            w.writeframes(quadro.audio_int16_bytes)
    return str(wav)


def disponivel() -> dict:
    """O que está pronto (para a webui avisar antes de o usuário clicar)."""
    return {"stt": WHISPER_DIR.exists(), "tts": PIPER_ONNX.exists()}
