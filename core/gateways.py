"""Gateways de saída — REGISTRO ÚNICO dos serviços externos (padrão comum).

Todo gateway do projeto (LLM, embedding, agente da GPU, sandbox) segue o
MESMO contrato: `url` base + `token` opcional (Bearer). Quem precisa de
HTTP usa `base(nome)` e `headers(nome)` — nunca mais padrões espalhados
(cada módulo com seu env e seu header era a bagunça que o dono pediu para
padronizar; SOLID: uma fonte, muitos consumidores).
"""
import os

# nome → (env da URL, default, env do token)
SERVICOS = {
    "llm":     ("LLM_BASE_URL", "http://127.0.0.1:8090/v1", None),
    "embed":   ("EMBED_BASE_URL", "http://127.0.0.1:8081/v1", None),
    "agente":  ("AGENTE_HOST_URL", "http://host.docker.internal:8010", "AGENTE_TOKEN"),
    "sandbox": ("SANDBOX_URL", "http://sandbox:8020", "SANDBOX_TOKEN"),
}


def base(nome: str) -> str:
    env_url, padrao, _tok = SERVICOS[nome]
    return (os.getenv(env_url) or padrao).rstrip("/")


def token(nome: str) -> str:
    _u, _p, env_tok = SERVICOS[nome]
    return (os.getenv(env_tok) or "").strip() if env_tok else ""


def headers(nome: str) -> dict:
    tok = token(nome)
    return {"Authorization": f"Bearer {tok}"} if tok else {}
