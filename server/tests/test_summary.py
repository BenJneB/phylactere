"""Résumé « Précédemment… » (jalon 15) : POST /summary de bout en bout (OCR, images et
Ollama simulés), et découpage par tranches."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from phylactere.config import SummaryConfig, load_settings
from phylactere.main import TOKEN_HEADER, create_app
from phylactere.summary import (
    format_images,
    split_chunks,
    summarize,
    summary_user_prompt,
)
from phylactere.translate import OllamaTranslator
from tests.test_translate_api import World, request


class SummaryWorld(World):
    """Ollama simulé qui répond aussi aux demandes de résumé (schéma ``summary``)."""

    def __init__(self, bubbles=("안녕", "가자")):
        super().__init__(bubbles)
        self.summary_bodies: list[dict] = []
        self.summary_reply: str | None = None  # réponse imposée (défaut : « Résumé n. »)

    def ollama(self, request):
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            if body.get("format", {}).get("properties", {}).get("summary"):
                self.summary_bodies.append(body)
                text = self.summary_reply
                if text is None:
                    text = f" Résumé {len(self.summary_bodies)}. "
                content = {"summary": text}
                return httpx.Response(200, json={"message": {"content": json.dumps(content)}})
        return super().ollama(request)

    def summary_prompt(self, n: int = -1) -> str:
        return self.summary_bodies[n]["messages"][1]["content"]


@pytest.fixture
def world():
    return SummaryWorld()


def make_client(world, settings=None):
    app = create_app(
        settings or load_settings(),
        ollama_transport=httpx.MockTransport(world.ollama),
        images_transport=httpx.MockTransport(world.images),
        detector_factory=world.detector,
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    client.headers[TOKEN_HEADER] = load_settings().server.token
    return client


@pytest.fixture
def client(world):
    with make_client(world) as c:
        c.app.state.ocr.wait_loaded(5)
        yield c


def summary(episode_id="66", **extra):
    return {"series_id": "729047", "episode_id": episode_id, **extra}


def translate(client, *indexes, **extra):
    for index in indexes:
        assert client.post("/translate", json=request(index, **extra)).status_code == 200


def test_summary_lists_bubbles_in_reading_order(client, world):
    # Images traduites dans le désordre : le prompt suit l'ordre de l'épisode.
    translate(client, 2, 0, 1, image_count=3)

    response = client.post("/summary", json=summary())

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "Résumé 1."
    assert body["cached"] is False
    assert body["partial"] is False
    assert (body["images"], body["bubbles"]) == (3, 6)
    assert body["model"] == load_settings().ollama.model
    prompt = world.summary_prompt()
    assert prompt.index("Image 1 :") < prompt.index("Image 2 :") < prompt.index("Image 3 :")
    assert "Image 1 :\n- FR:안녕\n- FR:가자" in prompt
    assert "lu qu'une partie" not in prompt
    # Seules les traductions partent : pas le coréen seul.
    assert "- 안녕" not in prompt
    sent = world.summary_bodies[0]
    assert sent["options"]["num_ctx"] == load_settings().summary.num_ctx
    assert "en français" in sent["messages"][0]["content"]


def test_summary_is_cached(client, world):
    translate(client, 0, 1)
    client.post("/summary", json=summary())

    response = client.post("/summary", json=summary())

    assert response.json()["cached"] is True
    assert response.json()["summary"] == "Résumé 1."
    assert len(world.summary_bodies) == 1
    assert client.get("/stats").json()["cache"]["summaries"] == 1


def test_summary_without_bubbles_is_404(client, world):
    translate(client, 0)  # autre épisode

    response = client.post("/summary", json=summary(episode_id="65"))

    assert response.status_code == 404
    assert "pas été lu" in response.json()["detail"]
    assert world.summary_bodies == []


def test_episode_read_in_part(client, world):
    # Moins d'images en cache que dans l'épisode (nombre envoyé avec chaque image).
    translate(client, 0, 1, image_count=5)

    body = client.post("/summary", json=summary()).json()

    assert body["partial"] is True
    assert "n'a lu qu'une partie" in world.summary_prompt()


def test_gap_in_images_means_read_in_part(client, world):
    translate(client, 0, 2)

    body = client.post("/summary", json=summary()).json()

    assert body["partial"] is True
    assert "Image 1 :" in world.summary_prompt() and "Image 3 :" in world.summary_prompt()


def test_summary_redone_when_bubbles_change(client, world):
    translate(client, 0, image_count=2)
    client.post("/summary", json=summary())  # lu en partie

    # Fin de l'épisode lue ensuite : nouvelles bulles, résumé refait (et complet).
    translate(client, 1, image_count=2)
    body = client.post("/summary", json=summary()).json()

    assert body["cached"] is False
    assert body["partial"] is False
    assert body["summary"] == "Résumé 2."
    assert len(world.summary_bodies) == 2


def test_image_count_learned_later_redoes_summary(client, world):
    # Épisode lu avant le jalon 17 : nombre d'images inconnu, résumé complet.
    translate(client, 0, 1)
    assert client.post("/summary", json=summary()).json()["partial"] is False

    # Nombre d'images appris ensuite (image servie par le cache) : épisode lu en partie.
    translate(client, 0, image_count=4)
    body = client.post("/summary", json=summary()).json()

    assert body["cached"] is False
    assert body["partial"] is True
    assert client.post("/summary", json=summary()).json()["cached"] is True


def test_corrections_are_summarized(client, world):
    translate(client, 0)
    box = client.post("/translate", json=request(0)).json()["blocks"][0]["box"]
    client.put(
        "/correction",
        json={**request(0), "box": box, "original": "안녕", "text": "Salut, toi !"},
    )

    client.post("/summary", json=summary())

    assert "- Salut, toi !\n- FR:가자" in world.summary_prompt()


def test_summary_kept_after_cache_is_cleared(client, world):
    translate(client, 0, 1)
    client.post("/summary", json=summary())

    client.delete("/cache")
    response = client.post("/summary", json=summary())

    assert response.status_code == 200
    assert response.json()["summary"] == "Résumé 1."
    assert response.json()["cached"] is True


def test_episode_read_with_another_model(client, world):
    translate(client, 0, 1, model="petit")

    body = client.post("/summary", json=summary(model="gros")).json()

    # Bulles du modèle qui a traduit l'épisode, résumé par le modèle demandé.
    assert "- FR:안녕" in world.summary_prompt()
    assert world.summary_bodies[0]["model"] == "gros"
    assert body["model"] == "gros"


def test_summary_requires_token(client):
    response = client.post("/summary", json=summary(), headers={TOKEN_HEADER: "faux"})

    assert response.status_code == 401


def test_invalid_summary_response_is_retried_then_502(client, world):
    translate(client, 0)
    world.summary_reply = " "

    response = client.post("/summary", json=summary())

    assert response.status_code == 502
    assert len(world.summary_bodies) == 2


# --- Découpage par tranches ---


def test_split_chunks_keeps_images_whole():
    images = [(i, ["x" * 100]) for i in range(5)]

    chunks = split_chunks(images, 250)

    assert [[index for index, _ in chunk] for chunk in chunks] == [[0, 1], [2, 3], [4]]
    # Une image plus longue que la limite forme une tranche à elle seule.
    assert split_chunks([(0, ["x" * 500]), (1, ["y"])], 250) == [[(0, ["x" * 500])], [(1, ["y"])]]
    assert split_chunks([], 250) == []


def test_format_images_marks_images_without_text():
    assert format_images([(0, []), (1, ["Oui", "Non"])]) == (
        "Image 1 :\n(pas de texte)\n\nImage 2 :\n- Oui\n- Non"
    )


def test_user_prompt_for_a_chunk():
    prompt = summary_user_prompt([(0, ["Oui"])], "fr", partial=False, part=(2, 3))

    assert prompt.startswith("Tranche 2 sur 3 de l'épisode")


def test_long_episode_is_summarized_in_chunks():
    calls: list[str] = []

    class Translator:
        chat_json = OllamaTranslator.chat_json

        async def chat(self, system, user, model, schema, num_ctx, timeout_s):
            calls.append(user)
            return json.dumps({"summary": f"S{len(calls)}"})

    images = [(i, [f"bulle {i} " + "x" * 400]) for i in range(6)]
    cfg = SummaryConfig(num_ctx=0, timeout_s=10, chunk_chars=1000)

    text = asyncio.run(summarize(Translator(), images, "fr", "m", partial=False, cfg=cfg))

    # 6 images de ~420 caractères : 3 tranches de 2, puis le résumé des résumés.
    assert len(calls) == 4
    assert "Tranche 1 sur 3" in calls[0] and "bulle 0" in calls[0] and "bulle 2" not in calls[0]
    assert "Tranche 1 : S1\n\nTranche 2 : S2\n\nTranche 3 : S3" in calls[3]
    assert text == "S4"
