"""Regroupement des lignes OCR en bulles (voir docs/SPEC.md, section 5.4).

PaddleOCR renvoie des lignes. Deux lignes vont dans la même bulle si elles sont
proches verticalement et alignées horizontalement ; les groupes sont formés par
fermeture transitive (union-find).
"""

from statistics import median

from phylactere.config import GroupingConfig
from phylactere.schemas import Box, Bubble, Line


def _same_bubble(a: Box, b: Box, line_height: float, cfg: GroupingConfig) -> bool:
    vertical_gap = max(a.y, b.y) - min(a.bottom, b.bottom)
    if vertical_gap >= cfg.max_vertical_gap * line_height:
        return False
    narrower = min(a.w, b.w)
    if narrower <= 0:
        return False
    overlap = min(a.right, b.right) - max(a.x, b.x)
    if overlap >= cfg.min_horizontal_overlap * narrower:
        return True
    center_offset = abs((a.x + a.right) / 2 - (b.x + b.right) / 2)
    return center_offset < cfg.max_center_offset * narrower


def reading_order(lines: list[Line]) -> list[Line]:
    """Haut en bas ; les lignes à la même hauteur (à une demi-ligne près) de gauche à droite."""
    if not lines:
        return []
    tolerance = median(line.box.h for line in lines) / 2
    ordered: list[Line] = []
    row: list[Line] = []
    for line in sorted(lines, key=lambda item: (item.box.y, item.box.x)):
        if row and line.box.y - row[0].box.y > tolerance:
            ordered.extend(sorted(row, key=lambda item: item.box.x))
            row = []
        row.append(line)
    ordered.extend(sorted(row, key=lambda item: item.box.x))
    return ordered


def group_lines(lines: list[Line], cfg: GroupingConfig) -> list[Bubble]:
    """Regroupe les lignes en bulles, triées dans l'ordre de lecture."""
    if not lines:
        return []
    lines = sorted(lines, key=lambda item: (item.box.y, item.box.x))
    line_height = median(line.box.h for line in lines)

    parent = list(range(len(lines)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if _same_bubble(lines[i].box, lines[j].box, line_height, cfg):
                parent[find(j)] = find(i)

    groups: dict[int, list[Line]] = {}
    for i, line in enumerate(lines):
        groups.setdefault(find(i), []).append(line)

    bubbles = []
    for members in groups.values():
        ordered = reading_order(members)
        bubbles.append(
            Bubble(
                box=Box.union([line.box for line in ordered]),
                text=" ".join(line.text for line in ordered),
                lines=ordered,
            )
        )
    bubbles.sort(key=lambda bubble: (bubble.box.y, bubble.box.x))
    return bubbles


def mark_edges(bubbles: list[Bubble], image_height: int, margin: int) -> list[Bubble]:
    """Signale les bulles qui touchent le haut ou le bas de l'image."""
    for bubble in bubbles:
        edges = []
        if bubble.box.y <= margin:
            edges.append("top")
        if bubble.box.bottom >= image_height - margin:
            edges.append("bottom")
        bubble.edges = edges
    return bubbles
