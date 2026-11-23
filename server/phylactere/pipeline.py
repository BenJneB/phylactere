"""Traitement d'une demande POST /translate : cache → téléchargement → OCR → traduction."""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Literal

import httpx
from PIL import Image
from pydantic import BaseModel, Field

from phylactere.cache import Cache, TranslatedBlock, TranslationResult, sha1
from phylactere.config import Settings
from phylactere.fetch import FetchError, check_url, decode_image, fetch_bytes
from phylactere.ocr import OcrEngine
from phylactere.queue import PriorityRunner
from phylactere.schemas import ImageSize, OcrResult
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
    priority: Literal["visible", "prefetch"] = "visible"
    # Modèle Ollama choisi dans l'extension (défaut : celui de la configuration).
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Images voisines dans l'épisode : permettent de lire les bulles coupées entre deux images.
    prev_image_url: str | None = None
    next_image_url: str | None = None


class TranslateResponse(BaseModel):
    blocks: list[TranslatedBlock]
    image_size: ImageSize
    cached: bool
    timings_ms: dict[str, int]


def ocr_fingerprint(settings: Settings) -> str:
    """Empreinte des réglages qui changent le résultat de l'OCR (pas l'appareil)."""
    relevant = {
        "ocr": settings.ocr.model_dump(exclude={"device"}),
        "grouping": settings.grouping.model_dump(),
    }
    return sha1(json.dumps(relevant, sort_keys=True))


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


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

    async def _download(self, url: str) -> bytes:
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

    async def _image(self, url: str) -> Image.Image:
        return decode_image(await self._download(url))

    async def _neighbor(self, url: str | None) -> Image.Image | None:
        """Une voisine illisible n'empêche pas de traiter l'image : on s'en passe."""
        if url is None:
            return None
        try:
            return await self._image(url)
        except FetchError as exc:
            logger.warning("Voisine ignorée (%s) : %s", url, exc)
            return None

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
        image, previous, following = await asyncio.gather(
            self._image(request.image_url),
            self._neighbor(request.prev_image_url),
            self._neighbor(request.next_image_url),
        )
        timings["fetch"] = _ms(start)

        start = time.perf_counter()
        result = await self.ocr_queue.run(
            key, request.priority, lambda: self._ocr.recognize(image, previous, following)
        )
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
            return TranslateResponse(
                blocks=cached.blocks, image_size=cached.image_size, cached=True, timings_ms=timings
            )

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
                self._settings.translation.context_size,
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
                    box=bubble.box, original=bubble.text, translated=text, edges=bubble.edges
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
        return TranslateResponse(
            blocks=result.blocks, image_size=result.image_size, cached=False, timings_ms=timings
        )
