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
from phylactere.cache import Cache, Correction, GlossaryEntry, TranslatedBlock
from phylactere.config import Settings, load_settings, reports_dir
from phylactere.debug import render_debug
from phylactere.errors import ServiceError
from phylactere.export import pdf_name
from phylactere.fetch import FetchError, decode_image
from phylactere.ocr import OcrEngine
from phylactere.pipeline import (
    CorrectionRequest,
    CorrectionTarget,
    ExportRequest,
    Pipeline,
    RetranslateRequest,
    Stats,
    SuggestRequest,
    SuggestResponse,
    SummaryRequest,
    SummaryResponse,
    TranslateRequest,
    TranslateResponse,
)
from phylactere.reports import ReportRequest, ReportResponse, delete_report
from phylactere.schemas import OcrResult
from phylactere.translate import OllamaTranslator

TOKEN_HEADER = "X-Phylactere-Token"
# Réponse de POST /export : nombre d'images exportées sans bulles (pas encore traduites).
MISSING_HEADER = "X-Phylactere-Missing"
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


async def ollama_tags(request: Request) -> dict:
    """Modèles installés dans Ollama (GET /api/tags) ; lève httpx.HTTPError s'il ne répond pas."""
    response = await request.app.state.ollama.get("/api/tags")
    response.raise_for_status()
    return response.json()


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
            cache = Cache(settings.cache.resolved_path(), settings.cache.max_bytes())
            translator = OllamaTranslator(ollama, settings.ollama, settings.translation)
            pipeline = Pipeline(settings, images, engine, translator, cache)
            pipeline.start()
            app.state.cache = cache
            app.state.pipeline = pipeline
            # Charge le modèle de traduction pendant que l'utilisateur ouvre l'épisode.
            preload = asyncio.create_task(translator.preload())
            # Ramène le cache sous sa taille maximale (réglage abaissé depuis le dernier
            # lancement) ; ensuite, toutes les PURGE_EVERY écritures.
            purge = asyncio.create_task(cache.purge())
            try:
                yield
            finally:
                preload.cancel()
                await asyncio.gather(purge, return_exceptions=True)
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
        expose_headers=[MISSING_HEADER, "Content-Disposition"],
    )

    # Téléchargement, OCR pas prêt, Ollama : un seul endroit pour le code HTTP renvoyé.
    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers=headers)

    @app.get("/health")
    async def health(request: Request) -> Health:
        try:
            await ollama_tags(request)
            ollama = "ok"
        except (httpx.HTTPError, ValueError):  # ValueError : réponse non JSON
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
            return await ollama_tags(request)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Ollama injoignable : {exc}"
            ) from exc

    @protected.get("/stats")
    async def stats(request: Request) -> Stats:
        """Cache, temps moyens et files d'attente, pour le popup de l'extension."""
        return await request.app.state.pipeline.stats(__version__, request.app.state.ocr.device)

    @protected.post("/translate")
    async def translate(request: Request, body: TranslateRequest) -> TranslateResponse:
        """OCR et traduction des bulles d'une image de l'épisode."""
        return await request.app.state.pipeline.translate(body)

    @protected.post("/retranslate")
    async def retranslate(request: Request, body: RetranslateRequest) -> TranslatedBlock:
        """Retraduit une bulle (Maj+clic dans l'extension) et la remplace dans le cache."""
        return await request.app.state.pipeline.retranslate(body)

    @protected.post("/summary")
    async def summary(request: Request, body: SummaryRequest) -> SummaryResponse:
        """Résumé « Précédemment… » d'un épisode, à partir des bulles traduites en cache.
        404 si l'épisode n'a pas été lu avec Phylactère."""
        return await request.app.state.pipeline.summary(body)

    @protected.post("/export", response_class=Response)
    async def export(request: Request, body: ExportRequest) -> Response:
        """PDF de l'épisode traduit (bouton « Exporter en PDF » du popup) : traductions du
        cache, images retéléchargées. 404 si l'épisode n'a aucune traduction en cache."""
        pdf, report = await request.app.state.pipeline.export(body)
        name = pdf_name(
            body.series_id, body.episode_id, body.target_lang or settings.translation.target_lang
        )
        return Response(
            pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                MISSING_HEADER: str(len(report.missing)),
            },
        )

    @protected.put("/correction")
    async def put_correction(request: Request, body: CorrectionRequest) -> Correction:
        """Correction d'une bulle à la main (Ctrl+clic dans l'extension). Indépendante du
        modèle et de la version du prompt ; « Vider le cache » ne l'efface pas."""
        return await request.app.state.pipeline.correct(body)

    @protected.delete("/correction")
    async def delete_correction(request: Request, body: CorrectionTarget) -> dict:
        """Efface la correction d'une bulle : la traduction du modèle revient."""
        return {"removed": await request.app.state.pipeline.uncorrect(body)}

    @protected.post("/report")
    async def report(request: Request, body: ReportRequest) -> ReportResponse:
        """Signalement d'une bulle (Alt+clic dans l'extension) : JSON et découpe PNG dans
        le dossier des signalements, pour constituer un corpus d'échecs."""
        return await request.app.state.pipeline.report(body)

    @protected.delete("/report/{report_id}")
    async def cancel_report(report_id: str) -> dict:
        """Annule un signalement (second Alt+clic sur la bulle) : supprime ses fichiers."""
        if not await asyncio.to_thread(delete_report, reports_dir(), report_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Signalement introuvable.")
        logger.info("Signalement %s annulé.", report_id)
        return {"removed": report_id}

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
        result = await pipeline.recognize(data, previous, following)

        if output == "json":
            return result
        image = await asyncio.to_thread(decode_image, data)
        return Response(render_debug(image, result), media_type="image/png")

    app.include_router(protected)
    return app
