"""POST /translate, /glossary et /cache de bout en bout (OCR, images et Ollama simulés)."""

import json
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phylactere.config import load_settings
from phylactere.main import TOKEN_HEADER, create_app
from tests.helpers import FakeDetector, line

BASE = "https://image-comic.pstatic.net/webtoon/1/2/"


def png(width=690, height=1000) -> bytes:  # une seule tranche OCR
    out = BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


class World:
    """Serveur d'images, Ollama et PaddleOCR simulés, avec compteurs."""

    def __init__(self, bubbles=("안녕", "가자")):
        self.image_requests: list[str] = []
        self.ollama_bodies: list[dict] = []
        self.ollama_down = False
        self.bubbles = bubbles
        self.detector_calls = 0

    def images(self, request):
        self.image_requests.append(str(request.url))
        return httpx.Response(200, content=png())

    def ollama(self, request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if self.ollama_down:
            raise httpx.ConnectError("refusé", request=request)
        body = json.loads(request.content)
        self.ollama_bodies.append(body)
        bubbles = json.loads(body["messages"][1]["content"].rsplit("\n", 1)[1])
        content = {"translations": [{"id": b["id"], "text": f"FR:{b['texte']}"} for b in bubbles]}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    def detector(self):
        world = self

        class Detector(FakeDetector):
            def detect(self, image_bgr):
                world.detector_calls += 1
                return [
                    line(100, 400 + 200 * i, 200, text=text) for i, text in enumerate(world.bubbles)
                ]

        return Detector()


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(world):
    app = create_app(
        load_settings(),
        ollama_transport=httpx.MockTransport(world.ollama),
        images_transport=httpx.MockTransport(world.images),
        detector_factory=world.detector,
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.app.state.ocr._executor.submit(lambda: None).result(5)
        c.headers[TOKEN_HEADER] = load_settings().server.token
        yield c


def request(index=0, **extra):
    return {
        "image_url": f"{BASE}{index}.jpg",
        "series_id": "729047",
        "episode_id": "66",
        "index": index,
        **extra,
    }


def test_translate_returns_blocks_and_timings(client, world):
    response = client.post("/translate", json=request())

    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is False
    assert body["image_size"] == {"w": 690, "h": 1000}
    assert [(b["original"], b["translated"]) for b in body["blocks"]] == [
        ("안녕", "FR:안녕"),
        ("가자", "FR:가자"),
    ]
    assert body["blocks"][0]["box"] == {"x": 100, "y": 400, "w": 200, "h": 30}
    assert set(body["timings_ms"]) == {"fetch", "ocr", "translate"}
    assert len(world.ollama_bodies) == 1


def test_second_call_comes_from_cache(client, world):
    client.post("/translate", json=request())
    response = client.post("/translate", json=request())

    assert response.json()["cached"] is True
    assert response.json()["timings_ms"] == {"fetch": 0, "ocr": 0, "translate": 0}
    assert len(world.ollama_bodies) == 1 and world.detector_calls == 1


def test_other_language_reuses_ocr_but_translates_again(client, world):
    client.post("/translate", json=request())
    response = client.post("/translate", json=request(target_lang="en"))

    assert response.json()["cached"] is False
    assert world.detector_calls == 1  # OCR en cache
    assert len(world.ollama_bodies) == 2
    assert "anglais" in world.ollama_bodies[1]["messages"][0]["content"]


def test_previous_images_give_context(client, world):
    client.post("/translate", json=request(index=0))
    client.post("/translate", json=request(index=1))

    user = world.ollama_bodies[1]["messages"][1]["content"]
    assert "안녕 → FR:안녕" in user and "가자 → FR:가자" in user
    assert "précédentes" not in world.ollama_bodies[0]["messages"][1]["content"]


def test_neighbors_are_downloaded(client, world):
    client.post(
        "/translate",
        json=request(index=1, prev_image_url=f"{BASE}0.jpg", next_image_url=f"{BASE}2.jpg"),
    )
    assert sorted(world.image_requests) == [f"{BASE}0.jpg", f"{BASE}1.jpg", f"{BASE}2.jpg"]


def test_images_are_downloaded_once(client, world):
    client.post("/translate", json=request(index=0, next_image_url=f"{BASE}1.jpg"))
    client.post(
        "/translate",
        json=request(index=1, prev_image_url=f"{BASE}0.jpg", next_image_url=f"{BASE}2.jpg"),
    )
    assert sorted(world.image_requests) == [f"{BASE}0.jpg", f"{BASE}1.jpg", f"{BASE}2.jpg"]


def test_unreadable_neighbor_is_ignored(client, world):
    def images(request):
        if request.url.path.endswith("2.jpg"):
            return httpx.Response(403)
        return httpx.Response(200, content=png())

    client.app.state.images._transport = httpx.MockTransport(images)
    response = client.post("/translate", json=request(index=1, next_image_url=f"{BASE}2.jpg"))
    assert response.status_code == 200


def test_image_without_text_skips_ollama(client, world):
    world.bubbles = ()
    response = client.post("/translate", json=request())

    assert response.status_code == 200
    assert response.json()["blocks"] == []
    assert world.ollama_bodies == []


@pytest.mark.parametrize("field", ["image_url", "prev_image_url", "next_image_url"])
def test_urls_must_be_allowed(client, field):
    response = client.post("/translate", json=request(**{field: "https://evil.example/a.jpg"}))
    assert response.status_code == 400


def test_ollama_down_gives_503(client, world):
    world.ollama_down = True
    response = client.post("/translate", json=request())
    assert response.status_code == 503
    assert "Ollama" in response.json()["detail"]


def test_failed_translation_is_not_cached(client, world):
    world.ollama_down = True
    client.post("/translate", json=request())
    world.ollama_down = False
    response = client.post("/translate", json=request())
    assert response.status_code == 200 and response.json()["cached"] is False
    assert world.detector_calls == 1  # l'OCR, lui, était en cache


def test_translate_requires_token(client):
    del client.headers[TOKEN_HEADER]
    assert client.post("/translate", json=request()).status_code == 401


@pytest.mark.parametrize(
    "patch", [{"index": -1}, {"series_id": ""}, {"priority": "urgent"}, {"image_url": None}]
)
def test_invalid_requests(client, patch):
    assert client.post("/translate", json=request() | patch).status_code == 422


def test_glossary_roundtrip_and_used_in_prompt(client, world):
    entries = [{"ko": "재웅", "fr": "Jae-woong", "note": "héros"}]

    assert client.get("/glossary/729047").json() == []
    assert client.put("/glossary/729047", json=entries).json() == entries
    assert client.get("/glossary/729047").json() == entries

    client.post("/translate", json=request())
    assert "재웅 → Jae-woong (héros)" in world.ollama_bodies[0]["messages"][1]["content"]


def test_new_glossary_retranslates(client, world):
    client.post("/translate", json=request())
    client.put("/glossary/729047", json=[{"ko": "가", "fr": "A"}])
    assert client.post("/translate", json=request()).json()["cached"] is False


def test_clear_cache(client, world):
    client.post("/translate", json=request())
    assert client.delete("/cache?series_id=autre").json() == {"removed": 0}
    assert client.delete("/cache?series_id=729047").json()["removed"] == 2  # OCR + traduction
    client.post("/translate", json=request())
    assert world.detector_calls == 2
    assert client.delete("/cache").json()["removed"] == 2
