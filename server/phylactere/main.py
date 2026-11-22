"""Application FastAPI et routes."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from phylactere import __version__
from phylactere.config import Settings, load_settings

TOKEN_HEADER = "X-Phylactere-Token"
# Origines des extensions : chrome-extension://<id> (Chrome) et moz-extension://<uuid> (Firefox).
EXTENSION_ORIGIN_REGEX = r"^(chrome|moz)-extension://[a-z0-9-]+$"
# Refuse les requêtes dont l'en-tête Host n'est pas local (protection contre le DNS rebinding).
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
OLLAMA_TIMEOUT_S = 2.0


class Health(BaseModel):
    ok: bool
    # "disabled" tant que l'OCR n'existe pas (jalon 1)
    ocr: Literal["ready", "loading", "error", "disabled"]
    ollama: Literal["ok", "down"]
    model: str
    device: Literal["gpu", "cpu"]
    version: str


def require_token(
    request: Request,
    token: str | None = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    expected = request.app.state.settings.server.token
    if token is None or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton absent ou invalide.")


def create_app(
    settings: Settings | None = None,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Fabrique l'application. ``ollama_transport`` permet de simuler Ollama dans les tests."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=settings.ollama.url,
            timeout=OLLAMA_TIMEOUT_S,
            transport=ollama_transport,
        ) as client:
            app.state.ollama = client
            yield

    app = FastAPI(title="Phylactère", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=EXTENSION_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
    )

    @app.get("/health")
    async def health(request: Request) -> Health:
        try:
            response = await request.app.state.ollama.get("/api/tags")
            response.raise_for_status()
            ollama = "ok"
        except httpx.HTTPError:
            ollama = "down"
        return Health(
            ok=True,
            ocr="disabled",
            ollama=ollama,
            model=settings.ollama.model,
            device=settings.ocr.device,
            version=__version__,
        )

    protected = APIRouter(dependencies=[Depends(require_token)])

    @protected.get("/models")
    async def models(request: Request) -> dict:
        """Modèles installés dans Ollama (proxy de GET /api/tags)."""
        try:
            response = await request.app.state.ollama.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Ollama injoignable : {exc}"
            ) from exc
        return response.json()

    app.include_router(protected)
    return app
