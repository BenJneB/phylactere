"""Export d'un épisode traduit en PDF (jalon 16) : page Naver, images et cache simulés,
PDF relu avec pypdf."""

import asyncio
import sqlite3
from io import BytesIO

import httpx
import pytest
from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader

from phylactere.cache import Cache
from phylactere.config import load_settings
from phylactere.export import (
    FONT,
    FONT_PATH,
    MIN_FONT,
    Area,
    ExportError,
    PageImage,
    Text,
    build_pdf,
    export_episode,
    exports_dir,
    fit_font,
    layout,
)
from phylactere.naver import episode_image_urls
from phylactere.pipeline import Pipeline
from phylactere.schemas import Box, Correction, ImageSize, TranslatedBlock, TranslationResult
from phylactere.translate import PROMPT_ID

SERIES, EPISODE = "729047", "66"
BASE = f"https://image-comic.pstatic.net/webtoon/{SERIES}/{EPISODE}/"
PAGE = f"https://comic.naver.com/webtoon/detail?titleId={SERIES}&no={EPISODE}"
W, H = 690, 1000


def image_url(index: int) -> str:
    return f"{BASE}img_{index}.jpg"


def jpeg(color=(200, 220, 240)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (W, H), color).save(out, format="JPEG")
    return out.getvalue()


def page_html(count: int) -> str:
    images = "".join(f'<img id="content_image_{i}" src="{image_url(i)}">' for i in range(count))
    return f"""<html><body>
<img src="{BASE}thumbnail_202x120_abc.jpg">
<div id="sectionContWide">{images}</div></body></html>"""


class Network:
    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.images = {image_url(i): jpeg((40 * i, 200, 240)) for i in range(count)}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "comic.naver.com":
            return httpx.Response(200, text=page_html(self.count))
        data = self.images.get(str(request.url))
        return httpx.Response(200, content=data) if data else httpx.Response(404)


def block(text: str, y: int = 100, h: int = 40, **extra) -> TranslatedBlock:
    return TranslatedBlock(
        box=Box(x=100, y=y, w=300, h=h), original=f"원문 {text}", translated=text, **extra
    )


def store(
    path,
    translations: dict[int, list[TranslatedBlock]],
    model="m",
    corrections=(),
    with_urls=False,
    image_count=None,
):
    """Écrit des traductions (index → bulles) et des corrections dans un cache.
    ``with_urls`` / ``image_count`` : comme le serveur depuis le jalon 17 (sinon, cache
    d'avant : l'export relit la page de Naver)."""

    async def write():
        cache = Cache(path)
        for index, blocks in translations.items():
            url = image_url(index)
            result = TranslationResult(blocks=blocks, image_size=ImageSize(w=W, h=H))
            key = Pipeline._translation_key(url, "fr", model)
            await cache.put_translation(
                key,
                SERIES,
                EPISODE,
                index,
                "fr",
                model,
                result,
                image_url=url if with_urls else None,
            )
        if image_count:
            await cache.put_image_count(SERIES, EPISODE, image_count)
        for index, correction in corrections:
            await cache.put_correction(image_url(index), SERIES, EPISODE, index, "fr", correction)
        cache.close()

    asyncio.run(write())


def export(path, network, model="m", **extra):
    return asyncio.run(
        export_episode(
            SERIES,
            EPISODE,
            "fr",
            model,
            path,
            load_settings().fetch,
            transport=httpx.MockTransport(network.handler),
            **extra,
        )
    )


def page_texts(path) -> list[str]:
    return [page.extract_text() for page in PdfReader(path).pages]


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache.sqlite3"


def test_one_page_per_image_with_its_bubbles(cache_path):
    store(cache_path, {0: [block("Bonjour !"), block("Ça va… ’ok’", y=500)], 1: [], 2: []})
    network = Network()

    report = export(cache_path, network)

    assert report.path == exports_dir() / f"{SERIES}-{EPISODE}-fr.pdf"
    reader = PdfReader(report.path)
    assert len(reader.pages) == 3
    assert [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages] == [
        (W, H)
    ] * 3
    texts = page_texts(report.path)
    # Vrai texte, hors latin-1 compris (DejaVu Sans) : recherchable.
    assert "Bonjour !" in texts[0] and "Ça va… ’ok’" in texts[0]
    assert texts[1] == texts[2] == ""
    assert (report.images, report.translated, report.bubbles) == (3, 3, 2)
    assert report.missing == []
    assert report.models == {"m"}
    # La vignette (même dossier) n'est ni une page ni téléchargée.
    assert not any("thumbnail" in str(r.url) for r in network.requests)
    # Page Naver demandée sans cookies, images avec le Referer de Naver.
    assert all("cookie" not in r.headers for r in network.requests)
    assert network.requests[1].headers["referer"] == "https://comic.naver.com/"


def test_jpeg_embedded_without_recompression(cache_path):
    store(cache_path, {0: [block("Salut")]})
    network = Network(count=1)

    report = export(cache_path, network)

    assert network.images[image_url(0)] in report.path.read_bytes()


def test_image_without_translation_is_exported_without_bubbles(cache_path):
    store(cache_path, {0: [block("Un")], 2: [block("Trois")]})

    report = export(cache_path, Network())

    assert report.missing == [1]
    assert len(PdfReader(report.path).pages) == 3
    assert page_texts(report.path)[1] == ""
    assert "Trois" in page_texts(report.path)[2]


def test_no_translation_at_all_is_an_error(cache_path):
    with pytest.raises(ExportError, match="Aucune traduction en cache"):
        export(cache_path, Network())


def test_correction_wins_over_translation(cache_path):
    original = block("Traduction du modèle")
    correction = Correction(box=original.box, original=original.original, text="Corrigée")
    store(cache_path, {0: [original]}, corrections=[(0, correction)])

    report = export(cache_path, Network(count=1))

    text = page_texts(report.path)[0]
    assert "Corrigée" in text
    assert "modèle" not in text


def test_requested_model_preferred_then_another(cache_path):
    store(cache_path, {0: [block("Petit")], 1: [block("Petit aussi")]}, model="petit")
    store(cache_path, {0: [block("Gros")]}, model="gros")

    report = export(cache_path, Network(count=2), model="gros")

    texts = page_texts(report.path)
    assert "Gros" in texts[0] and "Petit" not in texts[0]
    assert "Petit aussi" in texts[1]  # pas de traduction de « gros » : celle de « petit »
    assert report.models == {"gros", "petit"}


def test_bubble_across_two_images(cache_path):
    # Bulle dont la plus grande part est sur l'image suivante : fond sur les deux pages,
    # texte sur la seconde ; l'autre, surtout sur la première, y garde son texte.
    store(
        cache_path,
        {0: [block("Surtout dessous", y=980, h=60), block("Surtout dessus", y=930, h=60)], 1: []},
    )

    report = export(cache_path, Network(count=2))

    texts = page_texts(report.path)
    assert "Surtout dessous" in texts[1] and "Surtout dessous" not in texts[0]
    assert "Surtout dessus" in texts[0] and "Surtout dessus" not in texts[1]


def test_layout_draws_background_on_both_pages():
    result = TranslationResult(
        blocks=[block("Coupée", y=980, h=60, bg="#102030", fg="#ffffff")],
        image_size=ImageSize(w=W, h=H),
    )
    pages = [PageImage(b"", W, H, result), PageImage(b"", W, H)]

    layout(pages)

    # Zone de la bulle : boîte élargie de PAD = 6 px (974 à 1046).
    assert pages[0].backgrounds == [(Area(94, 974, 312, 72), "#102030")]
    assert pages[1].backgrounds == [(Area(94, -26, 312, 72), "#102030")]
    assert pages[0].texts == []
    assert pages[1].texts == [
        Text(Area(94, 0, 312, 46), "Coupée", "#ffffff", Area(94, -26, 312, 72))
    ]

    # Mise en page refaite : rien en double.
    layout(pages)
    assert len(pages[0].backgrounds) == len(pages[1].backgrounds) == len(pages[1].texts) == 1


def test_text_color_defaults_to_black_on_white():
    result = TranslationResult(
        blocks=[block("Sans couleur"), block("Sans texte", fg=None, bg="#000000")],
        image_size=ImageSize(w=W, h=H),
    )
    pages = [PageImage(b"", W, H, result)]

    layout(pages)

    assert [bg for _, bg in pages[0].backgrounds] == ["#ffffff", "#000000"]
    assert [text.color for text in pages[0].texts] == ["#000000", "#000000"]


def test_long_text_gets_a_smaller_font():
    pdf = FPDF(unit="pt")
    pdf.add_font(FONT, fname=str(FONT_PATH))
    pdf.add_page()
    area = Area(0, 0, 200, 60)

    short, _ = fit_font(pdf, "Oui", area)
    long, lines = fit_font(pdf, "Une réplique beaucoup trop longue pour cette bulle " * 3, area)

    assert short > long >= MIN_FONT
    assert len(lines) > 1


def test_episode_image_urls_like_the_extension():
    other = "https://image-comic.pstatic.net/webtoon/729047/65/img_0.jpg"
    html = f"""
<img src="{BASE}thumbnail_202x120_x.jpg">
<img src="{image_url(0)}"><img src="{image_url(0)}">
<img src="data:image/gif;base64,R0lG" data-src="{image_url(1)}">
<img src="{other}"><img src="http://image-comic.pstatic.net/webtoon/729047/66/img_9.jpg">
<img src="https://evil.example/webtoon/729047/66/img_8.jpg">
"""

    assert episode_image_urls(html, PAGE, SERIES, EPISODE) == [image_url(0), image_url(1)]


def test_text_on_last_image_stays_in_its_visible_part():
    result = TranslationResult(blocks=[block("Bas", y=980, h=60)], image_size=ImageSize(w=W, h=H))
    pages = [PageImage(b"", W, H, result)]

    layout(pages)

    assert pages[0].texts[0].area == Area(94, 974, 312, 26)


def test_narrow_tall_bubble_does_not_break_the_export(cache_path):
    # Onomatopée verticale : boîte de 15 px de large. Plus large qu'un caractère, le
    # texte déborde de la bulle, dont le fond grandit avec lui.
    narrow = TranslatedBlock(
        box=Box(x=300, y=200, w=3, h=100), original="쾅", translated="BOUM… Mmm"
    )
    store(cache_path, {0: [narrow]})

    report = export(cache_path, Network(count=1))

    # Une lettre par ligne, comme l'onomatopée d'origine.
    assert "".join(page_texts(report.path)[0].split()) == "BOUM…Mmm"


def test_background_grows_when_text_does_not_fit():
    tiny = TranslatedBlock(
        box=Box(x=300, y=200, w=48, h=8),
        original="응",
        translated="Une réplique bien trop longue pour cette toute petite bulle",
    )
    pages = [
        PageImage(jpeg(), W, H, TranslationResult(blocks=[tiny], image_size=ImageSize(w=W, h=H)))
    ]

    build_pdf(pages)

    background = pages[0].backgrounds[0][0]
    assert background.h > 8 + 12  # agrandi au-delà de la boîte et de sa marge
    assert background.y < 194  # centré : il grandit aussi vers le haut


def test_current_prompt_preferred_over_requested_model(cache_path):
    store(cache_path, {0: [block("Ancien prompt")]}, model="gros")
    store(cache_path, {0: [block("Prompt actuel")]}, model="petit")

    # Traduction de « gros » faite avec une version précédente du prompt.
    db = sqlite3.connect(cache_path)
    db.execute(
        "UPDATE translation SET key = replace(key, ?, 'ancien') WHERE model = 'gros'",
        (PROMPT_ID,),
    )
    db.commit()
    db.close()

    report = export(cache_path, Network(count=1), model="gros")

    assert "Prompt actuel" in page_texts(report.path)[0]
    assert report.models == {"petit"}


def test_unreadable_cache_row_means_image_without_bubbles(cache_path):
    store(cache_path, {0: [block("Lisible")], 1: [block("Abîmée")]})
    db = sqlite3.connect(cache_path)
    db.execute("UPDATE translation SET result = '{' WHERE image_index = 1")
    db.commit()
    db.close()

    report = export(cache_path, Network(count=2))

    assert report.missing == [1]
    assert "Lisible" in page_texts(report.path)[0]


def test_failed_download_is_an_export_error(cache_path):
    store(cache_path, {0: [block("Un")]})
    network = Network()
    del network.images[image_url(2)]

    with pytest.raises(ExportError, match="img_2"):
        export(cache_path, network)


# --- Jalon 17 : URL et nombre d'images pris dans le cache ---


def naver_pages(network) -> list[str]:
    return [str(r.url) for r in network.requests if r.url.host == "comic.naver.com"]


def test_export_from_cache_without_reading_naver(cache_path):
    store(cache_path, {0: [block("Un")], 1: [], 2: [block("Trois")]}, with_urls=True, image_count=3)
    network = Network()

    report = export(cache_path, network)

    assert naver_pages(network) == []
    assert report.page_read is False
    assert (report.images, report.missing) == (3, [])
    assert "Trois" in page_texts(report.path)[2]


def test_page_read_when_an_image_was_never_translated(cache_path):
    # Image 1 jamais traduite : son URL n'est pas dans le cache.
    store(cache_path, {0: [block("Un")], 2: [block("Trois")]}, with_urls=True, image_count=3)
    network = Network()

    report = export(cache_path, network)

    assert naver_pages(network) == [PAGE]
    assert report.page_read is True
    assert report.missing == [1]
    assert "Trois" in page_texts(report.path)[2]
