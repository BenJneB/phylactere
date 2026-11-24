"""Variante « suivantes » du banc d'essai (scripts/bench.py), jalon 20 : Ollama simulé."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

from phylactere.config import default_settings
from phylactere.translate import OllamaTranslator
from tests.test_translate import FakeOllama

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SETTINGS = default_settings()


@pytest.fixture(scope="module")
def bench():
    sys.path.insert(0, str(SCRIPTS))  # bench.py importe translate_samples.py
    try:
        spec = importlib.util.spec_from_file_location("bench_script", SCRIPTS / "bench.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(SCRIPTS))


TEXTS = [["가", "나"], [], ["다"], ["라", "마"]]
FIRST = [["A", "B"], [], ["C"], ["D", "E"]]


def test_following_window_in_reading_order(bench):
    assert bench.following_window(TEXTS, FIRST, 0, 15) == [("다", "C"), ("라", "D"), ("마", "E")]
    assert bench.following_window(TEXTS, FIRST, 0, 2) == [("다", "C"), ("라", "D")]
    assert bench.following_window(TEXTS, FIRST, 3, 15) == []


def test_following_window_skips_failed_translations(bench):
    first = [["A", "B"], [], [""], ["D", "E"]]
    assert bench.following_window(TEXTS, first, 0, 15) == [("라", "D"), ("마", "E")]


def run_episode(bench, fake, **kwargs):
    async def go():
        async with httpx.AsyncClient(
            base_url="http://ollama", transport=httpx.MockTransport(fake)
        ) as client:
            calls: list[dict] = []
            translator = OllamaTranslator(
                client, SETTINGS.ollama, SETTINGS.translation, on_stats=calls.append
            )
            return await bench.translate_episode(translator, TEXTS, "fr", 15, calls, **kwargs)

    return asyncio.run(go())


def user_messages(fake) -> list[str]:
    return [body["messages"][1]["content"] for body in fake.bodies]


def test_base_sends_no_following(bench):
    fake = FakeOllama({1: "A", 2: "B"}, {1: "C"}, {1: "D", 2: "E"})

    episode = run_episode(bench, fake)

    assert episode["translations"] == FIRST
    assert not any("Bulles suivantes" in m for m in user_messages(fake))


def test_second_pass_context(bench):
    """Bulles précédentes : celles du second passage ; suivantes : celles du premier."""
    fake = FakeOllama({1: "A2", 2: "B2"}, {1: "C2"}, {1: "D2", 2: "E2"})

    episode = run_episode(bench, fake, first_pass=FIRST)

    assert episode["translations"] == [["A2", "B2"], [], ["C2"], ["D2", "E2"]]
    first, second, last = user_messages(fake)
    assert "Bulles précédentes" not in first
    assert "Bulles suivantes de l'épisode, avec une première traduction" in first
    assert "- 다 → C\n- 라 → D\n- 마 → E" in first
    assert "- 가 → A2\n- 나 → B2" in second  # second passage, pas « A » / « B »
    assert "- 라 → D\n- 마 → E" in second
    assert "- 다 → C\n" not in second  # l'image traduite n'est pas dans ses suivantes
    assert "Bulles suivantes" not in last  # dernière image : section absente
    assert json.loads(last.rsplit("\n", 1)[1]) == [
        {"id": 1, "texte": "라"},
        {"id": 2, "texte": "마"},
    ]


def test_without_context_sends_neither_section(bench):
    fake = FakeOllama({1: "A", 2: "B"}, {1: "C"}, {1: "D", 2: "E"})

    run_episode(bench, fake, use_context=False)

    assert not any("Bulles précédentes" in m for m in user_messages(fake))
