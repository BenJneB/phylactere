"""Cache SQLite : résultats OCR, traductions et glossaires (voir docs/SPEC.md, section 5.6).

- OCR : clé = empreinte de l'URL, des voisines et des réglages OCR ; on peut changer de
  modèle de traduction ou de langue sans refaire l'OCR.
- Traductions : clé = sha1(image_url) + langue + modèle + PROMPT_VERSION. Chaque ligne
  garde la série, l'épisode et l'index pour fournir le contexte (bulles précédentes).
- Glossaires : une liste d'entrées par série. Le modifier efface seulement les traductions
  des images qui mentionnent un terme ajouté, retiré ou modifié.

sqlite3 est synchrone : les appels passent par un thread (asyncio.to_thread), protégés par
un verrou (une seule connexion).
"""

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from pydantic import BaseModel

from phylactere.schemas import Box, Edge, ImageSize, OcrResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr (
    key TEXT PRIMARY KEY,
    series_id TEXT,
    result TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS translation (
    key TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    image_index INTEGER NOT NULL,
    target_lang TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS translation_episode
    ON translation (series_id, episode_id, target_lang, model, image_index);
CREATE TABLE IF NOT EXISTS glossary (
    series_id TEXT PRIMARY KEY,
    entries TEXT NOT NULL
);
"""


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class GlossaryEntry(BaseModel):
    ko: str
    fr: str
    note: str = ""


def _compact(text: str) -> str:
    """Texte sans espaces : l'OCR en perd ou en ajoute (lignes jointes par une espace)."""
    return "".join(text.split())


def changed_terms(old: list[GlossaryEntry], new: list[GlossaryEntry]) -> set[str]:
    """Termes coréens dont l'entrée a été ajoutée, retirée ou modifiée (traduction, note)."""
    before = {_compact(e.ko): (e.fr.strip(), e.note.strip()) for e in old}
    after = {_compact(e.ko): (e.fr.strip(), e.note.strip()) for e in new}
    return {ko for ko in before.keys() | after.keys() if ko and before.get(ko) != after.get(ko)}


def mentions(original: str, terms: set[str]) -> bool:
    text = _compact(original)
    return any(term in text for term in terms)


class TranslatedBlock(BaseModel):
    """Une bulle traduite. ``box`` en pixels de l'image d'origine ; elle peut déborder
    en bas sur l'image suivante quand la bulle est coupée entre deux images."""

    box: Box
    original: str
    translated: str
    edges: list[Edge] = []
    # Couleurs prises sur l'image (voir Bubble). Absentes des traductions mises en cache
    # avant le jalon 9 : None, l'extension garde le fond blanc.
    bg: str | None = None
    fg: str | None = None


class TranslationResult(BaseModel):
    blocks: list[TranslatedBlock]
    image_size: ImageSize


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            # En WAL, NORMAL évite un fsync par écriture sans risque de corruption ; une
            # coupure de courant peut perdre les dernières entrées : acceptable pour un cache.
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    async def _run(self, func, *args):
        return await asyncio.to_thread(self._locked, func, *args)

    def _locked(self, func, *args):
        with self._lock:
            return func(*args)

    # --- OCR ---

    def _get_ocr(self, key: str) -> OcrResult | None:
        row = self._db.execute("SELECT result FROM ocr WHERE key = ?", (key,)).fetchone()
        return OcrResult.model_validate_json(row[0]) if row else None

    def _put_ocr(self, key: str, series_id: str, result: OcrResult) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO ocr VALUES (?, ?, ?, ?)",
            (key, series_id, result.model_dump_json(), time.time()),
        )
        self._db.commit()

    async def get_ocr(self, key: str) -> OcrResult | None:
        return await self._run(self._get_ocr, key)

    async def put_ocr(self, key: str, series_id: str, result: OcrResult) -> None:
        await self._run(self._put_ocr, key, series_id, result)

    # --- Traductions ---

    def _get_translation(self, key: str) -> TranslationResult | None:
        row = self._db.execute("SELECT result FROM translation WHERE key = ?", (key,)).fetchone()
        return TranslationResult.model_validate_json(row[0]) if row else None

    def _put_translation(
        self,
        key: str,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        result: TranslationResult,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO translation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                series_id,
                episode_id,
                image_index,
                target_lang,
                model,
                result.model_dump_json(),
                time.time(),
            ),
        )
        self._db.commit()

    def _context(
        self,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        limit: int,
    ) -> list[tuple[str, str]]:
        if limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT result FROM translation"
            " WHERE series_id = ? AND episode_id = ? AND target_lang = ? AND model = ?"
            " AND image_index < ? ORDER BY image_index DESC",
            (series_id, episode_id, target_lang, model, image_index),
        )
        pairs: list[tuple[str, str]] = []
        for (raw,) in rows:
            blocks = TranslationResult.model_validate_json(raw).blocks
            for block in reversed(blocks):
                pairs.append((block.original, block.translated))
                if len(pairs) == limit:
                    return pairs[::-1]
        return pairs[::-1]

    async def get_translation(self, key: str) -> TranslationResult | None:
        return await self._run(self._get_translation, key)

    async def put_translation(
        self,
        key: str,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        result: TranslationResult,
    ) -> None:
        await self._run(
            self._put_translation,
            key,
            series_id,
            episode_id,
            image_index,
            target_lang,
            model,
            result,
        )

    async def context(
        self,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        limit: int,
    ) -> list[tuple[str, str]]:
        """Dernières bulles traduites des images précédentes de l'épisode, dans l'ordre."""
        return await self._run(
            self._context, series_id, episode_id, image_index, target_lang, model, limit
        )

    def _series_bubbles(
        self, series_id: str, target_lang: str, episode_id: str | None, limit: int
    ) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT episode_id, image_index, created, result FROM translation"
            " WHERE series_id = ? AND target_lang = ?",
            (series_id, target_lang),
        ).fetchall()
        # Épisode demandé d'abord, puis les autres du plus récemment traduit au plus ancien ;
        # dans un épisode, l'ordre de lecture. Une image traduite par plusieurs modèles
        # n'est comptée qu'une fois (même original).
        latest: dict[str, float] = {}
        for episode, _, created, _ in rows:
            latest[episode] = max(created, latest.get(episode, 0.0))
        rows.sort(key=lambda r: (r[0] != episode_id, -latest[r[0]], r[0], r[1], -r[2]))
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for *_, raw in rows:
            for block in TranslationResult.model_validate_json(raw).blocks:
                if block.original in seen:
                    continue
                seen.add(block.original)
                pairs.append((block.original, block.translated))
                if len(pairs) == limit:
                    return pairs
        return pairs

    async def series_bubbles(
        self, series_id: str, target_lang: str, episode_id: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """Bulles traduites en cache de la série (original, traduction), sans doublon : celles
        de ``episode_id`` d'abord, puis des épisodes les plus récemment traduits."""
        return await self._run(self._series_bubbles, series_id, target_lang, episode_id, limit)

    # --- Glossaires ---

    def _get_glossary(self, series_id: str) -> list[GlossaryEntry]:
        row = self._db.execute(
            "SELECT entries FROM glossary WHERE series_id = ?", (series_id,)
        ).fetchone()
        return [GlossaryEntry.model_validate(e) for e in json.loads(row[0])] if row else []

    def _put_glossary(self, series_id: str, entries: list[GlossaryEntry]) -> int:
        changed = changed_terms(self._get_glossary(series_id), entries)
        data = json.dumps([e.model_dump() for e in entries], ensure_ascii=False)
        self._db.execute("INSERT OR REPLACE INTO glossary VALUES (?, ?)", (series_id, data))
        # Seules les images dont une bulle contient un terme ajouté, retiré ou modifié sont
        # à retraduire ; les autres restent en cache.
        stale = []
        if changed:
            rows = self._db.execute(
                "SELECT key, result FROM translation WHERE series_id = ?", (series_id,)
            )
            for key, raw in rows:
                blocks = TranslationResult.model_validate_json(raw).blocks
                if any(mentions(block.original, changed) for block in blocks):
                    stale.append((key,))
        self._db.executemany("DELETE FROM translation WHERE key = ?", stale)
        self._db.commit()
        return len(stale)

    async def get_glossary(self, series_id: str) -> list[GlossaryEntry]:
        return await self._run(self._get_glossary, series_id)

    async def put_glossary(self, series_id: str, entries: list[GlossaryEntry]) -> int:
        """Remplace le glossaire ; efface les traductions en cache des images de la série
        qui mentionnent un terme ajouté, retiré ou modifié. Renvoie leur nombre."""
        return await self._run(self._put_glossary, series_id, entries)

    # --- Vidage ---

    def _clear(self, series_id: str | None) -> int:
        if series_id is None:
            removed = self._db.execute("DELETE FROM translation").rowcount
            removed += self._db.execute("DELETE FROM ocr").rowcount
        else:
            removed = self._db.execute(
                "DELETE FROM translation WHERE series_id = ?", (series_id,)
            ).rowcount
            removed += self._db.execute(
                "DELETE FROM ocr WHERE series_id = ?", (series_id,)
            ).rowcount
        self._db.commit()
        return removed

    async def clear(self, series_id: str | None = None) -> int:
        """Vide le cache (OCR et traductions ; les glossaires sont conservés)."""
        return await self._run(self._clear, series_id)
