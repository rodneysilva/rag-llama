# Pesquisa: geração de mídia local (texto→imagem, texto→vídeo, imagem→vídeo)

Máquina: ASUS Zephyrus G16 — Ryzen AI 9 HX 370, 32 GB RAM LPDDR5X, RTX 4070
laptop (8 GB VRAM), 2 TB SSD. Análise e crítica do plano recebido antes de
executar qualquer coisa — nenhum download foi feito ainda.

---

## 1. Crítica do texto original (o que acerta e o que erra)

### ✅ O que acerta

| Afirmação | Veredito |
|---|---|
| 32 GB de RAM mudam o jogo (offload de T5/VAE pra RAM) | **Correto** — é o que viabiliza Flux dev Q4 em 8 GB de VRAM |
| Flux.1 **Dev** GGUF (city96/FLUX.1-dev-gguf) em vez de Schnell | **Correto** — o stable-diffusion.cpp suporta Flux e os GGUFs do city96 foram feitos pra ele |
| "CUDA - Sysmem Fallback Policy" → "Prefer No Sysmem Fallback" | **Correto e recomendado** — evita o driver derramar VRAM na RAM pela PCIe (degradação de 4–5×). ⚠️ Detalhe que o texto não conta: **a configuração volta pro padrão a cada atualização do driver** |
| Controlar o offload pelo código, não deixar o driver improvisar | **Correto** — princípio certo |
| flux1-dev-Q4_K_M ≈ 6–7 GB | **Correto** (~6,8 GB só o transformer; o T5-XXL de ~9 GB fica na RAM) |

### ❌ O que erra (e importa)

1. **HunyuanVideo NÃO roda no stable-diffusion.cpp.** O mantenedor disse
   explicitamente que a arquitetura é diferente demais e não há plano
   ([Discussion #828](https://github.com/leejet/stable-diffusion.cpp/discussions/828)).
   Os arquivos `hunyuan-video-t2v-q4_k.gguf` "de arquivo único" que o texto
   manda procurar **não existem**. O repo do Kijai citado é de *custom nodes
   de ComfyUI*, não arquivos soltos pra chamar de Python.
2. **A família de vídeo que o sd.cpp suporta é a Wan** — o próprio subtítulo
   do repo: "Diffusion model (SD, Flux, **Wan**, …)". GGUFs prutos em
   [city96/Wan2.1-T2V-14B-gguf](https://huggingface.co/city96/Wan2.1-T2V-14B-gguf),
   [city96/Wan2.1-I2V-14B-480P-gguf](https://huggingface.co/city96/Wan2.1-I2V-14B-480P-gguf)
   e [QuantStack/Wan2.2-I2V-A14B-GGUF](https://huggingface.co/QuantStack/Wan2.2-I2V-A14B-GGUF).
3. **O exemplo Python é inventado.** `StableDiffusion(compute_type="cuda",
   low_vram=True, cpu_vae=True)` não existe. O pacote real é
   [`stable-diffusion-cpp-python`](https://pypi.org/project/stable-diffusion-cpp-python/)
   (API alto nível `.txt_to_img(...)`, e o offload é `offload_to_cpu=True`
   nos parâmetros de carga). Fonte com o código:
   [binding no GitHub](https://github.com/william-murray1204/stable-diffusion-cpp-python/blob/main/stable_diffusion_cpp/stable_diffusion_cpp.py).
4. **Velocidade de vídeo irreal.** Wan/Hunyuan 14B em Q4 leva ~15 min por
   clipe de 5 s **numa 4090**; na 4070 laptop será pior (dezenas de minutos
   por clipe). Vídeo local em 8 GB é "existe", não é "fluido e estável".
5. Falta o caminho **rápido** de vídeo: [LTX-Video](https://github.com/Lightricks/ltx-video)
   faz 720×480×121 frames em **menos de 1 min numa 4060 de 8 GB**
   (benchmark oficial) — mas o ecossistema é diffusers/ComfyUI, não GGUF
   de arquivo único.

---

## 2. Lineup corrigida para o Zephyrus G16

Filosofia mantida (GGUF de arquivo único, Python limpo), corrigindo o que
não existe:

| Função | Arquivo | Tamanho | Fonte | Expectativa na 4070 laptop |
|---|---|---|---|---|
| **Imagem** (qualidade) | `flux1-dev-Q4_K_M.gguf` | ~6,8 GB | [city96/FLUX.1-dev-gguf](https://huggingface.co/city96/FLUX.1-dev-gguf) | ~1–3 min/imagem 1024 px, 20–25 passos |
| **Imagem** (rápida, opcional) | `flux1-schnell-Q4_K_S.gguf` | ~6,3 GB | city96 | 4 passos, ~15–40 s/imagem |
| **Texto→vídeo** | `wan2.1-t2v-14b-Q4_0.gguf` (480p) | ~9 GB (fica na RAM) | [city96/Wan2.1-T2V-14B-gguf](https://huggingface.co/city96/Wan2.1-T2V-14B-gguf) | muitos min/clipe (5 s, 480p) |
| **Imagem→vídeo** | `wan2.1-i2v-14b-480p-Q4_0.gguf` | ~9 GB | [city96/Wan2.1-I2V-14B-480P-gguf](https://huggingface.co/city96/Wan2.1-I2V-14B-480P-gguf) | idem (anima a imagem do Flux) |
| **Vídeo rápido** (alternativo) | LTX-Video 0.9.x distilled (diffusers) | pipeline | [Lightricks/ltx-video](https://github.com/Lightricks/ltx-video) | <1 min/clipe 736×480 (quebra o "arquivo único") |
| ~~HunyuanVideo~~ | — | — | — | **descartado**: só via ComfyUI, sem binding limpo |

Wan 1.3B (leve, t2v) existe, mas **não tem i2v oficial** — para o par
"gera imagem → anima" o 14B é o caminho.

## 3. Controle do offloading no Windows (o ritual)

1. **Painel NVIDIA** (uma vez por versão de driver): Gerenciar configurações
   3D → `CUDA - Sysmem Fallback Policy` → **"Prefer No Sysmem Fallback"**
   (global ou só para `python.exe`). Sem isso o driver "compra" memória
   compartilhada e tudo fica 4–5× mais lento em vez de falhar.
2. **No código**, o offload explícito: `offload_to_cpu=True` no binding
   Python (ou `--offload-to-cpu` no CLI do sd.cpp) — T5 e o que não couber
   ficam na RAM de 32 GB e só o bloco em cálculo vai pra VRAM.
3. **VRAM é workspace, não armazenamento**: geração de mídia em 8 GB exige
   parar o modelo de chat (o swap já implementado no `core/modelos.py`).

## 4. Desenho da integração no rag-llama (proposta)

Tudo se apoia no que já existe — registro por categoria (`core/modelos.py`
já reconhece `imagem`/`video` por padrão de nome; pastas `D:\models\imagem`
e `D:\models\video` já criadas) e no ritual de swap de VRAM.

**Pipeline com poder de decisão (o "critique antes de executar"):**

```
ideia do usuário
  → LLM local gera 3 variações de prompt (spec midia_prompt.md)
  → LLM critica cada variação: ambiguidade, termo que o modelo não conhece,
    composição, excesso de conceitos (spec midia_critica.md)
  → escolhe/combina a melhor (mostra as descartadas e o porquê)
  → gera (sd.cpp: imagem ou wan t2v/i2v)
  → devolve artefato + prompt final + caminho em saidas/
```

**Modo mídia (política de VRAM):** `POST /api/gerar` → job em background
(como a ingestão, com polling `/api/gerar/status/{job}`). Antes de gerar:
para o chat :8090 **e** o embedding :8081 (Flux Q4 + ativações precisa dos
8 GB inteiros; Qdrant continua de pé — só leitura de catálogo não usa VRAM).
Depois: sobe de volta o modelo de chat anterior (o alias vem no pedido).

**Webui:** aba "🎨 Mídia" com o campo de ideia, seletor do modelo
(imagem/t2v/i2v), preview das 3 variações + crítica antes do "gerar", e
galeria de `saidas/`. A mesma regra da frente 1: chamada sempre carrega
`model` + `provider`.

## 5. O que falta decidir (nada foi baixado)

1. Autorizar os downloads (~6,8 GB Flux dev + ~9 GB Wan t2v + ~9 GB Wan i2v
   ≈ 25 GB no SSD de 2 TB) — e se quer o Schnell também.
2. Runtime da imagem: **binding Python** (integrado à API, recomendado) ou
   CLI do sd.cpp como subprocesso. Wan depende do suporte a vídeo do
   binding (a API muda rápido no sd.cpp — validar na instalação).
3. Se quer LTX-Video como atalho rápido de vídeo (aceita quebrar o
   "arquivo único" e puxa diffusers + torch ~6 GB de dependências).
