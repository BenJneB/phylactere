"""Application FastAPI et routes."""

import asyncio
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from phylactere import __version__
from phylactere.cache import Cache, GlossaryEntry
from phylactere.config import Settings, load_settings
from phylactere.debug import render_debug
from phylactere.errors import ServiceError
from phylactere.fetch import FetchError
from phylactere.ocr import OcrEngine
from phylactere.pipeline import (
    Pipeline,
    SuggestRequest,
    SuggestResponse,
    TranslateRequest,
    TranslateResponse,
    decode_images,
)
from phylactere.reports import ReportRequest, ReportResponse
from phylactere.schemas import OcrResult
from phylactere.translate import OllamaTranslator

TOKEN_HEADER = "X-Phylactere-Token"
# Origines des extensions : chrome-extension://<id> (Chrome) et moz-extension://<uuid> (Firefox).
EXTENSION_ORIGIN_REGEX = r"^(chrome|moz)-extension://[a-z0-9-]+$"
# Refuse les requêtes dont l'en-tête Host n'est pas local (protection contre le DNS rebinding).
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

logger = logging.getLogger("phylactere.main")
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
            cache = Cache(settings.cache.resolved_path())
            translator = OllamaTranslator(ollama, settings.ollama, settings.translation)
            pipeline = Pipeline(settings, images, engine, translator, cache)
            pipeline.start()
            app.state.cache = cache
            app.state.pipeline = pipeline
            # Charge le modèle de traduction pendant que l'utilisateur ouvre l'épisode.
            preload = asyncio.create_task(translator.preload())
            try:
                yield
            finally:
                preload.cancel()
                await pipeline.stop()
                cache.close()
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

    # Téléchargement, OCR pas prêt, Ollama : un seul endroit pour le code HTTP renvoyé.
    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers=headers)

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

    @protected.post("/translate")
    async def translate(request: Request, body: TranslateRequest) -> TranslateResponse:
        """OCR et traduction des bulles d'une image de l'épisode."""
        return await request.app.state.pipeline.translate(body)

    @protected.post("/report")
    async def report(request: Request, body: ReportRequest) -> ReportResponse:
        """Signalement d'une bulle (Alt+clic dans l'extension) : JSON et découpe PNG dans
        le dossier des signalements, pour constituer un corpus d'échecs."""
        return await request.app.state.pipeline.report(body)

    @protected.get("/glossary/{series_id}")
    async def get_glossary(request: Request, series_id: str) -> list[GlossaryEntry]:
        return await request.app.state.cache.get_glossary(series_id)

    @protected.put("/glossary/{series_id}")
    async def put_glossary(
        request: Request, series_id: str, entries: list[GlossaryEntry]
    ) -> list[GlossaryEntry]:
        """Remplace le glossaire ; les traductions en cache des images de la série qui
        mentionnent un terme ajouté, retiré ou modifié sont effacées."""
        removed = await request.app.state.cache.put_glossary(series_id, entries)
        logger.info(
            "Glossaire %s : %d entrée(s), %d image(s) à retraduire.",
            series_id,
            len(entries),
            removed,
        )
        return entries

    @protected.post("/glossary/{series_id}/suggest")
    async def suggest_glossary(
        request: Request, series_id: str, body: SuggestRequest
    ) -> SuggestResponse:
        """Propositions d'entrées (rien n'est enregistré), à partir des bulles en cache."""
        return await request.app.state.pipeline.suggest_glossary(series_id, body)

    @protected.delete("/cache")
    async def clear_cache(request: Request, series_id: str | None = None) -> dict:
        removed = await request.app.state.cache.clear(series_id)
        return {"removed": removed}

    @protected.post("/debug/ocr", response_model=None)
    async def debug_ocr(
        request: Request,
        image_url: Annotated[str | None, Form()] = None,
        file: Annotated[UploadFile | None, File()] = None,
        prev_image_url: Annotated[str | None, Form()] = None,
        next_image_url: Annotated[str | None, Form()] = None,
        output: Annotated[Literal["png", "json"], Query(alias="format")] = "png",
    ) -> Response | OcrResult:
        """OCR d'une image (URL autorisée ou fichier envoyé), sans cache.

        ``prev_image_url`` / ``next_image_url`` : images voisines, pour lire les bulles
        coupées entre deux images. Renvoie un PNG annoté (lignes en rouge, bulles en vert,
        bulles au bord en orange), ou le résultat brut avec ``?format=json``.
        """
        if (image_url is None) == (file is None):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Fournir soit image_url, soit file (un seul)."
            )
        # Même chargement que /translate (allowlist, mémoire, voisine illisible ignorée).
        pipeline = request.app.state.pipeline
        if image_url is not None:
            data = await pipeline.download(image_url)
        else:
            data = await file.read(settings.fetch.max_bytes + 1)
            if len(data) > settings.fetch.max_bytes:
                raise FetchError("Fichier trop volumineux.", 413)
        previous, following = await asyncio.gather(
            pipeline.download_neighbor(prev_image_url or None),
            pipeline.download_neighbor(next_image_url or None),
        )
        images = await asyncio.to_thread(decode_images, data, previous, following)
        result = await request.app.state.ocr.recognize(*images)

        if output == "json":
            return result
        return Response(render_debug(images[0], result), media_type="image/png")

    app.include_router(protected)
    return app
