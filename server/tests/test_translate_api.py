"""POST /translate, /glossary et /cache de bout en bout (OCR, images et Ollama simulés)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from phylactere import pipeline
from phylactere.cache import TranslationResult
from phylactere.config import load_settings
from phylactere.main import TOKEN_HEADER, create_app
from tests.helpers import FakeDetector, line, png_bytes

BASE = "https://image-comic.pstatic.net/webtoon/1/2/"


# Une seule tranche OCR.
IMAGE_SIZE = (690, 1000)


class World:
    """Serveur d'images, Ollama et PaddleOCR simulés, avec compteurs."""

    def __init__(self, bubbles=("안녕", "가자")):
        self.image_requests: list[str] = []
        self.ollama_bodies: list[dict] = []
        self.preloads: list[dict] = []
        self.ollama_down = False
        self.bubbles = bubbles
        self.detector_calls = 0
        # Préfixe des traductions par modèle (défaut « FR ») : montre qui a traduit.
        self.prefixes: dict[str, str] = {}

    def images(self, request):
        self.image_requests.append(str(request.url))
        return httpx.Response(200, content=png_bytes(IMAGE_SIZE))

    def ollama(self, request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if self.ollama_down:
            raise httpx.ConnectError("refusé", request=request)
        body = json.loads(request.content)
        if not body["messages"]:  # préchargement du modèle au démarrage
            self.preloads.append(body)
            return httpx.Response(200, json={})
        self.ollama_bodies.append(body)
        bubbles = json.loads(body["messages"][1]["content"].rsplit("\n", 1)[1])
        prefix = self.prefixes.get(body["model"], "FR")
        content = {
            "translations": [{"id": b["id"], "text": f"{prefix}:{b['texte']}"} for b in bubbles]
        }
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
        c.app.state.ocr.wait_loaded(5)
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
    # Image blanche : fond blanc et texte noir, pris sur l'image.
    assert [(b["bg"], b["fg"]) for b in body["blocks"]] == [("#ffffff", "#000000")] * 2
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


def test_model_chosen_by_extension(client, world):
    client.post("/translate", json=request())
    response = client.post("/translate", json=request(model="gemma3:4b"))

    assert response.json()["cached"] is False  # le cache dépend du modèle
    assert world.detector_calls == 1  # OCR en cache
    assert [b["model"] for b in world.ollama_bodies] == [load_settings().ollama.model, "gemma3:4b"]


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
        return httpx.Response(200, content=png_bytes(IMAGE_SIZE))

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


def test_new_glossary_retranslates_only_images_with_the_term(client, world):
    client.post("/translate", json=request())  # bulles « 안녕 » et « 가자 »
    client.put("/glossary/729047", json=[{"ko": "없음", "fr": "Rien"}])
    assert client.post("/translate", json=request()).json()["cached"] is True
    client.put("/glossary/729047", json=[{"ko": "가", "fr": "A"}])
    assert client.post("/translate", json=request()).json()["cached"] is False


def test_clear_cache(client, world):
    client.post("/translate", json=request())
    assert client.delete("/cache?series_id=autre").json() == {"removed": 0}
    assert client.delete("/cache?series_id=729047").json()["removed"] == 2  # OCR + traduction
    client.post("/translate", json=request())
    assert world.detector_calls == 2
    assert client.delete("/cache").json()["removed"] == 2


def test_model_is_preloaded_at_startup(client, world):
    import time

    deadline = time.monotonic() + 5  # le préchargement tourne en tâche de fond
    while not world.preloads and time.monotonic() < deadline:
        time.sleep(0.01)
    settings = load_settings()
    assert world.preloads == [
        {"model": settings.ollama.model, "messages": [], "keep_alive": settings.ollama.keep_alive}
    ]


def test_ocr_format_version_invalidates_ocr_cache(monkeypatch):
    settings = load_settings()
    before = pipeline.ocr_fingerprint(settings)
    monkeypatch.setattr(pipeline, "OCR_FORMAT_VERSION", pipeline.OCR_FORMAT_VERSION + 1)
    assert pipeline.ocr_fingerprint(settings) != before


def test_translation_cached_before_colors_has_no_colors():
    # Traduction mise en cache avant le jalon 9 : pas de bg/fg, l'extension garde le blanc.
    old = {
        "blocks": [{"box": {"x": 1, "y": 2, "w": 3, "h": 4}, "original": "가", "translated": "a"}],
        "image_size": {"w": 690, "h": 1600},
    }
    block = TranslationResult.model_validate(old).blocks[0]
    assert (block.bg, block.fg) == (None, None)


# --- Statistiques (jalon 12) ---


def test_stats_requires_token(client):
    assert client.get("/stats", headers={TOKEN_HEADER: "mauvais"}).status_code == 401


def test_stats_counts_images_cache_and_timings(client, world):
    client.post("/translate", json=request(0))
    client.post("/translate", json=request(0))  # depuis le cache
    client.post("/translate", json=request(1, target_lang="en"))
    client.post("/translate", json=request(1, target_lang="de"))  # OCR en cache

    body = client.get("/stats").json()

    assert body["images"] == {"served": 4, "cached": 1}
    # Téléchargement et OCR : seulement pour les images vraiment lues (pas l'OCR en cache).
    assert body["timings_ms"]["fetch"]["count"] == 2
    assert body["timings_ms"]["ocr"]["count"] == 2
    assert body["timings_ms"]["translate"]["count"] == 3
    timing = body["timings_ms"]["translate"]
    assert 0 <= timing["avg_ms"] <= timing["max_ms"]
    assert body["cache"]["ocr"] == 2
    assert body["cache"]["translations"] == 3
    assert body["cache"]["glossaries"] == 0
    assert body["cache"]["size_bytes"] > 0
    assert body["cache"]["max_bytes"] == load_settings().cache.max_mb * 1_000_000
    assert body["queues"]["translation"] == {
        "visible": 0,
        "prefetch": 0,
        "background": 0,
        "running": 0,
    }
    assert body["model"] == load_settings().ollama.model
    assert body["uptime_s"] >= 0


# --- Retraduction (jalon 13) ---


def default_model() -> str:
    # Lu à chaque test : la configuration dépend du dossier de test (conftest).
    return load_settings().ollama.model


# Boîte de la seconde bulle (« 가자 ») du détecteur simulé.
SECOND_BOX = {"x": 100, "y": 600, "w": 200, "h": 30}


def test_blocks_name_their_model(client):
    blocks = client.post("/translate", json=request()).json()["blocks"]
    assert [b["model"] for b in blocks] == [default_model()] * 2


def test_force_translates_again_with_cached_ocr(client, world):
    client.post("/translate", json=request())
    world.prefixes[default_model()] = "V2"

    response = client.post("/translate", json=request(force=True))

    assert response.json()["cached"] is False
    assert [b["translated"] for b in response.json()["blocks"]] == ["V2:안녕", "V2:가자"]
    assert len(world.ollama_bodies) == 2 and world.detector_calls == 1  # OCR en cache
    # La nouvelle traduction remplace l'ancienne dans le cache.
    cached = client.post("/translate", json=request()).json()
    assert cached["cached"] is True
    assert [b["translated"] for b in cached["blocks"]] == ["V2:안녕", "V2:가자"]


def test_force_with_retranslate_model_keeps_displayed_key(client, world):
    world.prefixes["gros:14b"] = "GROS"
    client.post("/translate", json=request())

    client.post("/translate", json=request(force=True, retranslate_model="gros:14b"))

    assert world.ollama_bodies[-1]["model"] == "gros:14b"
    # Rangée sous la clé du modèle affiché (celui de la configuration) : retrouvée au
    # rechargement de la page.
    blocks = client.post("/translate", json=request()).json()["blocks"]
    assert [(b["translated"], b["model"]) for b in blocks] == [
        ("GROS:안녕", "gros:14b"),
        ("GROS:가자", "gros:14b"),
    ]


def test_retranslate_changes_only_that_bubble(client, world):
    client.post("/translate", json=request())
    world.prefixes["gros:14b"] = "GROS"

    response = client.post(
        "/retranslate", json=request(box=SECOND_BOX, retranslate_model="gros:14b")
    )

    assert response.status_code == 200
    block = response.json()
    assert (block["original"], block["translated"], block["model"]) == (
        "가자",
        "GROS:가자",
        "gros:14b",
    )
    assert block["box"] == SECOND_BOX and block["bg"] == "#ffffff"
    # Une seule bulle envoyée, les autres bulles de l'image en contexte.
    body = world.ollama_bodies[-1]
    assert body["model"] == "gros:14b"
    user = body["messages"][1]["content"]
    assert "Autres bulles de la même image" in user and "안녕 → FR:안녕" in user
    assert json.loads(user.rsplit("\n", 1)[1]) == [{"id": 1, "texte": "가자"}]
    # Seule cette bulle a changé dans le cache, toujours sous la clé du modèle affiché.
    cached = client.post("/translate", json=request()).json()
    assert cached["cached"] is True
    assert [b["translated"] for b in cached["blocks"]] == ["FR:안녕", "GROS:가자"]


def test_retranslate_uses_previous_images_as_context(client, world):
    client.post("/translate", json=request(index=0))
    client.post("/translate", json=request(index=1))

    client.post("/retranslate", json=request(index=1, box=SECOND_BOX))

    user = world.ollama_bodies[-1]["messages"][1]["content"]
    assert "Bulles précédentes" in user
    assert world.ollama_bodies[-1]["model"] == default_model()  # défaut : modèle affiché


def test_retranslate_finds_shifted_box(client, world):
    client.post("/translate", json=request())
    shifted = SECOND_BOX | {"x": 104, "y": 598}
    block = client.post("/retranslate", json=request(box=shifted)).json()
    assert block["original"] == "가자" and block["box"] == SECOND_BOX


def test_retranslate_without_cached_translation_gives_404(client, world):
    response = client.post("/retranslate", json=request(box=SECOND_BOX))
    assert response.status_code == 404
    assert world.ollama_bodies == []


def test_retranslate_unknown_box_gives_404(client):
    client.post("/translate", json=request())
    far = {"x": 0, "y": 0, "w": 10, "h": 10}
    assert client.post("/retranslate", json=request(box=far)).status_code == 404


def test_retranslate_requires_token(client):
    del client.headers[TOKEN_HEADER]
    assert client.post("/retranslate", json=request(box=SECOND_BOX)).status_code == 401


# --- Corrections (jalon 14) ---


def correct(client, text="À la main", box=SECOND_BOX, original="가자", **extra):
    return client.put(
        "/correction",
        json=request(box=box, original=original, text=text, translated="FR:가자", **extra),
    )


def texts(response) -> list[tuple[str, bool]]:
    return [(b["translated"], b["corrected"]) for b in response.json()["blocks"]]


def test_correction_is_applied_whatever_the_model(client, world):
    client.post("/translate", json=request())
    response = correct(client)
    assert response.status_code == 200
    assert response.json() == {"box": SECOND_BOX, "original": "가자", "text": "À la main"}

    cached = client.post("/translate", json=request())
    assert cached.json()["cached"] is True
    assert texts(cached) == [("FR:안녕", False), ("À la main", True)]
    assert cached.json()["blocks"][1]["machine"] == "FR:가자"
    # Autre modèle : nouvelle traduction, même correction.
    other = client.post("/translate", json=request(model="gemma3:4b"))
    assert other.json()["cached"] is False
    assert texts(other) == [("FR:안녕", False), ("À la main", True)]


def test_correction_survives_clear_cache_and_new_prompt_version(client, world, monkeypatch):
    client.post("/translate", json=request())
    correct(client)
    client.delete("/cache")
    monkeypatch.setattr(pipeline, "PROMPT_ID", "nouveau-prompt")

    response = client.post("/translate", json=request())

    assert response.json()["cached"] is False and world.detector_calls == 2
    assert texts(response)[1] == ("À la main", True)


def test_correction_found_after_a_slight_box_shift(client, world):
    shifted = SECOND_BOX | {"x": 103, "y": 602}
    correct(client, box=shifted)
    assert texts(client.post("/translate", json=request()))[1] == ("À la main", True)


def test_correction_feeds_the_context_of_next_images(client, world):
    client.post("/translate", json=request(index=0))
    correct(client)
    client.post("/translate", json=request(index=1))
    user = world.ollama_bodies[-1]["messages"][1]["content"]
    assert "가자 → À la main" in user and "FR:가자" not in user


def test_delete_correction_brings_back_the_model_translation(client, world):
    client.post("/translate", json=request())
    correct(client)
    target = request(box=SECOND_BOX, original="가자")
    response = client.request("DELETE", "/correction", json=target)
    assert response.json() == {"removed": 1}
    assert texts(client.post("/translate", json=request()))[1] == ("FR:가자", False)
    assert client.request("DELETE", "/correction", json=target).json() == {"removed": 0}


def test_corrected_bubble_cannot_be_retranslated(client, world):
    client.post("/translate", json=request())
    correct(client)
    response = client.post("/retranslate", json=request(box=SECOND_BOX))
    assert response.status_code == 409
    assert "effacez la correction" in response.json()["detail"]
    assert len(world.ollama_bodies) == 1
    # L'autre bulle se retraduit, avec la correction dans son contexte.
    first = {"x": 100, "y": 400, "w": 200, "h": 30}
    assert client.post("/retranslate", json=request(box=first)).status_code == 200
    assert "가자 → À la main" in world.ollama_bodies[-1]["messages"][1]["content"]


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_correction_is_refused(client, text):
    assert correct(client, text=text).status_code == 422


def test_correction_refuses_foreign_url_and_missing_token(client):
    assert correct(client, image_url="https://evil.example/a.jpg").status_code == 400
    del client.headers[TOKEN_HEADER]
    assert correct(client).status_code == 401
    target = request(box=SECOND_BOX, original="가자")
    assert client.request("DELETE", "/correction", json=target).status_code == 401
