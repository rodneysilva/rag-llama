"""
Configurações centrais do aplicativo, lidas do .env na raiz do projeto.

A webui edita esse arquivo pela API; reload() aplica as mudanças sem reiniciar.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env fica sempre na raiz do projeto (independente de onde o app rodar)
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# MODO CONTAINER (docker compose): a API roda em container e os endpoints de
# INFRA vêm do environment do compose (não do .env) — o .env continua montado
# para segredos/estado, mas NÃO sobrescreve o environment real.
EM_CONTAINER = os.getenv("RAGAROY_CONTAINER", "") == "1"

# Presets de modelo (rótulo, URL, nome servido) — llama-server nativo
# (<dir-llama.cpp>), ligado por servicos_llm.py na raiz do
# projeto (menu de modelos; o script já atualiza LLM_MODEL no .env). O chat
# sobe com -np 4: 4 chamadas simultâneas (chat + ingestão + agente MCP ao
# mesmo tempo; extras entram na fila, não falham). Os GGUFs ficam em
# D:\models; 8 GB de VRAM comporta UM modelo de conversa por vez, além do
# embedding — todos abaixo servidos na :8090, escolhidos no script.
MODELOS = {
    "llamacpp_qwen25": ("Qwen 2.5 Coder 7B GGUF — llama-server :8090 (padrão, 4 slots)",
                        "http://localhost:8090/v1", "qwen2.5-coder-7b"),
    "llamacpp_qwen3":  ("Qwen3 8B GGUF — llama-server :8090 (subir pelo servicos_llm.py)",
                        "http://localhost:8090/v1", "qwen3-8b"),
    "llamacpp_qwen25i": ("Qwen 2.5 7B Instruct GGUF — llama-server :8090 (via servicos_llm.py)",
                         "http://localhost:8090/v1", "qwen2.5-7b-instruct"),
    "llamacpp_llama31": ("Llama 3.1 8B Instruct GGUF — llama-server :8090 (via servicos_llm.py)",
                         "http://localhost:8090/v1", "llama3.1-8b-instruct"),
    "llamacpp_phi4":   ("Phi-4 Mini GGUF — llama-server :8090 (leve; via servicos_llm.py)",
                        "http://localhost:8090/v1", "phi4-mini-instruct"),
}

# Presets de embedding: ATENÇÃO à dimensão — trocar o embedding exige reingerir
# as coleções (a base atual é toda BGE-M3, 1024 dims). O BGE-M3 GGUF roda no
# próprio llama-server (:8081, subir junto no iniciar.bat).
EMBEDDINGS = {
    "llamacpp_bge_m3": ("BGE-M3 GGUF — llama-server :8081 (1024 dims, base existente)",
                        "http://localhost:8081/v1", "bge-m3"),
}

# Campos editáveis no painel: chave -> (grupo, rótulo, tipo)
# (COLLECTION não é editável: o chat lista TODAS as coleções; a ingestão sem
# coleção informada cria uma a partir do nome da pasta)
FIELDS = {
    "LLM_BASE_URL":   ("Serviços", "URL da LLM (llama-server)", "str"),
    "LLM_MODEL":      ("Serviços", "Modelo de conversa ativo", "str"),
    "EMBED_BASE_URL": ("Serviços", "URL do embedding", "str"),
    "EMBED_MODEL":    ("Serviços", "Embedding ativo", "str"),
    "QDRANT_URL":     ("Serviços", "URL do Qdrant", "str"),
    "SERPER_API_KEY": ("Serviços", "Chave do Serper (pesquisa na web)", "secret"),
    "LLM_PROVIDERS": ("Serviços", "Provedores externos (ids, vírgula — glm,"
                      "deepseek,openai,anthropic…; também auto-descobertos"
                      " por PROV_*_BASE_URL)", "str"),
    "HF_TOKEN":       ("Serviços", "Token do HuggingFace (datasets privados)", "secret"),
    "ESTUDIO_PAUSAR_CHAT": ("Estúdio · memória",
                            "Pausar o chat (:8090) durante geração de mídia "
                            "(1=sim; liberar VRAM p/ difusão; volta sozinho ao fim)", "int"),
    "ESTUDIO_VRAM_ASSENTAMENTO_S": ("Estúdio · memória",
                                    "Segundos de espera fixa após derrubar/erguer "
                                    "serviço (a VRAM libera sozinha; o app não mede)", "int"),
    "ESTUDIO_RESTORE_TENTATIVAS": ("Estúdio · memória",
                                   "Tentativas de reerguer o chat ao fim da geração", "int"),
    "ESTUDIO_PAUSAR_EMBED": ("Estúdio · memória",
                             "Pausar o embedding (:8081) também em geração LEVE "
                             "(0=padrão: vídeo pausa sempre, t2i/whisper convive; "
                             "1=pausa em toda geração — religa com prioridade ao fim)", "int"),
    "GPU_MODO": ("GPU · exclusividade",
                 "Uso da GPU: 'todos' (aberta a LLMs E difusão/whisper) ou "
                 "'somente_llms' (só os llama-servers; estúdio/difusão/whisper "
                 "recusados com erro claro). Controlável pelo badge 🎮 do topo.",
                 "str"),
    "CHUNK_SIZE":     ("Aplicação", "Tamanho do pedaço (chunk)", "int"),
    "CHUNK_OVERLAP":  ("Aplicação", "Sobreposição entre pedaços", "int"),
    "TOP_K":          ("Aplicação", "Documentos recuperados (top-k)", "int"),
    "SCORE_MIN":      ("Aplicação", "Pontuação mínima na busca (0 a 1)", "float"),
    "SCORE_DIRETO":   ("Aplicação",
                       "Score do melhor fragmento a partir do qual a base "
                       "RESPONDE por si (0.65): resposta direta do fragmento, "
                       "SEM consultar a LLM (pedido do dono — economia total "
                       "de tokens). Calibrado: docker×VM real 0.695 ✓",
                       "float"),
    "SCORE_FRACO":    ("Aplicação",
                       "Score abaixo do qual os fragmentos são FRACOS (0.55): "
                       "não sustentam a pergunta e NÃO entram no prompt — "
                       "híbrido responde só com o modelo (fim da alucinação "
                       "sobre contexto irrelevante, caso vatapá 0.470); rag "
                       "recusa. Relevante-médio real: 0.606", "float"),
    "TEMPERATURE":    ("Aplicação", "Temperatura da LLM", "float"),
    "PROMPT_SYSTEM":  ("Aplicação", "Prompt de sistema do RAG", "text"),
    "RERANKER":       ("Aplicação",
                       "Reranker cross-encoder local (1=liga; reordena os top "
                       "achados e corta para os 4 melhores — degrada em "
                       "silêncio se torch não estiver instalado)", "int"),
    "RERANK_MODEL":   ("Aplicação",
                       "Modelo do reranker no HuggingFace (testado: "
                       "BAAI/bge-reranker-base ~1,1 GB · BAAI/bge-reranker-"
                       "v2-m3 ~2,3 GB, PT-BR melhor — compare com "
                       "tests_manual\\bench_rerank.py antes de trocar)", "text"),
}

# chaves cujo valor é SEGREDO: exibidas mascaradas e nunca regravadas
# quando o formulário devolve a máscara (ou vazio) — só troca explícita
SECRETOS = {"SERPER_API_KEY", "HF_TOKEN", "AGENTE_TOKEN"}

# Valores atuais (preenchidos por reload())
LLM_BASE_URL = ""
LLM_MODEL = ""
EMBED_BASE_URL = ""
EMBED_MODEL = ""
QDRANT_URL = ""
COLLECTION = ""
SERPER_API_KEY = ""
AUTH_SECRET = ""
AUTH_ADMIN_USER = ""
AUTH_ADMIN_PASS = ""
LLAMA_BIN = ""      # binário do llama-server (servir GGUF)
SD_CLI = ""         # binário do sd-cli (difusão)
WHISPER_CLI = ""    # binário do whisper-cli (transcrição)
ESTUDIO_PAUSAR_CHAT = 1
ESTUDIO_VRAM_ASSENTAMENTO_S = 6
ESTUDIO_RESTORE_TENTATIVAS = 3
CHUNK_SIZE = 0
CHUNK_OVERLAP = 0
TOP_K = 0
SCORE_MIN = 0.0
SCORE_DIRETO = 0.65   # ≥: o fragmento É a resposta (zero LLM) — real: 0.695
SCORE_FRACO = 0.55    # <: fragmentos fracos não entram no prompt — real: 0.47/0.606
TEMPERATURE = 0.0
PROMPT_SYSTEM = ""
# F1b/F2: flags de desenvolvimento/qualidade (MOCK_LLM valida a UI sem LLM;
# RERANKER liga o cross-encoder da busca — degrada se torch ausente)
MOCK_LLM = False
RERANKER = True
RERANK_MODEL = "BAAI/bge-reranker-base"


def set_env(chave: str, valor: str) -> None:
    """Grava a chave no .env (preservando o resto) e recarrega em memória."""
    linhas = ENV_FILE.read_text(encoding="utf-8").splitlines() \
        if ENV_FILE.exists() else []
    feito = False
    for i, linha in enumerate(linhas):
        if linha.startswith(f"{chave}="):
            linhas[i] = f"{chave}={valor}"
            feito = True
            break
    if not feito:
        linhas.append(f"{chave}={valor}")
    ENV_FILE.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    reload()


def _bool_env(chave: str, padrao: bool) -> bool:
    return os.getenv(chave, "1" if padrao else "0").strip().lower() in ("1", "true", "sim", "yes")


def set_env_inplace(chave: str, valor: str) -> None:
    """Grava `chave=valor` no .env SEM rename — o bind mount de ARQUIVO
    único do Docker não aceita os.replace por cima do mount point (o
    dotenv.set_key padrão morre com 'Device or resource busy'). Reescreve
    o conteúdo no MESMO arquivo (truncate+write, permitido)."""
    caminho = Path(ENV_FILE)
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        linhas = []
    feito = False
    for i, l in enumerate(linhas):
        ls = l.strip()
        if ls.startswith(chave + "=") or ls.startswith(chave + " ="):
            linhas[i] = f"{chave}={valor}"
            feito = True
            break
    if not feito:
        linhas.append(f"{chave}={valor}")
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    # também no ambiente DO PROCESSO: em container o load_dotenv do reload é
    # SEM override (env do compose vence) — sem isto, gravar não aplicava
    os.environ[chave] = str(valor)


def reload():
    """Relê o .env e atualiza os valores deste módulo."""
    global LLM_BASE_URL, LLM_MODEL, EMBED_BASE_URL, EMBED_MODEL, QDRANT_URL
    global COLLECTION, SERPER_API_KEY, CHUNK_SIZE, CHUNK_OVERLAP, TOP_K
    global SCORE_MIN, TEMPERATURE, PROMPT_SYSTEM
    global SCORE_DIRETO, SCORE_FRACO
    global ESTUDIO_PAUSAR_CHAT, ESTUDIO_VRAM_ASSENTAMENTO_S, ESTUDIO_RESTORE_TENTATIVAS, ESTUDIO_PAUSAR_EMBED, GPU_MODO
    global AUTH_SECRET, AUTH_ADMIN_USER, AUTH_ADMIN_PASS
    global LLAMA_BIN, SD_CLI, WHISPER_CLI
    global MOCK_LLM, RERANKER
    serper_ambiente = os.environ.get("SERPER_API_KEY", "")  # env real tem prioridade
    # em container: environment do compose VENCE o .env (endpoints de infra);
    # no host: .env é a fonte da verdade (comportamento original)
    load_dotenv(ENV_FILE, override=not EM_CONTAINER)
    SERPER_API_KEY = serper_ambiente or os.getenv("SERPER_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:8080/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder-7b")
    EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "http://localhost:8081/v1")
    EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
    QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
    COLLECTION = os.getenv("COLLECTION", "meus_docs")
    AUTH_SECRET = os.getenv("AUTH_SECRET", "")
    AUTH_ADMIN_USER = os.getenv("AUTH_ADMIN_USER", "")
    AUTH_ADMIN_PASS = os.getenv("AUTH_ADMIN_PASS", "")
    # binários locais (caminhos da máquina — ajuste no .env se precisar)
    LLAMA_BIN = os.getenv("LLAMA_BIN", r"<dir-llama.cpp>\llama-server.exe")
    SD_CLI = os.getenv("SD_CLI", r"<dir-sdcli>\sd-cli.exe")
    WHISPER_CLI = os.getenv("WHISPER_CLI", r"<dir-whisper>\whisper-cli.exe")
    ESTUDIO_PAUSAR_CHAT = _bool_env("ESTUDIO_PAUSAR_CHAT", True)
    ESTUDIO_VRAM_ASSENTAMENTO_S = int(os.getenv("ESTUDIO_VRAM_ASSENTAMENTO_S", "6"))
    ESTUDIO_RESTORE_TENTATIVAS = int(os.getenv("ESTUDIO_RESTORE_TENTATIVAS", "3"))
    ESTUDIO_PAUSAR_EMBED = _bool_env("ESTUDIO_PAUSAR_EMBED", False)
    GPU_MODO = os.getenv("GPU_MODO", "todos").strip() or "todos"
    if GPU_MODO not in ("todos", "somente_llms"):
        GPU_MODO = "todos"
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "2000"))
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "400"))
    TOP_K = int(os.getenv("TOP_K", "4"))
    SCORE_MIN = float(os.getenv("SCORE_MIN", "0.35"))
    SCORE_DIRETO = float(os.getenv("SCORE_DIRETO", "0.65"))
    SCORE_FRACO = float(os.getenv("SCORE_FRACO", "0.55"))
    TEMPERATURE = float(os.getenv("TEMPERATURE", "0.5"))
    PROMPT_SYSTEM = os.getenv(
        "PROMPT_SYSTEM",
        "Você é um assistente que responde em português usando APENAS o contexto fornecido.",
    )
    MOCK_LLM = _bool_env("MOCK_LLM", False)
    RERANKER = _bool_env("RERANKER", True)
    RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base").strip()


def as_dict():
    """Todas as configurações como dicionário (usado pela API/webui)."""
    return {
        "LLM_BASE_URL": LLM_BASE_URL,
        "LLM_MODEL": LLM_MODEL,
        "EMBED_BASE_URL": EMBED_BASE_URL,
        "EMBED_MODEL": EMBED_MODEL,
        "QDRANT_URL": QDRANT_URL,
        "COLLECTION": COLLECTION,
        "SERPER_API_KEY": SERPER_API_KEY,
        "HF_TOKEN": os.getenv("HF_TOKEN", ""),
        "ESTUDIO_PAUSAR_CHAT": int(ESTUDIO_PAUSAR_CHAT),
        "ESTUDIO_VRAM_ASSENTAMENTO_S": ESTUDIO_VRAM_ASSENTAMENTO_S,
        "ESTUDIO_RESTORE_TENTATIVAS": ESTUDIO_RESTORE_TENTATIVAS,
        "ESTUDIO_PAUSAR_EMBED": int(ESTUDIO_PAUSAR_EMBED),
        "GPU_MODO": GPU_MODO,
        "CHUNK_SIZE": CHUNK_SIZE,
        "CHUNK_OVERLAP": CHUNK_OVERLAP,
        "TOP_K": TOP_K,
        "SCORE_MIN": SCORE_MIN,
        "SCORE_DIRETO": SCORE_DIRETO,
        "SCORE_FRACO": SCORE_FRACO,
        "TEMPERATURE": TEMPERATURE,
        "PROMPT_SYSTEM": PROMPT_SYSTEM,
        "RERANKER": int(RERANKER),
        "RERANK_MODEL": RERANK_MODEL,
    }


reload()  # carrega os valores na importação
