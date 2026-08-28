<div align="center">

<img src="static/logo.svg" width="72" alt="RagAroy"/>

# RagAroy

**Assistente local com RAG: base de conhecimento própria, chat com fontes citadas, multimídia e execução de código — LLM na sua GPU, custo zero por pergunta.**

[![Licença: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI multi-OS](https://img.shields.io/badge/CI-ubuntu%20%7C%20windows%20%7C%20macos-green.svg)](.github/workflows/ci-cd.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](requirements.txt)

[O que faz](#o-que-faz) · [Começo rápido](#começo-rápido) · [Ambientes](#ambientes-gitflow-develop--main) · [Arquitetura](#arquitetura) · [Documentação](#documentação)

</div>

---

## Visão geral

O RagAroy converte sua base de conhecimento na memória de um modelo de
linguagem local: perguntas do seu domínio respondem por busca vetorial
com **citação das fontes**, sem custo por token. Provedores externos
(GLM, DeepSeek, OpenAI, Claude) operam como complemento pontual quando o
assunto está fora das coleções e das ferramentas MCP.

| | local (RagAroy) | provedor externo |
|---|---|---|
| custo por pergunta | zero (GPU/CPU própria) | por token |
| latência | milissegundos | segundos |
| conhecimento | suas coleções (RAG) | treino do modelo + web |
| uso recomendado | domínio próprio, consultas repetidas, com fontes | o que não está nas bases nem nos MCPs |

## O que faz

**💬 Conversar com os documentos** — Ingestão por PDFs, pastas, datasets
HuggingFace e pesquisa web profunda, sempre em **modo Revisão** (nada
grava sem aprovação: chunks, duplicados, clusters e gate de tema). A
resposta cita trechos `[n]`; fragmento forte responde direto da base,
sem gastar LLM. Modos: `híbrido` (base + modelo), `rag` (só a base,
recusa honesta), `livre` e `auto` (roteador decide entre base e web).

**🧹 Base limpa por construção** — Cada chunk recebe um score de
qualidade 0–1 na ingestão (densidade de links, repetição, JSON cru,
tabelas sem prosa); abaixo de `SCORE_CHUNK_MIN` (.env) é rejeitado com
motivo no relatório. Coleções existentes têm higienização e varredura
com backup reversível.

**🔌 Provedores externos** — Qualquer endpoint OpenAI-compatible entra
pelo `.env` (`PROV_<id>_BASE_URL` + `_API_KEY`) e aparece no seletor
com os modelos **reais** do provedor; multimodais servem análise de
imagem. Chaves mascaradas na UI.

**🎨 Multimídia** — Texto→imagem (FLUX.1), texto→vídeo e imagem→vídeo
(Wan 2.1/2.2), GIF com loop, análise de imagem (Qwen2.5-VL local ou
multimodal externo). Progresso ao vivo; mídias geradas viram referência
na conversa.

**⚙️ Executar código** — Respostas com código viram projeto no painel;
▶ testar roda em container isolado (Python, Node, Java, .NET 8/10, Rust,
Ruby, PHP, Go, Dart), instala as dependências que o próprio código
importa e detecta o entry point. Sites Flask/FastAPI/ASP.NET sobem com
link temporário para navegar.

**📊 Observabilidade** — Dashboard por modelo (tokens, tok/s, chamadas),
infra Qdrant, histórico com log completo de cada job, telemetria
persistente.

| Chat (resposta + painel com ▶ testar) | Biblioteca (modo Revisão) |
|---|---|
| ![chat](docs/telas/chat.png) | ![biblioteca](docs/telas/biblioteca.png) |

Mais capturas e GIFs de uso real em [`docs/telas/`](docs/telas).

## Começo rápido

Pré-requisitos: **Docker** + **Python 3.11+**. GPU opcional (~8 GB VRAM
recomendada).

```bash
# 1) tudo em um comando (deps + docker + modelos essenciais)
./scripts/setup.sh --modelos chat,embed        # Linux/macOS/WSL
powershell -File setup.ps1 -Modelos chat,embed # Windows

# 2) subir os modelos (llama-server: chat + embedding)
python servicos_llm.py

# 3) abrir http://localhost:8000 e criar o login
```

<details>
<summary><b>Baixando modelos um por um</b> (ou outros tamanhos)</summary>

Um de cada tipo, em `~/models` (ou `D:\models`) —
`python scripts/baixar_modelos.py --listar` lista o catálogo com
comandos prontos:

| Tipo | Modelo | Tamanho |
|---|---|---|
| Conversa | Qwen2.5-Coder-7B Q4_K_M | 4,7 GB |
| Embedding | bge-m3 Q8 | 0,7 GB |
| Visão *(opcional)* | Qwen2.5-VL-7B Q4 + mmproj | 5,9 GB |
| Imagem *(opcional)* | FLUX.1-schnell Q4 (+T5/CLIP/VAE) | ~17 GB |
| Vídeo *(opcional)* | Wan2.1-T2V-1.3B Q8 · Wan2.2-TI2V-5B Q8 | 1,4 / 5 GB |
| Áudio *(opcional)* | whisper medium | 1,5 GB |

Mínimo para experimentar: **conversa + embedding**. O resto sobe por demanda.

</details>

<details>
<summary><b>Sem GPU local?</b></summary>

Aponte `LLM_BASE_URL`/`EMBED_BASE_URL` para qualquer endpoint
OpenAI-compatible (local via túnel ou provedor). Sem Docker no host, a
API roda com `uvicorn` — jobs no executor async embutido, contagem de
tokens em arquivo, nenhuma dependência extra.

</details>

<details>
<summary><b>Provedores externos (GLM, DeepSeek, OpenAI, Claude)</b></summary>

```env
# .env — o provedor aparece sozinho no seletor do chat (grupo 🌐)
PROV_GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
PROV_GLM_API_KEY=sk-...
PROV_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
PROV_DEEPSEEK_API_KEY=sk-...
# lista manual de reserva (se o provedor não listar /models):
# PROV_ANTHROPIC_MODELOS=claude-sonnet-4-5,claude-haiku-4-5
```

Modelos vêm do `GET /models` (cache 5 min); multimodais marcados com 👁.
A telemetria registra o modelo real (`[glm] glm-4.6`). Chaves nunca
aparecem na UI; o embedding segue local (bge-m3) para a base não perder
dimensão.

</details>

## Ambientes (gitflow: develop → main)

| | produção | desenvolvimento |
|---|---|---|
| URL | `ai.disroy.org` | `dev.disroy.org` |
| branch | `main` | `develop` |
| deploy | automático a cada push (CI/CD → VPS) | automático a cada push (job `cd-dev`) |
| stack | `~/apps/rag-llama` · containers `ragaroy-*` | `~/apps/rag-llama-dev` · containers `ragaroy-dev-*` |
| estado | volumes próprios (qdrant/sessions/logs) | volumes próprios e independentes |
| GPU/LLM | túneis `llm/embed/agente.disroy.org` (estação do usuário) | idem — a GPU nunca reside no servidor |
| reranker | ativo (cross-encoder) | desativado (`RERANKER=0`) — preserva a CPU compartilhada |

Os dois ambientes coexistem na mesma VPS com isolamento total de
estado. Fluxo: desenvolver e validar em `develop` (dev.disroy.org); o
avanço `develop → main` publica em produção.

## Arquitetura

<a href="https://github.com/rodneysilva/rag-llama/raw/main/docs/arquitetura.svg">
  <img src="docs/arquitetura.svg" width="880" alt="Arquitetura do RagAroy em camadas: usuário → webui HTMX → API FastAPI (composição + routers) + executor async de jobs + provedores → Qdrant/Sandbox, com estação GPU opcional à direita"/>
</a>

```
┌─ estação com GPU (opcional) ─────────────┐   ┌─ servidor (docker compose) ──────────┐
│ llama-server  chat :8090 · embed :8081   │⇄⇄│ api :8000   FastAPI + webui (HTMX)   │
│ llama-server  visão :8082 (on-demand)    │tú│ · composição + routers por domínio   │
│ agente :8010   troca de modelo · GPU     │nel│ · executor async de jobs (in-proc)   │
│ sd-cli  Flux · Wan2.1/2.2 · whisper      │   │ qdrant     vetores + full-text      │
└───────────────────────────────────────────┘   │ sandbox    execução isolada + sites │
                                                │ dev: mesma stack, ragaroy-dev-*     │
                                                └──────────────────────────────────────┘
```

- **Monólito modular em camadas** (SOLID/DDD/Clean): `api/app.py` é
  composição (~90 linhas); rotas em `api/routers/*` por domínio; domínio
  em `core/*`; contrato normativo em [`docs/arquitetura.md`](docs/arquitetura.md).
- **Toda tarefa longa é job** no executor async in-process (fila serial,
  retry com backoff para transientes) — UI nunca bloqueia, sem broker.
- **A GPU é a estação do usuário** — o servidor não hospeda modelos de
  linguagem; sem GPU local, provedores cloud cobrem o chat e a base
  segue no Qdrant do servidor.
- **Comportamento vive em specs** ([`core/specs/*.md`](core/specs)):
  mudar como o assistente responde é editar markdown, não código.
- API interativa em **`/docs`** (OpenAPI).

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/arquitetura.md`](docs/arquitetura.md) | Contrato de arquitetura (camadas, routers, SOLID, dívidas, procedimento de alteração) |
| [`AGENTS.md`](AGENTS.md) | Memória operacional: stack, comandos, armadilhas, decisões |
| [`docs/README.md`](docs/README.md) | Índice da documentação (análises, planos, specs) |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Como contribuir (issues e PRs; mudança de comportamento = editar spec) |
| [`SECURITY.md`](SECURITY.md) | Modelo de segurança (auth scrypt+HMAC, sandbox isolada, segredos no .env) |

## Projetos open source que o sustentam

[llama.cpp](https://github.com/ggml-org/llama.cpp) · [LangChain](https://github.com/langchain-ai/langchain) · [Qdrant](https://qdrant.tech) · [FastAPI](https://fastapi.tiangolo.com) · [HTMX](https://htmx.org) + [Tailwind](https://tailwindcss.com) · [FLUX.1](https://github.com/black-forest-labs/flux) · [Wan2.1](https://github.com/Wan-Video/Wan2.1) · [Qwen2.5](https://github.com/QwenLM/Qwen2.5) · [bge-m3](https://huggingface.co/BAAI) · [Trafilatura](https://github.com/adbar/trafilatura) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) · [Piper](https://github.com/rhasspy/piper) — licenças e lista completa em [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Segurança

Auth scrypt + tokens HMAC; sandbox em rede interna isolada (não-root);
ferramenta MCP só executa com aprovação; segredos apenas no `.env`
(gitignored). Detalhes em [`SECURITY.md`](SECURITY.md).

---

## Licença

MIT — veja [LICENSE](LICENSE).
