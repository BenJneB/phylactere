"""Tests de bout en bout de l'extension (jalon 7).

Chromium (Playwright) charge l'extension non empaquetée, ouvre une fausse page d'épisode
Naver et parle à un vrai serveur Phylactère lancé dans un thread, dont l'OCR, Ollama et le
serveur d'images sont simulés. Aucun accès réseau : Playwright sert lui-même les pages de
comic.naver.com et les images de image-comic.pstatic.net.

Exclus par défaut (marqueur ``e2e``). Lancement :
    uv run playwright install chromium     # une fois
    uv run pytest -m e2e

Firefox n'est pas couvert : Playwright ne sait pas y charger une extension.
"""

import asyncio
import json
import re
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
import pytest
import uvicorn
from PIL import Image

from phylactere.config import load_settings
from phylactere.main import create_app
from tests.helpers import FakeDetector, line
from tests.test_smoke import free_port

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.e2e

EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extension"
SERIES = "729047"
IMAGE_W, IMAGE_H = 690, 1000  # une seule tranche OCR (slice_height = 1200)
IMAGE_COUNT = 12
VIEWPORT = {"width": 900, "height": 800}  # seule la première image est visible à l'ouverture
# Ligne de texte simulée, en pixels de l'image d'origine.
LINE_X, LINE_Y, LINE_W, LINE_H = 100, 400, 200, 30
PAD = 6  # même marge que content.js
URL_RE = re.compile(r"/webtoon/(\d+)/(\d+)/img_(\d+)\.png$")
BUBBLE = ".phy-layer .phy-bubble"


def image_url(episode: int, index: int) -> str:
    return f"https://image-comic.pstatic.net/webtoon/{SERIES}/{episode}/img_{index}.png"


def episode_url(episode: int) -> str:
    return f"https://comic.naver.com/webtoon/detail?titleId={SERIES}&no={episode}"


def image_png(episode: int, index: int) -> bytes:
    """Image unie dont la couleur code l'épisode et l'index : l'OCR simulé les relit."""
    out = BytesIO()
    Image.new("RGB", (IMAGE_W, IMAGE_H), (index * 16, episode, 0)).save(out, format="PNG")
    return out.getvalue()


def expected_text(episode: int, index: int) -> str:
    return f"FR:가{episode}-{index}"


def page_html(episode: int) -> str:
    """Page d'épisode minimale, reprise de la structure réelle de Naver : la vignette (même
    dossier que les images, à ignorer) puis les images dans #sectionContWide. Le lien
    « suivant » change d'épisode sans recharger la page, comme le lecteur de Naver."""
    images = "\n".join(
        f'<img id="content_image_{i}" src="{image_url(episode, i)}" '
        f'width="{IMAGE_W}" height="{IMAGE_H}" style="display:block">'
        for i in range(IMAGE_COUNT)
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Épisode {episode}</title></head>
<body style="margin:0">
<img id="thumb" src="https://image-comic.pstatic.net/webtoon/{SERIES}/{episode}/thumbnail_202x120.png">
<a id="next" href="#">suivant</a>
<div id="sectionContWide" style="width:{IMAGE_W}px">
{images}
</div>
<script>
document.getElementById("next").addEventListener("click", (ev) => {{
  ev.preventDefault();
  const no = {episode} + 1;
  history.pushState({{}}, "", "/webtoon/detail?titleId={SERIES}&no=" + no);
  document.querySelectorAll("#sectionContWide img").forEach((img, i) => {{
    img.src = "https://image-comic.pstatic.net/webtoon/{SERIES}/" + no + "/img_" + i + ".png";
  }});
}});
</script>
</body></html>"""


class World:
    """Serveur d'images, Ollama et PaddleOCR simulés, et journal des requêtes /translate."""

    def __init__(self) -> None:
        self.ollama_delay = 0.3  # s par image : laisse la file de l'extension se remplir
        self.calls: list[tuple[int, int, str]] = []  # (épisode, index, priorité)
        self.in_flight = 0
        self.max_in_flight = 0

    def images(self, request: httpx.Request) -> httpx.Response:
        match = URL_RE.search(request.url.path)
        if not match:
            return httpx.Response(404)
        return httpx.Response(200, content=image_png(int(match[2]), int(match[3])))

    async def ollama(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "simulé"}]})
        body = json.loads(request.content)
        if not body["messages"]:  # préchargement du modèle
            return httpx.Response(200, json={})
        await asyncio.sleep(self.ollama_delay)
        bubbles = json.loads(body["messages"][1]["content"].rsplit("\n", 1)[1])
        content = {
            "translations": [
                {"id": b["id"], "original": b["texte"], "text": f"FR:{b['texte']}"} for b in bubbles
            ]
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    def detector(self):
        class Detector(FakeDetector):
            def detect(self, image_bgr):
                blue, green, red = (int(v) for v in image_bgr[0, 0])
                text = f"가{green}-{red // 16}"
                return [line(LINE_X, LINE_Y, LINE_W, LINE_H, text=text)]

        return Detector()

    def requests_for(self, episode: int) -> list[tuple[int, str]]:
        return [(index, priority) for ep, index, priority in self.calls if ep == episode]


class LiveServer:
    """Vrai serveur Phylactère (uvicorn) dans un thread, sur un port donné."""

    def __init__(self, world: World, port: int) -> None:
        settings = load_settings()
        # Pas de lecture des voisines : chaque image est lue seule, en une tranche.
        settings = settings.model_copy(
            update={"ocr": settings.ocr.model_copy(update={"neighbor_margin": 0})}
        )
        self.token = settings.server.token
        self.port = port
        self.world = world
        self.app = create_app(
            settings,
            ollama_transport=httpx.MockTransport(world.ollama),
            images_transport=httpx.MockTransport(world.images),
            detector_factory=world.detector,
        )
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert self.thread.is_alive(), "Le serveur de test n'a pas démarré."
            assert time.monotonic() < deadline, "Le serveur de test met trop de temps à démarrer."
            time.sleep(0.05)
        self.app.state.ocr.wait_loaded(5)
        assert self.app.state.ocr.status == "ready"
        self._record_requests()

    def _record_requests(self) -> None:
        """Journalise chaque requête /translate et le nombre de requêtes simultanées."""
        pipeline = self.app.state.pipeline
        original = pipeline.translate
        world = self.world

        async def translate(body):
            world.calls.append((int(body.episode_id), body.index, body.priority))
            world.in_flight += 1
            world.max_in_flight = max(world.max_in_flight, world.in_flight)
            try:
                return await original(body)
            finally:
                world.in_flight -= 1

        pipeline.translate = translate

    def stop(self) -> None:
        if self.thread is not None:
            self.server.should_exit = True
            self.thread.join(10)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def server(world):
    live = LiveServer(world, free_port())
    yield live
    live.stop()


@pytest.fixture
def browser(tmp_path):
    """Chromium avec l'extension chargée ; renvoie (contexte, service worker)."""
    with playwright_api.sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(tmp_path / "chromium-profile"),
            channel="chromium",  # Chromium complet : le shell sans fenêtre ignore les extensions
            headless=True,
            viewport=VIEWPORT,
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
            ],
        )
        try:
            context.route("https://comic.naver.com/**", serve_naver)
            context.route("https://image-comic.pstatic.net/**", serve_image)
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker")
            )
            yield context, worker
        finally:
            context.close()


def serve_naver(route) -> None:
    match = re.search(r"[?&]no=(\d+)", route.request.url)
    episode = int(match[1]) if match else 66
    route.fulfill(content_type="text/html; charset=utf-8", body=page_html(episode))


def serve_image(route) -> None:
    match = URL_RE.search(route.request.url.split("?")[0])
    if not match:  # vignette
        out = BytesIO()
        Image.new("RGB", (202, 120), "gray").save(out, format="PNG")
        route.fulfill(content_type="image/png", body=out.getvalue())
        return
    route.fulfill(content_type="image/png", body=image_png(int(match[2]), int(match[3])))


def configure(worker, port: int, token: str) -> None:
    worker.evaluate(
        "s => chrome.storage.local.set(s)",
        {"serverUrl": f"http://127.0.0.1:{port}", "token": token, "enabled": True},
    )


def open_episode(context, episode: int = 66):
    page = context.new_page()
    page.goto(episode_url(episode))
    return page


def bubble_on(page, episode: int, index: int):
    return page.locator(BUBBLE, has_text=expected_text(episode, index))


def test_bubble_is_placed_over_its_image(browser, server):
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token)
    page = open_episode(context)

    bubble = bubble_on(page, 66, 0)
    expect(bubble).to_be_visible(timeout=15_000)
    expect(bubble).to_have_attribute("title", "가66-0")  # survol : texte d'origine

    img = page.locator("#content_image_0").bounding_box()
    box = bubble.bounding_box()
    scale = img["width"] / IMAGE_W
    assert box["x"] == pytest.approx(img["x"] + (LINE_X - PAD) * scale, abs=2)
    assert box["y"] == pytest.approx(img["y"] + (LINE_Y - PAD) * scale, abs=2)
    assert box["width"] == pytest.approx((LINE_W + 2 * PAD) * scale, abs=2)
    # La vignette (même dossier que les images de l'épisode) n'est jamais envoyée.
    assert all(index < IMAGE_COUNT for _, index, _ in server.world.calls)


def test_visible_first_then_prefetch_with_at_most_three_requests(browser, server, world):
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token)
    page = open_episode(context)

    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)
    # Les 3 premières requêtes partent ensemble (leur ordre d'arrivée au serveur varie) :
    # l'image visible, puis le préchargement dans l'ordre de lecture.
    assert set(world.requests_for(66)[:3]) == {(0, "visible"), (1, "prefetch"), (2, "prefetch")}
    assert (0, "prefetch") not in world.requests_for(66)  # pas d'envoi en double
    assert world.max_in_flight == 3

    # Image lointaine devenue visible : envoyée (ou renvoyée) en « visible ».
    page.locator("#content_image_10").scroll_into_view_if_needed()
    expect(bubble_on(page, 66, 10)).to_be_visible(timeout=15_000)
    assert (10, "visible") in world.requests_for(66)

    # Tout l'épisode finit par être traduit, image par image.
    expect(page.locator(BUBBLE)).to_have_count(IMAGE_COUNT, timeout=30_000)
    expect(page.locator(".phy-badge")).to_have_text(
        f"Phylactère · Traduit · {IMAGE_COUNT}/{IMAGE_COUNT}"
    )


def test_server_down_then_started(browser, world):
    context, worker = browser
    live = LiveServer(world, free_port())
    configure(worker, live.port, live.token)
    page = open_episode(context)
    try:
        expect(page.locator(".phy-badge")).to_have_text(
            "Phylactère · Serveur arrêté", timeout=10_000
        )
        live.start()
        # Nouvel essai sur /health toutes les 5 s, puis reprise de la file.
        expect(bubble_on(page, 66, 0)).to_be_visible(timeout=20_000)
    finally:
        live.stop()


def test_refused_token_then_fixed(browser, server):
    context, worker = browser
    server.start()
    configure(worker, server.port, "jeton-faux")
    page = open_episode(context)

    badge = page.locator(".phy-badge")
    expect(badge).to_have_text("Phylactère · Jeton à saisir dans le popup", timeout=10_000)
    assert page.locator(BUBBLE).count() == 0

    configure(worker, server.port, server.token)  # comme une saisie dans le popup
    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)


def test_episode_change_without_reload(browser, server, world):
    """La page réutilise ses <img> : seul leur src change (currentSrc garde l'ancienne
    URL tant que la nouvelle image n'est pas chargée)."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token)
    page = open_episode(context)
    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)

    page.locator("#next").click()
    expect(bubble_on(page, 67, 0)).to_be_visible(timeout=15_000)
    expect(page.locator(BUBBLE, has_text="FR:가66-")).to_have_count(0)
    # Les images du nouvel épisode sont reconnues au fil de leur chargement : l'ordre des
    # premières requêtes varie. L'image visible part en « visible », jamais en « prefetch ».
    assert (0, "visible") in world.requests_for(67)
    assert (0, "prefetch") not in world.requests_for(67)


def test_alt_t_and_click_on_bubble(browser, server):
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token)
    page = open_episode(context)
    bubble = bubble_on(page, 66, 0)
    expect(bubble).to_be_visible(timeout=15_000)

    # Clic sur une bulle : elle s'efface pour montrer l'original, re-clic pour la remettre.
    bubble.click()
    expect(bubble).to_have_class(re.compile(r"\bphy-peek\b"))
    bubble.click()
    expect(bubble).not_to_have_class(re.compile(r"\bphy-peek\b"))

    # Alt+T : masque toutes les traductions, puis les réaffiche.
    page.keyboard.press("Alt+KeyT")
    expect(page.locator(".phy-layer").first).to_have_class(re.compile(r"\bphy-hidden\b"))
    expect(page.locator(".phy-badge")).to_contain_text("Original")
    page.keyboard.press("Alt+KeyT")
    expect(page.locator(".phy-layer").first).not_to_have_class(re.compile(r"\bphy-hidden\b"))
