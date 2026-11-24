import asyncio
import json

import httpx
import pytest

from phylactere.cache import GlossaryEntry
from phylactere.config import default_settings
from phylactere.translate import (
    RESPONSE_SCHEMA,
    OllamaTranslator,
    TranslationError,
    system_prompt,
    user_prompt,
)

SETTINGS = default_settings()


class FakeOllama:
    """Ollama simulé : chaque appel consomme la réponse suivante de ``replies``.

    Une réponse est un dict {id: texte} (converti au format attendu), une chaîne brute
    (contenu du message), ou une httpx.Response.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, dict):
            reply = json.dumps(
                {"translations": [{"id": k, "text": v} for k, v in reply.items()]},
                ensure_ascii=False,
            )
        return httpx.Response(200, json={"message": {"role": "assistant", "content": reply}})

    def asked_ids(self, call: int) -> list[int]:
        """ids des bulles envoyées lors de l'appel ``call``."""
        user = self.bodies[call]["messages"][1]["content"]
        bubbles = json.loads(user.rsplit("\n", 1)[1])
        return [bubble["id"] for bubble in bubbles]


def translate(fake, texts, **kwargs):
    async def go():
        async with httpx.AsyncClient(
            base_url="http://ollama", transport=httpx.MockTransport(fake)
        ) as client:
            translator = OllamaTranslator(client, SETTINGS.ollama, SETTINGS.translation)
            return await translator.translate(texts, "fr", **kwargs)

    return asyncio.run(go())


def test_one_request_for_all_bubbles():
    fake = FakeOllama({1: "Salut", 2: "On rentre ?"})

    assert translate(fake, ["안녕", "돌아가자"]) == ["Salut", "On rentre ?"]

    body = fake.bodies[0]
    assert len(fake.bodies) == 1
    assert body["model"] == SETTINGS.ollama.model
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == RESPONSE_SCHEMA
    assert body["keep_alive"] == SETTINGS.ollama.keep_alive
    assert body["options"]["temperature"] == SETTINGS.translation.temperature
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert fake.asked_ids(0) == [1, 2]


def test_retry_after_invalid_json():
    fake = FakeOllama("pas du json", {1: "Salut"})
    assert translate(fake, ["안녕"]) == ["Salut"]
    assert len(fake.bodies) == 2


def test_retry_only_missing_ids():
    fake = FakeOllama({1: "Un", 3: "Trois"}, {2: "Deux"})

    assert translate(fake, ["하나", "둘", "셋"]) == ["Un", "Deux", "Trois"]
    assert fake.asked_ids(1) == [2]


def test_empty_or_unknown_ids_count_as_missing():
    fake = FakeOllama({1: "Un", 2: "  ", 9: "intrus"}, {2: "Deux"})
    assert translate(fake, ["하나", "둘"]) == ["Un", "Deux"]


def test_falls_back_to_one_bubble_per_request():
    fake = FakeOllama({1: "Un"}, {1: "Un"}, {1: "Deux"}, {1: "Trois"})

    assert translate(fake, ["하나", "둘", "셋"]) == ["Un", "Deux", "Trois"]
    assert len(fake.bodies) == 4
    assert fake.asked_ids(2) == [1] and "둘" in fake.bodies[2]["messages"][1]["content"]


def test_gives_up_when_a_bubble_cannot_be_translated():
    fake = FakeOllama("{}", "{}", "{}")
    with pytest.raises(TranslationError, match="안녕") as info:
        translate(fake, ["안녕"])
    assert info.value.status_code == 502


def test_missing_model_gives_pull_hint():
    fake = FakeOllama(httpx.Response(404, json={"error": "model 'qwen3:4b' not found"}))
    with pytest.raises(TranslationError, match="ollama pull") as info:
        translate(fake, ["안녕"])
    assert info.value.status_code == 503


def test_model_without_think_option():
    fake = FakeOllama(
        httpx.Response(400, json={"error": '"gemma" does not support thinking'}),
        {1: "Salut"},
    )
    assert translate(fake, ["안녕"]) == ["Salut"]
    assert "think" in fake.bodies[0] and "think" not in fake.bodies[1]


def test_model_override():
    fake = FakeOllama({1: "Salut"})
    assert translate(fake, ["안녕"], model="gemma3:4b") == ["Salut"]
    assert fake.bodies[0]["model"] == "gemma3:4b"


def test_ollama_down():
    def down(request):
        raise httpx.ConnectError("refusé", request=request)

    with pytest.raises(TranslationError, match="injoignable") as info:
        translate(down, ["안녕"])
    assert info.value.status_code == 503


def test_ollama_timeout():
    def slow(request):
        raise httpx.ReadTimeout("trop long", request=request)

    with pytest.raises(TranslationError) as info:
        translate(slow, ["안녕"])
    assert info.value.status_code == 504


def test_chat_timeout_overrides_ollama_default():
    timeouts = []

    def slow(request):
        timeouts.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("trop long", request=request)

    async def go(**kwargs):
        async with httpx.AsyncClient(
            base_url="http://ollama", transport=httpx.MockTransport(slow)
        ) as client:
            translator = OllamaTranslator(client, SETTINGS.ollama, SETTINGS.translation)
            return await translator.chat("s", "u", "m", **kwargs)

    with pytest.raises(TranslationError, match="1234 s"):
        asyncio.run(go(timeout_s=1234))
    with pytest.raises(TranslationError):
        asyncio.run(go())
    assert timeouts == [1234, SETTINGS.ollama.timeout_s]


def test_context_and_glossary_reach_the_prompt():
    fake = FakeOllama({1: "Salut"})
    translate(
        fake,
        ["안녕 재웅"],
        context=[("꼬랑지!!", "Petite queue !!")],
        glossary=[GlossaryEntry(ko="재웅", fr="Jae-woong", note="héros")],
    )
    user = fake.bodies[0]["messages"][1]["content"]
    assert "재웅 → Jae-woong (héros)" in user
    assert "꼬랑지!! → Petite queue !!" in user
    assert user.index("Glossaire") < user.index("précédentes") < user.index("Bulles à traduire")


def test_prompts_name_the_target_language():
    assert "du coréen en anglais" in system_prompt("en")
    assert "français" in system_prompt("fr")
    assert "Bulles à traduire en japonais" in user_prompt({1: "안녕"}, "ja", [], [])
    assert "xx" in system_prompt("xx")  # langue inconnue : le code est repris tel quel


def preload(handler) -> bool:
    async def go():
        async with httpx.AsyncClient(
            base_url="http://ollama", transport=httpx.MockTransport(handler)
        ) as client:
            return await OllamaTranslator(client, SETTINGS.ollama, SETTINGS.translation).preload()

    return asyncio.run(go())


def test_preload_sends_empty_chat():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"model": SETTINGS.ollama.model, "done": True})

    assert preload(handler) is True
    assert seen == [
        {"model": SETTINGS.ollama.model, "messages": [], "keep_alive": SETTINGS.ollama.keep_alive}
    ]


@pytest.mark.parametrize("status", [404, 500])
def test_preload_failure_is_not_fatal(status):
    assert preload(lambda request: httpx.Response(status)) is False


def test_preload_with_ollama_down_is_not_fatal():
    def down(request):
        raise httpx.ConnectError("refusé", request=request)

    assert preload(down) is False


def test_examples_variant_adds_sfx_examples_in_french_only():
    assert "Exemples d'onomatopées" not in system_prompt("fr")
    assert "쿵 → Boum" in system_prompt("fr", examples=True)
    assert "Exemples d'onomatopées" not in system_prompt("en", examples=True)


def test_original_context_mode_hides_previous_translations():
    context = [("꼬랑지!!", "Qu'est-ce qui ?!")]
    pairs = user_prompt({1: "가"}, "fr", context, [], "pairs")
    original = user_prompt({1: "가"}, "fr", context, [], "original")

    assert "꼬랑지!! → Qu'est-ce qui ?!" in pairs
    assert "- 꼬랑지!!" in original and "Qu'est-ce qui" not in original


def test_variants_reach_the_request():
    fake = FakeOllama({1: "Salut"})
    cfg = SETTINGS.translation.model_copy(update={"examples": True, "context_mode": "original"})

    async def go():
        async with httpx.AsyncClient(base_url="http://o", transport=httpx.MockTransport(fake)) as c:
            await OllamaTranslator(c, SETTINGS.ollama, cfg).translate(
                ["안녕"], "fr", context=[("가", "A")]
            )

    asyncio.run(go())
    system, user = (m["content"] for m in fake.bodies[0]["messages"])
    assert "Exemples d'onomatopées" in system
    assert "- 가" in user and "→ A" not in user


def test_prompt_id_changes_with_variants():
    from phylactere.translate import prompt_id

    base = prompt_id(SETTINGS.translation)
    assert base != prompt_id(SETTINGS.translation.model_copy(update={"examples": True}))
    assert base != prompt_id(SETTINGS.translation.model_copy(update={"context_mode": "original"}))


def test_stats_callback_receives_ollama_counters():
    seen = []

    def handler(request):
        content = json.dumps({"translations": [{"id": 1, "original": "안녕", "text": "Salut"}]})
        return httpx.Response(
            200,
            json={
                "message": {"content": content},
                "eval_count": 30,
                "eval_duration": 600_000_000,
                "prompt_eval_count": 400,
                "prompt_eval_duration": 200_000_000,
                "total_duration": 900_000_000,
            },
        )

    async def go():
        async with httpx.AsyncClient(
            base_url="http://o", transport=httpx.MockTransport(handler)
        ) as c:
            translator = OllamaTranslator(
                c, SETTINGS.ollama, SETTINGS.translation, on_stats=seen.append
            )
            return await translator.translate(["안녕"], "fr")

    assert asyncio.run(go()) == ["Salut"]
    assert seen == [
        {
            "eval_count": 30,
            "eval_ms": 600.0,
            "prompt_eval_count": 400,
            "prompt_eval_ms": 200.0,
            "total_ms": 900.0,
        }
    ]


def test_stats_are_optional_and_tolerate_missing_counters():
    from phylactere.translate import ollama_stats

    assert ollama_stats({}) == {
        "eval_count": 0,
        "eval_ms": 0.0,
        "prompt_eval_count": 0,
        "prompt_eval_ms": 0.0,
        "total_ms": 0.0,
    }
