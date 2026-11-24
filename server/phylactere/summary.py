"""Résumé « Précédemment… » d'un épisode (jalon 15, voir docs/SPEC.md, section 9).

Le modèle reçoit les traductions des bulles en cache (corrections comprises), image par
image dans l'ordre de lecture, et rend 3 à 6 phrases dans la langue cible, avec une sortie
JSON structurée ``{"summary": …}``. Seules les traductions partent, pas le coréen : c'est
ce que le lecteur a lu, et le prompt est deux fois plus court.

Un épisode de 100 à 200 bulles tient dans ``num_ctx`` (729047/66 : 107 bulles, 3 400
caractères traduits, de l'ordre de 1 000 jetons). Au-delà de ``chunk_chars`` caractères,
l'épisode est résumé par tranches, puis les résumés des tranches sont résumés.

Ce prompt n'entre pas dans la clé des traductions : le modifier demande d'incrémenter
``SUMMARY_VERSION`` (les résumés en cache sont refaits), pas ``PROMPT_VERSION``.
"""

import logging
from typing import Annotated

from pydantic import BaseModel, StringConstraints

from phylactere.cache import EpisodeImages as Images
from phylactere.config import SummaryConfig
from phylactere.translate import OllamaTranslator, language_name

logger = logging.getLogger("phylactere.summary")

# À incrémenter à chaque changement du prompt : les résumés en cache sont refaits.
SUMMARY_VERSION = "1"

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


class _Response(BaseModel):
    # Résumé vide : réponse invalide, redemandée comme un JSON invalide.
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def summary_system_prompt(target_lang: str) -> str:
    language = language_name(target_lang)
    return f"""Tu résumes un épisode de webtoon pour un lecteur qui reprend la série après \
plusieurs semaines et a oublié l'intrigue. On te donne le texte des bulles de l'épisode, \
déjà traduit en {language}, image par image dans l'ordre de lecture. Les dessins ne sont \
pas fournis et le nom de qui parle n'est pas indiqué : déduis-le seulement quand c'est clair.

Écris en {language} (code {target_lang}) un résumé de 3 à 6 phrases :
- les événements importants et leur enchaînement, les révélations et les décisions des \
personnages ;
- la situation à la fin du passage, là où reprend la suite ;
- les noms tels qu'ils sont écrits dans les bulles ;
- au présent de narration, sans citer les bulles, sans juger ni commenter ;
- n'invente rien : une scène obscure est passée sous silence plutôt que devinée.

Réponds uniquement avec ce JSON :
{{"summary": "<résumé>"}}"""


def format_images(images: Images) -> str:
    """Bulles numérotées par image (numéro de l'image dans l'épisode, à partir de 1)."""
    blocks = []
    for index, texts in images:
        lines = "\n".join(f"- {text}" for text in texts) if texts else "(pas de texte)"
        blocks.append(f"Image {index + 1} :\n{lines}")
    return "\n\n".join(blocks)


def summary_user_prompt(
    images: Images, target_lang: str, partial: bool, part: tuple[int, int] | None = None
) -> str:
    """``part`` : (numéro, nombre) de la tranche, pour un épisode résumé par tranches."""
    language = language_name(target_lang)
    parts = []
    if part is not None:
        parts.append(
            f"Tranche {part[0]} sur {part[1]} de l'épisode : résume seulement cette tranche."
        )
    if partial:
        parts.append(
            "Le lecteur n'a lu qu'une partie de l'épisode : résume seulement les bulles "
            "fournies, sans deviner le reste."
        )
    parts.append(f"Bulles de l'épisode ({language}) :\n\n{format_images(images)}")
    return "\n\n".join(parts)


def merge_user_prompt(summaries: list[str], partial: bool) -> str:
    """Dernière étape d'un épisode résumé par tranches : un résumé des résumés."""
    parts = [
        "L'épisode, trop long pour être lu d'un coup, a été résumé par tranches. Voici "
        "ces résumés dans l'ordre : fais-en un seul résumé de l'épisode."
    ]
    if partial:
        parts.append("Le lecteur n'a lu qu'une partie de l'épisode : ne devine pas le reste.")
    lines = "\n\n".join(f"Tranche {i} : {text}" for i, text in enumerate(summaries, start=1))
    parts.append(lines)
    return "\n\n".join(parts)


def image_chars(texts: list[str]) -> int:
    return sum(len(text) + 3 for text in texts) + 12  # « - » et retours, « Image n : »


def split_chunks(images: Images, max_chars: int) -> list[Images]:
    """Découpe l'épisode en tranches d'au plus ``max_chars`` caractères, sans couper une
    image (une image plus longue que ``max_chars`` forme une tranche à elle seule)."""
    chunks: list[Images] = []
    current: Images = []
    size = 0
    for image in images:
        chars = image_chars(image[1])
        if current and size + chars > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(image)
        size += chars
    if current:
        chunks.append(current)
    return chunks


async def _ask(
    translator: OllamaTranslator,
    system: str,
    user: str,
    model: str,
    cfg: SummaryConfig,
) -> str:
    """Une demande de résumé ; une nouvelle tentative si la réponse est invalide ou vide,
    puis TranslationError (502)."""
    response = await translator.chat_json(
        system,
        user,
        model,
        _Response,
        SUMMARY_SCHEMA,
        cfg.num_ctx,
        cfg.timeout_s,
        label="Résumé",
        error="Le modèle n'a pas renvoyé de résumé valide.",
    )
    return response.summary


async def summarize(
    translator: OllamaTranslator,
    images: Images,
    target_lang: str,
    model: str,
    partial: bool,
    cfg: SummaryConfig,
) -> str:
    """Résumé de l'épisode, directement ou par tranches au-delà de ``cfg.chunk_chars``."""
    system = summary_system_prompt(target_lang)
    chunks = split_chunks(images, cfg.chunk_chars)
    if len(chunks) == 1:
        return await _ask(
            translator, system, summary_user_prompt(images, target_lang, partial), model, cfg
        )
    logger.info("Épisode long : résumé en %d tranches.", len(chunks))
    summaries = [
        await _ask(
            translator,
            system,
            summary_user_prompt(chunk, target_lang, partial, (i, len(chunks))),
            model,
            cfg,
        )
        for i, chunk in enumerate(chunks, start=1)
    ]
    return await _ask(translator, system, merge_user_prompt(summaries, partial), model, cfg)
