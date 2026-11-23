import os

import pytest

from phylactere.config import ENV_CACHE_DIR, ENV_CONFIG, ENV_CONFIG_DIR, cache_dir

# Modèles PaddleOCR : on garde le vrai cache (évite de les retélécharger à chaque test),
# calculé avant que les tests ne déplacent le dossier de cache.
REAL_MODELS_DIR = str(cache_dir() / "paddlex")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Chaque test utilise des dossiers de config et de cache temporaires,
    jamais ceux de l'utilisateur."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv(ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "cache"))
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    monkeypatch.setenv(
        "PADDLE_PDX_CACHE_HOME",
        os.environ.get("PADDLE_PDX_CACHE_HOME", REAL_MODELS_DIR),
    )
    return config_dir
