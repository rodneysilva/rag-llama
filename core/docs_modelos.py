"""
Roteiro de documentação dos MODELOS em uso: para cada modelo ativo (Flux,
Wan2.2, Qwen, bge-m3, Qwen2.5-VL…), baixa a documentação oficial via seed
profundo e cria a coleção `docs_<modelo>` — assim cada resposta sobre "como
usar/prompts deste modelo" vem da documentação real, não do palpite.

CLI: python -X utf8 -m core.docs_modelos [--fontes 8]
"""
import sys

from .seed import seed_collection

# alias do modelo → assunto da documentação oficial (inglês — melhor busca)
ROTEIRO = {
    "flux": ("Flux.1 image generation model official documentation: prompts "
             "guidance, schnell vs dev, best practices", "docs_flux"),
    "wan": ("Wan2.2 video generation model official documentation: text to "
            "video, image to video, prompts, negative prompts", "docs_wan"),
    "bge-m3": ("BGE-M3 embedding model official documentation: multilingual "
               "retrieval, dense and sparse, usage", "docs_bge_m3"),
    "qwen": ("Qwen2.5 and Qwen3 official model documentation: prompting, "
             "chat templates, best practices", "docs_qwen"),
    "vl": ("Qwen2.5-VL vision language model official documentation: image "
           "understanding, video analysis, prompts", "docs_qwen_vl"),
}


def rodar(fontes: int = 8, log=print) -> list[dict]:
    """Roda o seed de documentação de TODOS os modelos do roteiro."""
    resultados = []
    for chave, (assunto, colecao) in ROTEIRO.items():
        log(f"\n📚 documentação de {chave} → {colecao}")
        try:
            r = seed_collection(assunto, colecao, fontes, log=log)
            resultados.append({"modelo": chave, "colecao": colecao,
                               "pontos": r["ingestao"]["total_points"], "ok": True})
        except Exception as e:
            log(f"⚠️ {chave} falhou: {e}")
            resultados.append({"modelo": chave, "erro": str(e)[:200], "ok": False})
    ok = [r for r in resultados if r["ok"]]
    log(f"\n✅ {len(ok)}/{len(resultados)} documentações criadas")
    return resultados


def main():
    fontes = int(sys.argv[sys.argv.index("--fontes") + 1]) if "--fontes" in sys.argv else 8
    rodar(fontes)


if __name__ == "__main__":
    main()
