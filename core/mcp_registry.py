"""
Registro de servidores MCP (Model Context Protocol): fica no mcp_servers.json
na raiz do projeto. Cada servidor pode ser stdio (comando local) ou http/sse (URL).

As ferramentas de um servidor marcado no chat entram no modo híbrido
(via core/agent.py). CLI: python -X utf8 -m core.mcp_registry
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from langchain_mcp_adapters.client import MultiServerMCPClient

ARQUIVO = Path(__file__).resolve().parent.parent / "mcp_servers.json"
CONHECIDOS = Path(__file__).resolve().parent.parent / "mcp_conhecidos.json"

# Em CONTAINER o repo é image-layer (instalação MORRIA no recreate): o
# registro vivo vai para logs/ (bind-mounted — persiste); na 1ª vez semeia
# do mcp_servers.json empacotado. No host, o arquivo do repo segue valendo.
if os.getenv("RAGAROY_CONTAINER") == "1":
    _persistente = (Path(__file__).resolve().parent.parent / "logs"
                    / "mcp_servers.json")
    if not _persistente.exists() and ARQUIVO.exists():
        try:
            _persistente.parent.mkdir(parents=True, exist_ok=True)
            _persistente.write_text(ARQUIVO.read_text(encoding="utf-8"),
                                    encoding="utf-8")
        except Exception:
            pass
    ARQUIVO = _persistente

# como o usuário escreve → como o MultiServerMCPClient entende
TRANSPORTES = {"stdio": "stdio", "http": "streamable_http", "sse": "sse"}


def list_conhecidos() -> list[dict]:
    """Catálogo de MCPs conhecidos (mcp_conhecidos.json) — instaláveis pela webui."""
    try:
        dados = json.loads(CONHECIDOS.read_text(encoding="utf-8"))
        return dados.get("servidores", [])
    except Exception:
        return []


def instalar_conhecido(nome: str, params: dict | None = None) -> dict:
    """Instala um MCP do catálogo: substitui os {{param}} pelos valores
    informados e registra no mcp_servers.json (npx/uvx baixam o resto na
    primeira execução)."""
    alvo = next((s for s in list_conhecidos() if s["nome"] == nome), None)
    if not alvo:
        raise ValueError(f"'{nome}' não está no catálogo de MCPs conhecidos")
    if alvo.get("nativo") or alvo.get("transport") == "nativo":
        raise ValueError("'{}' é NATIVO do RagAroy — já disponível no chat "
                         "(modo auto / 'pesquise na web'); nada a instalar"
                         .format(nome))
    params = params or {}
    faltando = [p["chave"] for p in alvo.get("params", [])
                if not (params.get(p["chave"]) or "").strip()]
    if faltando:
        raise ValueError("parâmetros obrigatórios: " + ", ".join(faltando))
    args = [_substituir(a, params) for a in alvo.get("args", [])]
    return save_server(nome, alvo["transport"], alvo.get("command", ""), args,
                       alvo.get("url", ""))


def _substituir(texto: str, params: dict) -> str:
    for k, v in params.items():
        texto = texto.replace("{{" + k + "}}", v)
    return texto


def _carregar() -> dict:
    if ARQUIVO.exists():
        return json.loads(ARQUIVO.read_text(encoding="utf-8"))
    return {}


def _gravar(dados: dict) -> None:
    ARQUIVO.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")


def list_servers() -> list[dict]:
    """Servidores registrados: [{nome, transport, command, args, url}]."""
    return [
        {"nome": nome, **cfg}
        for nome, cfg in sorted(_carregar().items())
    ]


def save_server(nome: str, transport: str, command: str = "",
                args: list[str] | None = None, url: str = "") -> dict:
    """Registra/atualiza um servidor MCP (upsert pelo nome)."""
    if transport not in TRANSPORTES:
        raise ValueError(f"transport inválido: use {', '.join(TRANSPORTES)}")
    if transport == "stdio" and not command:
        raise ValueError("transporte stdio exige o command (executável)")
    if transport != "stdio" and not url:
        raise ValueError("transporte http/sse exige a url")
    dados = _carregar()
    dados[nome] = {"transport": transport, "command": command,
                   "args": args or [], "url": url}
    _gravar(dados)
    return {"nome": nome, **dados[nome]}


def remove_server(nome: str) -> bool:
    """Remove o servidor; devolve True se existia."""
    dados = _carregar()
    if nome in dados:
        del dados[nome]
        _gravar(dados)
        return True
    return False


# ---------- campo único: detectar o que o operador colou -------------------

SERVIDORES_DIR = Path(__file__).resolve().parent.parent / "mcp_servers"


def detectar(entrada: str) -> dict:
    """Classifica a entrada do campo único em um registro de servidor.

    Aceita: URL http(s) de servidor MCP · comando completo (npx/uvx/node/
    python + args) · repo do GitHub (clona e detecta o runtime local).
    """
    t = (entrada or "").strip()
    if not t:
        raise ValueError("informe uma URL, um comando (npx/uvx…) ou um repo do GitHub")
    if t.startswith(("http://", "https://")):
        nome = urlparse(t).hostname.split(".")[0]
        return {"nome": nome, "transport": "http", "command": "", "args": [], "url": t}
    if t.startswith(("npx ", "uvx ", "node ", "python ", "uv ", "npm ")):
        partes = t.split()
        # FLAGS antes do pacote (ex.: `npx -y @scope/server`) não podem virar
        # nome do servidor — só argumentos posicionais contam
        posicoais = [a for a in partes[1:] if not a.startswith("-")]
        pkg = next((a for a in posicoais if a.startswith("@") or "/" in a),
                   posicoais[0] if posicoais else partes[-1])
        nome = re.sub(r"^@?", "", pkg).split("/")[-1].replace("mcp-server-", "") \
                                                  .replace("server-", "")
        nome = re.sub(r"\.(js|py|mjs|ts)$", "", nome)
        return {"nome": (nome or "servidor")[:30], "transport": "stdio",
                "command": partes[0], "args": partes[1:], "url": ""}
    m = re.match(r"(?:https://)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", t)
    if m:
        owner, repo = m.group(1), m.group(2)
        return {"nome": repo.replace("mcp-server-", "")[:30], "transport": "git",
                "command": "", "args": [], "url": f"https://github.com/{owner}/{repo}"}
    raise ValueError("não reconheci a entrada — use URL http, comando "
                     "(ex.: npx -y @modelcontextprotocol/server-fetch) ou "
                     "github.com/owner/repo")


def clonar_git(url: str, log=print) -> dict:
    """Clona o repo em mcp_servers/<nome> e converte em registro stdio
    DETECTANDO o runtime (Node → npm exec; Python → uvx --from)."""
    repo = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    destino = SERVIDORES_DIR / repo
    if not destino.exists():
        destino.parent.mkdir(parents=True, exist_ok=True)
        log(f"   📥 clonando {url}…")
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(destino)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ValueError(f"clone falhou: {r.stderr.strip()[:150]}")
    if (destino / "package.json").exists():
        pkg = json.loads((destino / "package.json").read_text(encoding="utf-8"))
        bin_ = list((pkg.get("bin") or {}).values()) or [pkg.get("name", repo)]
        return {"nome": repo.replace("mcp-server-", "")[:30], "transport": "stdio",
                "command": "npm", "args": ["exec", "--prefix", str(destino),
                                           "--", str(bin_[0])], "url": ""}
    if (destino / "pyproject.toml").exists():
        return {"nome": repo.replace("mcp-server-", "")[:30], "transport": "stdio",
                "command": "uvx", "args": ["--from", str(destino),
                                           pkg_nome_py(destino) or repo], "url": ""}
    shutil.rmtree(destino, ignore_errors=True)
    raise ValueError("repo sem package.json/pyproject.toml — não sei executar")


def pkg_nome_py(pasta: Path) -> str:
    """Nome do pacote Python do pyproject.toml (linha name = '...'/'...')."""
    try:
        m = re.search(r'^name\s*=\s*["\']([^"\']+)', (pasta / "pyproject.toml")
                      .read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def testar(entrada: str) -> dict:
    """Conecta NO servidor (sem registrar) e lista as ferramentas — o botão
    'testar' da webui. Devolve {ok, nome, ferramentas[], erro}."""
    reg = detectar(entrada)
    if reg["transport"] == "git":
        reg = clonar_git(reg["url"])
    cliente = MultiServerMCPClient({reg["nome"]: _config_cliente(reg)})
    ferramentas = asyncio.run(cliente.get_tools())
    return {"ok": True, "nome": reg["nome"], "registro": reg,
            "ferramentas": [{"nome": f.name,
                             "descricao": (f.description or "")[:120]}
                            for f in ferramentas]}


def _config_cliente(cfg: dict) -> dict:
    """Converte o registro salvo no formato do MultiServerMCPClient."""
    if cfg["transport"] == "stdio":
        return {"transport": "stdio", "command": cfg["command"], "args": cfg.get("args", [])}
    return {"transport": TRANSPORTES[cfg["transport"]], "url": cfg["url"]}


def carregar_ferramentas(nomes: list[str], log=None) -> tuple[list, dict]:
    """Conecta nos servidores pedidos e devolve ([tools LangChain], {nome: erro}).

    Um servidor fora do ar não derruba os outros. Cada tool ganha
    `metadata.servidor` — é o que o portão de aprovação exibe (ferramenta
    sozinha, sem servidor, é indecifrável para o operador).

    `log(msg)` narrativa a CONEXÃO no "pensando…" do chat: o npx/uvx de um
    servidor stdio pode levar dezenas de segundos na 1ª execução (download
    do pacote) — sem log por servidor o painel ficava mudo esse tempo todo.
    """
    log = log or (lambda m: None)
    registrados = _carregar()
    ferramentas, erros = [], {}
    for nome in nomes:
        cfg = registrados.get(nome)
        if not cfg:
            erros[nome] = "não registrado"
            log(f"⚠️ {nome}: não registrado")
            continue
        try:
            log(f"🔌 {nome}: conectando ({cfg.get('transport')}"
                f"{' — a 1ª conexão pode baixar o servidor, demora um pouco' if cfg.get('transport') == 'stdio' else ''})…")
            cliente = MultiServerMCPClient({nome: _config_cliente(cfg)})
            tools = asyncio.run(cliente.get_tools())  # get_tools é assíncrono
            for t in tools:  # de onde cada ferramenta veio (visível no gate)
                try:
                    t.metadata = {**(getattr(t, "metadata", None) or {}), "servidor": nome}
                except Exception:
                    pass
            ferramentas += tools
            log(f"✅ {nome}: {len(tools)} ferramenta(s) pronta(s)")
        except Exception as e:
            erros[nome] = str(e)[:200]
            log(f"❌ {nome}: {erros[nome]}")
    return ferramentas, erros


def main():
    servidores = list_servers()
    print(f"{len(servidores)} servidor(es) MCP em {ARQUIVO}")
    for s in servidores:
        destino = s["command"] + (" " + " ".join(s["args"]) if s["args"] else "") if s["transport"] == "stdio" else s["url"]
        print(f"  {s['nome']:20} [{s['transport']:6}] {destino}")


if __name__ == "__main__":
    sys.exit(main())
