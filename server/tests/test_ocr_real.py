"""Tests avec le vrai PaddleOCR (lents ; modèles téléchargés au premier lancement).

Sauter avec : pytest -m "not ocr"
"""

from pathlib import Path

import pytest
from PIL import Image

from phylactere.config import default_settings
from phylactere.ocr import PaddleDetector, run_pipeline

pytestmark = pytest.mark.ocr

SAMPLE = Path(__file__).parent / "data" / "korean_sample.png"
SETTINGS = default_settings()


@pytest.fixture(scope="module")
def detector():
    return PaddleDetector(SETTINGS.ocr)


def test_reads_korean_bubbles_across_slices(detector):
    result = run_pipeline(Image.open(SAMPLE), detector, SETTINGS.ocr, SETTINGS.grouping)

    assert [b.text.replace(" ", "") for b in result.bubbles] == [
        "안녕하세요오늘날씨가좋네요",
        "어디가는거야?",
        "같이가자!",
    ]
    # La bulle 2 est dans la zone de chevauchement des tranches : lue deux fois, gardée une.
    assert 1080 < result.bubbles[1].box.y < 1200
    assert all(not b.edges for b in result.bubbles)
