"""Signalement d'une bulle mal lue ou mal traduite (jalon 11).

Alt+clic sur une bulle dans l'extension → ``POST /report``. Chaque signalement donne deux
fichiers dans le dossier des signalements (``config.reports_dir()``, hors du dépôt : on ne
versionne jamais d'image) : ``<id>.json`` (image, épisode, lignes OCR, bulle, traduction,
modèle, version du prompt) et ``<id>.png`` (découpe de la bulle). ``scripts/reports.py`` en
fait un rapport HTML. But : un corpus de vrais échecs pour régler le prompt et le
regroupement.
"""

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, Field

from phylactere.ocr import stack_images
from phylactere.schemas import Box, Bubble, ImageRequest, OcrResult, match_box

# Marge autour de la bulle dans la découpe (px de l'image d'origine) : un peu de dessin
# autour aide à juger le regroupement et la lecture.
CROP_MARGIN = 24
# Identifiant d'un signalement (voir write_report) : contrôlé avant toute suppression, pour
# ne jamais toucher un fichier hors du dossier des signalements.
REPORT_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class ReportRequest(ImageRequest):
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
    index = match_box([bubble.box for bubble in ocr.bubbles], box)
    return None if index is None else ocr.bubbles[index]


def crop_bubble(
    image: Image.Image, following: Image.Image | None, box: Box, margin: int = CROP_MARGIN
) -> Image.Image:
    """Découpe de la bulle avec une marge ; une bulle coupée entre deux images déborde en
    bas : on prolonge alors l'image par le haut de la suivante (même largeur). En RGB."""
    image = image.convert("RGB")
    width, height = image.size
    bottom = box.bottom + margin
    if bottom > height and following is not None and following.width == width:
        extra = min(bottom - height, following.height)
        image = stack_images(image, following.crop((0, 0, width, extra)))
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
    now = datetime.now().astimezone()
    report_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    png = directory / f"{report_id}.png"
    crop.save(png, format="PNG")
    data = {
        "id": report_id,
        # isoformat plutôt que strftime("%z") : sous Windows, %z peut donner le nom
        # localisé du fuseau au lieu du décalage.
        "created": now.isoformat(timespec="seconds"),
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
    try:
        (directory / f"{report_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except BaseException:
        # Sans JSON, la découpe n'apparaîtrait dans aucun rapport et ne pourrait pas être
        # annulée.
        png.unlink(missing_ok=True)
        raise
    return report_id


def delete_report(directory: Path, report_id: str) -> bool:
    """Supprime ``<id>.json`` et ``<id>.png`` ; False si l'identifiant est invalide ou si
    le signalement n'existe pas."""
    if not REPORT_ID.fullmatch(report_id):
        return False
    found = False
    for suffix in (".json", ".png"):
        path = directory / f"{report_id}{suffix}"
        if path.is_file():
            path.unlink()
            found = True
    return found
