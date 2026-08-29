"""Rotas de status das famílias de job — extraídas de api/app.py (split Fase 1).
_rota_status registra no router local (antes registrava no app; mesmo efeito).

Fase 2: o registry levanta JobNaoEncontrado (domínio) — a conversão em
HTTP 404 vive AQUI, na borda da API (o core não conhece FastAPI).
"""
from api.base import *  # noqa: F401,F403 — contrato do split
from api.routers.sistema import status  # noqa: F401 — chamada cross-router (era namespace global do monólito)

from fastapi import APIRouter

from core.jobs import JobNaoEncontrado

router = APIRouter()

def _rota_status(caminho: str, reg: "JobRegistry", msg404: str) -> None:
    """Registra a rota GET de status de uma família — o corpo era idêntico
    palavra por palavra em 8 lugares."""
    def status(job: str, cursor: int = 0):
        try:
            return reg.status(job, cursor, msg404)
        except JobNaoEncontrado as e:
            raise HTTPException(status_code=404, detail=str(e))
    router.get(caminho)(status)


_rota_status("/api/manutencao/status/{job}", _manutencao,
             "Job de manutenção não encontrado")

_rota_status("/api/ingest/preview/status/{job}", _preview,
             "Pré-visualização não encontrada")

_rota_status("/api/pesquisa/status/{job}", _pesquisa,
             "Job de pesquisa não encontrado")

_rota_status("/api/ingest/status/{job}", _ingest,
             "Job de ingestão não encontrado")

_rota_status("/api/higienizar/status/{job}", _higieniza,
             "Job de higienização não encontrado")

_rota_status("/api/sandbox/status/{job}", _sbx,
             "Teste de sandbox não encontrado")

_rota_status("/api/midia/status/{job}", _midia,
             "Análise multimodal não encontrada")

_rota_status("/api/limpeza/status/{job}", _limpeza,
             "Job de limpeza não encontrado")

_rota_status("/api/seed/status/{job}", _seed, "Job de seed não encontrado")

_rota_status("/api/varredura/status/{job}", _varredura,
             "Job de varredura não encontrado")

_rota_status("/api/query/status/{job}", _query,             "Job de consulta não encontrado")

_rota_status("/api/mcp/instalar-job/status/{job}", _mcp,
             "Job de instalação não encontrado")

