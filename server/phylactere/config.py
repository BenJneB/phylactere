"""Chargement de la configuration.

Trois niveaux, du moins au plus prioritaire :
1. ``config.example.toml`` (valeurs par défaut, versionné) ;
2. ``config.toml`` du dossier utilisateur (créé au premier lancement, contient le jeton) ;
3. le fichier désigné par ``PHYLACTERE_CONFIG`` (surcharges de dev, jamais de jeton).
"""

import os
import secrets
import tomllib
from pathlib import Path
from typing import Any, Literal

import platformdirs
import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

APP_NAME = "phylactere"
ENV_CONFIG = "PHYLACTERE_CONFIG"
# Permet de déplacer le dossier de config (tests, CI) sans toucher au vrai dossier utilisateur.
ENV_CONFIG_DIR = "PHYLACTERE_CONFIG_DIR"
EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config.example.toml"


class ConfigError(Exception):
    """Configuration absente, illisible ou invalide."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(_Section):
    port: int = Field(ge=1, le=65535)
    token: str
    log_level: Literal["critical", "error", "warning", "info", "debug"]


class OllamaConfig(_Section):
    url: str
    model: str


class TranslationConfig(_Section):
    target_lang: str


class OcrConfig(_Section):
    device: Literal["gpu", "cpu"]


class Settings(_Section):
    server: ServerConfig
    ollama: OllamaConfig
    translation: TranslationConfig
    ocr: OcrConfig


def config_dir() -> Path:
    if custom := os.environ.get(ENV_CONFIG_DIR):
        return Path(custom)
    # roaming=False : %LOCALAPPDATA% sous Windows ; appauthor=False : pas de sous-dossier éditeur.
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False, roaming=False))


def user_config_path() -> Path:
    return config_dir() / "config.toml"


def override_path() -> Path | None:
    value = os.environ.get(ENV_CONFIG)
    return Path(value) if value else None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Impossible de lire {path} : {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Fichier TOML invalide {path} : {exc}") from exc


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_user_config(path: Path) -> None:
    """Crée le config.toml utilisateur à partir de l'exemple et y écrit un jeton s'il manque."""
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = EXAMPLE_PATH.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as exc:
        raise ConfigError(f"Fichier TOML invalide {path} : {exc}") from exc

    if "server" not in doc:
        doc["server"] = tomlkit.table()
    if path.exists() and doc["server"].get("token"):
        return
    if not doc["server"].get("token"):
        doc["server"]["token"] = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def load_settings() -> Settings:
    user_path = user_config_path()
    ensure_user_config(user_path)

    data = _merge(_read_toml(EXAMPLE_PATH), _read_toml(user_path))
    if (extra_path := override_path()) is not None:
        extra = _read_toml(extra_path)
        if "token" in extra.get("server", {}):
            raise ConfigError(
                f"{extra_path} ne doit pas contenir de jeton : il reste dans {user_path}."
            )
        data = _merge(data, extra)

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Configuration invalide :\n{exc}") from exc
