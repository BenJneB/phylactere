"""Propositions de glossaire (jalon 10) : garde-fous et POST /glossary/{id}/suggest
(OCR, images et Ollama simulés)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from phylactere.cache import GlossaryEntry
from phylactere.config import load_settings
from phylactere.glossary import MAX_SUGGESTIONS, SUGGEST_SCHEMA, _RawEntry, filter_suggestions
from phylactere.main import TOKEN_HEADER, create_app
from tests.test_translate_api import World, request

BUBBLES = [
    ("민수가 왔어!", "Min-su est là !"),
    ("선배님 안녕하세요", "Bonjour, senpai"),
    ("민수야 가자", "Allez, Min-su"),
    ("민수 진짜?", "Min-su, vraiment ?"),
]


def raw(ko, fr="X", note=""):
    return _RawEntry(ko=ko, fr=fr, note=note)


def test_invented_terms_are_dropped():
    kept = filter_suggestions([raw("민수", "Min-su"), raw("지훈", "Ji-hun")], BUBBLES, [])
    assert [e.ko for e in kept] == ["민수"]


def test_terms_already_in_glossary_or_repeated_are_dropped():
    glossary = [GlossaryEntry(ko="선배님", fr="senpai")]
    entries = [raw(" 선배님 "), raw("민수", "Min-su"), raw("민수", "Minsu"), raw("민수야", "")]
    kept = filter_suggestions(entries, BUBBLES, glossary)
    # Déjà au glossaire, doublon, traduction vide : écartés.
    assert [(e.ko, e.fr) for e in kept] == [("민수", "Min-su")]


def test_examples_show_at_most_two_bubbles():
    [entry] = filter_suggestions([raw("민수", "Min-su", " héros ")], BUBBLES, [])
    assert entry.note == "héros"
    assert [(x.original, x.translated) for x in entry.examples] == BUBBLES[0:1] + BUBBLES[2:3]


def test_at_most_thirty_suggestions():
    bubbles = [(f"이름{i}", f"Nom {i}") for i in range(50)]
    entries = [raw(f"이름{i}", f"Nom {i}") for i in range(50)]
    kept = filter_suggestions(entries, bubbles, [])
    assert len(kept) == MAX_SUGGESTIONS == 30
    assert kept[0].ko == "이름0"


class SuggestWorld(World):
    """Ollama simulé qui répond aussi aux demandes de propositions (format « entries »)."""

    def __init__(self):
        super().__init__(bubbles=("민수가 왔어", "선배님 안녕"))
        self.suggest_bodies: list[dict] = []
        self.suggest_replies: list[str] = []

    def ollama(self, request):
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            if body.get("format") == SUGGEST_SCHEMA:
                self.suggest_bodies.append(body)
                content = self.suggest_replies.pop(0)
                return httpx.Response(200, json={"message": {"content": content}})
        return super().ollama(request)


def entries_json(*entries):
    return json.dumps({"entries": [{"ko": k, "fr": f, "note": n} for k, f, n in entries]})


@pytest.fixture
def world():
    return SuggestWorld()


@pytest.fixture
def client(world):
    app = create_app(
        load_settings(),
        ollama_transport=httpx.MockTransport(world.ollama),
        images_transport=httpx.MockTransport(world.images),
        detector_factory=world.detector,
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.app.state.ocr.wait_loaded(5)
        c.headers[TOKEN_HEADER] = load_settings().server.token
        yield c


def test_suggest_without_cached_bubbles_is_404(client, world):
    response = client.post("/glossary/729047/suggest", json={})
    assert response.status_code == 404
    assert "lisez d'abord un épisode" in response.json()["detail"]
    assert world.suggest_bodies == []


def test_suggest_returns_filtered_entries_without_saving(client, world):
    client.put("/glossary/729047", json=[{"ko": "선배님", "fr": "senpai"}])
    assert client.post("/translate", json=request()).status_code == 200
    world.suggest_replies.append(
        entries_json(("민수", "Min-su", "héros"), ("선배님", "aîné", ""), ("지훈", "Ji-hun", ""))
    )

    response = client.post("/glossary/729047/suggest", json={"episode_id": "66"})

    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            {
                "ko": "민수",
                "fr": "Min-su",
                "note": "héros",
                "examples": [{"original": "민수가 왔어", "translated": "FR:민수가 왔어"}],
            }
        ],
        "bubbles": 2,
    }
    [body] = world.suggest_bodies
    settings = load_settings()
    assert body["model"] == settings.ollama.model
    assert body["options"]["num_ctx"] == settings.glossary.num_ctx
    user = body["messages"][1]["content"]
    assert "- 민수가 왔어 → FR:민수가 왔어" in user
    assert "선배님 → senpai" in user  # glossaire actuel, à ne pas reprendre
    # Rien n'est enregistré.
    assert client.get("/glossary/729047").json() == [{"ko": "선배님", "fr": "senpai", "note": ""}]


def test_suggest_uses_requested_language_and_model(client, world):
    client.post("/translate", json=request(target_lang="en"))
    world.suggest_replies.append(entries_json(("민수", "Minsu", "")))

    # Rien en cache en français pour cette série.
    assert client.post("/glossary/729047/suggest", json={}).status_code == 404
    response = client.post(
        "/glossary/729047/suggest", json={"target_lang": "en", "model": "gemma3:4b"}
    )
    assert response.status_code == 200
    assert world.suggest_bodies[0]["model"] == "gemma3:4b"
    assert "anglais" in world.suggest_bodies[0]["messages"][0]["content"]


def test_invalid_json_is_retried_once(client, world):
    client.post("/translate", json=request())
    world.suggest_replies += ["pas du JSON", entries_json(("민수", "Min-su", ""))]

    response = client.post("/glossary/729047/suggest", json={})
    assert response.status_code == 200
    assert [e["ko"] for e in response.json()["entries"]] == ["민수"]
    assert len(world.suggest_bodies) == 2


def test_invalid_json_twice_is_502(client, world):
    client.post("/translate", json=request())
    world.suggest_replies += ["pas du JSON", '{"entries": "non"}']

    response = client.post("/glossary/729047/suggest", json={})
    assert response.status_code == 502
    assert len(world.suggest_bodies) == 2


def test_suggest_requires_token(client):
    del client.headers[TOKEN_HEADER]
    assert client.post("/glossary/729047/suggest", json={}).status_code == 401
