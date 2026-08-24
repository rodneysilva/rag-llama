# Especificação de como construir uma base de conhecimento completa

Uma base de conhecimento serve para o sistema **saber do assunto antes de
recorrer ao conhecimento interno do modelo**. O raciocínio lógico tem 5
passos, sempre nesta ordem:

## 1. Fonte oficial única e versionada

Escolha UMA fonte canônica do assunto (ex.: repositório oficial de
documentação, livro, manual). Fonte única evita duplicidade e conflito
entre versões. Prefira conteúdo com data/licença claras (ex.: MIT,
Creative Commons) e baixe sempre a versão mais recente:

```
git clone --depth 1 --filter=blob:none --sparse <repo> datasets/<nome>
git sparse-checkout set <pastas-curadas>
```

O clone esparso baixa só as pastas escolhidas, sem histórico — bases de
milhares de arquivos ficam pequenas e a atualização é um `git pull`.

## 2. Curadoria por área do conhecimento

Não ingira tudo junto. Olhe a estrutura da fonte e separe em **áreas que
correspondem ao uso real** — cada área vira uma coleção:

| Área               | O que contém                          | Por que separar               |
|--------------------|---------------------------------------|-------------------------------|
| comandos/CLI       | referência de comandos e flags        | resposta precisa, curta       |
| fundamentos        | conceitos, runtime, empacotamento     | contexto conceitual           |
| linguagem          | sintaxe, referência da linguagem      | código correto na versão atual|
| bibliotecas (BCL)  | APIs do framework, tipos base         | detalhes de API               |

Coleções separadas = o operador escolhe a área no chat e cada uma contribui
com suas melhores respostas sem dominar as outras.

## 3. Ingestão em modo lote

Base grande (centenas+ de arquivos) usa o **modo lote** (`--rapido`):
1 chamada de LLM por arquivo é inviável. No modo lote a categoria é o nome
da coleção e a descrição é o nome do arquivo. A categorização fina por
tema (destrinchar) pode ser feita depois, se fizer sentido.

## 4. Catalogação

Depois de ingerir, rode a análise por coleção: a LLM lê amostras e grava
categoria + descrição em português no catálogo (`meta_colecoes`). É isso
que aparece no painel e orienta a escolha no chat.

## 5. Uso no chat: base como referência primária

No modo híbrido, a base recuperada é a **referência primária** — o modelo
contextualiza e completa, mas parte do que a base diz. Perguntas factuais
do assunto ficam ancoradas na fonte oficial (com citação `[n]` e fontes),
não no conhecimento de treinamento do modelo (que pode estar defasado).

## Atualização

Para atualizar a base: `git pull` na fonte e reingira a área que mudou.
Para recomeçar uma coleção, apague-a no dashboard do Qdrant e ingira de
novo (reingestir sem apagar duplica os pontos).

## Instâncias construídas neste sistema

| Área                 | Coleções                                        | Fonte oficial (clone esparso em `datasets/`)            |
|----------------------|-------------------------------------------------|---------------------------------------------------------|
| .NET                 | `dotnet_cli`, `dotnet_fundamentos`, `dotnet_csharp`, `dotnet_bcl` | `github.com/dotnet/docs` (MIT)            |
| Python (linguagem)   | `py_linguagem` (tutorial/referência/how-tos/whatsnew), `py_stdlib` (biblioteca padrão) | `github.com/python/cpython` → `Doc/` (PSF) |
| IA — orquestração    | `py_ia_langchain` (LangChain/LangGraph/conceitos), `py_ia_langsmith` (LangSmith) | `github.com/langchain-ai/docs` |
| IA — SDKs e modelos  | `py_ia_openai` (SDK Python oficial), `py_ia_huggingface` (Transformers), `py_ia_llamaindex` (LlamaIndex) | `github.com/openai/openai-python`, `github.com/huggingface/transformers`, `github.com/run-llama/llama_index` |

O mesmo raciocínio vale para qualquer outro assunto: escolha a fonte
canônica, separe por área de uso, ingira em lote, catalogue e use no
híbrido.
