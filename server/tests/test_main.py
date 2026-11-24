import io
import sys

import httpx
import pytest
import uvicorn

from phylactere.__main__ import already_running, log_config, main, use_utf8_output


def test_output_switches_to_utf8_when_redirected_as_cp1252(monkeypatch):
    # Simule la sortie redirigée de Windows (CI, fichier) : encodage cp1252.
    raw = io.BytesIO()
    fake_stdout = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))

    use_utf8_output()
    print("안녕하세요 — modèles")
    fake_stdout.flush()

    assert raw.getvalue().decode("utf-8").strip() == "안녕하세요 — modèles"


def test_log_config_writes_to_rotating_file(tmp_path):
    import logging
    import logging.config

    path = tmp_path / "server.log"
    loggers = [logging.getLogger(name) for name in ("", "uvicorn", "uvicorn.access")]
    saved = [(lg, lg.handlers[:], lg.level, lg.propagate) for lg in loggers]
    logging.config.dictConfig(log_config("info", path))
    try:
        root_handlers = list(logging.getLogger().handlers)
        logging.getLogger("phylactere.test").info("bonjour 안녕")
        logging.getLogger("uvicorn.error").warning("attention")
        logging.getLogger("phylactere.test").debug("invisible au niveau info")
    finally:
        # Rend la configuration de logging d'avant : sinon les tests suivants écrivent
        # dans ce fichier et dans un stderr capturé déjà fermé.
        for lg, handlers, level, propagate in saved:
            for handler in lg.handlers:
                if handler not in handlers:
                    handler.close()
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate

    # La console garde aussi les journaux de Phylactère (fenêtre de start.ps1).
    assert any(type(h) is logging.StreamHandler for h in root_handlers)
    content = path.read_text(encoding="utf-8")
    assert "phylactere.test : bonjour 안녕" in content
    assert "attention" in content
    assert "invisible" not in content


def test_already_running_detects_phylactere(monkeypatch):
    def fake_get(url, timeout):
        return httpx.Response(
            200, json={"ok": True, "version": "9.9"}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert already_running(8765) == "9.9"


def test_already_running_ignores_other_services(monkeypatch):
    def other(url, timeout):
        return httpx.Response(200, text="<html>", request=httpx.Request("GET", url))

    def down(url, timeout):
        raise httpx.ConnectError("refusé")

    monkeypatch.setattr(httpx, "get", other)
    assert already_running(8765) is None
    monkeypatch.setattr(httpx, "get", down)
    assert already_running(8765) is None


def test_second_instance_exits_quietly(monkeypatch, capsys):
    monkeypatch.setattr("phylactere.__main__.already_running", lambda port: "1.0.0")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("ne doit pas démarrer"))

    assert main([]) == 0
    assert "déjà lancé" in capsys.readouterr().out
