"""Test de fumée : lance le vrai serveur (python -m phylactere) et interroge /health."""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from phylactere.config import ENV_CONFIG_DIR

pytestmark = pytest.mark.smoke

# Large : le premier démarrage sur un runner Windows est lent (antivirus, compilation des .pyc).
STARTUP_TIMEOUT_S = 90


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop_tree(process: subprocess.Popen) -> None:
    """Arrête le serveur et ses enfants.

    Sous Windows, le python.exe d'une venv est souvent un lanceur qui démarre le vrai
    interpréteur dans un processus enfant : terminate() seul laisserait le serveur tourner.
    """
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def test_server_starts_and_answers_health(isolated_config, tmp_path):
    port = free_port()
    env = {**os.environ, ENV_CONFIG_DIR: str(isolated_config), "PYTHONIOENCODING": "utf-8"}
    log_path = tmp_path / "server.log"
    # Sortie dans un fichier plutôt qu'un tube : aucune lecture ne peut bloquer le test.
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "phylactere", "--port", str(port)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        response = None
        while response is None:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            except httpx.TransportError:
                if process.poll() is not None or time.monotonic() > deadline:
                    break
                time.sleep(0.5)
    finally:
        stop_tree(process)

    output = log_path.read_text(encoding="utf-8", errors="replace")
    assert response is not None, f"Le serveur n'a pas répondu sur /health :\n{output}"
    assert response.status_code == 200, output
    assert response.json()["ok"] is True
    assert (isolated_config / "config.toml").exists()
