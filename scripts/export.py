"""Export d'un épisode traduit en PDF (jalon 16), pour le relire hors ligne.

Usage strictement personnel : le PDF n'est jamais publié ni partagé. Rien n'est calculé :
les traductions viennent du cache (corrections comprises) et les images sont
retéléchargées. Le serveur peut tourner ou non. Une image sans traduction en cache est
exportée sans bulles (avertissement). Depuis server/ :

    uv run python ../scripts/export.py 729047 66            # langue et modèle de la config
    uv run python ../scripts/export.py 729047 66 --open     # et ouvre le PDF
    uv run python ../scripts/export.py 729047 66 --lang en --model qwen3.5:4b

PDF écrit dans le dossier des données (jamais dans le dépôt) :
Windows %LOCALAPPDATA%\\phylactere\\exports\\<série>-<épisode>-<langue>.pdf,
Linux ~/.local/share/phylactere/exports/ (ou $PHYLACTERE_DATA_DIR/exports).
"""

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

from phylactere.config import load_settings
from phylactere.export import ExportError, export_episode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("series_id", help="titleId de la série (dans l'URL de l'épisode)")
    parser.add_argument("episode_id", help="no de l'épisode (dans l'URL de l'épisode)")
    parser.add_argument("--lang", help="langue cible (défaut : celle de la configuration)")
    parser.add_argument(
        "--model",
        help="modèle dont la traduction est préférée (défaut : celui de la configuration ; "
        "à défaut, la traduction la plus récente d'un autre modèle)",
    )
    parser.add_argument("--cache", type=Path, default=None, help="cache SQLite à lire")
    parser.add_argument("--out", type=Path, default=None, help="fichier PDF à écrire")
    parser.add_argument("--open", action="store_true", help="ouvrir le PDF")
    args = parser.parse_args()

    settings = load_settings()
    try:
        report = asyncio.run(
            export_episode(
                args.series_id,
                args.episode_id,
                args.lang or settings.translation.target_lang,
                args.model or settings.ollama.model,
                args.cache or settings.cache.resolved_path(),
                settings.fetch,
                args.out,
            )
        )
    except ExportError as exc:
        sys.exit(f"Export impossible : {exc}")

    if report.missing:
        numbers = ", ".join(str(i + 1) for i in report.missing)
        print(
            f"Attention : {len(report.missing)} image(s) sans traduction en cache, exportée(s) "
            f"sans bulles (n° {numbers}).",
            file=sys.stderr,
        )
    print(
        f"{report.images} page(s), {report.translated} traduite(s), {report.bubbles} bulle(s) "
        f"({', '.join(sorted(report.models))}) : {report.path}"
    )
    if args.open:
        webbrowser.open(report.path.as_uri())


if __name__ == "__main__":
    main()
