#!/usr/bin/env python3
"""Serviços LLM do RagAroy — sobe o embedding (bge-m3, :8081) e UM modelo
de conversa (:8090) com llama-server, em QUALQUER sistema operacional
(Windows, Linux, macOS) — 100% stdlib, sem dependência do projeto.

Como usar:
    python servicos_llm.py            # menu interativo (escolhe o modelo)
    python servicos_llm.py --listar   # só lista os GGUFs encontrados
    python servicos_llm.py --parar    # encerra os serviços desta sessão

Configuração (arquivo .env na pasta atual — TUDO opcional):
    MODELS_DIR   pasta dos GGUFs (perguntado interativamente se vazio)
    LLAMA_BIN    caminho do binário llama-server (default: do PATH)
    LLM_API_KEY  chave exigida pelos servidores (recomendado em produção)
    CORS_ORIGIN  origem liberada no CORS (default: *)

O script grava LLM_MODEL no .env ao trocar de modelo e os PIDs em
saidas/servicos_llm.json para o --parar.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
ENV = RAIZ / ".env"
PIDFILE = RAIZ / "saidas" / "servicos_llm.json"
CHAT_PORTA, EMBED_PORTA = 8090, 8081
EMBED_ALIAS = "bge-m3"


# ───────────────────────── .env minimalista (stdlib) ─────────────────────────
def env_ler() -> dict:
    dados = {}
    if ENV.is_file():
        for ln in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                dados[k.strip()] = v.strip()
    return dados


def env_gravar(chave: str, valor: str) -> None:
    """Grava a chave no .env preservando o resto (cria se não existe)."""
    linhas = ENV.read_text(encoding="utf-8", errors="replace").splitlines() \
        if ENV.is_file() else []
    feito = False
    for i, ln in enumerate(linhas):
        if ln.strip().startswith(chave + "="):
            linhas[i] = f"{chave}={valor}"
            feito = True
            break
    if not feito:
        linhas.append(f"{chave}={valor}")
    ENV.write_text("\n".join(linhas) + "\n", encoding="utf-8")


# ───────────────────────── util ─────────────────────────
def saude(porta: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/health",
                                    timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def servido(porta: int) -> str | None:
    """Alias do modelo que a porta serve (OpenAI /v1/models)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{porta}/v1/models",
                                    timeout=3) as r:
            dados = json.loads(r.read())
            ids = [m.get("id") for m in dados.get("data", [])]
            return ids[0] if ids else None
    except Exception:
        return None


def achar_bin() -> str | None:
    """llama-server no PATH (ou LLAMA_BIN do .env)."""
    cand = os.getenv("LLAMA_BIN", "").strip()
    if cand and (Path(cand).is_file() or subprocess.run(
            ["which" if os.name != "nt" else "where", cand],
            capture_output=True).returncode == 0):
        return cand
    for nome in ("llama-server", "llama-server.exe"):
        if subprocess.run(["which" if os.name != "nt" else "where", nome],
                          capture_output=True).returncode == 0:
            return nome
    return None


def perguntar_pasta(env: dict) -> Path | None:
    if not sys.stdin.isatty():   # CI/pipe: sem interação possível
        print("MODELS_DIR não definido no .env — defina e rode de novo")
        return None
    print("\nOnde estão seus modelos (.gguf)?")
    print("  (dica: cole o caminho da pasta — ex.: /home/user/models,")
    print("         D:\\models ou /Users/voce/models; Enter usa ./models)")
    resp = input("pasta: ").strip().strip('"').strip("'") or "./models"
    pasta = Path(resp).expanduser().resolve()
    if not pasta.is_dir():
        print(f"⚠️ pasta não encontrada: {pasta}")
        return None
    env_gravar("MODELS_DIR", str(pasta))
    return pasta


def listar_ggufs(pasta: Path) -> list[Path]:
    return sorted(p for p in pasta.rglob("*.gguf"))


def _gb(p: Path) -> float:
    return p.stat().st_size / (1024 ** 3)


def _alias(p: Path) -> str:
    return p.stem.lower()


# ───────────────────────── processos ─────────────────────────
def subir(binario: str, args: list[str], rotulo: str) -> subprocess.Popen:
    """Sobe o processo em background multi-OS (sem console novo, imune ao
    fechamento do terminal no Windows; novo grupo no POSIX)."""
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.DETACHED_PROCESS)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen([binario, *args], **kw)
    print(f"  ▲ {rotulo} (pid {proc.pid})")
    return proc


def _flags_comuns(env: dict) -> list[str]:
    flags = ["--host", "127.0.0.1"]
    if env.get("LLM_API_KEY"):
        flags += ["--api-key", env["LLM_API_KEY"]]
    flags += ["--cors-origins", env.get("CORS_ORIGIN") or "*"]
    return flags


def main() -> int:
    env = env_ler()
    if "--parar" in sys.argv:
        return parar()
    binario = achar_bin()
    pasta = Path(env["MODELS_DIR"]).expanduser() if env.get("MODELS_DIR") else None
    while (not pasta or not pasta.is_dir()) or "--escolher" in sys.argv:
        pasta = perguntar_pasta(env)
        if pasta or not sys.stdin.isatty():
            break
    if not pasta or not pasta.is_dir():
        print("⚠️ pasta de modelos inválida — configure MODELS_DIR no .env")
        return 1
    ggufs = listar_ggufs(pasta)
    embeds = [g for g in ggufs if "bge-m3" in g.name.lower()]
    # só GGUFs de CONVERSA: o menu antigo listava difusores (flux/wan) como
    # "modelo de conversa" — escolher um deles tentava bootar imagem no
    # llama-server. A categoria vem do mesmo _categorizar do modelos.listar
    from core.modelos import _categorizar
    chats = [g for g in ggufs
             if g not in embeds and _categorizar(g.stem) == "chat"]
    if not ggufs:
        print(f"nenhum .gguf em {pasta}")
        return 1
    print(f"\n📁 modelos em {pasta} ({len(ggufs)} GGUFs)")
    if "--listar" in sys.argv:
        for g in ggufs:
            print(f"  {_gb(g):5.1f} GB  {g.name}")
        return 0
    print("\nStatus: ", end="")
    print(f"chat :{CHAT_PORTA} [{'NO AR: ' + servido(CHAT_PORTA)}" if saude(CHAT_PORTA) else f":{CHAT_PORTA} FORA]", end=" · ")
    print(f"embedding :{EMBED_PORTA} [{'NO AR' if saude(EMBED_PORTA) else 'FORA'}]")
    print("\nModelo de conversa (embedding bge-m3 sobe junto sempre):")
    # marcação de VRAM + DEFAULT COMPATÍVEL (pedido do dono): o Enter escolhe
    # o PRIMEIRO que cabe na placa, não o 1º alfabético; incompatíveis vêm
    # marcados e exigem confirmação explícita
    limite = 6.0  # GB de GGUF que convive com o bge-m3 numa VRAM de 8 GB
    compat = [g for g in chats if _gb(g) <= limite]
    sugerido = compat[0] if compat else None
    for i, g in enumerate(chats, 1):
        marca = ("" if _gb(g) <= limite
                 else f"  ⚠️ {_gb(g):.1f} GB NÃO CABEM na VRAM de 8 GB "
                      "(com o embedding no ar)")
        seta = "  ← sugerido" if sugerido and g == sugerido else ""
        print(f"  [{i}] {_gb(g):5.1f} GB  {g.name}{marca}{seta}")
    if not sugerido:
        print("  ⚠️ nenhum modelo cabe na VRAM junto do embedding — "
              "escolha um menor ou apague/renomeie os grandes")
    try:
        n = input(f"número (Enter = {chats.index(sugerido) + 1 if sugerido else 1}): ").strip() \
            or (str(chats.index(sugerido) + 1) if sugerido else "1")
        escolhido = chats[int(n) - 1]
        if sugerido and escolhido != sugerido and _gb(escolhido) > limite:
            conf = input(f"  '{escolhido.name}' tem {_gb(escolhido):.1f} GB e "
                         "pode estourar a VRAM com o embedding no ar. "
                         "continuar? (s/N): ").strip().lower()
            if conf != "s":
                print("   cancelado — rode de novo e escolha o sugerido")
                return 1
    except (ValueError, IndexError):
        print("escolha inválida")
        return 1
    alias = _alias(escolhido)
    if not binario:
        print("⚠️ llama-server não achado no PATH — instale (brew/apt/build)"
              " ou defina LLAMA_BIN no .env")
        return 1

    embed_gguf = embeds[0] if embeds else None
    if saude(CHAT_PORTA) and (servido(CHAT_PORTA) or "") == alias:
        print(f"\n✅ {alias} já está no ar — nada a fazer")
    else:
        print(f"\n🚀 subindo {alias} + embedding…")
        procs = {}
        if embed_gguf and not saude(EMBED_PORTA):
            procs["embed"] = subir(binario, [
                "-m", str(embed_gguf), "--embeddings", "--pooling", "cls",
                "-ngl", "99", "-c", "8192", "-ub", "8192", "-b", "8192",
                "--alias", EMBED_ALIAS, "--port", str(EMBED_PORTA),
                *_flags_comuns(env)])
        procs["chat"] = subir(binario, [
            "-m", str(escolhido), "-ngl", "99", "-c", "32768", "-np", "2",
            "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0",
            "--alias", alias, "--metrics", "--port", str(CHAT_PORTA),
            *_flags_comuns(env)])
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(json.dumps(
            {"pids": {k: p.pid for k, p in procs.items()}}), encoding="utf-8")
        print("\naguardando /health", end="")
        for _ in range(120):
            time.sleep(1)
            print(".", end="", flush=True)
            if saude(CHAT_PORTA):
                break
        print()
        if saude(CHAT_PORTA):
            print(f"✅ chat :{CHAT_PORTA} ({alias}) · embedding "
                  f":{EMBED_PORTA} {'OK' if saude(EMBED_PORTA) else 'subindo…'}")
            env_gravar("LLM_MODEL", alias)
            print(f"   LLM_MODEL={alias} gravado no .env")
        else:
            print("⚠️ ainda subindo — confira http://127.0.0.1:8090/health")
    return 0


def parar() -> int:
    if not PIDFILE.is_file():
        print("nenhum registro de serviços desta sessão")
        return 0
    dados = json.loads(PIDFILE.read_text(encoding="utf-8"))
    mortos = 0
    for rotulo, pid in (dados.get("pids") or {}).items():
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            mortos += 1
            print(f"  ⏹ {rotulo} (pid {pid})")
        except (ProcessLookupError, PermissionError):
            print(f"  · {rotulo} (pid {pid}) já saiu")
    PIDFILE.unlink(missing_ok=True)
    print(f"{mortos} serviço(s) encerrado(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
