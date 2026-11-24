"""Modèles des requêtes et réponses de l'API HTTP (routes de main.py).

Séparés de ``schemas.py`` : ils dépendent du cache, du glossaire et des files, qui
importent eux-mêmes ``schemas.py``.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from phylactere.glossary import Suggestion
from phylactere.queue import ImagePriority
from phylactere.schemas import Box, ImageRequest, TranslationResult


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
    # « background » : épisode courant, résumé d'avance pour l'ouverture du suivant ;
    # « prefetch » : résumé de l'épisode précédent qui manque à l'ouverture.
    priority: Literal["prefetch", "background"] = "prefetch"


class ExportRequest(BaseModel):
    """Export PDF d'un épisode (bouton du popup, jalon 17)."""

    # Chiffres seulement : ils entrent dans l'URL de la page de l'épisode.
    series_id: str = Field(pattern=r"^[0-9]{1,20}$")
    episode_id: str = Field(pattern=r"^[0-9]{1,20}$")
    target_lang: str | None = Field(default=None, min_length=2, max_length=10)
    model: str | None = Field(default=None, min_length=1, max_length=100)


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
