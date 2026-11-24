"""Signalement d'une bulle (POST /report) et rapport HTML (scripts/reports.py), jalon 11."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phylactere.cache import read_corrections
from phylactere.config import load_settings, reports_dir
from phylactere.main import TOKEN_HEADER, create_app
from phylactere.reports import CROP_MARGIN, crop_bubble
from phylactere.schemas import Box
from phylactere.translate import PROMPT_VERSION
from tests.test_translate_api import BASE, World, request

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reports.py"


@pytest.fixture
def world():
    return World()


@pytest.fixture
def client(world):
    app = create_app(
        load_settings(),
        ollama_transport=httpx.MockTransport(world.ollama),
        images_transport=httpx.MockTransport(world.images),
        detector_factory=world.detector,
    )
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.app.state.ocr.wait_loaded(5)
        c.headers[TOKEN_HEADER] = load_settings().server.token
        yield c


def report_body(block: dict, **extra) -> dict:
    return {
        **request(),
        "box": block["box"],
        "original": block["original"],
        "translated": block["translated"],
        **extra,
    }


def saved(report_id: str) -> dict:
    return json.loads((reports_dir() / f"{report_id}.json").read_text(encoding="utf-8"))


def test_report_writes_json_and_crop(client, world):
    block = client.post("/translate", json=request()).json()["blocks"][1]
    detector_calls = world.detector_calls

    response = client.post("/report", json=report_body(block, model="gemma3:4b"))

    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert world.detector_calls == detector_calls  # lignes OCR prises dans le cache
    data = saved(body["id"])
    assert data["image_url"] == f"{BASE}0.jpg"
    assert (data["series_id"], data["episode_id"], data["index"]) == ("729047", "66", 0)
    assert data["model"] == "gemma3:4b"
    assert data["target_lang"] == load_settings().translation.target_lang
    assert data["prompt_version"] == PROMPT_VERSION
    assert data["bubble"]["original"] == "가자"
    assert data["bubble"]["translated"] == "FR:가자"
    assert data["bubble"]["bg"] == "#ffffff"
    assert [line["text"] for line in data["lines"]] == ["가자"]
    assert data["lines"][0]["score"] == pytest.approx(0.9)

    with Image.open(reports_dir() / data["crop"]) as crop:
        box = block["box"]
        assert crop.size == (box["w"] + 2 * CROP_MARGIN, box["h"] + 2 * CROP_MARGIN)


def test_report_recomputes_missing_ocr(client, world):
    """OCR absent du cache (vidé entre-temps) : il est refait pour retrouver les lignes."""
    block = client.post("/translate", json=request()).json()["blocks"][0]
    client.delete("/cache")

    body = client.post("/report", json=report_body(block)).json()

    assert body["matched"] is True
    assert world.detector_calls == 2
    assert [line["text"] for line in saved(body["id"])["lines"]] == ["안녕"]


def test_report_of_unknown_bubble_keeps_what_the_extension_sent(client):
    block = {"box": {"x": 0, "y": 0, "w": 10, "h": 10}, "original": "?", "translated": "?"}

    body = client.post("/report", json=report_body(block)).json()

    assert body["matched"] is False
    data = saved(body["id"])
    assert data["lines"] == []
    assert data["bubble"]["original"] == "?"


def test_report_refuses_foreign_url_and_missing_token(client):
    block = {"box": {"x": 0, "y": 0, "w": 10, "h": 10}, "original": "?", "translated": "?"}
    body = report_body(block, image_url="https://example.com/a.jpg")
    assert client.post("/report", json=body).status_code == 400

    del client.headers[TOKEN_HEADER]
    assert client.post("/report", json=report_body(block)).status_code == 401
    assert not reports_dir().exists()


def test_crop_extends_onto_next_image():
    """Bulle coupée entre deux images : la découpe se prolonge sur la suivante."""
    image = Image.new("RGB", (100, 100), "white")
    following = Image.new("RGB", (100, 100), "black")

    crop = crop_bubble(image, following, Box(x=20, y=80, w=40, h=40), margin=5)

    assert crop.size == (50, 50)  # y de 75 à 125 : 25 px sur chaque image
    assert crop.getpixel((10, 5)) == (255, 255, 255)
    assert crop.getpixel((10, 45)) == (0, 0, 0)
    # Sans voisine, la découpe s'arrête au bas de l'image.
    assert crop_bubble(image, None, Box(x=20, y=80, w=40, h=40), margin=5).size == (50, 25)


def load_script():
    spec = importlib.util.spec_from_file_location("reports_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_html_report_lists_reports_newest_first(client):
    blocks = client.post("/translate", json=request()).json()["blocks"]
    first = client.post("/report", json=report_body(blocks[0])).json()["id"]
    second = client.post("/report", json=report_body(blocks[1], translated="<b>")).json()["id"]
    (reports_dir() / "zz-broken.json").write_text("{", encoding="utf-8")
    script = load_script()

    reports = script.load_reports(reports_dir())
    html = script.render_html(reports)

    # L'horodatage a une précision d'une seconde : les deux ont pu tomber la même seconde.
    assert {r["id"] for r in reports} == {first, second}
    assert html.count('class="report"') == 2
    assert f'src="{first}.png"' in html
    assert "&lt;b&gt;" in html and "<b><b></b>" not in html  # texte échappé
    assert "안녕" in html


def test_html_report_skips_incomplete_reports(capsys):
    script = load_script()
    html = script.render_html([{"id": "incomplet", "bubble": {}}])
    assert 'class="report"' not in html and "Aucun signalement" in html
    assert "incomplet" in capsys.readouterr().err


def test_report_date_is_iso_with_offset(client):
    from datetime import datetime

    block = client.post("/translate", json=request()).json()["blocks"][0]
    report_id = client.post("/report", json=report_body(block)).json()["id"]
    assert datetime.fromisoformat(saved(report_id)["created"]).utcoffset() is not None


def test_failed_json_write_removes_the_crop(tmp_path, monkeypatch):
    from phylactere.reports import ReportRequest, write_report

    def fail(*_args, **_kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(Path, "write_text", fail)
    req = ReportRequest(
        image_url="https://x/a.jpg",
        series_id="s",
        episode_id="1",
        index=0,
        box=Box(x=0, y=0, w=1, h=1),
        original="가",
        translated="A",
    )
    with pytest.raises(OSError):
        write_report(tmp_path, req, "fr", "m", {}, None, Image.new("RGB", (4, 4)))
    assert list(tmp_path.iterdir()) == []


def test_cancel_report_deletes_its_files(client):
    block = client.post("/translate", json=request()).json()["blocks"][0]
    other = client.post("/report", json=report_body(block)).json()["id"]
    report_id = client.post("/report", json=report_body(block)).json()["id"]

    response = client.delete(f"/report/{report_id}")

    assert response.status_code == 200
    assert not (reports_dir() / f"{report_id}.json").exists()
    assert not (reports_dir() / f"{report_id}.png").exists()
    assert (reports_dir() / f"{other}.json").exists()  # les autres restent
    assert client.delete(f"/report/{report_id}").status_code == 404  # déjà annulé


def test_cancel_report_refuses_foreign_names(client):
    """Seul un identifiant de signalement est accepté : aucun autre fichier n'est touché."""
    reports_dir().mkdir(parents=True)
    victim = reports_dir() / "index.json"
    victim.write_text("{}", encoding="utf-8")
    for name in ("index", "..%2Fconfig", "20260101-000000-zzzzzz"):
        assert client.delete(f"/report/{name}").status_code == 404
    assert victim.exists()


def test_html_report_lists_corrections(client):
    blocks = client.post("/translate", json=request()).json()["blocks"]
    body = report_body(blocks[1], text="<i>À la main</i>")
    assert client.put("/correction", json=body).status_code == 200
    script = load_script()

    corrections = read_corrections(load_settings().cache.resolved_path())
    html = script.render_html([], corrections)

    assert "Bulles corrigées (1)" in html and "Aucun signalement" in html
    assert "&lt;i&gt;À la main&lt;/i&gt;" in html  # texte échappé
    assert blocks[1]["translated"] in html and "가자" in html
    assert "épisode 66" in html
