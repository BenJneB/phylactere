import io
import sys

from phylactere.__main__ import use_utf8_output


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
