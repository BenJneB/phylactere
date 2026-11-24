"""Cache SQLite : résultats OCR, traductions et glossaires (voir docs/SPEC.md, section 5.6).

- OCR : clé = empreinte de l'URL, des voisines et des réglages OCR ; on peut changer de
  modèle de traduction ou de langue sans refaire l'OCR.
- Traductions : clé = sha1(image_url) + langue + modèle + PROMPT_VERSION. Chaque ligne
  garde la série, l'épisode et l'index pour fournir le contexte (bulles précédentes).
- Glossaires : une liste d'entrées par série. Le modifier efface seulement les traductions
  des images qui mentionnent un terme ajouté, retiré ou modifié.
- Taille bornée (jalon 12) : au-delà de ``max_bytes``, les entrées OCR et traductions les
  moins récemment utilisées (colonne ``used``) sont supprimées ; jamais les glossaires.
- Corrections (jalon 14) : texte saisi à la main pour une bulle, par image (sha1 de l'URL)
  et langue cible, indépendant du modèle et de PROMPT_VERSION. Appliquées à la lecture
  (réponse de /translate, contexte, propositions de glossaire) : les traductions en cache
  restent celles du modèle. Ni ``clear`` ni la purge n'y touchent.
- Résumés (jalon 15) : un résumé par épisode, langue cible et modèle, avec l'empreinte des
  bulles résumées (``source``) pour savoir s'il est à refaire. Ni ``clear`` ni la purge n'y
  touchent : quelques centaines d'octets par épisode, et le résumé reste servi quand les
  traductions de l'épisode ne sont plus en cache.

sqlite3 est synchrone : les appels passent par un thread (asyncio.to_thread), protégés par
un verrou (une seule connexion).
"""

import asyncio
import functools
import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from pydantic import BaseModel

from phylactere.schemas import Box, Edge, ImageSize, OcrResult

logger = logging.getLogger("phylactere.cache")

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
CREATE TABLE IF NOT EXISTS correction (
    id INTEGER PRIMARY KEY,
    image TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    box TEXT NOT NULL,
    original TEXT NOT NULL,
    text TEXT NOT NULL,
    created REAL NOT NULL,
    -- Pour scripts/reports.py : où est la bulle, et ce que le modèle avait proposé.
    image_url TEXT NOT NULL,
    series_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    image_index INTEGER NOT NULL,
    model TEXT,
    translated TEXT
);
CREATE INDEX IF NOT EXISTS correction_image ON correction (image, target_lang);
CREATE TABLE IF NOT EXISTS summary (
    series_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    model TEXT NOT NULL,
    -- SUMMARY_VERSION du prompt, et empreinte des bulles résumées : résumé à refaire si
    -- l'une ou l'autre a changé.
    version TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    partial INTEGER NOT NULL,
    images INTEGER NOT NULL,
    bubbles INTEGER NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (series_id, episode_id, target_lang, model)
);
"""

# Tables purgées quand le cache dépasse sa taille maximale (pas les glossaires).
PURGEABLE = ("ocr", "translation")
# Une purge est tentée toutes les PURGE_EVERY écritures, et descend à PURGE_TARGET du
# maximum pour ne pas se relancer à chaque écriture.
PURGE_EVERY = 100
PURGE_TARGET = 0.9
# Entrées lues d'un coup pendant une purge (la taille est remesurée après chacune).
PURGE_BATCH = 50
# Valeur de PRAGMA auto_vacuum pour le mode INCREMENTAL.
AUTO_VACUUM_INCREMENTAL = 2
# Une correction s'applique à une bulle de boîte égale, sinon à la bulle de même original
# (sans espaces) qui la recouvre le plus, si le recouvrement atteint cette part de la plus
# grande des deux boîtes : l'OCR a pu changer légèrement (réglages, voisines).
CORRECTION_OVERLAP = 0.5


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class GlossaryEntry(BaseModel):
    ko: str
    fr: str
    note: str = ""


def compact(text: str) -> str:
    """Texte sans espaces : l'OCR en perd ou en ajoute (lignes jointes par une espace)."""
    return "".join(text.split())


def changed_terms(old: list[GlossaryEntry], new: list[GlossaryEntry]) -> set[str]:
    """Termes coréens dont l'entrée a été ajoutée, retirée ou modifiée (traduction, note)."""
    before = {compact(e.ko): (e.fr.strip(), e.note.strip()) for e in old}
    after = {compact(e.ko): (e.fr.strip(), e.note.strip()) for e in new}
    return {ko for ko in before.keys() | after.keys() if ko and before.get(ko) != after.get(ko)}


def mentions(original: str, terms: set[str]) -> bool:
    text = compact(original)
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
    # Modèle qui a produit la traduction (jalon 13 : une bulle retraduite peut venir d'un
    # autre modèle que celui de la clé de cache) ; absent des traductions plus anciennes.
    model: str | None = None
    # Bulle corrigée à la main (jalon 14) : ``translated`` est la correction, ``machine``
    # la traduction du modèle, qui revient si la correction est effacée.
    corrected: bool = False
    machine: str | None = None


class TranslationResult(BaseModel):
    blocks: list[TranslatedBlock]
    image_size: ImageSize


class StoredSummary(BaseModel):
    """Résumé d'un épisode en cache (jalon 15)."""

    text: str
    version: str
    # Empreinte des bulles résumées (voir Pipeline.summary).
    source: str
    partial: bool
    images: int
    bubbles: int


class Correction(BaseModel):
    box: Box
    original: str
    text: str


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


def image_hash(translation_key: str) -> str:
    """Empreinte de l'URL de l'image, en tête de la clé d'une traduction."""
    return translation_key.split(":", 1)[0]


def read_corrections(path: Path) -> list[dict]:
    """Toutes les corrections de la base ``path``, de la plus récente à la plus ancienne
    (scripts/reports.py). Base ouverte en lecture seule : le serveur peut tourner. Pas de
    base, ou base d'avant le jalon 14 : liste vide."""
    if not path.is_file():
        return []
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        cursor = db.execute(
            "SELECT target_lang, box, original, text, created, image_url, series_id,"
            " episode_id, image_index, model, translated FROM correction ORDER BY created DESC"
        )
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row, strict=True)) for row in cursor]
    except sqlite3.OperationalError:  # pas de table correction
        return []
    finally:
        db.close()
    for row in rows:
        row["box"] = json.loads(row["box"])
    return rows


def _threaded(func):
    """Méthode synchrone → coroutine exécutée dans un thread, sous le verrou du cache."""

    @functools.wraps(func)
    async def wrapper(self: "Cache", *args, **kwargs):
        def call():
            with self._lock:
                return func(self, *args, **kwargs)

        return await asyncio.to_thread(call)

    return wrapper


class Cache:
    def __init__(self, path: Path, max_bytes: int = 0) -> None:
        """``max_bytes`` : taille maximale du fichier (0 = sans limite)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._writes = 0
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._enable_incremental_vacuum()
            self._db.execute("PRAGMA journal_mode=WAL")
            # En WAL, NORMAL évite un fsync par écriture sans risque de corruption ; une
            # coupure de courant peut perdre les dernières entrées : acceptable pour un cache.
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._add_used_columns()
            self._db.commit()
        # Version du glossaire de chaque série, incrémentée à chaque enregistrement : une
        # traduction commencée avec l'ancien glossaire n'est pas mise en cache (voir
        # put_translation).
        self._glossary_versions: dict[str, int] = {}

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- Taille bornée ---

    def _enable_incremental_vacuum(self) -> None:
        """Sans auto_vacuum, SQLite garde la place des entrées supprimées : le fichier ne
        rapetisse jamais. Une base neuve prend le mode tout de suite ; une base existante
        (d'avant le jalon 12) doit être réécrite une fois par VACUUM."""
        (mode,) = self._db.execute("PRAGMA auto_vacuum").fetchone()
        if mode == AUTO_VACUUM_INCREMENTAL:
            return
        self._db.execute(f"PRAGMA auto_vacuum = {AUTO_VACUUM_INCREMENTAL}")
        (tables,) = self._db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        if tables:
            logger.info("Conversion du cache pour pouvoir le réduire (une seule fois)…")
            self._db.execute("VACUUM")

    def _add_used_columns(self) -> None:
        """Colonne ``used`` (dernière utilisation), absente des bases d'avant le jalon 12 :
        on y reprend la date de création."""
        for table in PURGEABLE:
            columns = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if "used" not in columns:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN used REAL")
                self._db.execute(f"UPDATE {table} SET used = created")

    def _size(self, live: bool = False) -> int:
        """Taille du fichier en octets ; ``live`` : sans les pages libres (place récupérable)."""
        (pages,) = self._db.execute("PRAGMA page_count").fetchone()
        (page_size,) = self._db.execute("PRAGMA page_size").fetchone()
        if live:
            (free,) = self._db.execute("PRAGMA freelist_count").fetchone()
            pages -= free
        return pages * page_size

    def _shrink(self) -> None:
        """Rend au disque la place des entrées supprimées (fichier principal et journal WAL)."""
        self._db.execute("PRAGMA incremental_vacuum").fetchall()
        self._db.commit()
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def _purge(self) -> int:
        """Supprime les entrées les moins récemment utilisées tant que le cache dépasse
        PURGE_TARGET × max_bytes. Renvoie le nombre d'entrées supprimées."""
        if not self.max_bytes or self._size(live=True) <= self.max_bytes:
            return 0
        target = self.max_bytes * PURGE_TARGET
        removed = 0
        while self._size(live=True) > target:
            rows = self._db.execute(
                "SELECT 'ocr', key, used FROM ocr UNION ALL"
                " SELECT 'translation', key, used FROM translation"
                " ORDER BY used LIMIT ?",
                (PURGE_BATCH,),
            ).fetchall()
            if not rows:
                break  # plus rien à purger (glossaires seuls, ou maximum minuscule)
            # Une entrée à la fois : la place libérée se lit tout de suite dans
            # freelist_count, avant même le commit.
            for table, key, _ in rows:
                self._db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
                removed += 1
                if self._size(live=True) <= target:
                    break
        self._db.commit()
        self._shrink()
        logger.info(
            "Cache réduit : %d entrée(s) supprimée(s), %.1f Mo.",
            removed,
            self._size() / 1_000_000,
        )
        return removed

    purge = _threaded(_purge)

    def _wrote(self) -> None:
        """À appeler après chaque écriture d'une entrée purgeable."""
        self._writes += 1
        if self._writes % PURGE_EVERY == 0:
            self._purge()

    def _touch(self, table: str, key: str) -> None:
        self._db.execute(f"UPDATE {table} SET used = ? WHERE key = ?", (time.time(), key))
        self._db.commit()

    @_threaded
    def stats(self) -> dict[str, int]:
        counts = {
            table: self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ocr", "translation", "glossary", "correction", "summary")
        }
        return {
            "size_bytes": self._size(),
            "max_bytes": self.max_bytes,
            "ocr": counts["ocr"],
            "translations": counts["translation"],
            "glossaries": counts["glossary"],
            "corrections": counts["correction"],
            "summaries": counts["summary"],
        }

    # --- OCR ---

    @_threaded
    def get_ocr(self, key: str) -> OcrResult | None:
        row = self._db.execute("SELECT result FROM ocr WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        self._touch("ocr", key)
        return OcrResult.model_validate_json(row[0])

    @_threaded
    def put_ocr(self, key: str, series_id: str, result: OcrResult) -> None:
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO ocr (key, series_id, result, created, used)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, series_id, result.model_dump_json(), now, now),
        )
        self._db.commit()
        self._wrote()

    # --- Traductions ---

    @_threaded
    def get_translation(self, key: str) -> TranslationResult | None:
        row = self._db.execute("SELECT result FROM translation WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        self._touch("translation", key)
        return TranslationResult.model_validate_json(row[0])

    @_threaded
    def replace_block(self, key: str, block: TranslatedBlock) -> bool:
        """Remplace, dans la traduction en cache ``key``, la bulle de même boîte (jalon 13).
        False si la traduction ou la bulle n'y est plus (cache vidé, glossaire modifié ou
        image retraduite entre-temps) : rien n'est écrit."""
        row = self._db.execute("SELECT result FROM translation WHERE key = ?", (key,)).fetchone()
        if row is None:
            return False
        result = TranslationResult.model_validate_json(row[0])
        for i, old in enumerate(result.blocks):
            if old.box == block.box:
                result.blocks[i] = block
                break
        else:
            return False
        self._db.execute(
            "UPDATE translation SET result = ?, used = ? WHERE key = ?",
            (result.model_dump_json(), time.time(), key),
        )
        self._db.commit()
        return True

    def glossary_version(self, series_id: str) -> int:
        """À lire avant le glossaire, puis à passer à put_translation."""
        return self._glossary_versions.get(series_id, 0)

    @_threaded
    def put_translation(
        self,
        key: str,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        result: TranslationResult,
        glossary_version: int | None = None,
    ) -> None:
        """``glossary_version`` : version du glossaire lue avant la traduction. S'il a été
        enregistré depuis, la traduction n'est pas mise en cache : put_glossary a déjà
        effacé les traductions concernées, elle les remettrait avec l'ancien glossaire."""
        if glossary_version is not None and glossary_version != self.glossary_version(series_id):
            return
        now = time.time()
        self._db.execute(
            "INSERT OR REPLACE INTO translation"
            " (key, series_id, episode_id, image_index, target_lang, model, result, created, used)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                series_id,
                episode_id,
                image_index,
                target_lang,
                model,
                result.model_dump_json(),
                now,
                now,
            ),
        )
        self._db.commit()
        self._wrote()

    @_threaded
    def context(
        self,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        model: str,
        limit: int,
    ) -> list[tuple[str, str]]:
        """Dernières bulles traduites des images précédentes de l'épisode, dans l'ordre."""
        if limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT image_index, key, result FROM translation"
            " WHERE series_id = ? AND episode_id = ? AND target_lang = ? AND model = ?"
            " AND image_index < ? ORDER BY image_index DESC, created DESC, rowid DESC",
            (series_id, episode_id, target_lang, model, image_index),
        ).fetchall()
        pairs: list[tuple[str, str]] = []
        seen: set[int] = set()
        for index, key, raw in rows:
            # Une image traduite avec plusieurs versions du prompt : la plus récente seule.
            if index in seen:
                continue
            seen.add(index)
            blocks = self._corrected_blocks(key, target_lang, raw)
            for block in reversed(blocks):
                pairs.append((block.original, block.translated))
                if len(pairs) == limit:
                    return pairs[::-1]
        return pairs[::-1]

    @_threaded
    def series_bubbles(
        self, series_id: str, target_lang: str, episode_id: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """Bulles traduites en cache de la série (original, traduction), sans doublon : celles
        de ``episode_id`` d'abord, puis des épisodes les plus récemment traduits."""
        rows = self._db.execute(
            "SELECT episode_id, image_index, created, key, result FROM translation"
            " WHERE series_id = ? AND target_lang = ?",
            (series_id, target_lang),
        ).fetchall()
        # Épisode demandé d'abord, puis les autres du plus récemment traduit au plus ancien ;
        # dans un épisode, l'ordre de lecture. Une image traduite par plusieurs modèles
        # n'est comptée qu'une fois (même original).
        latest: dict[str, float] = {}
        for episode, _, created, _, _ in rows:
            latest[episode] = max(created, latest.get(episode, 0.0))
        rows.sort(key=lambda r: (r[0] != episode_id, -latest[r[0]], r[0], r[1], -r[2]))
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for *_, key, raw in rows:
            for block in self._corrected_blocks(key, target_lang, raw):
                if block.original in seen:
                    continue
                seen.add(block.original)
                pairs.append((block.original, block.translated))
                if len(pairs) == limit:
                    return pairs
        return pairs

    @_threaded
    def episode_bubbles(
        self, series_id: str, episode_id: str, target_lang: str, model: str
    ) -> list[tuple[int, list[str]]]:
        """Traductions des bulles de l'épisode en cache, corrections appliquées : une entrée
        par image traduite, (index, textes dans l'ordre de lecture), par index croissant.

        Celles de ``model`` ; à défaut (épisode lu avec un autre modèle), celles du modèle
        qui a traduit le plus d'images de l'épisode, le plus récent en cas d'égalité."""
        rows = self._db.execute(
            "SELECT model, image_index, created, key, result FROM translation"
            " WHERE series_id = ? AND episode_id = ? AND target_lang = ?"
            " ORDER BY image_index, created DESC, rowid DESC",
            (series_id, episode_id, target_lang),
        ).fetchall()
        if not rows:
            return []
        by_model: dict[str, list[tuple]] = {}
        for row in rows:
            by_model.setdefault(row[0], []).append(row)
        if model not in by_model:
            model = max(
                by_model,
                key=lambda m: (
                    len({row[1] for row in by_model[m]}),
                    max(row[2] for row in by_model[m]),
                ),
            )
        images: list[tuple[int, list[str]]] = []
        for _, index, _, key, raw in by_model[model]:
            # Une image traduite avec plusieurs versions du prompt : la plus récente seule.
            if images and images[-1][0] == index:
                continue
            blocks = self._corrected_blocks(key, target_lang, raw)
            images.append((index, [block.translated for block in blocks]))
        return images

    # --- Résumés (jalon 15) ---

    @_threaded
    def get_summary(
        self, series_id: str, episode_id: str, target_lang: str, model: str
    ) -> StoredSummary | None:
        row = self._db.execute(
            "SELECT text, version, source, partial, images, bubbles FROM summary"
            " WHERE series_id = ? AND episode_id = ? AND target_lang = ? AND model = ?",
            (series_id, episode_id, target_lang, model),
        ).fetchone()
        if row is None:
            return None
        text, version, source, partial, images, bubbles = row
        return StoredSummary(
            text=text,
            version=version,
            source=source,
            partial=bool(partial),
            images=images,
            bubbles=bubbles,
        )

    @_threaded
    def put_summary(
        self,
        series_id: str,
        episode_id: str,
        target_lang: str,
        model: str,
        summary: StoredSummary,
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO summary (series_id, episode_id, target_lang, model,"
            " version, source, text, partial, images, bubbles, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                series_id,
                episode_id,
                target_lang,
                model,
                summary.version,
                summary.source,
                summary.text,
                int(summary.partial),
                summary.images,
                summary.bubbles,
                time.time(),
            ),
        )
        self._db.commit()

    # --- Corrections (jalon 14) ---

    def _corrections(self, image: str, target_lang: str) -> list[Correction]:
        rows = self._db.execute(
            "SELECT box, original, text FROM correction WHERE image = ? AND target_lang = ?"
            " ORDER BY id",
            (image, target_lang),
        )
        return [
            Correction(box=Box.model_validate_json(box), original=original, text=text)
            for box, original, text in rows
        ]

    def _corrected_blocks(self, key: str, target_lang: str, raw: str) -> list[TranslatedBlock]:
        """Bulles d'une traduction en cache (``raw``), corrections appliquées."""
        blocks = TranslationResult.model_validate_json(raw).blocks
        return apply_corrections(blocks, self._corrections(image_hash(key), target_lang))

    @_threaded
    def corrected(
        self, image_url: str, target_lang: str, result: TranslationResult
    ) -> TranslationResult:
        """``result`` avec les corrections de l'image appliquées."""
        corrections = self._corrections(sha1(image_url), target_lang)
        return result.model_copy(update={"blocks": apply_corrections(result.blocks, corrections)})

    def _delete_correction(self, image: str, target_lang: str, box: Box, original: str) -> int:
        """Supprime les corrections qui s'appliquent à la bulle ``(box, original)``."""
        rows = self._db.execute(
            "SELECT id, box, original, text FROM correction WHERE image = ? AND target_lang = ?",
            (image, target_lang),
        ).fetchall()
        stale = [
            (row_id,)
            for row_id, raw_box, row_original, text in rows
            if match_correction(
                [(box, original)],
                Correction(box=Box.model_validate_json(raw_box), original=row_original, text=text),
            )
            == 0
        ]
        self._db.executemany("DELETE FROM correction WHERE id = ?", stale)
        return len(stale)

    @_threaded
    def put_correction(
        self,
        image_url: str,
        series_id: str,
        episode_id: str,
        image_index: int,
        target_lang: str,
        correction: Correction,
        model: str | None = None,
        translated: str | None = None,
    ) -> None:
        """Enregistre la correction d'une bulle ; remplace celle qui s'y appliquait déjà.
        ``model`` et ``translated`` : modèle affiché et sa traduction (corpus des corrections)."""
        image = sha1(image_url)
        self._delete_correction(image, target_lang, correction.box, correction.original)
        self._db.execute(
            "INSERT INTO correction (image, target_lang, box, original, text, created,"
            " image_url, series_id, episode_id, image_index, model, translated)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                image,
                target_lang,
                correction.box.model_dump_json(),
                correction.original,
                correction.text,
                time.time(),
                image_url,
                series_id,
                episode_id,
                image_index,
                model,
                translated,
            ),
        )
        self._db.commit()

    @_threaded
    def delete_correction(self, image_url: str, target_lang: str, box: Box, original: str) -> int:
        """Efface la correction de la bulle ; renvoie le nombre de corrections supprimées."""
        removed = self._delete_correction(sha1(image_url), target_lang, box, original)
        self._db.commit()
        return removed

    # --- Glossaires ---

    def _get_glossary(self, series_id: str) -> list[GlossaryEntry]:
        row = self._db.execute(
            "SELECT entries FROM glossary WHERE series_id = ?", (series_id,)
        ).fetchone()
        return [GlossaryEntry.model_validate(e) for e in json.loads(row[0])] if row else []

    get_glossary = _threaded(_get_glossary)

    @_threaded
    def put_glossary(self, series_id: str, entries: list[GlossaryEntry]) -> int:
        """Remplace le glossaire ; efface les traductions en cache des images de la série
        qui mentionnent un terme ajouté, retiré ou modifié. Renvoie leur nombre."""
        changed = changed_terms(self._get_glossary(series_id), entries)
        data = json.dumps([e.model_dump() for e in entries], ensure_ascii=False)
        self._db.execute("INSERT OR REPLACE INTO glossary VALUES (?, ?)", (series_id, data))
        self._glossary_versions[series_id] = self.glossary_version(series_id) + 1
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

    # --- Vidage ---

    @_threaded
    def clear(self, series_id: str | None = None) -> int:
        """Vide le cache (OCR et traductions ; les glossaires, les corrections et les
        résumés sont conservés)."""
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
        self._shrink()
        return removed
