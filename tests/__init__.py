"""Suite de testes do RagAroy — 3 camadas.

Camadas (rodar a partir da raiz):
    python -m pytest tests/ -x -q              # tudo
    python -m pytest tests/core -q             # CORE: logica pura (sem rede/GPU)
    python -m pytest tests/api -q              # API: rotas com FastAPI TestClient
    python -m pytest tests/ui -q               # UI: templates + JS (render real)

Princípio: o CORE deve ser PERFEITO — testes rápidos, determinísticos e
sem dependência externa (Qdrant/LLM/Redis sobem como fixtures marcáveis;
sem eles, o teste falha com marca clara, não finge passar).
"""
