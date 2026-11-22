"""OCR des images de webtoon avec PaddleOCR (voir docs/SPEC.md, section 5.3).

Le pipeline : découpage de l'image en tranches → détection + reconnaissance des lignes
par tranche → recalage des coordonnées → filtres (hangul, confiance) → dédoublonnage des
lignes vues dans deux tranches → regroupement en bulles.

Le modèle est chargé une seule fois, en arrière-plan, et tout l'OCR tourne dans un unique
thread pour ne pas saturer le GPU.
"""

import asyncio
import logging
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Protocol

import numpy as np
from PIL import Image

from phylactere.config import GroupingConfig, OcrConfig, cache_dir, user_config_path
from phylactere.grouping import group_lines, mark_edges
from phylactere.schemas import Box, ImageSize, Line, OcrResult

logger = logging.getLogger("phylactere.ocr")

HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")

OcrStatus = Literal["loading", "ready", "error"]


class OcrUnavailable(Exception):
    """L'OCR n'est pas prêt (chargement en cours ou échec)."""


class LineDetector(Protocol):
    def detect(self, image_bgr: np.ndarray) -> list[Line]:
        """Détecte et lit les lignes d'une image BGR (convention d'OpenCV/PaddleOCR)."""
        ...


def slice_ranges(height: int, slice_height: int, overlap: int) -> list[tuple[int, int]]:
    """Tranches [haut, bas) couvrant toute l'image, consécutives avec ``overlap`` px communs."""
    if height <= slice_height:
        return [(0, height)]
    ranges = []
    top = 0
    while True:
        bottom = min(top + slice_height, height)
        ranges.append((top, bottom))
        if bottom == height:
            return ranges
        top = bottom - overlap


def keep_line(line: Line, min_confidence: float) -> bool:
    return line.score >= min_confidence and bool(HANGUL.search(line.text))


def dedup_lines(lines: list[Line], threshold: float) -> list[Line]:
    """Supprime les doublons (recouvrement > ``threshold`` de la plus petite boîte).

    Entre deux doublons, garde la ligne de meilleure confiance.
    """
    kept: list[Line] = []
    for line in sorted(lines, key=lambda item: item.score, reverse=True):
        duplicate = any(
            line.box.intersection_area(other.box) > threshold * min(line.box.area, other.box.area)
            for other in kept
        )
        if not duplicate:
            kept.append(line)
    return kept


def run_pipeline(
    image: Image.Image,
    detector: LineDetector,
    ocr_cfg: OcrConfig,
    grouping_cfg: GroupingConfig,
) -> OcrResult:
    rgb = np.asarray(image.convert("RGB"))
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    height, width = bgr.shape[:2]

    lines: list[Line] = []
    for top, bottom in slice_ranges(height, ocr_cfg.slice_height, ocr_cfg.slice_overlap):
        for line in detector.detect(bgr[top:bottom]):
            if keep_line(line, ocr_cfg.min_confidence):
                lines.append(line.model_copy(update={"box": line.box.shifted(top)}))

    lines = dedup_lines(lines, ocr_cfg.dedup_overlap)
    bubbles = mark_edges(group_lines(lines, grouping_cfg), height, ocr_cfg.edge_margin)
    return OcrResult(image_size=ImageSize(w=width, h=height), bubbles=bubbles)


class PaddleDetector:
    """Détecteur réel. Le constructeur charge les modèles (lent : plusieurs secondes)."""

    def __init__(self, cfg: OcrConfig) -> None:
        # À définir avant l'import de paddlex, qui les lit au chargement du module.
        os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(cache_dir() / "paddlex"))
        # Évite un test de connectivité lent à chaque démarrage ; les téléchargements
        # restent possibles (Hugging Face d'abord, puis les autres hébergeurs).
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

        with warnings.catch_warnings():
            # Avertissement sans objet ici (compilation d'extensions C++).
            warnings.filterwarnings("ignore", message="No ccache found")
            import paddle
            from paddleocr import PaddleOCR

        gpu_ready = paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
        self.gpu_available = gpu_ready
        if cfg.device == "gpu" and not gpu_ready:
            raise RuntimeError(
                "GPU demandé mais indisponible (PaddlePaddle sans CUDA, ou carte NVIDIA "
                'non détectée) : relance install.ps1, ou mets device = "auto".'
            )
        options: dict = {}
        if cfg.device == "gpu" or (cfg.device == "auto" and gpu_ready):
            options["device"] = "gpu:0"
            self.device = "gpu"
        else:
            self.device = "cpu"
            options["device"] = "cpu"
            # oneDNN (accélération CPU d'Intel) plante avec Paddle 3.3 sur ces modèles
            # (« ConvertPirAttribute2RuntimeAttribute not support »).
            options["enable_mkldnn"] = False

        # Les deux noms de modèle sont imposés : si un seul l'est, PaddleOCR ignore
        # lang="korean" pour l'autre et charge un modèle qui ne lit pas le coréen.
        # Avec les deux noms, lang est ignoré (d'où l'avertissement masqué) : c'est la
        # validation korean_* de la configuration qui garantit la lecture du coréen.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*`lang` and `ocr_version` will be ignored")
            self._ocr = PaddleOCR(
                lang="korean",
                text_detection_model_name=cfg.detection_model,
                text_recognition_model_name=cfg.recognition_model,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                **options,
            )

    def detect(self, image_bgr: np.ndarray) -> list[Line]:
        result = self._ocr.predict(image_bgr)[0]
        lines = []
        for text, score, (x1, y1, x2, y2) in zip(
            result["rec_texts"], result["rec_scores"], result["rec_boxes"], strict=True
        ):
            box = Box(x=int(x1), y=int(y1), w=int(x2 - x1), h=int(y2 - y1))
            lines.append(Line(box=box, text=text, score=float(score)))
        return lines


def unused_gpu_warning(detector: object, cfg: OcrConfig) -> str | None:
    """Message si une carte NVIDIA est utilisable mais que l'OCR tourne sur le CPU."""
    if getattr(detector, "device", None) != "cpu" or not getattr(detector, "gpu_available", False):
        return None
    return (
        f'Une carte NVIDIA est utilisable mais l\'OCR tourne sur le CPU (device = "{cfg.device}"). '
        f'Pour l\'utiliser : mets device = "auto" dans {user_config_path()}, '
        "ou supprime cette ligne."
    )


class OcrEngine:
    """Charge le détecteur en arrière-plan et exécute l'OCR dans un thread unique."""

    def __init__(
        self,
        ocr_cfg: OcrConfig,
        grouping_cfg: GroupingConfig,
        detector_factory=None,
    ) -> None:
        self._ocr_cfg = ocr_cfg
        self._grouping_cfg = grouping_cfg
        self._factory = detector_factory or (lambda: PaddleDetector(ocr_cfg))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr")
        self._detector: LineDetector | None = None
        self.status: OcrStatus = "loading"
        self.error: str | None = None
        # Appareil réellement utilisé, connu une fois le modèle chargé.
        self.device: str = ocr_cfg.device

    def start(self) -> None:
        self._executor.submit(self._load)

    def _load(self) -> None:
        try:
            logger.info("Chargement de PaddleOCR (%s)…", self._ocr_cfg.device)
            self._detector = self._factory()
            self.device = getattr(self._detector, "device", self._ocr_cfg.device)
            self.status = "ready"
            logger.info("PaddleOCR prêt (%s).", self.device)
            if (warning := unused_gpu_warning(self._detector, self._ocr_cfg)) is not None:
                logger.warning(warning)
        except Exception as exc:
            self.error = str(exc)
            self.status = "error"
            logger.exception("Échec du chargement de PaddleOCR")

    async def recognize(self, image: Image.Image) -> OcrResult:
        if self.status == "loading":
            raise OcrUnavailable("OCR en cours de chargement, réessaie dans quelques secondes.")
        if self.status == "error" or self._detector is None:
            raise OcrUnavailable(f"OCR indisponible : {self.error}")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            run_pipeline,
            image,
            self._detector,
            self._ocr_cfg,
            self._grouping_cfg,
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
