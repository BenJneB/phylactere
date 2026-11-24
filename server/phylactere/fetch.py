"""Téléchargement des images de webtoon (voir docs/SPEC.md, section 5.2).

Seules les URL HTTPS d'un hôte autorisé sont acceptées. Les redirections sont refusées :
elles permettraient de contourner l'allowlist.
"""

from io import BytesIO
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from phylactere.config import FetchConfig
from phylactere.errors import ServiceError

# Les serveurs d'images de Naver refusent (403) les requêtes sans ce Referer.
REFERER = "https://comic.naver.com/"


class FetchError(ServiceError):
    """Échec du téléchargement ; ``status_code`` est le code HTTP à renvoyer au client."""


def check_url(url: str, cfg: FetchConfig) -> None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - lève ValueError si le port est invalide
    except ValueError as exc:
        raise FetchError(f"URL invalide : {url}", 400) from exc
    if parts.scheme != "https":
        raise FetchError("Seules les URL HTTPS sont acceptées.", 400)
    if parts.username or parts.password:
        raise FetchError("URL avec identifiants refusée.", 400)
    if host not in {h.lower() for h in cfg.allowed_hosts}:
        raise FetchError(f"Hôte non autorisé : {host or '(vide)'}", 400)


async def fetch_bytes(client: httpx.AsyncClient, url: str, cfg: FetchConfig) -> bytes:
    check_url(url, cfg)
    too_large = FetchError(f"Image trop volumineuse (plus de {cfg.max_bytes} octets).", 413)
    try:
        async with client.stream(
            "GET",
            url,
            headers={"Referer": REFERER},
            follow_redirects=False,
            timeout=cfg.timeout_s,
        ) as response:
            if response.is_redirect:
                raise FetchError("Redirection refusée.", 502)
            if response.status_code != 200:
                raise FetchError(f"Le serveur d'images a répondu {response.status_code}.", 502)
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cfg.max_bytes:
                raise too_large
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > cfg.max_bytes:
                    raise too_large
            return bytes(data)
    except httpx.TimeoutException as exc:
        raise FetchError(f"Délai dépassé ({cfg.timeout_s} s).", 504) from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Téléchargement impossible : {exc}", 502) from exc


def decode_image(data: bytes) -> Image.Image:
    """Décode l'image en RGB. Lève FetchError (422) si ce n'est pas une image lisible."""
    try:
        with Image.open(BytesIO(data)) as image:
            return image.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise FetchError("Le fichier n'est pas une image lisible.", 422) from exc
