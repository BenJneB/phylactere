"""Traitement d'une demande POST /translate : cache → téléchargement → OCR → traduction."""

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from typing import Annotated, Literal

import httpx
from PIL import Image
from pydantic import BaseModel, Field, StringConstraints

from phylactere.cache import (
    Cache,
    Correction,
    StoredSummary,
    TranslatedBlock,
    TranslationResult,
    sha1,
    translation_key,
)
from phylactere.config import Settings, reports_dir
from phylactere.errors import ServiceError
from phylactere.fetch import FetchError, check_url, decode_image, fetch_bytes
from phylactere.glossary import Suggestion, suggest_entries
from phylactere.ocr import OcrEngine
from phylactere.queue import ImagePriority, PriorityRunner
from phylactere.reports import (
    ReportRequest,
    ReportResponse,
    crop_bubble,
    find_bubble,
    write_report,
)
from phylactere.schemas import Box, ImageRequest, OcrResult, match_box
from phylactere.summary import SUMMARY_VERSION, summarize
from phylactere.translate import PROMPT_ID, PROMPT_VERSION, OllamaTranslator

logger = logging.getLogger("phylactere.pipeline")

# Images gardées en mémoire : une image sert aussi de voisine aux deux images adjacentes.
IMAGE_MEMORY = 64
# Statistiques (GET /stats) : temps des N dernières images calculées.
TIMINGS_WINDOW = 50
PHASES = ("fetch", "ocr", "translate")


class RetranslateModel(BaseModel):
    # Modèle choisi dans le popup pour retraduire (jalon 13 ; défaut : ``model``). Le
    # résultat reste rangé sous la clé de ``model``, le modèle affiché, sinon il
    # disparaîtrait au rechargement de la page.
    retranslate_model: str | None = Field(default=None, min_length=1, max_length=100)


class TranslateRequest(ImageRequest, RetranslateModel):
    priority: ImagePriority = "visible"
    # Maj+Alt+T (jalon 13) : retraduit l'image en ignorant la traduction en cache, puis la
    # remplace ; l'OCR en cache est réutilisé.
    force: bool = False


class RetranslateRequest(ImageRequest, RetranslateModel):
    """Maj+clic sur une bulle (jalon 13) : bulle telle que l'extension l'affiche."""

    box: Box


class CorrectionTarget(ImageRequest):
    """Bulle telle que l'extension l'affiche (jalon 14) : la correction est rangée par image
    et langue cible, et retrouvée par sa boîte et son original."""

    box: Box
    original: str = Field(max_length=5000)


class CorrectionRequest(CorrectionTarget):
    # Texte saisi : vide, l'extension efface la correction (DELETE /correction).
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
    # Traduction du modèle remplacée (corpus des corrections, scripts/reports.py).
    translated: str | None = Field(default=None, max_length=5000)


class TranslateResponse(TranslationResult):
    cached: bool
    timings_ms: dict[str, int]


class PhaseTimings(BaseModel):
    """Temps d'une étape sur les dernières images où elle a vraiment tourné (attente en
    file comprise pour l'OCR et la traduction)."""

    count: int
    avg_ms: int
    max_ms: int


class ImageStats(BaseModel):
    # Réponses à /translate depuis le démarrage, et parmi elles celles venues du cache.
    served: int
    cached: int


class CacheStats(BaseModel):
    size_bytes: int
    max_bytes: int  # 0 = sans limite
    ocr: int
    translations: int
    glossaries: int
    corrections: int
    summaries: int


class Stats(BaseModel):
    version: str
    uptime_s: int
    model: str
    device: str
    images: ImageStats
    timings_ms: dict[str, PhaseTimings]
    # Tâches en file par priorité (visible, prefetch, background) et en cours (running).
    queues: dict[str, dict[str, int]]
    cache: CacheStats


class SuggestRequest(BaseModel):
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Épisode ouvert : ses bulles passent en premier (défaut : le plus récemment traduit).
    episode_id: str | None = Field(default=None, min_length=1, max_length=64)


class SuggestResponse(BaseModel):
    entries: list[Suggestion]
    # Nombre de bulles envoyées au modèle.
    bubbles: int


class SummaryRequest(BaseModel):
    """Résumé « Précédemment… » d'un épisode (jalon 15)."""

    series_id: str = Field(min_length=1, max_length=64)
    episode_id: str = Field(min_length=1, max_length=64)
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    # Nombre d'images de l'épisode, quand l'extension le connaît : moins d'images traduites
    # en cache = épisode lu en partie.
    image_count: int | None = Field(default=None, ge=1, le=2000)
    # « background » : épisode courant, résumé d'avance pour l'ouverture du suivant ;
    # « prefetch » : résumé de l'épisode précédent qui manque à l'ouverture.
    priority: Literal["prefetch", "background"] = "prefetch"


class SummaryResponse(BaseModel):
    series_id: str
    episode_id: str
    summary: str
    # Épisode lu en partie : des images de l'épisode manquent dans le cache.
    partial: bool
    images: int  # images traduites résumées
    bubbles: int
    model: str
    cached: bool


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
        self._started = time.monotonic()
        self._served = 0
        self._served_cached = 0
        self._timings: dict[str, deque[int]] = {
            phase: deque(maxlen=TIMINGS_WINDOW) for phase in PHASES
        }

    def start(self) -> None:
        self.ocr_queue.start()
        self.translation_queue.start()

    async def stop(self) -> None:
        await self.ocr_queue.stop()
        await self.translation_queue.stop()

    # --- Statistiques ---

    def _record(self, phase: str, ms: int) -> None:
        self._timings[phase].append(ms)

    async def stats(self, version: str, device: str) -> Stats:
        timings = {
            phase: PhaseTimings(
                count=len(values),
                avg_ms=round(sum(values) / len(values)) if values else 0,
                max_ms=max(values, default=0),
            )
            for phase, values in self._timings.items()
        }
        return Stats(
            version=version,
            uptime_s=round(time.monotonic() - self._started),
            model=self._settings.ollama.model,
            device=device,
            images=ImageStats(served=self._served, cached=self._served_cached),
            timings_ms=timings,
            queues={
                "ocr": self.ocr_queue.snapshot(),
                "translation": self.translation_queue.snapshot(),
            },
            cache=CacheStats(**await self._cache.stats()),
        )

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

    def _lang_model(self, target_lang: str | None, model: str | None) -> tuple[str, str]:
        """Langue et modèle demandés par l'extension, sinon ceux de la configuration."""
        return (
            target_lang or self._settings.translation.target_lang,
            model or self._settings.ollama.model,
        )

    def _check(self, request: ImageRequest) -> tuple[str, str]:
        """Contrôle les URL de l'image et de ses voisines ; renvoie la langue et le modèle."""
        for url in (request.image_url, request.prev_image_url, request.next_image_url):
            if url is not None:
                check_url(url, self._settings.fetch)
        return self._lang_model(request.target_lang, request.model)

    # --- OCR ---

    async def _ocr_result(
        self, request: ImageRequest, priority: ImagePriority, timings: dict[str, int]
    ) -> OcrResult:
        """Résultat OCR de l'image (cache, sinon téléchargement et file OCR)."""
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
        self._record("fetch", timings["fetch"])

        # Décodage dans la tâche de la file : une demande fusionnée avec une autre ne
        # décode rien, et la tâche en attente ne garde que les octets.
        start = time.perf_counter()
        result = await self.ocr_queue.run(key, priority, lambda: self.recognize(*data))
        timings["ocr"] = _ms(start)
        self._record("ocr", timings["ocr"])
        await self._cache.put_ocr(key, request.series_id, result)
        return result

    # --- Traduction ---

    @staticmethod
    def _translation_key(image_url: str, lang: str, model: str) -> str:
        return translation_key(image_url, lang, model, PROMPT_ID)

    async def translate(self, request: TranslateRequest) -> TranslateResponse:
        lang, model = self._check(request)
        key = self._translation_key(request.image_url, lang, model)
        timings = {"fetch": 0, "ocr": 0, "translate": 0}
        # Modèle qui traduit : celui de la clé, sauf pour une retraduction forcée.
        translator_model = (request.force and request.retranslate_model) or model
        # Une retraduction ne doit pas être fusionnée avec une traduction ordinaire en
        # cours de la même image : elle en attendrait le résultat, sans rien retraduire.
        task_key = f"force:{key}:{translator_model}" if request.force else key

        if not request.force:
            cached = await self._cache.get_translation(key)
            if cached is not None:
                self._served += 1
                self._served_cached += 1
                cached = await self._cache.corrected(request.image_url, lang, cached)
                return TranslateResponse(**dict(cached), cached=True, timings_ms=timings)

        ocr = await self._ocr_result(request, request.priority, timings)
        texts = [bubble.text for bubble in ocr.bubbles]

        start = time.perf_counter()
        translations: list[str] = []
        glossary_version = self._cache.glossary_version(request.series_id)
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
                task_key,
                request.priority,
                lambda: self._translator.translate(
                    texts, lang, context, glossary, translator_model
                ),
            )
        timings["translate"] = _ms(start)
        if texts:
            self._record("translate", timings["translate"])

        result = TranslationResult(
            blocks=[
                TranslatedBlock(
                    **bubble.model_dump(exclude={"text", "lines"}),
                    original=bubble.text,
                    translated=text,
                    model=translator_model,
                )
                for bubble, text in zip(ocr.bubbles, translations, strict=True)
            ],
            image_size=ocr.image_size,
        )
        await self._cache.put_translation(
            key,
            request.series_id,
            request.episode_id,
            request.index,
            lang,
            model,
            result,
            glossary_version,
        )
        logger.info(
            "Image %s/%s #%d%s : %d bulle(s), %s",
            request.series_id,
            request.episode_id,
            request.index,
            f" retraduite par {translator_model}" if request.force else "",
            len(result.blocks),
            timings,
        )
        self._served += 1
        # Corrections appliquées à la réponse seulement : le cache garde la traduction du
        # modèle, qui revient si la correction est effacée.
        result = await self._cache.corrected(request.image_url, lang, result)
        return TranslateResponse(**dict(result), cached=False, timings_ms=timings)

    # --- Retraduction d'une bulle (jalon 13) ---

    async def retranslate(self, request: RetranslateRequest) -> TranslatedBlock:
        """Retraduit une seule bulle de l'image, avec le contexte habituel plus les autres
        bulles de l'image, et la remplace dans la traduction en cache. Renvoie la bulle.

        La bulle est cherchée dans la traduction en cache (même recherche par boîte que
        /report dans l'OCR) : c'est elle qu'on remplace, et elle porte déjà l'original.
        """
        lang, model = self._check(request)
        translator_model = request.retranslate_model or model
        key = self._translation_key(request.image_url, lang, model)
        cached = await self._cache.get_translation(key)
        if cached is None:
            raise ServiceError(
                "Traduction de l'image absente du cache (cache vidé ou glossaire modifié) : "
                "rechargez la page.",
                404,
            )
        # Corrections appliquées : une bulle corrigée n'est pas retraduite, et les autres
        # bulles passent au modèle sous leur forme corrigée.
        cached = await self._cache.corrected(request.image_url, lang, cached)
        index = match_box([block.box for block in cached.blocks], request.box)
        if index is None:
            raise ServiceError("Bulle introuvable dans la traduction en cache.", 404)
        block = cached.blocks[index]
        if block.corrected:
            raise ServiceError("Bulle corrigée : effacez la correction d'abord.", 409)
        neighbors = [
            (other.original, other.translated)
            for i, other in enumerate(cached.blocks)
            if i != index
        ]

        glossary_version = self._cache.glossary_version(request.series_id)
        context = await self._cache.context(
            request.series_id,
            request.episode_id,
            request.index,
            lang,
            model,
            limit=self._settings.translation.context_size,
        )
        glossary = await self._cache.get_glossary(request.series_id)
        start = time.perf_counter()
        (text,) = await self.translation_queue.run(
            f"retranslate:{key}:{index}:{translator_model}",
            "visible",
            lambda: self._translator.translate(
                [block.original], lang, context, glossary, translator_model, neighbors
            ),
        )
        updated = block.model_copy(update={"translated": text, "model": translator_model})
        # Glossaire enregistré pendant la retraduction : put_glossary a pu effacer l'image,
        # qui sera retraduite en entier avec lui ; on ne remet pas l'ancien glossaire.
        stored = glossary_version == self._cache.glossary_version(
            request.series_id
        ) and await self._cache.replace_block(key, updated)
        logger.info(
            "Bulle %d de l'image %s/%s #%d retraduite par %s en %d ms%s.",
            index,
            request.series_id,
            request.episode_id,
            request.index,
            translator_model,
            _ms(start),
            "" if stored else " (pas mise en cache : traduction de l'image modifiée)",
        )
        return updated

    # --- Corrections (jalon 14) ---

    async def correct(self, request: CorrectionRequest) -> Correction:
        """Enregistre le texte saisi pour une bulle (Ctrl+clic dans l'extension)."""
        lang, model = self._check(request)
        correction = Correction(box=request.box, original=request.original, text=request.text)
        await self._cache.put_correction(
            request.image_url,
            request.series_id,
            request.episode_id,
            request.index,
            lang,
            correction,
            model,
            request.translated,
        )
        logger.info(
            "Bulle de l'image %s/%s #%d corrigée.",
            request.series_id,
            request.episode_id,
            request.index,
        )
        return correction

    async def uncorrect(self, request: CorrectionTarget) -> int:
        """Efface la correction d'une bulle : la traduction du modèle revient."""
        lang, _ = self._check(request)
        removed = await self._cache.delete_correction(
            request.image_url, lang, request.box, request.original
        )
        logger.info(
            "Correction effacée sur l'image %s/%s #%d (%d).",
            request.series_id,
            request.episode_id,
            request.index,
            removed,
        )
        return removed

    # --- Résumé « Précédemment… » (jalon 15) ---

    async def summary(self, request: SummaryRequest) -> SummaryResponse:
        """Résumé de l'épisode à partir des traductions en cache (corrections comprises).

        Gardé en cache avec l'empreinte des bulles résumées : refait seulement si elles ont
        changé (images traduites depuis, correction, retraduction) ou si le prompt a
        changé. Plus aucune bulle en cache (cache vidé ou purgé) : le résumé gardé reste
        servi. Passe par la file de traduction : jamais deux modèles chargés à la fois.
        """
        lang, model = self._lang_model(request.target_lang, request.model)
        images = await self._cache.episode_bubbles(
            request.series_id, request.episode_id, lang, model
        )
        bubbles = sum(len(texts) for _, texts in images)
        # Images manquantes : un trou dans les index, ou moins d'images que l'épisode.
        expected = max(request.image_count or 0, images[-1][0] + 1 if images else 0)
        partial = len(images) < expected
        source = sha1(json.dumps(images, ensure_ascii=False))

        def response(stored: StoredSummary, cached: bool) -> SummaryResponse:
            return SummaryResponse(
                series_id=request.series_id,
                episode_id=request.episode_id,
                summary=stored.text,
                partial=stored.partial,
                images=stored.images,
                bubbles=stored.bubbles,
                model=model,
                cached=cached,
            )

        stored = await self._cache.get_summary(request.series_id, request.episode_id, lang, model)
        if stored is not None and (
            not bubbles
            or (
                stored.version == SUMMARY_VERSION
                and stored.source == source
                # Sans nombre d'images, rien ne contredit la mention « lu en partie ».
                and (request.image_count is None or stored.partial == partial)
            )
        ):
            return response(stored, True)
        if not bubbles:
            raise ServiceError(
                "Aucune bulle traduite en cache pour cet épisode : il n'a pas été lu avec "
                "Phylactère.",
                404,
            )

        async def compute() -> StoredSummary:
            start = time.perf_counter()
            text = await summarize(
                self._translator, images, lang, model, partial, self._settings.summary
            )
            result = StoredSummary(
                text=text,
                version=SUMMARY_VERSION,
                source=source,
                partial=partial,
                images=len(images),
                bubbles=bubbles,
            )
            await self._cache.put_summary(
                request.series_id, request.episode_id, lang, model, result
            )
            logger.info(
                "Résumé de l'épisode %s/%s%s : %d image(s), %d bulle(s), %d ms.",
                request.series_id,
                request.episode_id,
                " (lu en partie)" if partial else "",
                len(images),
                bubbles,
                _ms(start),
            )
            return result

        # Même clé pour le résumé demandé d'avance et celui demandé à l'ouverture : la
        # seconde demande rejoint la première, et la fait passer devant si elle est plus
        # prioritaire.
        key = f"summary:{request.series_id}:{request.episode_id}:{lang}:{model}:{source}"
        stored = await self.translation_queue.run(key, request.priority, compute)
        return response(stored, False)

    # --- Propositions de glossaire ---

    async def suggest_glossary(self, series_id: str, request: SuggestRequest) -> SuggestResponse:
        """Propose des entrées de glossaire à partir des bulles traduites en cache.

        Passe par la file de traduction en priorité « background » : après les images, et
        jamais deux modèles chargés à la fois dans Ollama.
        """
        lang, model = self._lang_model(request.target_lang, request.model)
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
            lambda: suggest_entries(
                self._translator, bubbles, glossary, lang, model, cfg.num_ctx, cfg.timeout_s
            ),
        )
        logger.info(
            "Propositions de glossaire %s : %d entrée(s) en %d ms.",
            series_id,
            len(entries),
            _ms(start),
        )
        return SuggestResponse(entries=entries, bubbles=len(bubbles))

    # --- Signalements ---

    async def report(self, request: ReportRequest) -> ReportResponse:
        """Enregistre le signalement d'une bulle : JSON (lignes OCR, traduction, modèle)
        et découpe PNG, dans le dossier des signalements."""
        lang, model = self._check(request)

        # Mêmes voisines que /translate : le résultat OCR vient du cache (sinon il est
        # recalculé, en priorité « visible » puisque l'utilisateur attend).
        ocr = await self._ocr_result(request, "visible", {})
        bubble = find_bubble(ocr, request.box)
        data, following = await asyncio.gather(
            self.download(request.image_url), self.download_neighbor(request.next_image_url)
        )

        def save() -> str:
            image, _, below = decode_images(data, None, following)
            return write_report(
                reports_dir(),
                request,
                lang,
                model,
                {
                    "prompt_version": PROMPT_VERSION,
                    "prompt_id": PROMPT_ID,
                },
                bubble,
                crop_bubble(image, below, request.box),
            )

        report_id = await asyncio.to_thread(save)
        logger.info(
            "Signalement %s : bulle de l'image %s/%s #%d.",
            report_id,
            request.series_id,
            request.episode_id,
            request.index,
        )
        return ReportResponse(id=report_id, matched=bubble is not None)
