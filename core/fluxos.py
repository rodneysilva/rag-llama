"""Registry de FLUXOS de geração (F1b-4): builtins do estúdio + EXTERNOS.

Cada fluxo é um CARD resumido na aba Estúdio — nome, 1 linha do que faz e
status. Os BUILTINS são as modalidades locais (sd-cli: t2i Flux; wan2.2:
vídeo via t2v/i2v); os EXTERNOS (wan2gp, ComfyUI) rodam em processo/servidor
próprio: o card faz health-check GET na URL configurada no .env
(FLUXO_WAN2GP_URL / FLUXO_COMFY_URL, timeout 2 s) e, quando não disponíveis,
explica como subir. O dispatch pelas modalidades existentes continua sendo
o caminho local; quando o externo está pronto, ele entra como opção.

Status: pronto | parado | nao_configurado.
"""
import os
from pathlib import Path

import httpx

from . import config, midia


def _url(chave: str) -> str:
    return str(os.getenv(chave, "") or "").strip()


def _online(url: str, timeout: float = 2.0) -> bool:
    """Health-check tolerante: qualquer resposta HTTP < 500 conta como no ar."""
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return r.status_code < 500
    except Exception:
        return False


def _wan_modelos() -> bool:
    difusor = next(iter(sorted(midia.VIDEO_DIR.glob("*ti2v*.gguf"))), None)
    return bool(difusor
                and (midia.VIDEO_DIR / "wan2.2_vae.safetensors").is_file()
                and (midia.VIDEO_DIR / "umt5-xxl-encoder-Q8_0.gguf").is_file())


def listar() -> list[dict]:
    """[{id, nome, tipo: builtin|externo, resumo, status, url?, motivo?}] —
    pronto = usável agora; parado = configurado mas fora do ar;
    nao_configurado = falta definir (env/binário/modelos)."""
    gpu_livre = config.GPU_MODO == "todos"
    fluxos: list[dict] = []

    # ---------- builtin: sd-cli (Flux t2i) ----------
    sd_ok = Path(config.SD_CLI).is_file() and gpu_livre
    fluxos.append({
        "id": "sd-cli",
        "nome": "sd-cli · Flux",
        "tipo": "builtin",
        "resumo": "Imagens Flux dev/schnell 100% locais (modalidade t2i do estúdio)",
        "status": "pronto" if sd_ok else "parado",
        "url": None,
        "motivo": (f"binário não encontrado ({config.SD_CLI})" if not Path(config.SD_CLI).is_file()
                   else None if gpu_livre
                   else "GPU em modo 'somente LLMs' — altere no badge 🎮"),
    })

    # ---------- builtin: wan2.2 (vídeo t2v/i2v) ----------
    wan_ok = _wan_modelos() and gpu_livre
    fluxos.append({
        "id": "wan2.2",
        "nome": "Wan2.2 TI2V-5B",
        "tipo": "builtin",
        "resumo": "Vídeo texto→vídeo e imagem→vídeo local (modalidades t2v/i2v, sai .mp4/.gif)",
        "status": "pronto" if wan_ok else "parado",
        "url": None,
        "motivo": ("modelos de vídeo ausentes em D:\\models\\video" if not _wan_modelos()
                   else None if gpu_livre
                   else "GPU em modo 'somente LLMs' — altere no badge 🎮"),
    })

    # ---------- externos: wan2gp e ComfyUI ----------
    for fid, nome, resumo, env, dica in (
        ("wan2gp", "wan2gp",
         "Geração de vídeo Wan2.2 com interface própria (notebook/app externo)",
         "FLUXO_WAN2GP_URL",
         "suba o wan2gp (python app.py) e aponte a URL — ex.: http://host.docker.internal:7860"),
        ("comfyui", "ComfyUI",
         "Pipelines node-based de difusão: imagens e vídeos com workflows próprios",
         "FLUXO_COMFY_URL",
         "suba o ComfyUI (python main.py) e aponte a URL — ex.: http://host.docker.internal:8188"),
    ):
        url = _url(env)
        if not url:
            fluxos.append({
                "id": fid, "nome": nome, "tipo": "externo", "resumo": resumo,
                "status": "nao_configurado", "url": None,
                "motivo": f"defina {env} no .env com a URL do serviço — {dica}",
            })
        elif _online(url):
            fluxos.append({
                "id": fid, "nome": nome, "tipo": "externo", "resumo": resumo,
                "status": "pronto", "url": url, "motivo": None,
            })
        else:
            fluxos.append({
                "id": fid, "nome": nome, "tipo": "externo", "resumo": resumo,
                "status": "parado", "url": url,
                "motivo": f"não respondeu em {url} — {dica}",
            })
    return fluxos
