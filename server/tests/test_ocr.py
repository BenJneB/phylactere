import asyncio
import threading

import pytest
from PIL import Image

from phylactere.config import OcrConfig
from phylactere.ocr import (
    OcrEngine,
    OcrUnavailable,
    dedup_lines,
    keep_line,
    run_pipeline,
    run_pipeline_with_neighbors,
    slice_ranges,
    unused_gpu_warning,
)
from tests.helpers import GROUPING, FakeDetector, line

OCR = OcrConfig(
    device="cpu",
    detection_model="PP-OCRv5_mobile_det",
    recognition_model="korean_PP-OCRv5_mobile_rec",
    min_confidence=0.5,
    slice_height=1200,
    slice_overlap=120,
    dedup_overlap=0.5,
    edge_margin=8,
    neighbor_margin=400,
    bg_margin=6,
    bg_min_share=0.6,
)


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (800, [(0, 800)]),
        (1200, [(0, 1200)]),
        (1600, [(0, 1200), (1080, 1600)]),
        (2280, [(0, 1200), (1080, 2280)]),
        (2281, [(0, 1200), (1080, 2280), (2160, 2281)]),
    ],
)
def test_slice_ranges(height, expected):
    assert slice_ranges(height, 1200, 120) == expected


def test_slices_cover_image_with_overlap():
    ranges = slice_ranges(10_000, 1200, 120)
    assert ranges[0][0] == 0 and ranges[-1][1] == 10_000
    for (_, previous_bottom), (top, _) in zip(ranges, ranges[1:], strict=False):
        assert previous_bottom - top == 120


@pytest.mark.parametrize(
    ("text", "score", "kept"),
    [
        ("안녕", 0.9, True),
        ("안녕", 0.49, False),
        ("VA", 0.99, False),  # aucun hangul
        ("123!", 0.99, False),
        ("ㅎ이이익", 0.8, True),  # jamos isolés
        ("OK 안녕", 0.6, True),
    ],
)
def test_keep_line(text, score, kept):
    assert keep_line(line(0, 0, 10, text=text, score=score), 0.5) is kept


def test_dedup_keeps_best_confidence():
    a = line(100, 1100, 200, text="첫째", score=0.8)
    b = line(102, 1101, 198, text="둘째", score=0.95)
    other = line(100, 1300, 200, text="다른", score=0.7)
    assert {item.text for item in dedup_lines([a, b, other], 0.5)} == {"둘째", "다른"}


def test_dedup_uses_smaller_box():
    big = line(0, 0, 400, h=100, score=0.9)
    small_inside = line(10, 10, 50, h=20, score=0.8)  # 100 % de la petite boîte
    assert len(dedup_lines([big, small_inside], 0.5)) == 1


def test_dedup_keeps_lines_with_small_overlap():
    a = line(0, 0, 100, h=30)
    b = line(0, 20, 100, h=30)  # recouvrement de 10/30 = 33 %
    assert len(dedup_lines([a, b], 0.5)) == 2


def test_pipeline_shifts_boxes_and_deduplicates_across_slices():
    image = Image.new("RGB", (690, 1600), "white")
    # Ligne vue en bas de la tranche 1 (y=1100) et en haut de la tranche 2 (y=20 → 1100).
    detector = FakeDetector(
        [
            [line(100, 200, 200, text="위에"), line(100, 1100, 200, text="겹침", score=0.7)],
            [line(100, 20, 200, text="겹침", score=0.9), line(100, 400, 200, text="아래")],
        ]
    )

    result = run_pipeline(image, detector, OCR, GROUPING)

    assert detector.shapes == [(1200, 690), (520, 690)]
    assert result.image_size.model_dump() == {"w": 690, "h": 1600}
    assert [(b.text, b.box.y) for b in result.bubbles] == [
        ("위에", 200),
        ("겹침", 1100),
        ("아래", 1480),
    ]
    assert result.bubbles[1].lines[0].score == 0.9


def test_pipeline_filters_before_grouping():
    image = Image.new("RGB", (690, 600), "white")
    detector = FakeDetector([[line(100, 100, 200, text="안녕"), line(100, 135, 200, text="VA")]])
    assert [b.text for b in run_pipeline(image, detector, OCR, GROUPING).bubbles] == ["안녕"]


def test_pipeline_marks_edges():
    image = Image.new("RGB", (690, 600), "white")
    detector = FakeDetector([[line(100, 580, 200, h=20, text="잘림")]])
    assert run_pipeline(image, detector, OCR, GROUPING).bubbles[0].edges == ["bottom"]


def test_pipeline_sends_bgr_to_detector():
    image = Image.new("RGB", (10, 10), (255, 0, 0))  # rouge pur

    class Spy(FakeDetector):
        def detect(self, image_bgr):
            self.pixel = tuple(image_bgr[0, 0])
            return []

    spy = Spy()
    run_pipeline(image, spy, OCR, GROUPING)
    assert spy.pixel == (0, 0, 255)


def test_engine_loading_then_ready():
    release = threading.Event()

    def slow_factory():
        release.wait(5)
        return FakeDetector()

    engine = OcrEngine(OCR, GROUPING, slow_factory)
    engine.start()
    try:
        assert engine.status == "loading"
        with pytest.raises(OcrUnavailable, match="chargement"):
            asyncio.run(engine.recognize(Image.new("RGB", (10, 10))))
        release.set()
        engine.wait_loaded(5)
        assert engine.status == "ready"
        result = asyncio.run(engine.recognize(Image.new("RGB", (10, 10))))
        assert result.bubbles == []
    finally:
        release.set()
        engine.close()


def test_engine_reports_load_error():
    def broken():
        raise RuntimeError("pas de GPU")

    engine = OcrEngine(OCR, GROUPING, broken)
    engine.start()
    engine.wait_loaded(5)
    try:
        assert engine.status == "error"
        assert engine.error == "pas de GPU"
        with pytest.raises(OcrUnavailable, match="pas de GPU"):
            asyncio.run(engine.recognize(Image.new("RGB", (10, 10))))
    finally:
        engine.close()


def test_recognition_model_must_be_korean():
    with pytest.raises(ValueError, match="coréen"):
        OCR.model_validate({**OCR.model_dump(), "recognition_model": "PP-OCRv6_medium_rec"})


def test_overlap_must_be_smaller_than_slice():
    with pytest.raises(ValueError, match="slice_overlap"):
        OCR.model_validate({**OCR.model_dump(), "slice_overlap": 1200})


def test_warns_when_gpu_is_available_but_unused():
    class CpuWithGpu(FakeDetector):
        gpu_available = True

    warning = unused_gpu_warning(CpuWithGpu(), OCR)
    assert warning is not None and 'device = "cpu"' in warning


def test_no_gpu_warning_without_gpu_or_on_gpu():
    class Gpu(FakeDetector):
        device = "gpu"
        gpu_available = True

    assert unused_gpu_warning(FakeDetector(), OCR) is None
    assert unused_gpu_warning(Gpu(), OCR) is None


# --- OCR prolongé par les images voisines ---


class SeamDetector(FakeDetector):
    """Renvoie une ligne placée en coordonnées de l'image prolongée."""

    def __init__(self, lines):
        super().__init__([lines])


def neighbors_ocr(lines, previous=True, following=True, width=690):
    image = Image.new("RGB", (690, 1600), "white")
    prev_img = Image.new("RGB", (width, 1600), "white") if previous else None
    next_img = Image.new("RGB", (width, 1600), "white") if following else None
    cfg = OCR.model_copy(update={"slice_height": 5000})  # une seule tranche, plus lisible
    detector = SeamDetector(lines)
    result = run_pipeline_with_neighbors(image, prev_img, next_img, detector, cfg, GROUPING)
    return result, detector


def test_neighbors_extend_the_image():
    result, detector = neighbors_ocr([])
    assert detector.shapes == [(400 + 1600 + 400, 690)]
    assert result.image_size.model_dump() == {"w": 690, "h": 1600}


def test_bubble_crossing_the_bottom_seam_belongs_to_this_image():
    # Image prolongée : 400 px de la précédente, puis l'image (400 → 2000), puis la suivante.
    result, _ = neighbors_ocr(
        [line(200, 1975, 150, text="어이 꼬랑지"), line(190, 2010, 170, text="구하러 왔어")]
    )
    assert [(b.text, b.box.y, b.box.bottom) for b in result.bubbles] == [
        ("어이 꼬랑지 구하러 왔어", 1575, 1640)
    ]


def test_bubble_starting_in_previous_image_is_left_to_it():
    result, _ = neighbors_ocr(
        [
            line(200, 380, 150, text="이전"),
            line(200, 415, 150, text="이어짐"),
            line(200, 900, 100, text="여기"),
        ]
    )
    assert [(b.text, b.box.y) for b in result.bubbles] == [("여기", 500)]


def test_bubble_in_next_image_is_left_to_it():
    result, _ = neighbors_ocr([line(200, 2100, 150, text="다음")])
    assert result.bubbles == []


def test_neighbor_with_another_width_is_ignored():
    result, detector = neighbors_ocr([], previous=True, following=True, width=202)
    assert detector.shapes == [(1600, 690)]


def test_no_neighbors_is_plain_ocr():
    result, detector = neighbors_ocr([line(100, 100, 200)], previous=False, following=False)
    assert detector.shapes == [(1600, 690)]
    assert result.bubbles[0].box.y == 100


def test_only_next_neighbor():
    result, detector = neighbors_ocr([line(100, 1590, 200, h=30, text="잘림")], previous=False)
    assert detector.shapes == [(2000, 690)]
    assert [(b.text, b.box.y, b.edges) for b in result.bubbles] == [("잘림", 1590, [])]
