"""Image de contrôle de l'OCR : lignes en rouge, bulles en vert et numérotées,
bulles possiblement coupées (bord de l'image) en orange. À droite du numéro, une pastille
montre la couleur de fond calculée et, en lettre « A », la couleur du texte ; une pastille
barrée signale un fond non uni (l'extension garde le blanc)."""

from io import BytesIO

from PIL import Image, ImageDraw

from phylactere.schemas import OcrResult

LINE_COLOR = (230, 30, 30)
BUBBLE_COLOR = (20, 170, 60)
EDGE_COLOR = (255, 140, 0)
SWATCH = 18  # côté de la pastille de couleur, en px


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def render_debug(image: Image.Image, result: OcrResult) -> bytes:
    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    for number, bubble in enumerate(result.bubbles, start=1):
        for line in bubble.lines:
            b = line.box
            draw.rectangle((b.x, b.y, b.right, b.bottom), outline=LINE_COLOR, width=1)
        b = bubble.box
        color = EDGE_COLOR if bubble.edges else BUBBLE_COLOR
        draw.rectangle((b.x - 3, b.y - 3, b.right + 3, b.bottom + 3), outline=color, width=3)
        label = str(number)
        lx, ly = b.x - 3, max(0, b.y - 22)
        draw.rectangle((lx, ly, lx + 10 * len(label) + 8, ly + 18), fill=color)
        draw.text((lx + 4, ly + 3), label, fill=(255, 255, 255))
        sx = lx + 10 * len(label) + 12
        swatch = (sx, ly, sx + SWATCH, ly + SWATCH)
        if bubble.bg is None:
            draw.rectangle(swatch, fill=(255, 255, 255), outline=color, width=2)
            draw.line(swatch, fill=color, width=2)
        else:
            draw.rectangle(swatch, fill=_hex_to_rgb(bubble.bg), outline=color, width=2)
            draw.text((sx + 6, ly + 3), "A", fill=_hex_to_rgb(bubble.fg or "#000000"))
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
