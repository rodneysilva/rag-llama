"""
Motor de processos externos com progresso parseado (sd-cli / whisper-cli).

Toda geração pesada roda como subprocesso do binário oficial — sem binding,
sem risco de ABI — e o progresso é extraído das próprias linhas que a
ferramenta imprime (barras "cur/total" do sd.cpp e "%" do whisper),
transformadas em fração 0..1 por fase e repassadas ao chamador.
"""
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config

# binários da máquina (ajustáveis no .env: SD_CLI / WHISPER_CLI) — lidos na
# HORA da chamada (copiar no import fazia edição no .env não ter efeito)
def sd_cli() -> str:
    return config.SD_CLI


def whisper_cli() -> str:
    return config.WHISPER_CLI

# barra de progresso do sd.cpp: |####>      | 7/20 - 3.5s/it
_BARRA = re.compile(r"\|[#=>\s]*\|\s*(\d+)\s*/\s*(\d+)")
# fases do sd.cpp por palavra-chave na linha recente
_FASES_SD = [
    ("carregando", ("load", "loading", "convert", "copy_to_backend")),
    ("codificando", ("text encoder", "encode prompt", "t5", "clip")),
    ("amostrando", ("sampling", "generating", "diffusion")),
    ("decodificando", ("decode", "vae", "latent")),
    ("salvando", ("save", "export", "writ")),
]
# whisper: "progress = X%" e segmentos [hh:mm:ss.mmm --> ...]
_WHISPER_PCT = re.compile(r"progress\s*=\s*(\d+)%")
_WHISPER_SEG = re.compile(r"\[(\d+):(\d+):(\d+)[.,]\d+\s*-->")


def _fase_sd(buffer_recente: str) -> str:
    b = buffer_recente.lower()
    for fase, chaves in _FASES_SD:
        if any(k in b for k in chaves):
            return fase
    return "processando"


def rodar(cmd: list[str], ao_vivo=None, fases: dict[str, float] | None = None,
          fase_fixa: str | None = None, timeout_s: int = 3600,
          cwd: str | None = None) -> tuple[int, list[str]]:
    """Roda `cmd` e devolve (codigo_saida, linhas).

    `ao_vivo(linha, fase, fracao)` é chamado a cada linha; `fases` mapeia o
    nome da fase → peso relativo no total (ex.: {"carregando": .1, ...}) para
    calcular a fração GLOBAL do processo a partir da fração da fase atual.
    """
    ao_vivo = ao_vivo or (lambda *a: None)
    pesos = fases or {}
    total_pesos = sum(pesos.values()) or 1.0
    # pesos cumulativos por fase (fração GLOBAL): pré-computado porque a fase
    # pode não estar no mapa (fallback "processando") — não deve explodir
    cumulativo, acc = {}, 0.0
    for nome, peso in pesos.items():
        cumulativo[nome] = acc
        acc += peso / total_pesos
    linhas, recentes = [], ""
    t0 = time.time()

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        cwd=cwd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert proc.stdout is not None
        for linha in proc.stdout:
            linha = linha.rstrip("\r\n")
            if not linha.strip():
                continue
            linhas.append(linha)
            recentes = (linha + "\n" + recentes)[:600]
            fase = fase_fixa or _fase_sd(recentes)
            m = _BARRA.search(linha)
            if m and fase in pesos:
                cur, tot = int(m.group(1)), int(m.group(2))
                fracao = min(1.0, cumulativo[fase]
                             + (cur / tot if tot else 1.0) * pesos[fase] / total_pesos)
            else:
                fracao = None
            ao_vivo(linha, fase, fracao)
        proc.wait(timeout=max(1, timeout_s - (time.time() - t0)))
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode or 0, linhas


def rodar_whisper(cmd: list[str], ao_vivo=None, duracao_s: float | None = None,
                  timeout_s: int = 3600) -> tuple[int, list[str]]:
    """Whisper-cli com --print-progress: fração = %/100 (ou tempo do segmento)."""
    ao_vivo = ao_vivo or (lambda *a: None)
    linhas = []
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert proc.stdout is not None
        for linha in proc.stdout:
            linha = linha.rstrip("\r\n")
            if not linha.strip():
                continue
            linhas.append(linha)
            fracao = None
            m = _WHISPER_PCT.search(linha)
            if m:
                fracao = int(m.group(1)) / 100
            elif duracao_s:
                s = _WHISPER_SEG.search(linha)
                if s:
                    t = int(s.group(1)) * 3600 + int(s.group(2)) * 60 + int(s.group(3))
                    fracao = min(0.99, t / duracao_s)
            ao_vivo(linha, "transcrevendo", fracao)
        try:  # SEM wait infinito: transcrição pendurada não trava o job p/ sempre
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            linhas.append(f"⏱️ timeout de {timeout_s}s — transcrição interrompida")
            return -9, linhas
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode or 0, linhas


def duracao_midia(arquivo: str) -> float | None:
    """Duração em segundos via ffprobe (para ETA do whisper/v2t)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", arquivo],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None
