import os

import pytest

from phylactere.config import (
    ENV_CACHE_DIR,
    ENV_CONFIG,
    ENV_CONFIG_DIR,
    ENV_DATA_DIR,
    ENV_LOG_DIR,
    cache_dir,
)

# Modèles PaddleOCR : on garde le vrai cache (évite de les retélécharger à chaque test),
# calculé avant que les tests ne déplacent le dossier de cache.
REAL_MODELS_DIR = str(cache_dir() / "paddlex")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Chaque test utilise des dossiers de config, de cache, de données et de journaux temporaires,
    jamais ceux de l'utilisateur."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv(ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "cache"))
    monkeypatch.setenv(ENV_LOG_DIR, str(tmp_path / "logs"))
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "data"))
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    monkeypatch.setenv(
        "PADDLE_PDX_CACHE_HOME",
        os.environ.get("PADDLE_PDX_CACHE_HOME", REAL_MODELS_DIR),
    )
    return config_dir
