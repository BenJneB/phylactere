"""Image de contrôle de l'OCR : lignes en rouge, bulles en vert et numérotées,
bulles possiblement coupées (bord de l'image) en orange."""

from io import BytesIO

from PIL import Image, ImageDraw

from phylactere.schemas import OcrResult

LINE_COLOR = (230, 30, 30)
BUBBLE_COLOR = (20, 170, 60)
EDGE_COLOR = (255, 140, 0)


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
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()
