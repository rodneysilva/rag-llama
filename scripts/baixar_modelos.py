# -*- coding: utf-8 -*-
"""Baixa TODOS os modelos do RagAroy em um comando (multi-OS, retomável).

    python scripts/baixar_modelos.py --tipos chat,embed            # mínimo
    python scripts/baixar_modelos.py --tipos tudo --dir ~/models   # tudo
    python scripts/baixar_modelos.py --listar                      # catálogo

Tipos: chat · embed · visao · imagem · video (Wan2.1 leve) · video2
       (Wan2.2 5B) · audio (whisper) · tudo

Cada download é retomável (huggingface_hub continua de onde parou) e pula
arquivos que já existem no destino. O destino padrão é ~/models
(MODELS_DIR do .env — o servicos_llm.py pergunta/grava na 1ª execução).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from huggingface_hub import hf_hub_download, list_repo_files
except ImportError:
    sys.exit("pip install -U huggingface_hub  (ou: pip install -r requirements.txt)")

# ═══ catálogo: (repo, arquivo, subpasta, tamanho aproximado) ═══
CATALOGO: dict[str, list[tuple[str, str, str, str]]] = {
    "chat": [
        ("Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
         "qwen2.5-coder-7b-instruct-q4_k_m.gguf", "", "4,7 GB"),
    ],
    "embed": [
        ("CompendiumLabs/bge-m3-gguf", "bge-m3-q8_0.gguf", "", "0,7 GB"),
    ],
    "visao": [
        ("ggml-org/Qwen2.5-VL-7B-Instruct-GGUF",
         "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf", "visao", "5,9 GB"),
        ("ggml-org/Qwen2.5-VL-7B-Instruct-GGUF",  # mmproj (projetor da visão)
         None, "visao", "~0,7 GB"),
    ],
    "imagem": [
        ("city96/FLUX.1-schnell-gguf", "flux1-schnell-Q4_K_S.gguf",
         "imagem", "6,3 GB"),
        ("comfyanonymous/flux_text_encoders", "t5xxl_fp16.safetensors",
         "imagem", "9,8 GB"),
        ("comfyanonymous/flux_text_encoders", "clip_l.safetensors",
         "imagem", "0,25 GB"),
        ("foxmail/flux_vae", "ae.safetensors", "imagem", "0,34 GB"),
    ],
    "video": [
        ("samuelchristlie/Wan2.1-T2V-1.3B-GGUF",
         "Wan2.1-T2V-1.3B-Q8_0.gguf", "video", "1,4 GB"),
        ("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q8_0.gguf",
         "video", "2,3 GB"),
        ("Comfy-Org/Wan_2.1_ComfyUI_Repackaged",
         "split_files/vae/wan_2.1_vae.safetensors", "video", "0,5 GB"),
    ],
    "video2": [
        ("QuantStack/Wan2.2-TI2V-5B-GGUF", None, "video", "~5 GB (Q8)"),
        ("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-Q8_0.gguf",
         "video", "2,3 GB"),
        ("Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
         "wan2.2_vae.safetensors", "video", "0,5 GB"),
    ],
    "audio": [
        ("ggerganov/whisper.cpp", "ggml-medium.bin", "audio", "1,5 GB"),
    ],
}
ROTULOS = {"chat": "conversa (Qwen2.5-Coder 7B Q4)", "embed": "embedding (bge-m3 Q8)",
           "visao": "visão (Qwen2.5-VL 7B)", "imagem": "imagem (FLUX.1 schnell)",
           "video": "vídeo leve (Wan2.1 T2V 1.3B)", "video2": "vídeo pesado (Wan2.2 TI2V 5B)",
           "audio": "transcrição (whisper medium)"}


def _resolver(repo: str, nome: str | None) -> str:
    """nome=None → acha sozinho (Q8_0 do Wan2.2 / mmproj da visão)."""
    arquivos = list_repo_files(repo)
    if nome:
        achado = next((f for f in arquivos if f.split("/")[-1] == nome), None)
        if not achado:
            raise SystemExit(f"❌ '{nome}' não existe em {repo}")
        return achado
    if "Wan2.2" in repo:
        return next(f for f in arquivos if f.endswith("Q8_0.gguf"))
    return next(f for f in arquivos if "mmproj" in f.lower() and f.endswith(".gguf"))


def baixar(tipo: str, destino: Path) -> None:
    print(f"\n═══ {ROTULOS.get(tipo, tipo)} ═══")
    for repo, nome, sub, _gb in CATALOGO[tipo]:
        pasta = destino / sub if sub else destino
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo = _resolver(repo, nome)
        final = pasta / arquivo.split("/")[-1]
        if final.exists():
            print(f"✔ {final.name} já existe — pulando")
            continue
        print(f"⬇ {repo}/{arquivo} → {pasta}")
        hf_hub_download(repo_id=repo, filename=arquivo, local_dir=str(pasta))
        print(f"✅ {final.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Baixa os modelos do RagAroy")
    ap.add_argument("--tipos", default="chat,embed",
                    help="chat,embed,visao,imagem,video,video2,audio,tudo (padrão: chat,embed)")
    ap.add_argument("--dir", default=str(Path.home() / "models"),
                    help="pasta de destino (padrão: ~/models — use a MESMA do MODELS_DIR)")
    ap.add_argument("--listar", action="store_true", help="só mostra o catálogo")
    args = ap.parse_args()

    if args.listar:
        for t, itens in CATALOGO.items():
            print(f"{t:8} {ROTULOS.get(t, '')}")
            for repo, nome, _s, gb in itens:
                print(f"         {repo}/{nome or '(auto)'} — {gb}")
        return 0

    tipos = (args.tipos or "").lower().split(",")
    if "tudo" in tipos:
        tipos = list(CATALOGO)
    invalidos = [t for t in tipos if t not in CATALOGO]
    if invalidos:
        ap.error(f"tipos inválidos: {', '.join(invalidos)} — use --listar")
    destino = Path(args.dir).expanduser()
    print(f"🎲 baixando {', '.join(tipos)} para {destino}")
    for t in tipos:
        baixar(t.strip(), destino)
    print(f"\n🎉 concluído. Rode `python servicos_llm.py` apontando para {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
