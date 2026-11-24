"""Propositions d'entrées de glossaire (jalon 10, voir docs/SPEC.md, section 9).

Le modèle reçoit les bulles déjà traduites de la série (coréen → traduction actuelle) et
relève les noms propres, surnoms, titres et termes d'adresse récurrents, avec une sortie
JSON structurée ``{"entries": [{"ko", "fr", "note"}]}``.

Garde-fous : le modèle invente parfois des termes absents du texte. On ne garde que les
``ko`` présents tels quels dans au moins une bulle et absents du glossaire actuel, au plus
``MAX_SUGGESTIONS``. Rien n'est enregistré : l'utilisateur choisit dans la page de
glossaire de l'extension.

Ce prompt n'entre pas dans la clé du cache des traductions : le modifier ne demande pas
d'incrémenter ``PROMPT_VERSION``.
"""

import logging

from pydantic import BaseModel

from phylactere.cache import GlossaryEntry
from phylactere.translate import (
    OllamaTranslator,
    language_name,
)

logger = logging.getLogger("phylactere.glossary")

MAX_SUGGESTIONS = 30
# Bulles montrées avec chaque proposition, pour juger sans lire le coréen.
EXAMPLES_PER_ENTRY = 2

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ko": {"type": "string"},
                    "fr": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["ko", "fr", "note"],
            },
        }
    },
    "required": ["entries"],
}


class Example(BaseModel):
    original: str
    translated: str


class Suggestion(GlossaryEntry):
    examples: list[Example] = []


class _RawEntry(BaseModel):
    ko: str
    fr: str = ""
    note: str = ""


class _RawResponse(BaseModel):
    entries: list[_RawEntry]


def suggest_system_prompt(target_lang: str) -> str:
    language = language_name(target_lang)
    return f"""Tu aides à constituer le glossaire d'un webtoon coréen traduit en {language} \
(code {target_lang}). On te donne des bulles de la série : le texte coréen, issu d'un OCR \
(des espaces peuvent manquer), et sa traduction actuelle.

Relève seulement les termes qu'il faut traduire toujours de la même façon :
- noms propres : personnages, lieux, organisations, objets ou techniques nommés ;
- surnoms donnés à un personnage ;
- titres et termes d'adresse (형, 누나, 언니, 오빠, 선배, 사장님, 선생님…).
Les personnages principaux et les termes qui reviennent souvent d'abord.

N'en propose jamais :
- interjections et exclamations (아, 어, 어이, 헉, 깜짝이야…) ni onomatopées ;
- phrases, expressions ou verbes, même courts (뭐야, 가자, 왔어…) ;
- mots courants qui ne désignent personne en particulier ;
- crédits de l'épisode (글, 그림, 작가, noms d'auteurs).
Mieux vaut quelques entrées sûres qu'une longue liste : au plus {MAX_SUGGESTIONS}, \
souvent beaucoup moins.

Pour chaque terme :
- ko : le terme seul, exactement tel qu'il est écrit dans le texte coréen, sans \
particule ni terminaison ni mot voisin (le nom suivi de 가, 는, 야 ou 아 : le nom seul) ;
- fr : une seule forme à utiliser partout en {language} (romanisation simple et \
cohérente pour un nom, équivalent naturel pour un surnom ou un terme d'adresse), reprise \
des traductions actuelles quand elles sont bonnes ;
- note : une précision très courte en français tirée des bulles (rôle, relation, \
registre), ou une chaîne vide. N'invente rien qui ne soit pas dans les bulles.
Ne reprends pas les termes du glossaire actuel.

Réponds uniquement avec ce JSON :
{{"entries": [{{"ko": "<terme coréen>", "fr": "<traduction>", "note": "<précision>"}}]}}"""


def suggest_user_prompt(
    bubbles: list[tuple[str, str]], glossary: list[GlossaryEntry], target_lang: str
) -> str:
    language = language_name(target_lang)
    parts = []
    if glossary:
        lines = [f"- {e.ko} → {e.fr}" for e in glossary]
        parts.append("Glossaire actuel (ne pas reprendre ces termes) :\n" + "\n".join(lines))
    lines = [f"- {original} → {translated}" for original, translated in bubbles]
    parts.append(f"Bulles de la série (coréen → {language}) :\n" + "\n".join(lines))
    return "\n\n".join(parts)


def filter_suggestions(
    entries: list[_RawEntry],
    bubbles: list[tuple[str, str]],
    glossary: list[GlossaryEntry],
) -> list[Suggestion]:
    """Garde les termes présents tels quels dans une bulle, nouveaux et traduits ; ajoute
    pour chacun quelques bulles où il apparaît."""
    known = {e.ko.strip() for e in glossary}
    kept: list[Suggestion] = []
    for entry in entries:
        ko, fr = entry.ko.strip(), entry.fr.strip()
        if not ko or not fr or ko in known:
            continue
        examples = [
            Example(original=original, translated=translated)
            for original, translated in bubbles
            if ko in original
        ]
        if not examples:
            logger.info("Proposition écartée (absente des bulles) : %s → %s", ko, fr)
            continue
        known.add(ko)
        kept.append(
            Suggestion(
                ko=ko, fr=fr, note=entry.note.strip(), examples=examples[:EXAMPLES_PER_ENTRY]
            )
        )
        if len(kept) == MAX_SUGGESTIONS:
            break
    return kept


async def suggest_entries(
    translator: OllamaTranslator,
    bubbles: list[tuple[str, str]],
    glossary: list[GlossaryEntry],
    target_lang: str,
    model: str,
    num_ctx: int = 0,
    timeout_s: float = 0,
) -> list[Suggestion]:
    """Demande des entrées au modèle ; une nouvelle tentative si la réponse est invalide,
    puis TranslationError (502)."""
    system = suggest_system_prompt(target_lang)
    user = suggest_user_prompt(bubbles, glossary, target_lang)
    parsed = await translator.chat_json(
        system,
        user,
        model,
        _RawResponse,
        SUGGEST_SCHEMA,
        num_ctx,
        timeout_s,
        label="Propositions",
        error="Le modèle n'a pas renvoyé de propositions valides.",
    )
    suggestions = filter_suggestions(parsed.entries, bubbles, glossary)
    logger.info(
        "Glossaire : %d proposition(s) gardée(s) sur %d, à partir de %d bulle(s).",
        len(suggestions),
        len(parsed.entries),
        len(bubbles),
    )
    return suggestions
