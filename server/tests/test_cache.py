import asyncio
import sqlite3

import pytest

from phylactere import cache as cache_module
from phylactere.cache import Cache, GlossaryEntry, TranslatedBlock, TranslationResult
from phylactere.schemas import Box, Bubble, ImageSize, OcrResult


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / "sub" / "cache.sqlite3")
    yield c
    c.close()


def run(coro):
    return asyncio.run(coro)


def result(*pairs) -> TranslationResult:
    return TranslationResult(
        blocks=[
            TranslatedBlock(box=Box(x=0, y=i, w=1, h=1), original=o, translated=t)
            for i, (o, t) in enumerate(pairs)
        ],
        image_size=ImageSize(w=690, h=1600),
    )


def put(cache, index, *pairs, episode="66", lang="fr", model="m", series="s"):
    run(
        cache.put_translation(
            f"{series}{episode}{index}{lang}{model}",
            series,
            episode,
            index,
            lang,
            model,
            result(*pairs),
        )
    )


def test_ocr_roundtrip(cache):
    ocr = OcrResult(
        image_size=ImageSize(w=690, h=1600),
        bubbles=[Bubble(box=Box(x=1, y=2, w=3, h=4), text="안녕", lines=[], edges=["bottom"])],
    )
    assert run(cache.get_ocr("k")) is None
    run(cache.put_ocr("k", "s", ocr))
    assert run(cache.get_ocr("k")) == ocr


def test_translation_roundtrip(cache):
    put(cache, 0, ("안녕", "Salut"))
    assert run(cache.get_translation("s660frm")) == result(("안녕", "Salut"))


def test_context_is_previous_images_in_reading_order(cache):
    put(cache, 2, ("c", "C"), ("d", "D"))
    put(cache, 0, ("a", "A"))
    put(cache, 5, ("futur", "F"))  # après l'image demandée : exclu
    put(cache, 1, ("b", "B"))

    assert run(cache.context("s", "66", 3, "fr", "m", 15)) == [
        ("a", "A"),
        ("b", "B"),
        ("c", "C"),
        ("d", "D"),
    ]


def test_context_keeps_the_most_recent_bubbles(cache):
    put(cache, 0, ("a", "A"), ("b", "B"))
    put(cache, 1, ("c", "C"), ("d", "D"))
    assert run(cache.context("s", "66", 2, "fr", "m", 3)) == [("b", "B"), ("c", "C"), ("d", "D")]
    assert run(cache.context("s", "66", 2, "fr", "m", 0)) == []


def test_context_is_limited_to_episode_language_and_model(cache):
    put(cache, 0, ("autre épisode", "x"), episode="65")
    put(cache, 0, ("autre langue", "x"), lang="en")
    put(cache, 0, ("autre modèle", "x"), model="m2")
    put(cache, 0, ("autre série", "x"), series="s2")
    put(cache, 0, ("bon", "ok"))
    assert run(cache.context("s", "66", 1, "fr", "m", 15)) == [("bon", "ok")]


def test_context_keeps_one_translation_per_image(cache):
    # Même image traduite avec deux versions du prompt (clés différentes) : la plus récente.
    run(cache.put_translation("ancien", "s", "66", 0, "fr", "m", result(("a", "ancien"))))
    run(cache.put_translation("nouveau", "s", "66", 0, "fr", "m", result(("a", "nouveau"))))
    assert run(cache.context("s", "66", 1, "fr", "m", 15)) == [("a", "nouveau")]


def test_translation_started_before_a_glossary_change_is_not_cached(cache):
    version = cache.glossary_version("s")
    run(cache.put_glossary("s", [GlossaryEntry(ko="가", fr="Ga")]))
    run(cache.put_translation("k", "s", "66", 0, "fr", "m", result(("가", "A")), version))
    assert run(cache.get_translation("k")) is None

    version = cache.glossary_version("s")
    run(cache.put_translation("k", "s", "66", 0, "fr", "m", result(("가", "Ga")), version))
    assert run(cache.get_translation("k")) is not None


def test_glossary_roundtrip_and_invalidates_images_mentioning_the_term(cache):
    put(cache, 0, ("재웅아 가자", "Allez, Réung"))
    put(cache, 1, ("안녕", "Salut"))
    put(cache, 2, ("재 웅", "Réung"))  # espace ajoutée par l'OCR (deux lignes)
    put(cache, 0, ("재웅", "x"), series="s2")
    entries = [GlossaryEntry(ko="재웅", fr="Jae-woong", note="héros")]

    assert run(cache.put_glossary("s", entries)) == 2

    assert run(cache.get_glossary("s")) == entries
    assert run(cache.get_glossary("inconnue")) == []
    assert run(cache.get_translation("s660frm")) is None
    assert run(cache.get_translation("s662frm")) is None
    assert run(cache.get_translation("s661frm")) is not None  # sans le terme : gardée
    assert run(cache.get_translation("s2660frm")) is not None  # autre série


def test_unchanged_glossary_entries_keep_translations(cache):
    run(cache.put_glossary("s", [GlossaryEntry(ko="재웅", fr="Jae-woong")]))
    put(cache, 0, ("재웅아", "Jae-woong !"))
    put(cache, 1, ("꼬랑지!!", "Petite queue !!"))

    # Même glossaire, réordonné, plus une entrée : seule l'image du nouveau terme part.
    entries = [
        GlossaryEntry(ko="꼬랑지", fr="Petite Queue"),
        GlossaryEntry(ko=" 재웅", fr="Jae-woong"),
    ]
    assert run(cache.put_glossary("s", entries)) == 1
    assert run(cache.get_translation("s660frm")) is not None
    assert run(cache.get_translation("s661frm")) is None

    # Note modifiée : elle entre dans le prompt, l'image est retraduite.
    put(cache, 1, ("꼬랑지!!", "Petite Queue !!"))
    entries[0] = GlossaryEntry(ko="꼬랑지", fr="Petite Queue", note="surnom")
    assert run(cache.put_glossary("s", entries)) == 1

    # Terme retiré : l'image qui l'utilisait est retraduite sans lui.
    assert run(cache.put_glossary("s", entries[:1])) == 1
    assert run(cache.get_translation("s660frm")) is None


def test_clear_series_or_all(cache):
    put(cache, 0, ("a", "A"))
    put(cache, 0, ("b", "B"), series="s2")
    ocr = OcrResult(image_size=ImageSize(w=1, h=1), bubbles=[])
    run(cache.put_ocr("o1", "s", ocr))
    run(cache.put_ocr("o2", "s2", ocr))
    run(cache.put_glossary("s", [GlossaryEntry(ko="가", fr="A")]))

    assert run(cache.clear("s")) == 2  # traduction (sans le terme « 가 ») et OCR de « s »
    assert run(cache.get_ocr("o1")) is None and run(cache.get_ocr("o2")) is not None
    assert run(cache.clear()) == 2
    assert run(cache.get_glossary("s")) != []  # les glossaires sont conservés


def test_series_bubbles_start_with_the_given_episode(cache, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr("phylactere.cache.time.time", lambda: float(next(clock)))
    put(cache, 0, ("a1", "A1"), episode="1")
    put(cache, 1, ("a2", "A2"), episode="1")
    put(cache, 0, ("c1", "C1"), episode="3")
    put(cache, 0, ("b1", "B1"), ("a1", "A1 bis"), episode="2")  # doublon de a1
    put(cache, 1, ("x", "X"), episode="2", lang="en")  # autre langue
    put(cache, 0, ("y", "Y"), episode="2", series="autre")  # autre série
    put(cache, 0, ("b1", "B1 m2"), episode="2", model="m2")  # même image, autre modèle

    bubbles = run(cache.series_bubbles("s", "fr", "1", 10))
    assert bubbles == [("a1", "A1"), ("a2", "A2"), ("b1", "B1 m2"), ("c1", "C1")]

    # Sans épisode : du plus récemment traduit au plus ancien ; limite respectée.
    assert run(cache.series_bubbles("s", "fr", None, 3)) == [
        ("b1", "B1 m2"),
        ("a1", "A1 bis"),
        ("c1", "C1"),
    ]
    assert run(cache.series_bubbles("vide", "fr", None, 10)) == []


# --- Taille bornée (jalon 12) ---


def big_result(seed: int) -> TranslationResult:
    """Traduction d'environ 20 ko, incompressible pour SQLite (texte différent)."""
    text = "".join(chr(0xAC00 + (seed * 7919 + i * 31) % 11172) for i in range(7000))
    return result((text, str(seed)))


def put_big(cache, i: int) -> None:
    run(cache.put_translation(f"k{i}", "s", "1", i, "fr", "m", big_result(i)))


def file_size(path) -> int:
    wal = path.with_name(path.name + "-wal")
    return path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


def test_purge_removes_least_recently_used(tmp_path, monkeypatch):
    path = tmp_path / "cache.sqlite3"
    c = Cache(path, max_bytes=600_000)
    monkeypatch.setattr(cache_module, "PURGE_EVERY", 10**9)  # purge appelée à la main
    run(c.put_glossary("s", [GlossaryEntry(ko="가", fr="A")]))
    for i in range(60):
        put_big(c, i)
    run(c.get_translation("k0"))  # k0 devient la plus récemment utilisée

    removed = run(c.purge())

    stats = run(c.stats())
    assert removed > 0
    assert stats["size_bytes"] <= 600_000 * cache_module.PURGE_TARGET
    assert run(c.get_translation("k0")) is not None
    assert run(c.get_translation("k1")) is None  # la moins récemment utilisée
    assert run(c.get_translation("k59")) is not None
    assert run(c.get_glossary("s")) == [GlossaryEntry(ko="가", fr="A")]
    c.close()
    assert file_size(path) <= 700_000  # la place est rendue au disque


def test_purge_runs_every_n_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_module, "PURGE_EVERY", 5)
    c = Cache(tmp_path / "cache.sqlite3", max_bytes=300_000)
    for i in range(60):
        put_big(c, i)
    # Au plus PURGE_EVERY - 1 écritures depuis la dernière purge.
    assert run(c.stats())["size_bytes"] <= 300_000 + 5 * 25_000
    assert run(c.get_translation("k59")) is not None
    c.close()


def test_no_limit_keeps_everything(tmp_path):
    c = Cache(tmp_path / "cache.sqlite3")
    for i in range(30):
        put_big(c, i)
    assert run(c.purge()) == 0
    assert run(c.stats())["translations"] == 30
    c.close()


def test_old_database_gets_used_column_and_incremental_vacuum(tmp_path):
    """Base d'avant le jalon 12 : pas de colonne used, pas d'auto_vacuum."""
    path = tmp_path / "cache.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE ocr (key TEXT PRIMARY KEY, series_id TEXT, result TEXT NOT NULL,
                          created REAL NOT NULL);
        CREATE TABLE translation (key TEXT PRIMARY KEY, series_id TEXT NOT NULL,
            episode_id TEXT NOT NULL, image_index INTEGER NOT NULL, target_lang TEXT NOT NULL,
            model TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
        """
    )
    db.execute(
        "INSERT INTO translation VALUES ('old', 's', '1', 0, 'fr', 'm', ?, 123.0)",
        (result(("가", "A")).model_dump_json(),),
    )
    db.commit()
    db.close()

    c = Cache(path)
    assert run(c.get_translation("old")) == result(("가", "A"))
    put(c, 1, ("나", "B"))
    c.close()

    db = sqlite3.connect(path)
    assert db.execute("PRAGMA auto_vacuum").fetchone() == (2,)
    used = dict(db.execute("SELECT key, used FROM translation"))
    db.close()
    assert used["old"] > 123.0  # mis à jour par la lecture
    assert used["s661frm"] is not None


def test_clear_gives_space_back(tmp_path):
    path = tmp_path / "cache.sqlite3"
    c = Cache(path)
    for i in range(40):
        put_big(c, i)
    before = run(c.stats())["size_bytes"]
    run(c.clear())
    assert run(c.stats())["size_bytes"] < before / 10
    c.close()


def test_stats_counts(cache):
    put(cache, 0, ("가", "A"))
    run(cache.put_glossary("s", [GlossaryEntry(ko="나", fr="B")]))
    stats = run(cache.stats())
    assert (stats["ocr"], stats["translations"], stats["glossaries"]) == (0, 1, 1)
    assert stats["max_bytes"] == 0
