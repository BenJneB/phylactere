import pytest

from phylactere.config import ENV_CONFIG, ENV_CONFIG_DIR


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Chaque test utilise un dossier de config temporaire, jamais le vrai dossier utilisateur."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv(ENV_CONFIG_DIR, str(config_dir))
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    return config_dir
