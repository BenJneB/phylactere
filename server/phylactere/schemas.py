"""Types partagés : boîtes, lignes OCR et bulles. Coordonnées en pixels de l'image d'origine."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Edge = Literal["top", "bottom"]


class Box(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def shifted(self, dy: int) -> "Box":
        return Box(x=self.x, y=self.y + dy, w=self.w, h=self.h)

    def intersection_area(self, other: "Box") -> int:
        w = min(self.right, other.right) - max(self.x, other.x)
        h = min(self.bottom, other.bottom) - max(self.y, other.y)
        return max(0, w) * max(0, h)

    @staticmethod
    def union(boxes: "list[Box]") -> "Box":
        x = min(b.x for b in boxes)
        y = min(b.y for b in boxes)
        return Box(x=x, y=y, w=max(b.right for b in boxes) - x, h=max(b.bottom for b in boxes) - y)


class Line(BaseModel):
    """Une ligne de texte reconnue par l'OCR."""

    model_config = ConfigDict(frozen=True)

    box: Box
    text: str
    score: float


class Bubble(BaseModel):
    """Un groupe de lignes, supposé correspondre à une bulle."""

    box: Box
    text: str
    lines: list[Line]
    # Bords de l'image touchés : la bulle est peut-être coupée entre deux images Naver.
    edges: list[Edge] = []
    # Couleurs de fond et de texte prises sur l'image ("#rrggbb"), None si le fond
    # n'est pas uni (l'extension garde alors le fond blanc).
    bg: str | None = None
    fg: str | None = None


class ImageSize(BaseModel):
    w: int
    h: int


class OcrResult(BaseModel):
    image_size: ImageSize
    bubbles: list[Bubble]


class ImageRequest(BaseModel):
    """Image d'un épisode, telle que l'extension la désigne (/translate, /report)."""

    image_url: str
    series_id: str = Field(min_length=1, max_length=64)
    episode_id: str = Field(min_length=1, max_length=64)
    index: int = Field(ge=0)
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    # Modèle Ollama choisi dans l'extension (défaut : celui de la configuration).
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Images voisines dans l'épisode : permettent de lire les bulles coupées entre deux
    # images, et entrent dans la clé du cache OCR.
    prev_image_url: str | None = None
    next_image_url: str | None = None
