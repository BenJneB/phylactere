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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_starts_and_answers_health(isolated_config):
    port = free_port()
    env = {**os.environ, ENV_CONFIG_DIR: str(isolated_config), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        [sys.executable, "-m", "phylactere", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except httpx.TransportError:
                if process.poll() is not None or time.monotonic() > deadline:
                    output = process.stdout.read().decode("utf-8", "replace")
                    pytest.fail(f"Le serveur n'a pas démarré :\n{output}")
                time.sleep(0.2)

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert (isolated_config / "config.toml").exists()
    finally:
        process.terminate()
        process.wait(timeout=10)
