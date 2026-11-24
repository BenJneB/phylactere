"""Export d'un épisode traduit en PDF (jalon 16, voir docs/SPEC.md, section 9).

Usage strictement personnel : relire hors ligne un épisode déjà lu avec Phylactère. Rien
n'est calculé : les traductions viennent du cache (corrections comprises), les images sont
retéléchargées (même allowlist que le serveur). Une image sans traduction en cache est
exportée sans bulles.

- Les URL des images sont lues dans la page de l'épisode, comme le fait l'extension : le
  cache ne garde que leur empreinte, et une image jamais traduite doit figurer aussi.
- Une page par image, aux dimensions de l'image (1 px = 1 pt). Le JPEG d'origine est
  embarqué tel quel par fpdf2, sans recompression (vérifié : ses octets sont dans le PDF).
- Bulles comme dans l'extension (content.js) : rectangle arrondi de la couleur ``bg``,
  marge PAD autour du texte détecté, puis la traduction en vrai texte (net à tous les
  zooms, sélectionnable, recherchable) de couleur ``fg``, en DejaVu Sans Bold, à la plus
  grande taille qui tient (dichotomie, comme ``fitText``).
- Une bulle à cheval sur deux images a son fond sur les deux pages, et son texte sur la
  page qui en porte la plus grande part, dans la partie visible de la bulle.
"""

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import httpx
from fpdf import FPDF
from PIL import Image

from phylactere.cache import TranslatedBlock, TranslationResult, read_translations
from phylactere.config import FetchConfig, data_dir
from phylactere.fetch import FetchError, fetch_bytes
from phylactere.naver import USER_AGENT, episode_image_urls, episode_page_url
from phylactere.translate import PROMPT_ID

FONT_PATH = Path(__file__).resolve().parent / "fonts" / "DejaVuSans-Bold.ttf"
FONT = "DejaVuSans"
# Mêmes valeurs que content.js et content.css.
PAD = 6  # marge autour du texte détecté (px de l'image)
PADDING_X, PADDING_Y = 4, 2  # padding de .phy-bubble
LINE_HEIGHT = 1.12
MIN_FONT, MAX_FONT, START_MIN_FONT = 9, 22, 12
CORNER_RADIUS = 14
DEFAULT_BG, DEFAULT_FG = "#ffffff", "#000000"
# Téléchargements d'images simultanés.
DOWNLOADS = 4
PAGE_TIMEOUT_S = 15


class ExportError(Exception):
    """Export impossible (page de l'épisode illisible, aucune traduction en cache…)."""


def exports_dir() -> Path:
    return data_dir() / "exports"


# --- Mise en page ---


def hex_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


@dataclass
class Area:
    x: float
    y: float
    w: float
    h: float

    def grow_to(self, other: "Area") -> None:
        """Agrandit la zone pour qu'elle contienne aussi ``other``."""
        right, bottom = (
            max(self.x + self.w, other.x + other.w),
            max(self.y + self.h, other.y + other.h),
        )
        self.x, self.y = min(self.x, other.x), min(self.y, other.y)
        self.w, self.h = right - self.x, bottom - self.y


@dataclass
class Text:
    """Texte d'une bulle sur une page : centré dans ``area``. ``background`` est le fond de
    la bulle sur la même page, agrandi si le texte n'y tient pas (comme la bulle de
    l'extension, qui grandit avec son texte)."""

    area: Area
    text: str
    color: str
    background: Area


@dataclass
class PageImage:
    data: bytes
    width: int
    height: int
    translation: TranslationResult | None = None
    # Dessins de la page (remplis par ``layout``) : fonds (zone, couleur) et textes.
    backgrounds: list[tuple[Area, str]] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)


def bubble_area(block: TranslatedBlock, image_w: int, scale: float) -> Area:
    """Zone de la bulle en pixels de l'image, comme ``render`` dans content.js ; elle peut
    déborder en bas sur l'image suivante."""
    box = block.box
    x = max(0.0, (box.x - PAD) * scale)
    y = max(0.0, (box.y - PAD) * scale)
    w = min(image_w - x, (box.w + PAD * 2) * scale)
    h = (box.h + PAD * 2) * scale
    return Area(x, y, w, h)


def layout(pages: list[PageImage]) -> None:
    """Répartit fonds et textes des bulles sur les pages (bulle à cheval sur deux images :
    fond sur les deux, texte sur celle qui en porte la plus grande part, dans la partie
    visible)."""
    for page in pages:
        page.backgrounds, page.texts = [], []
    for i, page in enumerate(pages):
        if page.translation is None:
            continue
        scale = page.width / page.translation.image_size.w
        following = pages[i + 1] if i + 1 < len(pages) else None
        for block in page.translation.blocks:
            area = bubble_area(block, page.width, scale)
            bg = block.bg or DEFAULT_BG
            fg = (block.fg or DEFAULT_FG) if block.bg else DEFAULT_FG
            page.backgrounds.append((area, bg))
            here = min(area.h, page.height - area.y)  # part visible sur cette page
            overflow = area.h - here
            if overflow > 0 and following is not None:
                below = Area(area.x, area.y - page.height, area.w, area.h)
                following.backgrounds.append((below, bg))
                if overflow > here:
                    visible = Area(area.x, 0, area.w, overflow)
                    following.texts.append(Text(visible, block.translated, fg, below))
                    continue
            visible = Area(area.x, area.y, area.w, here)
            page.texts.append(Text(visible, block.translated, fg, area))


def text_width(area: Area, size: float) -> float:
    """Largeur des lignes : celle de la bulle, au moins un caractère (sinon fpdf2 lève
    FPDFException pour une bulle très étroite, une onomatopée verticale par exemple)."""
    return max(area.w - 2 * PADDING_X, size * 1.2)


def text_lines(pdf: FPDF, text: str, width: float, size: float) -> list[str]:
    pdf.set_font(FONT, size=size)
    return pdf.multi_cell(width, size * LINE_HEIGHT, text, align="C", dry_run=True, output="LINES")


def fit_font(pdf: FPDF, text: str, area: Area) -> tuple[int, list[str]]:
    """Plus grande taille (entre MIN_FONT et la taille de départ) pour laquelle le texte
    tient dans la zone, par dichotomie, comme ``fitText`` dans content.js."""
    target = area.h * 1.1

    def fits(size: int) -> tuple[bool, list[str]]:
        lines = text_lines(pdf, text, text_width(area, size), size)
        return len(lines) * size * LINE_HEIGHT + 2 * PADDING_Y <= target, lines

    hi = round(min(MAX_FONT, max(START_MIN_FONT, target * 0.3)))
    ok, lines = fits(hi)
    if ok:
        return hi, lines
    lo, lo_lines = MIN_FONT, None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        ok, lines = fits(mid)
        if ok:
            lo, lo_lines = mid, lines
        else:
            hi = mid
    return lo, lo_lines if lo_lines is not None else fits(lo)[1]


def place_text(pdf: FPDF, item: Text) -> tuple[int, Area]:
    """Taille de police et zone du texte (centrée sur la bulle) ; le fond de la bulle est
    agrandi si le texte le dépasse, même à la plus petite taille."""
    size, lines = fit_font(pdf, item.text, item.area)
    width = text_width(item.area, size)
    height = len(lines) * size * LINE_HEIGHT
    center_x, center_y = item.area.x + item.area.w / 2, item.area.y + item.area.h / 2
    box = Area(center_x - width / 2, center_y - height / 2, width, height)
    padded = Area(
        box.x - PADDING_X, box.y - PADDING_Y, box.w + 2 * PADDING_X, box.h + 2 * PADDING_Y
    )
    item.background.grow_to(padded)
    return size, box


def build_pdf(pages: list[PageImage], title: str = "") -> bytes:
    pdf = FPDF(unit="pt")
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.c_margin = 0
    pdf.add_font(FONT, fname=str(FONT_PATH))
    pdf.set_title(title)
    pdf.set_creator("Phylactère")
    layout(pages)
    for page in pages:
        pdf.add_page(format=(page.width, page.height))
        # Textes placés d'abord : ils peuvent agrandir les fonds, dessinés dessous.
        placed = [(item, *place_text(pdf, item)) for item in page.texts]
        pdf.image(BytesIO(page.data), x=0, y=0, w=page.width, h=page.height)
        for area, color in page.backgrounds:
            pdf.set_fill_color(*hex_rgb(color))
            radius = min(CORNER_RADIUS, area.w / 2, area.h / 2)
            pdf.rect(
                area.x, area.y, area.w, area.h, style="F", round_corners=True, corner_radius=radius
            )
        for item, size, box in placed:
            pdf.set_font(FONT, size=size)
            pdf.set_text_color(*hex_rgb(item.color))
            pdf.set_xy(box.x, box.y)
            pdf.multi_cell(box.w, size * LINE_HEIGHT, item.text, align="C")
    return bytes(pdf.output())


# --- Épisode ---


@dataclass
class ExportReport:
    path: Path
    images: int
    bubbles: int
    models: set[str]
    missing: list[int]  # index des images exportées sans bulles

    @property
    def translated(self) -> int:
        return self.images - len(self.missing)


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    """Page de l'épisode, sans cookies (comme le préchargement de l'extension)."""
    try:
        response = await client.get(url, timeout=PAGE_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise ExportError(f"Page de l'épisode injoignable : {exc}") from exc
    if response.status_code != 200:
        raise ExportError(f"Page de l'épisode : Naver a répondu {response.status_code}.")
    return response.text


async def download_all(client: httpx.AsyncClient, urls: list[str], cfg: FetchConfig) -> list[bytes]:
    semaphore = asyncio.Semaphore(DOWNLOADS)

    async def one(url: str) -> bytes:
        async with semaphore:
            return await fetch_bytes(client, url, cfg)

    # return_exceptions : toutes les tâches sont finies avant la fermeture du client, même
    # quand l'une échoue.
    results = await asyncio.gather(*(one(url) for url in urls), return_exceptions=True)
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, FetchError):
            raise ExportError(f"Image impossible à télécharger ({url}) : {result}") from result
        if isinstance(result, BaseException):
            raise result
    return results


async def export_episode(
    series_id: str,
    episode_id: str,
    target_lang: str,
    model: str,
    cache_path: Path,
    fetch_cfg: FetchConfig,
    out: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ExportReport:
    """Écrit le PDF de l'épisode et renvoie ce qui a été exporté. ``transport`` : réseau
    simulé (tests)."""
    page_url = episode_page_url(series_id, episode_id)
    async with httpx.AsyncClient(transport=transport, headers={"User-Agent": USER_AGENT}) as client:
        html = await fetch_page(client, page_url)
        urls = episode_image_urls(html, page_url, series_id, episode_id)
        if not urls:
            raise ExportError(f"Aucune image d'épisode trouvée dans {page_url}.")
        translations = await asyncio.to_thread(
            read_translations, cache_path, urls, target_lang, model, PROMPT_ID
        )
        if not translations:
            raise ExportError(
                f"Aucune traduction en cache ({target_lang}) pour l'épisode {series_id}/"
                f"{episode_id} : lisez-le d'abord avec Phylactère."
            )
        images = await download_all(client, urls, fetch_cfg)

    pages = []
    for url, data in zip(urls, images, strict=True):
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
        except OSError as exc:
            raise ExportError(f"Image illisible : {url}") from exc
        found = translations.get(url)
        pages.append(PageImage(data, width, height, found[1] if found else None))

    title = f"Épisode {episode_id} ({series_id}) · {target_lang}"
    pdf = await asyncio.to_thread(build_pdf, pages, title)
    path = out or exports_dir() / f"{series_id}-{episode_id}-{target_lang}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf)
    return ExportReport(
        path=path,
        images=len(urls),
        bubbles=sum(len(p.translation.blocks) for p in pages if p.translation),
        models={found[0] for found in translations.values()},
        missing=[i for i, url in enumerate(urls) if url not in translations],
    )
