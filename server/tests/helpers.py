"""Outils communs aux tests."""

from io import BytesIO

from PIL import Image

from phylactere.config import GroupingConfig
from phylactere.schemas import Box, Line

GROUPING = GroupingConfig(max_vertical_gap=0.8, min_horizontal_overlap=0.3, max_center_offset=0.6)


def png_bytes(size: tuple[int, int] = (690, 600), color: str = "white") -> bytes:
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def line(x: int, y: int, w: int, h: int = 30, text: str = "가", score: float = 0.9) -> Line:
    return Line(box=Box(x=x, y=y, w=w, h=h), text=text, score=score)


class FakeDetector:
    """Détecteur simulé : renvoie des lignes fixes, et mémorise les tranches reçues."""

    device = "cpu"

    def __init__(self, lines_by_call: list[list[Line]] | None = None) -> None:
        self._lines_by_call = lines_by_call or []
        self.shapes: list[tuple[int, int]] = []

    def detect(self, image_bgr) -> list[Line]:
        self.shapes.append(image_bgr.shape[:2])
        index = len(self.shapes) - 1
        return self._lines_by_call[index] if index < len(self._lines_by_call) else []
