import httpx
import pytest
from fastapi.testclient import TestClient

from phylactere import __version__
from phylactere.config import load_settings
from phylactere.main import TOKEN_HEADER, create_app
from tests.helpers import FakeDetector, line

TAGS = {"models": [{"name": "qwen3:1.7b"}]}


def ollama_up(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/tags"
    return httpx.Response(200, json=TAGS)


def ollama_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connexion refusée", request=request)


def make_client(handler=ollama_up, images=None, detector_factory=FakeDetector) -> TestClient:
    app = create_app(
        load_settings(),
        ollama_transport=httpx.MockTransport(handler),
        images_transport=httpx.MockTransport(images or (lambda request: httpx.Response(404))),
        detector_factory=detector_factory,
    )
    return TestClient(app, base_url="http://127.0.0.1")


def wait_ocr(client: TestClient) -> None:
    client.app.state.ocr._executor.submit(lambda: None).result(5)


@pytest.fixture
def client():
    with make_client() as c:
        yield c


@pytest.fixture
def token():
    return load_settings().server.token


def test_health_without_token(client):
    wait_ocr(client)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "ocr": "ready",
        "ollama": "ok",
        "model": "qwen3:8b",
        "device": "cpu",
        "version": __version__,
    }


def test_health_reports_ollama_down():
    with make_client(ollama_down) as c:
        assert c.get("/health").json()["ollama"] == "down"


def test_health_reports_ollama_error_status():
    with make_client(lambda request: httpx.Response(500)) as c:
        assert c.get("/health").json()["ollama"] == "down"


@pytest.mark.parametrize(
    "headers", [{}, {TOKEN_HEADER: "mauvais"}, {TOKEN_HEADER: "é".encode("latin-1")}]
)
def test_protected_route_rejects_missing_or_wrong_token(client, headers):
    assert client.get("/models", headers=headers).status_code == 401


def test_models_with_token(client, token):
    response = client.get("/models", headers={TOKEN_HEADER: token})

    assert response.status_code == 200
    assert response.json() == TAGS


def test_models_when_ollama_down(token):
    with make_client(ollama_down) as c:
        assert c.get("/models", headers={TOKEN_HEADER: token}).status_code == 503


@pytest.mark.parametrize(
    "origin",
    ["chrome-extension://abcdefghijklmnopabcdefghijklmnop", "moz-extension://1b2c3d4e-0000-4f00"],
)
def test_cors_accepts_extension_origins(client, origin):
    response = client.options(
        "/models",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": TOKEN_HEADER,
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin", ["https://comic.naver.com", "http://127.0.0.1:8000", "https://evil.example"]
)
def test_cors_rejects_web_origins(client, origin):
    response = client.get("/health", headers={"Origin": origin})

    assert "access-control-allow-origin" not in response.headers


def test_health_reports_ocr_error():
    def broken():
        raise RuntimeError("pas de GPU")

    with make_client(detector_factory=broken) as c:
        wait_ocr(c)
        assert c.get("/health").json()["ocr"] == "error"


def test_rejects_non_local_host_header():
    app = create_app(
        load_settings(),
        ollama_transport=httpx.MockTransport(ollama_up),
        detector_factory=FakeDetector,
    )
    with TestClient(app, base_url="http://attaquant.example") as c:
        assert c.get("/health").status_code == 400


# --- /debug/ocr ---

IMAGE_URL = "https://image-comic.pstatic.net/webtoon/1/2/a.jpg"


def png_bytes(size=(690, 600)) -> bytes:
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", size, "white").save(out, format="PNG")
    return out.getvalue()


def detector_with_bubble():
    return FakeDetector([[line(100, 100, 200, text="안녕하세요")]])


def test_debug_ocr_requires_token(client):
    assert client.post("/debug/ocr", data={"image_url": IMAGE_URL}).status_code == 401


@pytest.mark.parametrize(
    "payload", [{}, {"data": {"image_url": IMAGE_URL}, "files": {"file": b"x"}}]
)
def test_debug_ocr_needs_exactly_one_source(client, token, payload):
    response = client.post("/debug/ocr", headers={TOKEN_HEADER: token}, **payload)
    assert response.status_code == 400


def test_debug_ocr_from_url_returns_png(token):
    seen = {}

    def images(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=png_bytes())

    with make_client(images=images, detector_factory=detector_with_bubble) as c:
        wait_ocr(c)
        response = c.post(
            "/debug/ocr", headers={TOKEN_HEADER: token}, data={"image_url": IMAGE_URL}
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    assert seen["url"] == IMAGE_URL


def test_debug_ocr_from_file_as_json(token):
    with make_client(detector_factory=detector_with_bubble) as c:
        wait_ocr(c)
        response = c.post(
            "/debug/ocr?format=json",
            headers={TOKEN_HEADER: token},
            files={"file": ("a.png", png_bytes(), "image/png")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["image_size"] == {"w": 690, "h": 600}
    assert [b["text"] for b in body["bubbles"]] == ["안녕하세요"]
    assert body["bubbles"][0]["box"] == {"x": 100, "y": 100, "w": 200, "h": 30}


def test_debug_ocr_refuses_other_hosts(client, token):
    response = client.post(
        "/debug/ocr",
        headers={TOKEN_HEADER: token},
        data={"image_url": "https://evil.example/a.jpg"},
    )
    assert response.status_code == 400
    assert "non autorisé" in response.json()["detail"]


def test_debug_ocr_upstream_403(token):
    with make_client(images=lambda request: httpx.Response(403)) as c:
        response = c.post(
            "/debug/ocr", headers={TOKEN_HEADER: token}, data={"image_url": IMAGE_URL}
        )
    assert response.status_code == 502


def test_debug_ocr_invalid_file(client, token):
    response = client.post(
        "/debug/ocr", headers={TOKEN_HEADER: token}, files={"file": ("a.png", b"pas une image")}
    )
    assert response.status_code == 422


def test_debug_ocr_while_loading(token):
    import threading

    release = threading.Event()

    def slow():
        release.wait(5)
        return FakeDetector()

    try:
        with make_client(detector_factory=slow) as c:
            response = c.post(
                "/debug/ocr", headers={TOKEN_HEADER: token}, files={"file": ("a.png", png_bytes())}
            )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
            assert c.get("/health").json()["ocr"] == "loading"
    finally:
        release.set()
