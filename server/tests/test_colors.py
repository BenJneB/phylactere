"""Couleur de fond et de texte des bulles (jalon 9), sur images synthétiques."""

import numpy as np
import pytest
from PIL import Image, ImageDraw

from phylactere.colors import background_color, relative_luminance, text_color, to_hex
from phylactere.ocr import run_pipeline, run_pipeline_with_neighbors
from phylactere.schemas import Box, Bubble
from tests.helpers import GROUPING, FakeDetector, line
from tests.test_ocr import OCR

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
NAVY = (20, 30, 70)


def draw_text(image: Image.Image, box: Box, color) -> None:
    """« Texte » : barres verticales serrées qui remplissent la boîte de la ligne."""
    draw = ImageDraw.Draw(image)
    for x in range(box.x, box.right, 4):
        draw.rectangle((x, box.y, x + 2, box.bottom - 1), fill=color)


def image_with_text(background, text_color, lines, size=(690, 600)) -> Image.Image:
    image = Image.new("RGB", size, background)
    for item in lines:
        draw_text(image, item.box, text_color)
    return image


def colors_of(image: Image.Image, lines) -> list[tuple[str | None, str | None]]:
    result = run_pipeline(image, FakeDetector([lines]), OCR, GROUPING)
    return [(bubble.bg, bubble.fg) for bubble in result.bubbles]


def test_white_bubble_with_black_text():
    lines = [line(200, 200, 180), line(210, 232, 160)]
    image = image_with_text(WHITE, BLACK, lines)
    assert colors_of(image, lines) == [("#ffffff", "#000000")]


def test_navy_narration_with_white_text():
    lines = [line(100, 300, 300), line(100, 332, 280)]
    image = image_with_text(NAVY, WHITE, lines)
    assert colors_of(image, lines) == [(to_hex(NAVY), "#ffffff")]


def test_text_pixels_do_not_count():
    # La boîte des lignes couvre presque toute la zone échantillonnée : sans le masque,
    # le texte noir l'emporterait sur le fond bleu nuit.
    lines = [line(100, 100, 400, h=200)]
    image = Image.new("RGB", (690, 600), NAVY)
    ImageDraw.Draw(image).rectangle((100, 100, 499, 299), fill=BLACK)
    assert colors_of(image, lines) == [(to_hex(NAVY), "#ffffff")]


def test_noisy_background_has_no_color():
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 256, size=(600, 690, 3), dtype=np.uint8)
    image = Image.fromarray(noise)
    lines = [line(200, 200, 180)]
    assert colors_of(image, lines) == [(None, None)]


def test_drawing_behind_text_has_no_color():
    # Moitié bleu nuit, moitié dessin noir : aucune couleur n'atteint 60 %.
    image = Image.new("RGB", (690, 600), NAVY)
    ImageDraw.Draw(image).rectangle((0, 0, 290, 599), fill=BLACK)
    lines = [line(200, 200, 180)]
    assert colors_of(image, lines) == [(None, None)]


def test_slight_noise_is_averaged():
    # JPEG : un fond uni varie de quelques niveaux ; la moyenne de la classe est rendue.
    rng = np.random.default_rng(1)
    base = np.full((600, 690, 3), (200, 120, 40), dtype=np.int16)
    noisy = np.clip(base + rng.integers(-3, 4, size=base.shape), 0, 255).astype(np.uint8)
    image = Image.fromarray(noisy)
    bg, fg = colors_of(image, [line(200, 200, 180)])[0]
    r, g, b = (int(bg[i : i + 2], 16) for i in (1, 3, 5))
    assert abs(r - 200) <= 2 and abs(g - 120) <= 2 and abs(b - 40) <= 2
    assert fg == "#000000"


def test_bubble_overflowing_on_next_image():
    # Narration bleu nuit coupée par le bas de l'image : sa boîte déborde sur la suivante,
    # dont le haut est bleu nuit aussi ; le bas de l'image courante est blanc sous la bulle.
    image = Image.new("RGB", (690, 1600), WHITE)
    ImageDraw.Draw(image).rectangle((50, 1540, 640, 1599), fill=NAVY)
    following = Image.new("RGB", (690, 1600), NAVY)
    ImageDraw.Draw(following).rectangle((0, 80, 689, 1599), fill=WHITE)
    # Image prolongée de 400 px de chaque voisine : 2400 px, tranches [0, 1200),
    # [1080, 2280), [2160, 2400). Les lignes (y = 1560 et 1592 dans l'image) sont dans
    # la deuxième, en coordonnées de la tranche.
    top = 1080 - 400
    lines = [line(100, 1560 - top, 400), line(100, 1592 - top, 400)]
    previous = Image.new("RGB", (690, 1600), WHITE)
    detector = FakeDetector([[], lines, []])
    result = run_pipeline_with_neighbors(image, previous, following, detector, OCR, GROUPING)
    [bubble] = result.bubbles
    assert bubble.box.bottom > 1600
    assert (bubble.bg, bubble.fg) == (to_hex(NAVY), "#ffffff")


def test_other_bubble_text_is_masked():
    # Le texte noir d'une bulle voisine occupe près de la moitié de la marge : il ne
    # compte pas, sinon le fond bleu nuit passerait sous les 60 %.
    rgb = np.full((300, 300, 3), NAVY, dtype=np.uint8)
    rgb[40:260, 70:300] = BLACK
    own = line(20, 100, 40, h=40)
    other = line(70, 40, 230, h=220)
    bubble = Bubble(box=own.box, text="가", lines=[own])
    assert background_color(rgb, bubble, [own.box, other.box], 60, 0.6) == NAVY
    assert background_color(rgb, bubble, [own.box], 60, 0.6) is None


@pytest.mark.parametrize(
    ("background", "expected"),
    [(WHITE, BLACK), (BLACK, WHITE), (NAVY, WHITE), ((255, 230, 0), BLACK), ((200, 0, 0), WHITE)],
)
def test_text_color_has_best_contrast(background, expected):
    assert text_color(background) == expected


def test_relative_luminance_bounds():
    assert relative_luminance(BLACK) == 0
    assert relative_luminance(WHITE) == pytest.approx(1)
