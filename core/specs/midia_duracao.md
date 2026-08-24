# Duração de vídeo/gif (seletor do chat → frames do Wan)

## Negative prompt (🚫 editável no pedido)

A difusão usa um negative prompt PADRÃO do modelo (Wan: cores saturadas/
estáticas/desfoque/legenda…). O pedido pode SOBRESCREVER com a cláusula
`negativo: <texto>` no fim do prompt — condiz com a solicitação e aparece
no log ("🚫 negative prompt do pedido"). O ✨ pode adicioná-la quando o
pedido mencionar o que NÃO quer (ex.: "sem pessoas, negativo: pessoas,
multidão").

O Wan gera a ~16 fps. A duração pedida em SEGUNDOS vira frames assim:

| duração | frames | para que serve |
|---|---|---|
| 2 s | 33 | UM beat de ação: um gesto, uma olhada, um ciclo curto |
| 3 s | 49 | um arco mínimo: preparo → ação → reação |
| 5 s | 81 | uma cena que se desenvolve: ação + consequência visível |
| 8 s | 129 | sequência contínua com 2–3 beats encadeados (teto prático) |

Regras:
- O tempo de geração e a VRAM crescem ~linear com os frames (8 s ≈ 4× o
  custo de 2 s). Em GPU de 8 GB, prefira ≤5 s quando não precisar.
- Acima de ~8 s a coerência do Wan degrada (cena única sem cortes é o
  contrato do modelo) — para histórias longas, gere VÁRIOS clipes e
  edite; não peça um plano-sequência.

## GIF (seletor em FRAMES — loop a 12 fps)

| frames | duração do loop | para que serve |
|---|---|---|
| 17 | ~1,5 s | um ciclo puro (respirar, piscar, girar) — o clássico |
| 33 | ~3 s | ciclo com desenvolvimento (onda que quebra, passo duplo) |
| 49 | ~4 s | ida-e-volta mais elaborado (ainda SEM cortes) |

Regra de ouro do gif segue valendo em QUALQUER frame count: o último
frame precisa levar de volta ao primeiro (loop perfeito); movimento no
centro; sem texto. Mais frames = ação mais longa, não mais coisas
acontecendo.
- GIF segue FIXO em 17 frames (~1,5 s a 12 fps no loop): gif bom é ciclo
  perfeito, não duração.

## Como escrever o prompt POR duração (para o assistente ✨ e o guia)

- **2 s**: uma única ação legível no centro do quadro ("o gato vira a
  cabeça", "a vela tremula"). Nada mais acontece.
- **3 s**: começo-meio-fim mínimos: pose inicial → gesto → micro-reação.
- **5 s**: ação principal + desenvolvimento (a câmera acompanha, o
  ambiente reage, o sujeito conclui o movimento).
- **8 s**: 2–3 beats ENCADEADOS na MESMA cena contínua (ela atravessa →
  encontra → reage), SEM cortes — transições por movimento, não por edição.
- A cláusula de câmera (dolly/pan/fixa) fecha o prompt em qualquer
  duração; em 8 s a câmera pode EVOLUIR uma vez (dolly lento que acelera
  no 2º beat, por exemplo).
