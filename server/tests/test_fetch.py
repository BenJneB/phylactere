import asyncio

import httpx
import pytest

from phylactere.config import FetchConfig
from phylactere.fetch import REFERER, FetchError, check_url, decode_image, fetch_bytes
from tests.helpers import png_bytes

CFG = FetchConfig(allowed_hosts=["image-comic.pstatic.net"], max_bytes=1000, timeout_s=15)
URL = "https://image-comic.pstatic.net/webtoon/1/2/image.jpg"


def fetch(handler, url=URL, cfg=CFG) -> bytes:
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_bytes(client, url, cfg)

    return asyncio.run(go())


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://IMAGE-COMIC.pstatic.net/x.jpg",
        "https://image-comic.pstatic.net:443/x.jpg",
    ],
)
def test_allowed_urls(url):
    check_url(url, CFG)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://image-comic.pstatic.net/x.jpg", "HTTPS"),
        ("https://evil.example/x.jpg", "non autorisé"),
        ("https://image-comic.pstatic.net.evil.example/x.jpg", "non autorisé"),
        ("https://evil.example#@image-comic.pstatic.net/x.jpg", "non autorisé"),
        ("https://user:pass@image-comic.pstatic.net/x.jpg", "identifiants"),
        ("https://127.0.0.1/x.jpg", "non autorisé"),
        ("file:///etc/passwd", "HTTPS"),
        ("https://image-comic.pstatic.net:99999/x.jpg", "invalide"),
        ("pas une url", "HTTPS"),
    ],
)
def test_refused_urls(url, message):
    with pytest.raises(FetchError, match=message) as info:
        check_url(url, CFG)
    assert info.value.status_code == 400


def test_sends_naver_referer():
    seen = {}

    def handler(request):
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, content=b"data")

    assert fetch(handler) == b"data"
    assert seen["referer"] == REFERER


def test_refused_url_makes_no_request():
    def handler(request):
        raise AssertionError("aucune requête attendue")

    with pytest.raises(FetchError):
        fetch(handler, url="https://evil.example/x.jpg")


def test_redirect_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.example/x.jpg"})

    with pytest.raises(FetchError, match="Redirection") as info:
        fetch(handler)
    assert info.value.status_code == 502


def test_upstream_error_status():
    with pytest.raises(FetchError, match="403") as info:
        fetch(lambda request: httpx.Response(403))
    assert info.value.status_code == 502


def test_declared_size_too_large():
    def handler(request):
        return httpx.Response(200, headers={"Content-Length": "5000"}, content=b"x" * 5000)

    with pytest.raises(FetchError, match="volumineuse") as info:
        fetch(handler)
    assert info.value.status_code == 413


def test_streamed_size_too_large_without_content_length():
    async def chunks():
        for _ in range(20):
            yield b"x" * 100

    def handler(request):
        return httpx.Response(200, content=chunks())

    with pytest.raises(FetchError, match="volumineuse"):
        fetch(handler)


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("trop long", request=request)

    with pytest.raises(FetchError, match="Délai") as info:
        fetch(handler)
    assert info.value.status_code == 504


def test_connection_error():
    def handler(request):
        raise httpx.ConnectError("refusé", request=request)

    with pytest.raises(FetchError) as info:
        fetch(handler)
    assert info.value.status_code == 502


def test_decode_image():
    image = decode_image(png_bytes((4, 3), "red"))
    assert image.size == (4, 3) and image.mode == "RGB"


def test_decode_invalid_image():
    with pytest.raises(FetchError) as info:
        decode_image(b"<html>pas une image</html>")
    assert info.value.status_code == 422
