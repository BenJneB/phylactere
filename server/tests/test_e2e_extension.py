"""Tests de bout en bout de l'extension (jalons 7, 8, 10 et 11).

Chromium (Playwright) charge l'extension non empaquetée, ouvre une fausse page d'épisode
Naver et parle à un vrai serveur Phylactère lancé dans un thread, dont l'OCR, Ollama et le
serveur d'images sont simulés. Aucun accès réseau : Playwright sert lui-même les pages de
comic.naver.com et les images de image-comic.pstatic.net.

Exclus par défaut (marqueur ``e2e``). Lancement :
    uv run playwright install chromium     # une fois
    uv run pytest -m e2e

Les tests communs tournent dans les deux modes de traduction (jalon 8) : « episode » (tout
l'épisode dès l'ouverture) et « scroll » (images visibles et ``lookahead`` suivantes).

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

from phylactere.config import load_settings, reports_dir
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
BADGE = ".phy-badge"
MODES = pytest.mark.parametrize("mode", ["episode", "scroll"])


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


def page_html(episode: int, next_no: int | None = None) -> str:
    """Page d'épisode minimale, reprise de la structure réelle de Naver : la vignette (même
    dossier que les images, à ignorer) puis les images dans #sectionContWide. Le lien
    « suivant » change d'épisode sans recharger la page.

    ``next_no`` : ajoute le script d'état de Naver (``window.__state__``) qui annonce
    l'épisode suivant, déclencheur du préchargement de cet épisode (jalon 11)."""
    state = (
        f"""<script>
window.__state__ = {{
    article: {{"no":{episode},"subtitle":"test"}},
    nextArticle: {{
        no: '{next_no}',
        charge: false,
        blindInspection: false,
    }},
}};
</script>"""
        if next_no is not None
        else ""
    )
    images = "\n".join(
        f'<img id="content_image_{i}" src="{image_url(episode, i)}" '
        f'width="{IMAGE_W}" height="{IMAGE_H}" style="display:block">'
        for i in range(IMAGE_COUNT)
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Épisode {episode}</title>{state}</head>
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
        self.suggest_requests = 0
        self.translations = 0  # requêtes de traduction reçues par Ollama
        self.ocr_runs = 0  # images lues par l'OCR simulé
        # Délai propre à une bulle (texte simulé → secondes), pour les délais dépassés.
        self.slow: dict[str, float] = {}
        # Réponse du modèle aux propositions : un terme présent dans les bulles, un inventé.
        self.suggestions = [
            {"ko": "가66", "fr": "Ga", "note": "héros"},
            {"ko": "없는말", "fr": "Inventé", "note": ""},
        ]

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
        if "entries" in body["format"]["properties"]:  # propositions de glossaire (jalon 10)
            self.suggest_requests += 1
            content = {"entries": self.suggestions}
            return httpx.Response(200, json={"message": {"content": json.dumps(content)}})
        user = body["messages"][1]["content"]
        bubbles = json.loads(user.rsplit("\n", 1)[1])
        self.translations += 1
        await asyncio.sleep(max((self.slow.get(b["texte"], 0) for b in bubbles), default=0))
        # Avec un glossaire, la traduction le montre : on voit l'épisode retraduit.
        prefix = "GL" if "Glossaire de la série" in user else "FR"
        content = {
            "translations": [
                {"id": b["id"], "original": b["texte"], "text": f"{prefix}:{b['texte']}"}
                for b in bubbles
            ]
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})

    def detector(self):
        world = self

        class Detector(FakeDetector):
            def detect(self, image_bgr):
                world.ocr_runs += 1
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


def set_storage(worker, **values) -> None:
    """Écrit des réglages comme le ferait le popup."""
    worker.evaluate("s => chrome.storage.local.set(s)", values)


def configure(worker, port: int, token: str, mode: str = "episode", lookahead: int = 4) -> None:
    set_storage(
        worker,
        serverUrl=f"http://127.0.0.1:{port}",
        token=token,
        enabled=True,
        translateMode=mode,
        lookahead=lookahead,
    )


def open_episode(context, episode: int = 66):
    page = context.new_page()
    page.goto(episode_url(episode))
    return page


def bubble_on(page, episode: int, index: int):
    return page.locator(BUBBLE, has_text=expected_text(episode, index))


@MODES
def test_bubble_is_placed_over_its_image(browser, server, mode):
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, mode)
    page = open_episode(context)

    bubble = bubble_on(page, 66, 0)
    expect(bubble).to_be_visible(timeout=15_000)
    expect(bubble).to_have_attribute("title", "가66-0")  # survol : texte d'origine
    # Fond pris sur l'image (vert foncé uni, rgb(0, 66, 0)) et texte blanc, plus lisible.
    expect(bubble).to_have_css("background-color", "rgb(0, 66, 0)")
    expect(bubble).to_have_css("color", "rgb(255, 255, 255)")

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
    expect(page.locator(BADGE)).to_contain_text("en cours")
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


def requested(world, episode: int = 66) -> set[int]:
    return {index for index, _ in world.requests_for(episode)}


def test_scroll_mode_follows_the_reading_window(browser, server, world):
    """Mode « scroll » : seulement les images visibles et les `lookahead` suivantes ; un saut
    en fin d'épisode ne traite pas ce qui précède ; les images dépassées attendent d'être
    revues."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, "scroll", lookahead=2)
    page = open_episode(context)
    badge = page.locator(BADGE)

    # À l'ouverture, seule l'image 0 est visible : fenêtre 0 à 2.
    expect(page.locator(BUBBLE)).to_have_count(3, timeout=15_000)
    expect(badge).to_have_text("Phylactère · Traduit · 3/12 · en attente du défilement")
    assert requested(world) == {0, 1, 2}
    assert (0, "visible") in world.requests_for(66)

    # Saut vers l'image 10 (centrée : seule visible) : fenêtre 10 à 12, 3 à 9 sautées.
    page.locator("#content_image_10").scroll_into_view_if_needed()
    expect(bubble_on(page, 66, 11)).to_be_visible(timeout=15_000)
    expect(badge).to_have_text("Phylactère · Traduit · 5/12 · en attente du défilement")
    assert requested(world) == {0, 1, 2, 10, 11}
    assert (10, "visible") in world.requests_for(66)

    # Retour en arrière : une image dépassée est traduite quand elle redevient visible.
    page.locator("#content_image_5").scroll_into_view_if_needed()
    expect(bubble_on(page, 66, 5)).to_be_visible(timeout=15_000)
    assert (5, "visible") in world.requests_for(66)
    expect(badge).to_have_text("Phylactère · Traduit · 8/12 · en attente du défilement")
    assert requested(world) == {0, 1, 2, 5, 6, 7, 10, 11}  # fenêtre 5 à 7


def test_mode_change_applies_without_reload(browser, server, world):
    """« scroll » → « episode » relance le préchargement, sans retraduire ce qui est fait."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, "scroll", lookahead=1)
    page = open_episode(context)
    badge = page.locator(BADGE)
    expect(badge).to_have_text(
        "Phylactère · Traduit · 2/12 · en attente du défilement", timeout=15_000
    )
    assert requested(world) == {0, 1}

    # Avance augmentée à chaud : la fenêtre s'élargit.
    set_storage(worker, lookahead=3)
    expect(badge).to_have_text(
        "Phylactère · Traduit · 4/12 · en attente du défilement", timeout=15_000
    )
    assert requested(world) == {0, 1, 2, 3}

    set_storage(worker, translateMode="episode")
    expect(badge).to_have_text(
        f"Phylactère · Traduit · {IMAGE_COUNT}/{IMAGE_COUNT}", timeout=30_000
    )
    # Pas de remise à zéro : chaque image n'est demandée qu'une fois.
    indices = [index for index, _ in world.requests_for(66)]
    assert sorted(indices) == list(range(IMAGE_COUNT))


@MODES
def test_server_down_then_started(browser, world, mode):
    context, worker = browser
    live = LiveServer(world, free_port())
    configure(worker, live.port, live.token, mode)
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


@MODES
def test_refused_token_then_fixed(browser, server, mode):
    context, worker = browser
    server.start()
    configure(worker, server.port, "jeton-faux", mode)
    page = open_episode(context)

    badge = page.locator(".phy-badge")
    expect(badge).to_have_text("Phylactère · Jeton à saisir dans le popup", timeout=10_000)
    assert page.locator(BUBBLE).count() == 0

    configure(worker, server.port, server.token, mode)  # comme une saisie dans le popup
    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)


@MODES
def test_episode_change_without_reload(browser, server, world, mode):
    """La page réutilise ses <img> : seul leur src change (currentSrc garde l'ancienne
    URL tant que la nouvelle image n'est pas chargée)."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, mode)
    page = open_episode(context)
    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)

    page.locator("#next").click()
    expect(bubble_on(page, 67, 0)).to_be_visible(timeout=15_000)
    expect(page.locator(BUBBLE, has_text="FR:가66-")).to_have_count(0)
    # Les images du nouvel épisode sont reconnues au fil de leur chargement : l'ordre des
    # premières requêtes varie. L'image visible part en « visible », jamais en « prefetch ».
    assert (0, "visible") in world.requests_for(67)
    assert (0, "prefetch") not in world.requests_for(67)


@MODES
def test_alt_t_and_click_on_bubble(browser, server, mode):
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, mode)
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


def test_popup_saves_translate_mode(browser):
    """Le popup enregistre le mode et l'avance dès leur changement (avance bornée à 1-20)."""
    context, worker = browser
    extension_id = worker.url.split("/")[2]
    page = context.new_page()
    page.goto(f"chrome-extension://{extension_id}/popup.html")
    expect(page.locator("#translateMode")).to_have_value("episode")
    expect(page.locator("#lookaheadRow")).to_be_hidden()

    page.select_option("#translateMode", "scroll")
    expect(page.locator("#lookaheadRow")).to_be_visible()
    expect(page.locator("#lookahead")).to_have_value("4")
    page.fill("#lookahead", "50")
    page.locator("#lookahead").dispatch_event("change")
    expect(page.locator("#lookahead")).to_have_value("20")
    expect(page.locator("#status")).to_have_text("20 image(s) d’avance.")  # après l'écriture

    stored = worker.evaluate("() => chrome.storage.local.get(['translateMode', 'lookahead'])")
    assert stored == {"translateMode": "scroll", "lookahead": 20}


def test_glossary_page_suggests_saves_and_retranslates(browser, server, world):
    """Jalon 10 : propositions du modèle (terme inventé écarté), ajout de la sélection,
    enregistrement, puis l'onglet de l'épisode se retraduit avec le glossaire."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, "scroll", lookahead=1)
    episode = open_episode(context)
    expect(bubble_on(episode, 66, 1)).to_be_visible(timeout=15_000)

    extension_id = worker.url.split("/")[2]
    page = context.new_page()
    page.goto(f"chrome-extension://{extension_id}/glossary.html?series={SERIES}&episode=66")
    expect(page.locator("#seriesId")).to_have_text(SERIES)
    expect(page.locator("#noEntries")).to_be_visible()

    page.click("#suggest")
    rows = page.locator("#suggestionRows tr")
    expect(rows).to_have_count(1, timeout=10_000)  # « 없는말 » n'est dans aucune bulle
    expect(rows.first).to_contain_text("가66")
    expect(rows.first).to_contain_text("가66-0 → FR:가66-0")  # bulle d'exemple
    assert world.suggest_requests == 1
    rows.first.locator("[data-field=fr]").fill("Ga-yu")

    page.click("#addSelected")
    expect(page.locator("#suggestions")).to_be_hidden()
    expect(page.locator("#entries tr")).to_have_count(1)
    assert server_glossary(server) == []  # rien n'est enregistré avant « Enregistrer »

    page.click("#save")
    expect(page.locator("#saveStatus")).to_contain_text("Glossaire enregistré (1 entrée(s)).")
    expect(page.locator("#saveStatus")).to_contain_text("1 onglet(s)")
    assert server_glossary(server) == [{"ko": "가66", "fr": "Ga-yu", "note": "héros"}]

    # L'épisode se retraduit avec le glossaire, sans rechargement.
    expect(bubble_on(episode, 66, 0)).to_have_count(0, timeout=15_000)
    expect(episode.locator(BUBBLE, has_text="GL:가66-0")).to_be_visible(timeout=15_000)
    assert world.requests_for(66).count((0, "visible")) == 2


def server_glossary(server) -> list[dict]:
    response = httpx.get(
        f"http://127.0.0.1:{server.port}/glossary/{SERIES}",
        headers={"X-Phylactere-Token": server.token},
    )
    return response.json()


# --- Jalon 11 : signalement, délai dépassé, préchargement de l'épisode suivant ---


def test_alt_click_reports_bubble(browser, server):
    """Alt+clic : le serveur enregistre la bulle (lignes OCR, découpe) ; pas d'effacement."""
    context, worker = browser
    server.start()
    configure(worker, server.port, server.token, "scroll", lookahead=1)
    page = open_episode(context)
    bubble = bubble_on(page, 66, 0)
    expect(bubble).to_be_visible(timeout=15_000)

    bubble.click(modifiers=["Alt"])
    expect(bubble).to_have_attribute("data-report", "done", timeout=10_000)
    expect(bubble).not_to_have_class(re.compile(r"\bphy-peek\b"))
    expect(page.locator(BADGE)).to_contain_text("Bulle signalée")

    reports = list(reports_dir().glob("*.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert (data["episode_id"], data["index"]) == ("66", 0)
    assert data["bubble"]["translated"] == expected_text(66, 0)
    assert [line["text"] for line in data["lines"]] == ["가66-0"]
    assert (reports_dir() / data["crop"]).is_file()

    bubble.click(modifiers=["Alt"])  # déjà signalée : pas de doublon
    page.wait_for_timeout(500)
    assert len(list(reports_dir().glob("*.json"))) == 1


def shorten_timeout(worker, ms: int) -> None:
    worker.evaluate(f"() => {{ TRANSLATE_TIMEOUT_MS = {ms}; }}")


def test_timeout_puts_image_back_in_queue(browser, server, world):
    """Délai dépassé côté extension : l'image repart en file et la nouvelle demande est
    fusionnée avec la tâche du serveur (ou servie par le cache), sans erreur."""
    context, worker = browser
    server.start()
    world.ollama_delay = 0.05
    world.slow = {"가66-0": 1.5}
    configure(worker, server.port, server.token, "scroll", lookahead=1)
    shorten_timeout(worker, 1000)
    page = open_episode(context)

    expect(bubble_on(page, 66, 0)).to_be_visible(timeout=15_000)
    expect(page.locator(BADGE)).to_have_text(
        "Phylactère · Traduit · 2/12 · en attente du défilement", timeout=10_000
    )
    assert world.requests_for(66).count((0, "visible")) == 2
    assert world.translations == 2  # une seule traduction par image


def test_repeated_timeouts_end_in_error(browser, server, world):
    context, worker = browser
    server.start()
    world.ollama_delay = 0.05
    world.slow = {"가66-0": 4}
    configure(worker, server.port, server.token, "scroll", lookahead=1)
    shorten_timeout(worker, 400)
    page = open_episode(context)

    # L'image 1 attend derrière l'image 0 dans la file du serveur : elle expire aussi.
    expect(page.locator(BADGE)).to_contain_text("2 erreur(s)", timeout=15_000)
    expect(page.locator(BADGE)).to_have_attribute("title", re.compile(r"\(3 essais\)"))
    assert world.requests_for(66).count((0, "visible")) == 3  # 1 envoi + 2 nouvelles tentatives


def test_next_episode_is_prefetched_after_80_percent(browser, server, world):
    """Mode « episode » : une fois l'épisode traduit et la lecture passée à 80 %, les
    images de l'épisode suivant partent en « prefetch » ; à son ouverture, tout vient du
    cache (mêmes voisines, donc même clé OCR)."""
    context, worker = browser
    context.route(
        "https://comic.naver.com/**",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body=page_html(66, next_no=67) if "no=66" in route.request.url else page_html(67),
        ),
    )
    server.start()
    configure(worker, server.port, server.token)
    page = open_episode(context)
    badge = page.locator(BADGE)
    all_done = f"Phylactère · Traduit · {IMAGE_COUNT}/{IMAGE_COUNT}"

    # Épisode traduit, mais lecture au début : rien de l'épisode suivant.
    expect(badge).to_have_text(all_done, timeout=30_000)
    page.wait_for_timeout(500)
    assert world.requests_for(67) == []

    page.locator("#content_image_10").scroll_into_view_if_needed()  # 11 images sur 12 atteintes
    expect(badge).to_contain_text("épisode suivant", timeout=10_000)
    expect(badge).to_have_text(all_done, timeout=30_000)
    assert sorted(world.requests_for(67)) == [(i, "prefetch") for i in range(IMAGE_COUNT)]
    translations, ocr_runs = world.translations, world.ocr_runs

    # Ouverture de l'épisode suivant : traduit sans nouvel OCR ni nouvelle traduction.
    page.locator("#next").click()
    expect(page.locator(BUBBLE, has_text="FR:가67-")).to_have_count(IMAGE_COUNT, timeout=15_000)
    assert (world.translations, world.ocr_runs) == (translations, ocr_runs)
    assert world.requests_for(68) == []  # le script d'état de la page quittée est ignoré


def test_no_next_episode_prefetch_in_scroll_mode(browser, server, world):
    context, worker = browser
    context.route(
        "https://comic.naver.com/**",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=page_html(66, next_no=67)
        ),
    )
    server.start()
    configure(worker, server.port, server.token, "scroll", lookahead=20)
    page = open_episode(context)
    expect(page.locator(BUBBLE)).to_have_count(IMAGE_COUNT, timeout=30_000)
    page.locator("#content_image_11").scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    assert world.requests_for(67) == []
