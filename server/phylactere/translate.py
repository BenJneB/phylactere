"""Traduction des bulles par Ollama (voir docs/SPEC.md, section 5.5).

Toutes les bulles d'une image partent dans une seule requête ``/api/chat``, avec un
schéma JSON imposé (sortie structurée) :
``{"translations": [{"id": …, "original": …, "text": …}]}``.

Le modèle recopie le texte coréen (``original``) avant de le traduire : sans ça, qwen3:4b
mélangeait ou oubliait des bulles (0 réponse correcte sur 4 essais, contre 4 sur 4 avec).
Le serveur n'utilise pas cette recopie.
Si la réponse est invalide ou incomplète : une nouvelle tentative pour les bulles
manquantes, puis une requête par bulle.
"""

import json
import logging
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ValidationError

from phylactere.cache import GlossaryEntry
from phylactere.config import OllamaConfig, TranslationConfig
from phylactere.errors import ServiceError

logger = logging.getLogger("phylactere.translate")

# À incrémenter à chaque changement du prompt : invalide les traductions en cache.
PROMPT_VERSION = "2"

LANGUAGE_NAMES = {
    "fr": "français",
    "en": "anglais",
    "es": "espagnol",
    "de": "allemand",
    "it": "italien",
    "pt": "portugais",
    "nl": "néerlandais",
    "ja": "japonais",
    "zh-CN": "chinois simplifié",
    "ar": "arabe",
    "vi": "vietnamien",
    "id": "indonésien",
}

# Exemples d'onomatopées (variante ``examples``). Choisis absents de l'épisode de test du
# jalon 4 (729047/66) pour ne pas fausser la comparaison des modèles.
SFX_EXAMPLES = [
    ("쿵", "Boum"),
    ("휙", "Fiou"),
    ("두근두근", "Boum boum"),
    ("헉", "Hein ?!"),
    ("흑흑", "Snif snif"),
    ("쾅", "Bang"),
    ("휴", "Pfiou"),
    ("킥킥", "Hi hi"),
]


def prompt_id(cfg: TranslationConfig) -> str:
    """Identifiant du prompt pour la clé de cache : version et variantes utilisées."""
    return f"{PROMPT_VERSION}-{cfg.context_mode}-{int(cfg.examples)}"


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "original": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["id", "original", "text"],
            },
        }
    },
    "required": ["translations"],
}


class TranslationError(ServiceError):
    """Échec de la traduction ; ``status_code`` est le code HTTP à renvoyer au client."""


class InvalidResponse(Exception):
    """Réponse du modèle inexploitable (JSON invalide ou hors schéma)."""


class _Item(BaseModel):
    id: int
    original: str = ""
    text: str


class _Response(BaseModel):
    translations: list[_Item]


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def system_prompt(target_lang: str, examples: bool = False) -> str:
    language = language_name(target_lang)
    prompt = f"""Tu es un traducteur professionnel de webtoons coréens. Tu traduis chaque bulle \
du coréen en {language} (code {target_lang}). Écris uniquement en {language}, jamais en \
anglais ni dans une autre langue (sauf si la cible est l'anglais).

Règles :
- Style oral, naturel et concis, comme une vraie bulle de BD en {language} : la traduction \
doit tenir dans une bulle de taille comparable à l'original.
- Respecte le registre : familier ou poli, tutoiement ou vouvoiement selon la relation \
entre les personnages, honorifiques coréens rendus naturellement.
- Onomatopées et bruitages : remplace-les par l'équivalent naturel en {language}, ne les \
traduis pas mot à mot.
- Noms propres : utilise le glossaire s'il est fourni, sinon une romanisation simple et \
cohérente avec le contexte.
- Le texte vient d'un OCR : des espaces peuvent manquer et quelques caractères être mal \
lus. Traduis le sens voulu.
- Chaque bulle est traduite séparément, avec son id : ne fusionne pas, ne divise pas, \
n'en oublie aucune.
- Aucune explication, note, transcription ni commentaire : seulement la traduction.

Réponds uniquement avec ce JSON, une entrée par bulle, en recopiant d'abord le texte \
coréen de la bulle puis sa traduction :
{{"translations": [{{"id": <id>, "original": "<coréen recopié>", "text": "<traduction>"}}]}}"""
    # Les exemples sont en français : inutiles (voire trompeurs) pour une autre langue.
    if examples and target_lang == "fr":
        pairs = ", ".join(f"{ko} → {fr}" for ko, fr in SFX_EXAMPLES)
        prompt += f"\n\nExemples d'onomatopées : {pairs}."
    return prompt


def user_prompt(
    items: dict[int, str],
    target_lang: str,
    context: list[tuple[str, str]],
    glossary: list[GlossaryEntry],
    context_mode: str = "pairs",
) -> str:
    language = language_name(target_lang)
    parts = []
    if glossary:
        lines = [f"- {e.ko} → {e.fr}" + (f" ({e.note})" if e.note else "") for e in glossary]
        parts.append(f"Glossaire de la série (coréen → {language}) :\n" + "\n".join(lines))
    if context and context_mode == "original":
        lines = [f"- {original}" for original, _ in context]
        parts.append(
            "Bulles précédentes de l'épisode, en coréen (contexte pour comprendre la scène, "
            "ne pas les traduire) :\n" + "\n".join(lines)
        )
    elif context:
        lines = [f"- {original} → {translated}" for original, translated in context]
        parts.append(
            "Bulles précédentes de l'épisode, déjà traduites (contexte, ne pas les "
            "retraduire) :\n" + "\n".join(lines)
        )
    bubbles = json.dumps(
        [{"id": item_id, "texte": text} for item_id, text in items.items()], ensure_ascii=False
    )
    parts.append(f"Bulles à traduire en {language} :\n{bubbles}")
    return "\n\n".join(parts)


def ollama_stats(data: dict) -> dict:
    """Statistiques d'une réponse Ollama (durées en nanosecondes converties en ms).

    eval = génération de la réponse ; prompt_eval = lecture du prompt.
    """
    ms = lambda key: (data.get(key) or 0) / 1e6  # noqa: E731
    return {
        "eval_count": data.get("eval_count") or 0,
        "eval_ms": ms("eval_duration"),
        "prompt_eval_count": data.get("prompt_eval_count") or 0,
        "prompt_eval_ms": ms("prompt_eval_duration"),
        "total_ms": ms("total_duration"),
    }


class OllamaTranslator:
    def __init__(
        self,
        client: httpx.AsyncClient,
        ollama_cfg: OllamaConfig,
        translation_cfg: TranslationConfig,
        on_stats: Callable[[dict], None] | None = None,
    ) -> None:
        """``on_stats`` (facultatif, banc d'essai) reçoit les statistiques d'Ollama pour
        chaque requête : jetons générés et lus, et durées correspondantes."""
        self._client = client
        self._on_stats = on_stats
        self._ollama = ollama_cfg
        self._cfg = translation_cfg
        # Qwen3 « réfléchit » par défaut avant de répondre (10 fois plus lent) : on le
        # désactive. Modèles qui ne connaissent pas cette option : on ne l'envoie plus.
        self._no_think: set[str] = set()

    async def preload(self) -> bool:
        """Charge le modèle en mémoire (requête sans message) pour que la première
        traduction n'attende pas ce chargement (jusqu'à une minute à froid)."""
        model = self._ollama.model
        try:
            response = await self._client.post(
                "/api/chat",
                json={"model": model, "messages": [], "keep_alive": self._ollama.keep_alive},
                timeout=self._ollama.timeout_s,
            )
        except httpx.HTTPError as exc:
            logger.warning("Préchargement de %s impossible (Ollama injoignable) : %s", model, exc)
            return False
        if response.status_code == 404:
            logger.warning(
                "Modèle « %s » absent d'Ollama : lance « ollama pull %s ».", model, model
            )
            return False
        if response.status_code != 200:
            logger.warning(
                "Préchargement de %s : Ollama a répondu %s.", model, response.status_code
            )
            return False
        logger.info("Modèle de traduction %s chargé.", model)
        return True

    async def chat(
        self,
        system: str,
        user: str,
        model: str,
        schema: dict = RESPONSE_SCHEMA,
        num_ctx: int = 0,
    ) -> str:
        """Une requête /api/chat avec sortie JSON imposée par ``schema`` ; renvoie le texte
        de la réponse (JSON à valider par l'appelant). ``num_ctx`` : taille du contexte
        Ollama (0 = celle d'Ollama). TranslationError si Ollama échoue."""
        options: dict = {"temperature": self._cfg.temperature}
        if num_ctx:
            options["num_ctx"] = num_ctx
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "keep_alive": self._ollama.keep_alive,
            "options": options,
        }
        send_think = model not in self._no_think
        if send_think:
            body["think"] = False
        try:
            response = await self._client.post(
                "/api/chat", json=body, timeout=self._ollama.timeout_s
            )
        except httpx.TimeoutException as exc:
            raise TranslationError(
                f"Ollama n'a pas répondu en {self._ollama.timeout_s:.0f} s.", 504
            ) from exc
        except httpx.HTTPError as exc:
            raise TranslationError(f"Ollama injoignable ({self._ollama.url}) : {exc}", 503) from exc

        if response.status_code == 400 and send_think and "think" in response.text:
            logger.info("Le modèle %s ne gère pas l'option think : désactivée.", model)
            self._no_think.add(model)
            return await self.chat(system, user, model, schema, num_ctx)
        if response.status_code == 404:
            raise TranslationError(
                f"Modèle « {model} » absent d'Ollama : lance « ollama pull {model} ».", 503
            )
        if response.status_code != 200:
            raise TranslationError(
                f"Ollama a répondu {response.status_code} : {response.text[:300]}", 502
            )
        try:
            data = response.json()
            content = data["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidResponse("réponse Ollama sans message") from exc
        if self._on_stats is not None:
            self._on_stats(ollama_stats(data))
        return content

    async def _request(
        self,
        items: dict[int, str],
        target_lang: str,
        context: list[tuple[str, str]],
        glossary: list[GlossaryEntry],
        model: str,
    ) -> dict[int, str]:
        """Une requête ; renvoie les traductions reçues pour les ids demandés."""
        content = await self.chat(
            system_prompt(target_lang, self._cfg.examples),
            user_prompt(items, target_lang, context, glossary, self._cfg.context_mode),
            model,
        )
        try:
            parsed = _Response.model_validate_json(content)
        except ValidationError as exc:
            raise InvalidResponse(f"JSON invalide : {content[:200]!r}") from exc
        return {
            item.id: item.text.strip()
            for item in parsed.translations
            if item.id in items and item.text.strip()
        }

    async def translate(
        self,
        texts: list[str],
        target_lang: str,
        context: list[tuple[str, str]] | None = None,
        glossary: list[GlossaryEntry] | None = None,
        model: str | None = None,
    ) -> list[str]:
        """Traduit les bulles d'une image ; renvoie une traduction par texte, dans l'ordre.

        ``model`` : modèle Ollama à utiliser (défaut : celui de la configuration).
        """
        model = model or self._ollama.model
        context = context or []
        glossary = glossary or []
        pending = {index: text for index, text in enumerate(texts, start=1)}
        results: dict[int, str] = {}

        # Requête groupée, puis une nouvelle tentative pour les bulles manquantes.
        for attempt in (1, 2):
            if not pending:
                break
            try:
                results.update(await self._request(pending, target_lang, context, glossary, model))
            except InvalidResponse as exc:
                logger.warning("Tentative %d : %s", attempt, exc)
            pending = {k: v for k, v in pending.items() if k not in results}

        # Dernier recours : une requête par bulle encore manquante.
        for item_id, text in pending.items():
            try:
                found = await self._request({1: text}, target_lang, context, glossary, model)
            except InvalidResponse as exc:
                logger.warning("Bulle %d : %s", item_id, exc)
                found = {}
            if 1 not in found:
                raise TranslationError(f"Le modèle n'a pas su traduire la bulle « {text} ».", 502)
            results[item_id] = found[1]

        return [results[index] for index in range(1, len(texts) + 1)]
