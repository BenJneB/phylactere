"""Signalement d'une bulle mal lue ou mal traduite (jalon 11).

Alt+clic sur une bulle dans l'extension → ``POST /report``. Chaque signalement donne deux
fichiers dans le dossier des signalements (``config.reports_dir()``, hors du dépôt : on ne
versionne jamais d'image) : ``<id>.json`` (image, épisode, lignes OCR, bulle, traduction,
modèle, version du prompt) et ``<id>.png`` (découpe de la bulle). ``scripts/reports.py`` en
fait un rapport HTML. But : un corpus de vrais échecs pour régler le prompt et le
regroupement.
"""

import json
import secrets
import time
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from phylactere.schemas import Box, Bubble, OcrResult

# Marge autour de la bulle dans la découpe (px de l'image d'origine) : un peu de dessin
# autour aide à juger le regroupement et la lecture.
CROP_MARGIN = 24


class ReportRequest(BaseModel):
    image_url: str
    series_id: str = Field(min_length=1, max_length=64)
    episode_id: str = Field(min_length=1, max_length=64)
    index: int = Field(ge=0)
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Mêmes voisines que pour /translate : elles entrent dans la clé du cache OCR, et la
    # bulle peut déborder sur l'image suivante.
    prev_image_url: str | None = None
    next_image_url: str | None = None
    # Bulle telle que l'extension l'affiche.
    box: Box
    original: str = Field(max_length=5000)
    translated: str = Field(max_length=5000)


class ReportResponse(BaseModel):
    id: str
    # True si la bulle a été retrouvée dans le résultat OCR (lignes enregistrées).
    matched: bool


def find_bubble(ocr: OcrResult, box: Box) -> Bubble | None:
    """Bulle de l'OCR qui correspond à la boîte affichée : même boîte, sinon le plus grand
    recouvrement (résultat OCR recalculé depuis l'affichage)."""
    for bubble in ocr.bubbles:
        if bubble.box == box:
            return bubble
    best = max(ocr.bubbles, key=lambda b: b.box.intersection_area(box), default=None)
    return best if best is not None and best.box.intersection_area(box) > 0 else None


def crop_bubble(
    image: Image.Image, following: Image.Image | None, box: Box, margin: int = CROP_MARGIN
) -> Image.Image:
    """Découpe de la bulle avec une marge ; une bulle coupée entre deux images déborde en
    bas : on prolonge alors l'image par le haut de la suivante (même largeur)."""
    image = image.convert("RGB")
    width, height = image.size
    bottom = box.bottom + margin
    if bottom > height and following is not None and following.width == width:
        extra = min(bottom - height, following.height)
        extended = Image.new("RGB", (width, height + extra))
        extended.paste(image, (0, 0))
        extended.paste(following.convert("RGB").crop((0, 0, width, extra)), (0, height))
        image = extended
    left, top = max(0, box.x - margin), max(0, box.y - margin)
    right, bottom = min(image.width, box.right + margin), min(image.height, bottom)
    if right <= left or bottom <= top:  # boîte hors de l'image : on garde l'image entière
        return image
    return image.crop((left, top, right, bottom))


def write_report(
    directory: Path,
    request: ReportRequest,
    lang: str,
    model: str,
    prompt: dict[str, str],
    bubble: Bubble | None,
    crop: Image.Image,
) -> str:
    """Écrit ``<id>.json`` et ``<id>.png`` ; renvoie l'identifiant du signalement."""
    directory.mkdir(parents=True, exist_ok=True)
    # Horodatage d'abord : les fichiers se trient dans l'ordre des signalements.
    report_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    crop.save(directory / f"{report_id}.png", format="PNG")
    data = {
        "id": report_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "image_url": request.image_url,
        "prev_image_url": request.prev_image_url,
        "next_image_url": request.next_image_url,
        "series_id": request.series_id,
        "episode_id": request.episode_id,
        "index": request.index,
        "target_lang": lang,
        "model": model,
        **prompt,
        "bubble": {
            "box": request.box.model_dump(),
            "original": request.original,
            "translated": request.translated,
            "edges": bubble.edges if bubble else [],
            "bg": bubble.bg if bubble else None,
            "fg": bubble.fg if bubble else None,
        },
        # Lignes OCR de la bulle (texte, boîte, confiance), vides si elle n'a pas été
        # retrouvée dans le résultat OCR.
        "lines": [line.model_dump() for line in bubble.lines] if bubble else [],
        "crop": f"{report_id}.png",
    }
    (directory / f"{report_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report_id
