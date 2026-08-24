"""Rodeiro ÚNICO de linguagens de programação (Fase E — fim do drift).

Três listas divergiam (unificar_arquiteturas 7, auto 8, app 12) e a regra
"base unificada entra junto" batia diferente conforme o arquivo. Uma fonte
só: `LINGUAGENS` (coleções de linguagem que puxam a arquitetura_unificada e
entram na unificação) e `EH_DEV` (nomes fixos de coleções dev, ex-catálogo).
"""
# coleções de LINGUAGEM de programação (regra da base unificada + unificação)
LINGUAGENS = {
    "rust", "java", "csharp", "nodejs", "go", "python", "ruby", "dotnet",
    "javascript", "typescript", "kotlin", "swift", "php", "c", "cpp",
}

# coleções DEV fixas (display/agrupamento do catálogo — não só linguagens:
# frameworks e docs de modelos também são "dev")
EH_DEV = LINGUAGENS | {
    "angular", "react", "htmx", "arquitetura_unificada", "docs_flux",
    "docs_wan", "docs_bge_m3", "docs_qwen", "docs_qwen_vl",
}
