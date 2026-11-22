"""Génère korean_sample.png : image synthétique (pas un webtoon) pour les tests OCR.

690 × 1600 px comme une bande Naver, trois bulles de texte coréen dont une à cheval
sur la frontière des tranches (1080–1200 px) pour tester le recalage et le dédoublonnage.
Police : Noto Sans CJK KR (licence SIL OFL). Usage :
    uv run python tests/data/make_korean_sample.py /chemin/vers/NotoSansCJK-Regular.ttc
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# (centre x, centre y, lignes)
BUBBLES = [
    (345, 250, ["안녕하세요", "오늘 날씨가 좋네요"]),
    (300, 1140, ["어디 가는 거야?"]),
    (380, 1450, ["같이 가자!"]),
]


def main(font_path: str) -> None:
    font = ImageFont.truetype(font_path, 30, index=1)  # index 1 = KR dans la collection
    image = Image.new("RGB", (690, 1600), (235, 235, 240))
    draw = ImageDraw.Draw(image)
    for cx, cy, lines in BUBBLES:
        height = 44 * len(lines)
        width = max(draw.textlength(line, font=font) for line in lines)
        draw.ellipse(
            (cx - width / 2 - 50, cy - height / 2 - 40, cx + width / 2 + 50, cy + height / 2 + 40),
            fill="white",
            outline="black",
            width=3,
        )
        for i, line in enumerate(lines):
            y = cy - height / 2 + i * 44
            draw.text((cx, y), line, font=font, fill="black", anchor="mt")
    image.save(Path(__file__).with_name("korean_sample.png"), optimize=True)


if __name__ == "__main__":
    main(sys.argv[1])
