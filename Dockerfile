# ═══════════════════════════════════════════════════════════════
# RagAroy — imagem da API (FastAPI + UI HTMX/Jinja)
# ═══════════════════════════════════════════════════════════════
# A API conversa com os serviços: Qdrant/Redis/Rabbit (compose, rede
# interna) e llama-server/EMBED no HOST (host.docker.internal — GPU e
# binários ficam no host de propósito). Estado (sessions/saidas/logs/
# datasets/.env/users.json) vem de volumes — veja docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/ragaroy/.cache/huggingface

WORKDIR /app

# dependências primeiro (camada cacheada) — ffmpeg: conversão de mídia
# (webm do microfone → wav do whisper, mp4 dos vídeos, gif dos vídeos).
# torch vem do índice CPU (a API não usa GPU: reranker roda em CPU; o wheel
# CUDA do Linux seriam ~2,5 GB à toa) e satisfaz o pin do requirements.txt.
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# aplicação + UI server-rendered (templates/ + static/) + configs
COPY api/ api/
COPY core/ core/
COPY templates/ templates/
COPY static/ static/
COPY mcp_servers.json mcp_conhecidos.json usuarios_permitidos.txt ./

# usuário não-root
RUN useradd -m ragaroy && chown -R ragaroy:ragaroy /app
USER ragaroy

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
