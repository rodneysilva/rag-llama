#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# RagAroy — instalação em UM comando (Linux/macOS/WSL/Git-Bash)
#
#   ./scripts/setup.sh                     # stack (deps + docker) mínima
#   ./scripts/setup.sh --modelos chat,embed  # também baixa os modelos
#   ./scripts/setup.sh --modelos tudo        # chat+embed+visao+imagem+vídeo
#
# Faz: venv + pip → .env/users/dirs → docker compose up → (opcional)
# download dos modelos (scripts/baixar_modelos.py) → dicas finais.
# Os MODELOS rodam no host com `python servicos_llm.py` (llama-server).
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")/.."

MODELOS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --modelos) MODELOS="${2:-chat,embed}"; shift 2 ;;
    *) echo "uso: ./scripts/setup.sh [--modelos chat,embed|tudo]"; exit 1 ;;
  esac
done

echo "== RagAroy setup =="

# 1) dependências Python (venv isolada)
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
[[ -d .venv ]] || "$PY" -m venv .venv
# ativa dentro deste shell (portável: bin/ ou Scripts/)
if [[ -x .venv/bin/python ]]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python; fi
"$VPY" -m pip install -q -U pip
"$VPY" -m pip install -q -r requirements.txt
echo "✔ dependências Python instaladas (.venv)"

# 2) .env e arquivos de estado que o compose monta
[[ -f .env ]] || { cp .env.example .env; \
  echo "!! .env criado do exemplo — EDITE: AUTH_ADMIN_USER/AUTH_ADMIN_PASS"; }
[[ -f users.json ]] || echo '{"usuarios": {}}' > users.json
[[ -f usuarios_permitidos.txt ]] || \
  echo "# Nomes permitidos a criar login (um por linha)" > usuarios_permitidos.txt
mkdir -p sessions saidas logs datasets

# 3) stack docker (qdrant + rabbit + redis + sandbox + api)
if command -v docker >/dev/null 2>&1; then
  docker compose up -d --build
  echo "✔ docker compose no ar"
else
  echo "!! Docker não encontrado — rode a API dev: $VPY -m uvicorn api.app:app --port 8000"
fi

# 4) modelos (opcional — o mínimo é chat+embed)
if [[ -n "$MODELOS" ]]; then
  echo "== baixando modelos ($MODELOS) =="
  "$VPY" scripts/baixar_modelos.py --tipos "$MODELOS"
fi

cat <<'FIM'

== RagAroy pronto =="
   app         : http://localhost:8000  (crie o login na 1ª visita)
   qdrant      : http://localhost:6333/dashboard
   rabbit mgmt : http://localhost:15672 (RABBIT_USER/PASS do .env)

   MODELOS (host): python servicos_llm.py   # sobe chat :8090 + embed :8081
   baixar mais   : python scripts/baixar_modelos.py --listar
FIM
