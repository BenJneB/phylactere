"""Application FastAPI et routes."""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from phylactere import __version__
from phylactere.config import Settings, load_settings
from phylactere.debug import render_debug
from phylactere.fetch import FetchError, decode_image, fetch_bytes
from phylactere.ocr import OcrEngine, OcrUnavailable
from phylactere.schemas import OcrResult

TOKEN_HEADER = "X-Phylactere-Token"
# Origines des extensions : chrome-extension://<id> (Chrome) et moz-extension://<uuid> (Firefox).
EXTENSION_ORIGIN_REGEX = r"^(chrome|moz)-extension://[a-z0-9-]+$"
# Refuse les requêtes dont l'en-tête Host n'est pas local (protection contre le DNS rebinding).
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
OLLAMA_TIMEOUT_S = 2.0


class Health(BaseModel):
    ok: bool
    ocr: Literal["ready", "loading", "error"]
    ollama: Literal["ok", "down"]
    model: str
    # "auto" tant que le modèle charge et que l'appareil n'est pas encore choisi
    device: Literal["gpu", "cpu", "auto"]
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
    images_transport: httpx.AsyncBaseTransport | None = None,
    detector_factory=None,
) -> FastAPI:
    """Fabrique l'application.

    Les tests simulent Ollama (``ollama_transport``), le serveur d'images
    (``images_transport``) et PaddleOCR (``detector_factory``).
    """
    settings = settings or load_settings()
    # paddlex active la journalisation globale : sans ça, chaque requête HTTP interne
    # (Ollama, images) s'afficherait dans la console.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = OcrEngine(settings.ocr, settings.grouping, detector_factory)
        engine.start()
        app.state.ocr = engine
        async with (
            httpx.AsyncClient(
                base_url=settings.ollama.url,
                timeout=OLLAMA_TIMEOUT_S,
                transport=ollama_transport,
            ) as ollama,
            httpx.AsyncClient(transport=images_transport) as images,
        ):
            app.state.ollama = ollama
            app.state.images = images
            try:
                yield
            finally:
                engine.close()

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
            ocr=request.app.state.ocr.status,
            ollama=ollama,
            model=settings.ollama.model,
            device=request.app.state.ocr.device,
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

    @protected.post("/debug/ocr", response_model=None)
    async def debug_ocr(
        request: Request,
        image_url: Annotated[str | None, Form()] = None,
        file: Annotated[UploadFile | None, File()] = None,
        output: Annotated[Literal["png", "json"], Query(alias="format")] = "png",
    ) -> Response | OcrResult:
        """OCR d'une image (URL autorisée ou fichier envoyé).

        Renvoie un PNG annoté (lignes en rouge, bulles en vert, bulles au bord en orange),
        ou le résultat brut avec ``?format=json``.
        """
        if (image_url is None) == (file is None):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Fournir soit image_url, soit file (un seul)."
            )
        try:
            if image_url is not None:
                data = await fetch_bytes(request.app.state.images, image_url, settings.fetch)
            else:
                data = await file.read(settings.fetch.max_bytes + 1)
                if len(data) > settings.fetch.max_bytes:
                    raise FetchError("Fichier trop volumineux.", 413)
            image = decode_image(data)
        except FetchError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

        try:
            result = await request.app.state.ocr.recognize(image)
        except OcrUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, str(exc), headers={"Retry-After": "5"}
            ) from exc

        if output == "json":
            return result
        return Response(render_debug(image, result), media_type="image/png")

    app.include_router(protected)
    return app
