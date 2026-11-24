"""Couleur de fond et de texte des bulles, prises sur l'image (voir docs/SPEC.md, jalon 9).

L'extension recouvre le texte d'origine par un rectangle. Blanc par défaut, il masque le
dessin quand le texte est hors bulle (narration sur fond coloré) : on lui donne la couleur
du fond autour du texte quand ce fond est uni. Pas d'inpainting : un fond dessiné ou en
dégradé donne ``None``, et l'extension garde le blanc.
"""

import numpy as np

from phylactere.schemas import Box, Bubble

# Marge (px) retirée autour des boîtes des lignes : l'anticrénelage du texte en déborde.
LINE_PAD = 2


def _clip(box: Box, pad: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Boîte élargie de ``pad`` px et rognée à l'image : (gauche, haut, droite, bas)."""
    return (
        max(0, box.x - pad),
        max(0, box.y - pad),
        min(width, box.right + pad),
        min(height, box.bottom + pad),
    )


def background_color(
    rgb: np.ndarray, bubble: Bubble, text_boxes: list[Box], margin: int, min_share: float
) -> tuple[int, int, int] | None:
    """Couleur de fond dominante autour du texte de la bulle, ou None si elle n'est pas unie.

    Pixels pris dans la boîte de la bulle élargie de ``margin`` px, hors boîtes des lignes
    OCR (``text_boxes`` : celles de toutes les bulles, un texte voisin peut empiéter).
    Couleurs quantifiées sur 4 bits par canal : la classe la plus fréquente doit couvrir au
    moins ``min_share`` des pixels ; la couleur rendue est la moyenne de ses pixels.
    """
    height, width = rgb.shape[:2]
    left, top, right, bottom = _clip(bubble.box, margin, width, height)
    if right <= left or bottom <= top:
        return None
    region = rgb[top:bottom, left:right]
    mask = np.ones(region.shape[:2], dtype=bool)
    for box in text_boxes:
        x1, y1, x2, y2 = _clip(box, LINE_PAD, width, height)
        x1, x2 = max(x1, left) - left, min(x2, right) - left
        y1, y2 = max(y1, top) - top, min(y2, bottom) - top
        if x2 > x1 and y2 > y1:
            mask[y1:y2, x1:x2] = False
    pixels = region[mask].astype(np.int32)
    if len(pixels) == 0:
        return None

    quantized = pixels >> 4
    classes = (quantized[:, 0] << 8) | (quantized[:, 1] << 4) | quantized[:, 2]
    counts = np.bincount(classes, minlength=4096)
    dominant = int(counts.argmax())
    if counts[dominant] < min_share * len(pixels):
        return None
    mean = pixels[classes == dominant].mean(axis=0)
    return tuple(int(round(v)) for v in mean)


def relative_luminance(color: tuple[int, int, int]) -> float:
    """Luminance relative WCAG 2 (0 = noir, 1 = blanc)."""

    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_color(background: tuple[int, int, int]) -> tuple[int, int, int]:
    """Noir ou blanc, celui qui contraste le plus avec le fond (rapport de contraste WCAG)."""
    lum = relative_luminance(background)
    on_black = (lum + 0.05) / 0.05
    on_white = 1.05 / (lum + 0.05)
    return (0, 0, 0) if on_black >= on_white else (255, 255, 255)


def to_hex(color: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color)


def color_bubbles(
    rgb: np.ndarray, bubbles: list[Bubble], margin: int, min_share: float
) -> list[Bubble]:
    """Renseigne ``bg`` et ``fg`` de chaque bulle (None si le fond n'est pas uni)."""
    text_boxes = [line.box for bubble in bubbles for line in bubble.lines]
    for bubble in bubbles:
        bg = background_color(rgb, bubble, text_boxes, margin, min_share)
        bubble.bg = None if bg is None else to_hex(bg)
        bubble.fg = None if bg is None else to_hex(text_color(bg))
    return bubbles
