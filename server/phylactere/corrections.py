"""Corrections à la main des bulles (jalon 14) : rattachement d'une correction à sa bulle
et lecture dans la base. Le stockage (ajout, suppression) reste dans ``cache.py``."""

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from phylactere.schemas import Box, Correction, TranslatedBlock, compact

# Une correction s'applique à une bulle de boîte égale, sinon à la bulle de même original
# (sans espaces) qui la recouvre le plus, si le recouvrement atteint cette part de la plus
# grande des deux boîtes : l'OCR a pu changer légèrement (réglages, voisines).
CORRECTION_OVERLAP = 0.5


def match_correction(
    bubbles: list[tuple[Box, str]], correction: Correction, exclude: set[int] = frozenset()
) -> int | None:
    """Indice de la bulle ``(boîte, original)`` à laquelle s'applique la correction : boîte
    égale, sinon même original (sans espaces) et plus grand recouvrement, s'il atteint
    CORRECTION_OVERLAP de la plus grande des deux boîtes. Les indices ``exclude`` (bulles
    déjà corrigées) sont ignorés."""
    candidates = [(i, box, text) for i, (box, text) in enumerate(bubbles) if i not in exclude]
    for i, box, _ in candidates:
        if box == correction.box:
            return i
    original = compact(correction.original)
    best, best_ratio = None, 0.0
    for i, box, text in candidates:
        if compact(text) != original:
            continue
        largest = max(box.area, correction.box.area)
        ratio = box.intersection_area(correction.box) / largest if largest else 0.0
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    return best if best_ratio >= CORRECTION_OVERLAP else None


def apply_corrections(
    blocks: list[TranslatedBlock], corrections: list[Correction]
) -> list[TranslatedBlock]:
    """Remplace le texte des bulles corrigées (``corrected``, traduction du modèle gardée
    dans ``machine``). Une correction qui ne retrouve pas sa bulle est ignorée."""
    if not corrections:
        return blocks
    out = list(blocks)
    bubbles = [(block.box, block.original) for block in blocks]
    taken: set[int] = set()
    for correction in corrections:
        index = match_correction(bubbles, correction, taken)
        if index is None:
            continue
        taken.add(index)
        block = out[index]
        out[index] = block.model_copy(
            update={"translated": correction.text, "corrected": True, "machine": block.translated}
        )
    return out


def load_corrections(db: sqlite3.Connection, image: str, target_lang: str) -> list[Correction]:
    """Corrections d'une image (empreinte de l'URL) dans une langue, dans l'ordre de saisie."""
    rows = db.execute(
        "SELECT box, original, text FROM correction WHERE image = ? AND target_lang = ?"
        " ORDER BY id",
        (image, target_lang),
    )
    return [
        Correction(box=Box.model_validate_json(box), original=original, text=text)
        for box, original, text in rows
    ]


@contextlib.contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Base ``path`` en lecture seule (scripts : le serveur peut tourner) ; None si elle
    n'existe pas."""
    if not path.is_file():
        yield None
        return
    with contextlib.closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        yield db


def read_corrections(path: Path) -> list[dict]:
    """Toutes les corrections de la base ``path``, de la plus récente à la plus ancienne
    (scripts/reports.py). Base ouverte en lecture seule : le serveur peut tourner. Pas de
    base, ou base d'avant le jalon 14 : liste vide."""
    with open_readonly(path) as db:
        if db is None:
            return []
        try:
            cursor = db.execute(
                "SELECT target_lang, box, original, text, created, image_url, series_id,"
                " episode_id, image_index, model, translated FROM correction"
                " ORDER BY created DESC"
            )
        except sqlite3.OperationalError:  # pas de table correction
            return []
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row, strict=True)) for row in cursor]
    for row in rows:
        row["box"] = json.loads(row["box"])
    return rows
