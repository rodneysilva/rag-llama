# Contribuindo com o RagAroy

## Rodar o projeto localmente

Ver `README.md` (início rápido). Resumo:

```powershell
.\setup.ps1          # stack completa (api + qdrant + rabbit + redis + sandbox)
python servicos_llm.py   # LLM + embedding no host com GPU (opcional p/ endpoint remoto)
```

Mudou código da API? `docker compose up -d --build api` (nunca uvicorn direto em
operação — o stack espera o container).

## Onde mora o comportamento — a regra de ouro

**Toda comunicação com a LLM é via RAG**: prompts e formatos vivem em
`core/specs/*.md` ou no conteúdo indexado no Qdrant; o código só monta o
envelope (dados + `ETAPA: x`). Nada de prompt hardcoded. Coleções são sempre
genéricas (nenhum texto do sistema cita coleções específicas).

- Mudar COMPORTAMENTO → editar a spec (índice em `docs/README.md`).
- Specs são `lru_cache` — editar exige restart da API ou `POST /api/specs/reload`.
- Documentação em 3 camadas: README (usar) · AGENTS.md (modificar — memória
  operacional) · docs/ (análises). Conflito? **código > AGENTS.md > README >
  docs** — corrija o documento errado na mesma mudança.

## Ciclo de commit e deploy

`main` publica automaticamente (GitHub Actions → servidor). Prefira branch +
PR para mudanças grandes. Roda antes de commitar:

```powershell
python -m py_compile api/app.py core/<o-que-mudou>.py   # sintaxe
```

E use o E2E da área tocada (algo de `tests_manual/`) após o deploy.

## Higiene (repo público)

- **NUNCA** commitar: `.env`, `users.json`, `sessions/`, `saidas/`, `logs/`,
  `datasets/`, `qdrant_data/`, `rabbit_data/`, `hf_cache/`, `modelos_voz/` —
  todos no `.gitignore`; se criar um arquivo de estado novo, ignore-o também.
- Credenciais/endpoints reais vivem no `.env` — no código, só defaults de DEV
  claramente marcados (ver `docker-compose.yml`).
- Nunca nomear uma rota FastAPI com o nome de um módulo importado (sombreamento
  já derrubou produção — ver AGENTS.md "falso Qdrant indisponível").
- Textos que viram UI/specs: sempre UTF-8 (`Set-Content -Encoding UTF8`).
- O PowerShell 5.1 não tem `&&` — sequencie com `;`.

## Testes

- `tests_manual/` guarda os E2E por área (login, chat, ingestão, revisão, mídia,
  sandbox, telemetria…) — rodam contra a instância publicada ou local
  (`python -X utf8 tests_manual/e2e_final.py <base-url>`).
- Credenciais de teste vêm do `.env` (nunca hardcoded).
