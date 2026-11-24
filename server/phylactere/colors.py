"""Couleur de fond et de texte des bulles, prises sur l'image (voir docs/SPEC.md, jalon 9).

L'extension recouvre le texte d'origine par un rectangle. Blanc par défaut, il masque le
dessin quand le texte est hors bulle (narration sur fond coloré) : on lui donne la couleur
du fond autour du texte quand ce fond est uni. Pas d'inpainting : un fond dessiné ou en
dégradé donne ``None``, et l'extension garde le blanc.

Un cartouche qui épouse le texte (texte blanc sur un bandeau noir à peine plus grand que
lui, par exemple un nom de lieu) a une marge surtout prise hors du cartouche : on regarde
donc aussi le fond entre les lettres, et une couleur qui y domine nettement l'emporte.
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
    pixels = region[mask]
    if len(pixels) == 0:
        return None
    color, share = _dominant(pixels)
    return color if share >= min_share else None


def _dominant(pixels: np.ndarray) -> tuple[tuple[int, int, int], float]:
    """Classe de couleur la plus fréquente (4 bits par canal) : moyenne de ses pixels et
    part des pixels qu'elle couvre."""
    quantized = pixels.astype(np.int32) >> 4
    classes = (quantized[:, 0] << 8) | (quantized[:, 1] << 4) | quantized[:, 2]
    counts = np.bincount(classes, minlength=4096)
    dominant = int(counts.argmax())
    mean = pixels[classes == dominant].astype(np.float64).mean(axis=0)
    return tuple(int(round(v)) for v in mean), counts[dominant] / len(pixels)


def text_area_color(
    rgb: np.ndarray, bubble: Bubble, min_share: float
) -> tuple[int, int, int] | None:
    """Couleur qui domine à l'intérieur des boîtes des lignes de la bulle (le fond entre
    les lettres), si elle couvre au moins ``min_share`` des pixels ; sinon None.

    Mesuré sur un épisode réel (124 bulles) : le texte et son anticrénelage couvrent 35 à
    58 % de la boîte d'une ligne. Un seuil de 0,7 ne retient donc que les fonds unis
    nettement majoritaires, pas la couleur d'un texte épais.
    """
    height, width = rgb.shape[:2]
    parts = []
    for line in bubble.lines:
        x1, y1, x2, y2 = _clip(line.box, 0, width, height)
        if x2 > x1 and y2 > y1:
            parts.append(rgb[y1:y2, x1:x2].reshape(-1, 3))
    if not parts:
        return None
    color, share = _dominant(np.concatenate(parts))
    return color if share >= min_share else None


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
    rgb: np.ndarray,
    bubbles: list[Bubble],
    margin: int,
    min_share: float,
    text_share: float = 1.0,
) -> list[Bubble]:
    """Renseigne ``bg`` et ``fg`` de chaque bulle (None si le fond n'est pas uni).

    Le fond entre les lettres l'emporte s'il est uni sur au moins ``text_share`` de la
    boîte des lignes (cartouche ajusté au texte) ; sinon, la marge autour du texte.
    """
    text_boxes = [line.box for bubble in bubbles for line in bubble.lines]
    for bubble in bubbles:
        bg = text_area_color(rgb, bubble, text_share) or background_color(
            rgb, bubble, text_boxes, margin, min_share
        )
        bubble.bg = None if bg is None else to_hex(bg)
        bubble.fg = None if bg is None else to_hex(text_color(bg))
    return bubbles
