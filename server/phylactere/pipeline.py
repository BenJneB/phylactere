"""Traitement d'une demande POST /translate : cache → téléchargement → OCR → traduction."""

import asyncio
import json
import logging
import time
from collections import OrderedDict

import httpx
from PIL import Image
from pydantic import BaseModel, Field

from phylactere.cache import Cache, TranslatedBlock, TranslationResult, sha1
from phylactere.config import Settings
from phylactere.errors import ServiceError
from phylactere.fetch import FetchError, check_url, decode_image, fetch_bytes
from phylactere.glossary import Suggestion, suggest_entries
from phylactere.ocr import OcrEngine
from phylactere.queue import ImagePriority, PriorityRunner
from phylactere.schemas import OcrResult
from phylactere.translate import OllamaTranslator, prompt_id

logger = logging.getLogger("phylactere.pipeline")

# Images gardées en mémoire : une image sert aussi de voisine aux deux images adjacentes.
IMAGE_MEMORY = 64


class TranslateRequest(BaseModel):
    image_url: str
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    series_id: str = Field(min_length=1, max_length=64)
    episode_id: str = Field(min_length=1, max_length=64)
    index: int = Field(ge=0)
    priority: ImagePriority = "visible"
    # Modèle Ollama choisi dans l'extension (défaut : celui de la configuration).
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Images voisines dans l'épisode : permettent de lire les bulles coupées entre deux images.
    prev_image_url: str | None = None
    next_image_url: str | None = None


class TranslateResponse(TranslationResult):
    cached: bool
    timings_ms: dict[str, int]


class SuggestRequest(BaseModel):
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Épisode ouvert : ses bulles passent en premier (défaut : le plus récemment traduit).
    episode_id: str | None = Field(default=None, min_length=1, max_length=64)


class SuggestResponse(BaseModel):
    entries: list[Suggestion]
    # Nombre de bulles envoyées au modèle.
    bubbles: int


# Version du format des résultats OCR mis en cache : à incrémenter quand Bubble change
# (2 : couleurs bg/fg du jalon 9), pour invalider les résultats déjà en cache.
OCR_FORMAT_VERSION = 2


def ocr_fingerprint(settings: Settings) -> str:
    """Empreinte des réglages qui changent le résultat de l'OCR (pas l'appareil)."""
    relevant = {
        "format": OCR_FORMAT_VERSION,
        "ocr": settings.ocr.model_dump(exclude={"device"}),
        "grouping": settings.grouping.model_dump(),
    }
    return sha1(json.dumps(relevant, sort_keys=True))


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def decode_images(
    data: bytes, previous: bytes | None, following: bytes | None
) -> tuple[Image.Image, Image.Image | None, Image.Image | None]:
    """Décode l'image (FetchError si illisible) et ses voisines (ignorées si illisibles).

    Décodage complet d'un JPEG : à appeler hors de la boucle asyncio.
    """

    def neighbor(raw: bytes | None) -> Image.Image | None:
        if raw is None:
            return None
        try:
            return decode_image(raw)
        except FetchError as exc:
            logger.warning("Voisine ignorée : %s", exc)
            return None

    return decode_image(data), neighbor(previous), neighbor(following)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        images: httpx.AsyncClient,
        ocr: OcrEngine,
        translator: OllamaTranslator,
        cache: Cache,
    ) -> None:
        self._settings = settings
        self._images = images
        self._ocr = ocr
        self._translator = translator
        self._cache = cache
        self._fingerprint = ocr_fingerprint(settings)
        self._memory: OrderedDict[str, bytes] = OrderedDict()
        self._downloads: dict[str, asyncio.Task[bytes]] = {}
        self.ocr_queue = PriorityRunner("ocr")
        self.translation_queue = PriorityRunner("translation")

    def start(self) -> None:
        self.ocr_queue.start()
        self.translation_queue.start()

    async def stop(self) -> None:
        await self.ocr_queue.stop()
        await self.translation_queue.stop()

    # --- Téléchargement (mémoire + fusion des téléchargements simultanés) ---

    async def download(self, url: str) -> bytes:
        if url in self._memory:
            self._memory.move_to_end(url)
            return self._memory[url]
        task = self._downloads.get(url)
        if task is None:
            task = asyncio.create_task(fetch_bytes(self._images, url, self._settings.fetch))
            self._downloads[url] = task
            task.add_done_callback(lambda _: self._downloads.pop(url, None))
        data = await asyncio.shield(task)
        self._memory[url] = data
        self._memory.move_to_end(url)
        while len(self._memory) > IMAGE_MEMORY:
            self._memory.popitem(last=False)
        return data

    async def download_neighbor(self, url: str | None) -> bytes | None:
        """Une voisine illisible n'empêche pas de traiter l'image : on s'en passe."""
        if url is None:
            return None
        try:
            return await self.download(url)
        except FetchError as exc:
            logger.warning("Voisine ignorée (%s) : %s", url, exc)
            return None

    async def recognize(
        self, data: bytes, previous: bytes | None, following: bytes | None
    ) -> OcrResult:
        """Décode les images dans un thread, puis les lit avec l'OCR."""
        images = await asyncio.to_thread(decode_images, data, previous, following)
        return await self._ocr.recognize(*images)

    # --- OCR ---

    async def _ocr_result(self, request: TranslateRequest, timings: dict[str, int]) -> OcrResult:
        key = sha1(
            json.dumps(
                [
                    request.image_url,
                    request.prev_image_url,
                    request.next_image_url,
                    self._fingerprint,
                ]
            )
        )
        cached = await self._cache.get_ocr(key)
        if cached is not None:
            return cached

        start = time.perf_counter()
        data = await asyncio.gather(
            self.download(request.image_url),
            self.download_neighbor(request.prev_image_url),
            self.download_neighbor(request.next_image_url),
        )
        timings["fetch"] = _ms(start)

        # Décodage dans la tâche de la file : une demande fusionnée avec une autre ne
        # décode rien, et la tâche en attente ne garde que les octets.
        start = time.perf_counter()
        result = await self.ocr_queue.run(key, request.priority, lambda: self.recognize(*data))
        timings["ocr"] = _ms(start)
        await self._cache.put_ocr(key, request.series_id, result)
        return result

    # --- Traduction ---

    async def translate(self, request: TranslateRequest) -> TranslateResponse:
        for url in (request.image_url, request.prev_image_url, request.next_image_url):
            if url is not None:
                check_url(url, self._settings.fetch)

        lang = request.target_lang or self._settings.translation.target_lang
        model = request.model or self._settings.ollama.model
        key = f"{sha1(request.image_url)}:{lang}:{model}:{prompt_id(self._settings.translation)}"
        timings = {"fetch": 0, "ocr": 0, "translate": 0}

        cached = await self._cache.get_translation(key)
        if cached is not None:
            return TranslateResponse(**dict(cached), cached=True, timings_ms=timings)

        ocr = await self._ocr_result(request, timings)
        texts = [bubble.text for bubble in ocr.bubbles]

        start = time.perf_counter()
        translations: list[str] = []
        if texts:
            context = await self._cache.context(
                request.series_id,
                request.episode_id,
                request.index,
                lang,
                model,
                limit=self._settings.translation.context_size,
            )
            glossary = await self._cache.get_glossary(request.series_id)
            translations = await self.translation_queue.run(
                key,
                request.priority,
                lambda: self._translator.translate(texts, lang, context, glossary, model),
            )
        timings["translate"] = _ms(start)

        result = TranslationResult(
            blocks=[
                TranslatedBlock(
                    box=bubble.box,
                    original=bubble.text,
                    translated=text,
                    edges=bubble.edges,
                    bg=bubble.bg,
                    fg=bubble.fg,
                )
                for bubble, text in zip(ocr.bubbles, translations, strict=True)
            ],
            image_size=ocr.image_size,
        )
        await self._cache.put_translation(
            key, request.series_id, request.episode_id, request.index, lang, model, result
        )
        logger.info(
            "Image %s/%s #%d : %d bulle(s), %s",
            request.series_id,
            request.episode_id,
            request.index,
            len(result.blocks),
            timings,
        )
        return TranslateResponse(**dict(result), cached=False, timings_ms=timings)

    # --- Propositions de glossaire ---

    async def suggest_glossary(self, series_id: str, request: SuggestRequest) -> SuggestResponse:
        """Propose des entrées de glossaire à partir des bulles traduites en cache.

        Passe par la file de traduction en priorité « background » : après les images, et
        jamais deux modèles chargés à la fois dans Ollama.
        """
        lang = request.target_lang or self._settings.translation.target_lang
        model = request.model or self._settings.ollama.model
        cfg = self._settings.glossary
        bubbles = await self._cache.series_bubbles(
            series_id, lang, request.episode_id, cfg.suggest_bubbles
        )
        if not bubbles:
            raise ServiceError(
                "Aucune bulle traduite en cache pour cette série : lisez d'abord un épisode "
                "avec la traduction activée.",
                404,
            )
        glossary = await self._cache.get_glossary(series_id)
        key = f"suggest:{series_id}:{lang}:{model}:{request.episode_id}"
        start = time.perf_counter()
        entries = await self.translation_queue.run(
            key,
            "background",
            lambda: suggest_entries(self._translator, bubbles, glossary, lang, model, cfg.num_ctx),
        )
        logger.info(
            "Propositions de glossaire %s : %d entrée(s) en %d ms.",
            series_id,
            len(entries),
            _ms(start),
        )
        return SuggestResponse(entries=entries, bubbles=len(bubbles))
