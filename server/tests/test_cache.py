import asyncio

import pytest

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


def test_glossary_roundtrip_and_invalidates_series_translations(cache):
    put(cache, 0, ("a", "A"))
    put(cache, 0, ("autre", "x"), series="s2")
    entries = [GlossaryEntry(ko="재웅", fr="Jae-woong", note="héros")]

    run(cache.put_glossary("s", entries))

    assert run(cache.get_glossary("s")) == entries
    assert run(cache.get_glossary("inconnue")) == []
    assert run(cache.get_translation("s660frm")) is None
    assert run(cache.get_translation("s2660frm")) is not None


def test_clear_series_or_all(cache):
    put(cache, 0, ("a", "A"))
    put(cache, 0, ("b", "B"), series="s2")
    ocr = OcrResult(image_size=ImageSize(w=1, h=1), bubbles=[])
    run(cache.put_ocr("o1", "s", ocr))
    run(cache.put_ocr("o2", "s2", ocr))
    run(cache.put_glossary("s", [GlossaryEntry(ko="가", fr="A")]))

    assert run(cache.clear("s")) == 1  # le glossaire a déjà effacé la traduction de « s »
    assert run(cache.get_ocr("o1")) is None and run(cache.get_ocr("o2")) is not None
    assert run(cache.clear()) == 2
    assert run(cache.get_glossary("s")) != []  # les glossaires sont conservés
