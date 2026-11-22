import tomllib

import pytest

from phylactere.config import ENV_CONFIG, ConfigError, load_settings, user_config_path


def read_user_config() -> dict:
    return tomllib.loads(user_config_path().read_text(encoding="utf-8"))


def test_first_launch_creates_user_config_with_token(isolated_config):
    settings = load_settings()

    path = isolated_config / "config.toml"
    assert path.exists()
    assert len(settings.server.token) >= 32
    assert read_user_config()["server"]["token"] == settings.server.token


def test_user_config_keeps_example_comments():
    load_settings()
    assert "# Jeton à saisir" in user_config_path().read_text(encoding="utf-8")


def test_token_is_reused_on_next_launch():
    assert load_settings().server.token == load_settings().server.token


def test_empty_token_in_existing_file_is_regenerated(isolated_config):
    isolated_config.mkdir()
    user_config_path().write_text('[server]\ntoken = ""\nport = 9000\n', encoding="utf-8")

    settings = load_settings()

    assert settings.server.token
    assert settings.server.port == 9000
    assert read_user_config()["server"]["port"] == 9000


def test_missing_keys_fall_back_to_example_defaults(isolated_config):
    isolated_config.mkdir()
    user_config_path().write_text('[server]\ntoken = "abc"\n', encoding="utf-8")

    settings = load_settings()

    assert settings.server.token == "abc"
    assert settings.server.port == 8765
    assert settings.ollama.url == "http://127.0.0.1:11434"


def test_override_file_wins_over_user_config(tmp_path, monkeypatch):
    override = tmp_path / "dev.toml"
    override.write_text('[ollama]\nmodel = "petit:1b"\n', encoding="utf-8")
    monkeypatch.setenv(ENV_CONFIG, str(override))

    settings = load_settings()

    assert settings.ollama.model == "petit:1b"
    assert settings.ollama.url == "http://127.0.0.1:11434"


def test_override_file_must_not_contain_token(tmp_path, monkeypatch):
    override = tmp_path / "dev.toml"
    override.write_text('[server]\ntoken = "fuite"\n', encoding="utf-8")
    monkeypatch.setenv(ENV_CONFIG, str(override))

    with pytest.raises(ConfigError, match="jeton"):
        load_settings()


def test_versioned_dev_config_is_valid(monkeypatch):
    from phylactere.config import EXAMPLE_PATH

    monkeypatch.setenv(ENV_CONFIG, str(EXAMPLE_PATH.parent / "config.dev.toml"))
    assert load_settings().ocr.device == "cpu"


@pytest.mark.parametrize(
    "content",
    [
        "[server\n",  # TOML invalide
        '[server]\nport = "huit"\n',  # mauvais type
        "[server]\nprot = 8765\n",  # clé inconnue (faute de frappe)
    ],
)
def test_invalid_user_config_raises_config_error(isolated_config, content):
    isolated_config.mkdir()
    user_config_path().write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_settings()


def test_default_settings_does_not_touch_user_dir(isolated_config):
    from phylactere.config import default_settings

    settings = default_settings()

    assert settings.server.token == ""
    assert settings.ocr.recognition_model.startswith("korean_")
    assert not isolated_config.exists()
