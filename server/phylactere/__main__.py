"""Point d'entrée : ``python -m phylactere``.

Lit le port dans la configuration et écoute toujours sur 127.0.0.1.
"""

import argparse
import copy
import sys
import time
from pathlib import Path

import httpx
import uvicorn
import uvicorn.config

from phylactere import __version__
from phylactere.config import (
    ConfigError,
    Settings,
    load_settings,
    log_dir,
    override_path,
    user_config_path,
)

HOST = "127.0.0.1"
CHECK_IMAGE = Path(__file__).resolve().parent.parent / "tests" / "data" / "korean_sample.png"
CHECK_EXPECTED = "안녕하세요"
LOG_FILE = "server.log"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 3


def use_utf8_output() -> None:
    """Force l'UTF-8 sur stdout/stderr.

    Sous Windows, quand la sortie est redirigée (CI, fichier, tube), Python utilise
    l'encodage historique cp1252, qui ne contient pas le hangul : print() planterait.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log_config(level: str, path: Path) -> dict:
    """Configuration des journaux : console (comme uvicorn) et fichier tournant.

    Le fichier sert surtout au démarrage automatique, où la console est cachée.
    Le jeton n'y est jamais écrit (il n'est qu'affiché dans la console).
    """
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config["formatters"]["file"] = {
        "format": "%(asctime)s %(levelname)-7s %(name)s : %(message)s",
    }
    config["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "file",
        "filename": str(path),
        "maxBytes": LOG_MAX_BYTES,
        "backupCount": LOG_BACKUPS,
        "encoding": "utf-8",
    }
    for name in ("uvicorn", "uvicorn.access"):
        config["loggers"][name]["handlers"].append("file")
    # Journaux de Phylactère (et des bibliothèques) : fichier seulement, la console
    # les reçoit déjà par le gestionnaire que paddlex installe.
    config["root"] = {"handlers": ["file"], "level": level.upper()}
    # Détail de chaque connexion HTTP (Ollama, images) : illisible même en debug.
    config["loggers"]["httpcore"] = {"level": "WARNING"}
    return config


def already_running(port: int) -> str | None:
    """Version du serveur Phylactère qui répond déjà sur ce port, ou None."""
    try:
        response = httpx.get(f"http://{HOST}:{port}/health", timeout=1.0)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code == 200 and isinstance(data, dict) and data.get("ok") is True:
        return str(data.get("version", "?"))
    return None


def check_ocr(settings: Settings) -> int:
    """Charge PaddleOCR et lit une image de test : vérifie l'installation (et le GPU)."""
    from PIL import Image

    from phylactere.ocr import PaddleDetector, run_pipeline, unused_gpu_warning

    print("Chargement de PaddleOCR (les modèles sont téléchargés au premier lancement)…")
    start = time.perf_counter()
    try:
        detector = PaddleDetector(settings.ocr)
    except Exception as exc:
        print(f"ÉCHEC du chargement : {exc}", file=sys.stderr)
        return 1
    print(
        f"Chargé en {time.perf_counter() - start:.1f} s, appareil : {detector.device.upper()}"
        f' (réglage device = "{settings.ocr.device}")'
    )
    if (warning := unused_gpu_warning(detector, settings.ocr)) is not None:
        print(f"ATTENTION : {warning}")

    image = Image.open(CHECK_IMAGE)
    run_pipeline(image, detector, settings.ocr, settings.grouping)  # préchauffage
    start = time.perf_counter()
    result = run_pipeline(image, detector, settings.ocr, settings.grouping)
    print(f"OCR de l'image de test : {time.perf_counter() - start:.2f} s")
    for bubble in result.bubbles:
        print(f"  - {bubble.text}")
    if not result.bubbles or CHECK_EXPECTED not in result.bubbles[0].text:
        print("ÉCHEC : le texte coréen attendu n'a pas été reconnu.", file=sys.stderr)
        return 1
    print("OCR opérationnel.")
    return 0


def main(argv: list[str] | None = None) -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(prog="phylactere", description="Serveur local Phylactère")
    parser.add_argument("--port", type=int, help="remplace le port de la configuration")
    parser.add_argument(
        "--reload", action="store_true", help="redémarre à chaque modification du code (dev)"
    )
    parser.add_argument(
        "--check-ocr",
        action="store_true",
        help="vérifie l'OCR (et le GPU) sur une image de test, sans lancer le serveur",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 1

    if args.check_ocr:
        return check_ocr(settings)

    port = args.port or settings.server.port
    # Démarrage automatique + lancement manuel : le second s'arrête proprement.
    if (running := already_running(port)) is not None:
        print(f"Phylactère {running} est déjà lancé sur http://{HOST}:{port} : rien à faire.")
        return 0

    logs = log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    print(f"Phylactère {__version__}")
    print(f"Configuration : {user_config_path()}")
    if (extra := override_path()) is not None:
        print(f"Surcharges    : {extra}")
    print(f"Journal       : {logs / LOG_FILE}")
    print(f"Adresse       : http://{HOST}:{port}")
    print(f"Jeton         : {settings.server.token}")
    print("(à saisir dans le popup de l'extension)", flush=True)

    uvicorn.run(
        "phylactere.main:create_app",
        factory=True,
        host=HOST,
        port=port,
        reload=args.reload,
        log_level=settings.server.log_level,
        log_config=log_config(settings.server.log_level, logs / LOG_FILE),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
