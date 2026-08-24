"""
Acervo de prompts exemplares para o RAG de prompts (coleção prompts_midia).

O pipeline de mídia recupera os 3 exemplares mais parecidos com a ideia e
os entrega ao gerador de prompts como referência de estrutura — o padrão
"prompt corpus + retrieval" usado em produção (AWS/Medium/Qdrant).

Indexar:  python -X utf8 -m core.prompts_corpus
"""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from . import config, rag

COLECAO = "prompts_midia"

# (estilo, prompt) — inglês (Flux/Wan entendem melhor), ~75 palavras, com
# a estrutura sujeito → ação → ambiente → luz → estilo → técnica.
CORPUS = [
    # — fotorrealismo
    ("fotorrealista", "weathered Brazilian fisherman mending nets on a colorful wooden jangada boat at dawn, northeast coast, soft pink and gold sunrise light, mist over calm sea, cinematic documentary photography, 35mm lens, shallow depth of field, natural skin texture"),
    ("fotorrealista", "elderly street vendor arranging tropical fruits at a crowded market stall, late afternoon sun cutting through canvas awnings, dust particles in light beams, candid photojournalism, 50mm f/1.8, warm color grade, sharp focus on hands"),
    ("retrato studio", "close-up portrait of a young woman with freckles and short curly hair, soft window light from the left, dark grey seamless background, editorial fashion photography, 85mm f/1.4, catchlights in the eyes, muted color palette"),
    ("fotorrealista", "black cat sitting on a rain-soaked Tokyo balcony at night, neon signs reflecting on wet fur and glass, bokeh city lights in background, moody cinematic photography, shallow depth of field, cyan and magenta color contrast"),
    # — cinematográfico
    ("cinematografico", "lone rider on a motorcycle crossing an empty desert highway at golden hour, long dramatic shadows, heat haze on asphalt, wide anamorphic shot, cinematic color grade with teal shadows and warm highlights, film grain, 2.39:1 composition"),
    ("cinematografico", "detective in a trench coat under a flickering streetlamp in heavy rain, 1940s noir city alley, wet cobblestones mirroring the light, volumetric fog, high contrast black and white cinematography, dramatic low-key lighting"),
    ("cinematografico", "astronaut floating outside a space station above Earth at sunrise, terminator line glowing across the planet, lens flare from the sun cresting the horizon, hard science fiction realism, IMAX quality, deep space blacks"),
    # — ilustração / editorial
    ("ilustracao editorial", "flat vector illustration of a diverse team collaborating around a giant smartphone, bold geometric shapes, limited palette of coral, navy and cream, clean negative space, editorial magazine style, subtle paper texture"),
    ("ilustracao digital", "whimsical treehouse village interconnected by rope bridges, tiny glowing lanterns, storybook illustration, soft painterly brushstrokes, warm autumn palette, cozy atmosphere, detailed foliage, children's book art style"),
    ("anime", "teenage girl standing on a rooftop overlooking a sprawling Japanese city at dusk, wind blowing her school uniform, dramatic clouds tinged with orange and purple, cel-shaded anime style, crisp lineart, lens flare, Makoto Shinkai inspired"),
    # — 3D / render
    ("3d render", "isometric cutaway of a cozy programmer's room, multiple monitors with code, plants and coffee mugs, soft global illumination, pastel color scheme, Blender 3D render, octane lighting, miniature diorama style, clean edges"),
    ("3d produto", "premium wireless headphones floating on a gradient studio backdrop, soft rim lighting, subtle reflections on matte surfaces, product visualization render, 100mm macro lens equivalent, razor sharp details, minimalist composition"),
    # — arte conceitual / paisagem
    ("concept art", "vast floating islands with waterfalls pouring into clouds below, ancient stone temples overgrown with vines, tiny airships in the distance, epic fantasy concept art, golden hour lighting, atmospheric perspective, matte painting detail"),
    ("paisagem", "Iguazu Falls thundering at sunrise, immense spray clouds catching rainbow light, lush tropical jungle framing the view, ultra wide landscape photography, long exposure silky water, vibrant greens against warm sky"),
    ("paisagem urbana", "favela hillside at blue hour, thousands of warm window lights dotting the slope, Christ the Redeemer silhouette in mist far away, drone photography perspective, rich texture of rooftops, deep blue and amber contrast"),
    # — produto / comida
    ("foto de comida", "rustic wooden board with sliced sourdough, aged cheese and figs, morning light from a kitchen window, crumbs and flour dust, shallow depth of field, food magazine styling, earthy tones, linen napkin"),
    ("foto de produto", "handcrafted ceramic mug on a concrete surface, single dramatic beam of window light, steam rising, deep shadows, product hero shot, 85mm lens, negative space on the right for text"),
    # — abstrato / textura
    ("abstrato", "macro shot of ferrofluid spikes responding to a magnetic field, glossy black liquid metal against a white background, studio lighting with colored gels, extreme detail, scientific photography aesthetic"),
    ("textura", "aerial view of glacial rivers braiding through black volcanic sand in Iceland, turquoise water veins, abstract natural pattern, drone photography, high altitude perspective, crisp detail"),
    # — vídeo: movimentos de câmera (t2v/i2v)
    ("video cinematico", "slow dolly-in on a lighthouse keeper lighting the lamp at dusk, waves crashing below, beam sweeping across fog, cinematic lighting, smooth camera motion, film grain"),
    ("video natureza", "aerial drone shot gliding forward over misty pine forest at sunrise, god rays piercing through trees, birds crossing the frame, smooth continuous motion, cinematic color grade"),
    ("video urbano", "handheld tracking shot following a cyclist through narrow Lisbon streets at golden hour, laundry lines overhead, warm light flares, motion blur on background, documentary feel"),
    ("video macro", "extreme macro timelapse of an ice cube melting on dark slate, water droplets beading and rolling, refractions shifting, studio lighting, slow elegant motion"),
    ("video personagem", "young chef tossing vegetables in a flaming wok, sparks and steam rising, slow motion, kitchen ambient light, dynamic camera orbit, cinematic food commercial style"),
    ("video abstrato", "fluid ink swirling in water, deep blue and gold pigments blooming and folding into each other, black background, ultra slow motion, hypnotic elegance"),
    ("video i2v natureza", "gentle parallax over a mountain lake at dawn, mist drifting slowly, water rippling softly, subtle cloud movement, serene continuous motion"),
    ("video i2v retrato", "portrait of an old sailor looking at the horizon, subtle wind in his hair and coat, slow blink, soft breathing motion, cinematic depth, natural skin movement"),
]

def indexar() -> dict:
    """(Re)cria a coleção prompts_midia com o corpus acima."""
    cliente = QdrantClient(url=config.QDRANT_URL, timeout=10)
    if cliente.collection_exists(COLECAO):
        cliente.delete_collection(COLECAO)
    cliente.create_collection(
        collection_name=COLECAO,
        vectors_config=VectorParams(size=1024, distance=Distance.COSINE))
    embedder = rag.embeddings()
    # formato que o LangChain lê: texto em page_content, resto em metadata
    pontos = [PointStruct(id=i, vector=embedder.embed_query(prompt),
                          payload={"page_content": prompt,
                                   "metadata": {"estilo": estilo,
                                                "source": "corpus_prompts",
                                                "categoria": COLECAO}})
              for i, (estilo, prompt) in enumerate(CORPUS)]
    cliente.upsert(COLECAO, points=pontos)
    return {"colecao": COLECAO, "pontos": len(pontos)}


if __name__ == "__main__":
    print(indexar())
