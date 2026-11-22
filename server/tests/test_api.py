import httpx
import pytest
from fastapi.testclient import TestClient

from phylactere import __version__
from phylactere.config import load_settings
from phylactere.main import TOKEN_HEADER, create_app

TAGS = {"models": [{"name": "qwen3:1.7b"}]}


def ollama_up(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/tags"
    return httpx.Response(200, json=TAGS)


def ollama_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connexion refusée", request=request)


def make_client(handler=ollama_up) -> TestClient:
    app = create_app(load_settings(), ollama_transport=httpx.MockTransport(handler))
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def client():
    with make_client() as c:
        yield c


@pytest.fixture
def token():
    return load_settings().server.token


def test_health_without_token(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "ocr": "disabled",
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


def test_rejects_non_local_host_header():
    app = create_app(load_settings(), ollama_transport=httpx.MockTransport(ollama_up))
    with TestClient(app, base_url="http://attaquant.example") as c:
        assert c.get("/health").status_code == 400
