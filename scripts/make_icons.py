"""Génère les icônes de l'extension (extension/icons/icon-<taille>.png).

Deux bulles de BD qui se chevauchent : l'original (blanche, derrière) et la traduction
(bleue, devant, avec trois lignes de texte). Dessin vectoriel simple avec Pillow, sans
police : rien d'autre à installer. Lancement, depuis server/ :

    uv run python ../scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 32, 48, 128)
# Couleurs du popup (popup.html) : encre et accent.
INK = (29, 42, 68, 255)
ACCENT = (47, 91, 211, 255)
WHITE = (255, 255, 255, 255)
SCALE = 8  # suréchantillonnage : dessin à 8 fois la taille, puis réduction (anticrénelage)
OUT = Path(__file__).resolve().parents[1] / "extension" / "icons"


def bubble(draw, box, tail, fill, border):
    """Bulle arrondie avec une pointe ; ``tail`` : trois points du triangle de la pointe."""
    x1, y1, x2, y2 = box
    radius = (y2 - y1) * 0.42
    # Contour : la même forme dessinée plus grande en encre, puis le remplissage dessus.
    draw.polygon(_grow(tail, border), fill=INK)
    draw.rounded_rectangle(
        (x1 - border, y1 - border, x2 + border, y2 + border), radius + border, fill=INK
    )
    draw.polygon(tail, fill=fill)
    draw.rounded_rectangle(box, radius, fill=fill)


def _grow(points, by):
    cx = sum(x for x, _ in points) / len(points)
    cy = sum(y for _, y in points) / len(points)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        norm = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        out.append((x + dx / norm * by * 1.6, y + dy / norm * by * 1.6))
    return out


def draw_icon(size: int) -> Image.Image:
    n = size * SCALE
    u = n / 128  # unité : coordonnées pensées pour 128 px
    image = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Trait plus épais en petite taille, sinon il disparaît à la réduction.
    border = u * (9 if size <= 16 else 7 if size <= 32 else 5)

    # Bulle d'origine, derrière, en haut à gauche ; pointe vers le bas à gauche.
    bubble(
        draw,
        (6 * u, 8 * u, 86 * u, 64 * u),
        [(20 * u, 56 * u), (40 * u, 60 * u), (14 * u, 82 * u)],
        WHITE,
        border,
    )
    # Traduction, devant, en bas à droite ; pointe vers le bas à droite.
    bubble(
        draw,
        (36 * u, 48 * u, 122 * u, 108 * u),
        [(92 * u, 100 * u), (112 * u, 96 * u), (118 * u, 124 * u)],
        ACCENT,
        border,
    )
    # Trois lignes de « texte » blanches (deux seulement en 16 px, plus lisible).
    lines = [(52, 64, 106), (52, 78, 98)] + ([] if size <= 16 else [(52, 92, 86)])
    thickness = (10 if size <= 16 else 8) * u
    for x1, y, x2 in lines:
        draw.rounded_rectangle(
            (x1 * u, y * u - thickness / 2, x2 * u, y * u + thickness / 2),
            thickness / 2,
            fill=WHITE,
        )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon-{size}.png"
        draw_icon(size).save(path, optimize=True)
        print(path)


if __name__ == "__main__":
    main()
