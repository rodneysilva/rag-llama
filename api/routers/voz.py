"""Rotas de voz — extraídas mecanicamente de api/app.py (split Fase 1).
Ordem interna preservada; decorator @app -> @router.
"""
from api.base import *  # noqa: F401,F403 — contrato do split

from fastapi import APIRouter

router = APIRouter()
@router.get("/api/voz/disponivel")
def voz_disponivel():
    """O que está pronto (STT/TTS) — a webui habilita os botões por isto."""
    return voz.disponivel()


@router.post("/api/voz/falar")
def voz_falar(body: VozFalarIn, request: Request):
    """Texto → fala (piper pt_BR, CPU). Devolve .wav para tocar no browser."""
    _usuario(request)
    if not (body.texto or "").strip():
        raise HTTPException(status_code=400, detail="texto vazio")
    try:
        cam = voz.falar(body.texto,
                        f"saidas/audios/fala_{int(time.time() * 1000)}.wav",
                        log=lambda m: print(m))
        return FileResponse(cam, media_type="audio/wav",
                            filename=Path(cam).name,
                            headers={"Cache-Control": "no-store"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/api/voz/transcrever")
async def voz_transcrever(request: Request):
    """Fala → texto (whisper small CPU): o microfone do chat manda webm/wav,
    devolve o texto para o campo de pergunta."""
    _usuario(request)
    from fastapi import UploadFile
    form = await request.form()
    arquivo = form.get("file")
    # request.form() devolve Starlette UploadFile (fastapi.UploadFile é
    # SUBCLASSE — isinstance falha): checamos pelo contrato (filename/read)
    if arquivo is None or not getattr(arquivo, "filename", None) or \
            not hasattr(arquivo, "read"):
        raise HTTPException(status_code=400, detail="envie um áudio (file)")
    conteudo = await arquivo.read()
    if len(conteudo) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="áudio muito grande (máx 25 MB)")
    import tempfile
    sufixo = Path(arquivo.filename).suffix or ".webm"
    tmp = Path(tempfile.gettempdir()) / f"ragaroy_voz_{int(time.time() * 1000)}{sufixo}"
    wav = tmp.with_suffix(".16k.wav")
    try:
        await asyncio.to_thread(tmp.write_bytes, conteudo)
        # webm do MediaRecorder → wav 16 kHz (whisper entende direto, mas o
        # wav evita depender do container de áudio no decode)
        import subprocess
        proc = await asyncio.to_thread(
            subprocess.run, ["ffmpeg", "-y", "-i", str(tmp), "-ar", "16000",
                             "-ac", "1", str(wav)],
            **{"capture_output": True, "timeout": 120})
        alvo = str(wav) if proc.returncode == 0 and wav.exists() else str(tmp)
        texto = await asyncio.to_thread(voz.transcrever, alvo,
                                        lambda m: print(m))
        return {"texto": texto}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])
    finally:
        tmp.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)


