"""Rapport HTML des bulles signalées (Alt+clic dans l'extension, jalon 11).

Lit les signalements (``<id>.json`` + ``<id>.png``) du dossier des signalements et écrit
``index.html`` à côté d'eux (les découpes y sont liées, pas recopiées). Depuis server/ :

    uv run python ../scripts/reports.py            # dossier par défaut
    uv run python ../scripts/reports.py --open     # et ouvre le rapport dans le navigateur
    uv run python ../scripts/reports.py --dir <dossier>

Dossier par défaut : Windows %LOCALAPPDATA%\\phylactere\\reports,
Linux ~/.local/share/phylactere/reports (ou $PHYLACTERE_DATA_DIR/reports).
"""

import argparse
import json
import sys
import webbrowser
from html import escape
from pathlib import Path

from phylactere.config import reports_dir

STYLE = """
body { font: 14px/1.4 system-ui, sans-serif; margin: 24px; color: #1d2a44; background: #f6f7f9; }
h1 { font-size: 20px; }
.report { display: flex; gap: 16px; background: #fff; border-radius: 8px; padding: 12px;
  margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.report img { max-width: 360px; max-height: 480px; border: 1px solid #ccd; align-self: flex-start; }
.meta { color: #667; font-size: 12px; }
.text { font-size: 16px; margin: 6px 0; }
table { border-collapse: collapse; font-size: 12px; margin-top: 6px; }
td, th { border: 1px solid #dde; padding: 2px 6px; text-align: left; }
"""


def load_reports(directory: Path) -> list[dict]:
    """Signalements du dossier, du plus récent au plus ancien ; un JSON illisible est
    ignoré avec un avertissement."""
    reports = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Ignoré : {path.name} ({exc})", file=sys.stderr)
    return reports


def render_report(report: dict) -> str:
    bubble = report.get("bubble", {})
    lines = "".join(
        f"<tr><td>{escape(line['text'])}</td><td>{line['score']:.2f}</td>"
        f"<td>{line['box']['x']}, {line['box']['y']}, {line['box']['w']} × {line['box']['h']}</td>"
        "</tr>"
        for line in report.get("lines", [])
    )
    lines_html = (
        f"<table><tr><th>Ligne OCR</th><th>Confiance</th><th>Boîte</th></tr>{lines}</table>"
        if lines
        else "<p class='meta'>Bulle non retrouvée dans le résultat OCR : pas de lignes.</p>"
    )
    episode = (
        f"https://comic.naver.com/webtoon/detail?titleId={report['series_id']}"
        f"&no={report['episode_id']}"
    )
    return f"""<div class="report">
<img src="{escape(report.get("crop", ""))}" alt="découpe de la bulle">
<div>
<div class="meta">{escape(report.get("created", ""))} · <a href="{escape(episode)}">série
{escape(report["series_id"])}, épisode {escape(report["episode_id"])}</a> · image
<a href="{escape(report["image_url"])}">#{report["index"]}</a> · {escape(report.get("model", ""))}
· {escape(report.get("target_lang", ""))} · prompt {escape(str(report.get("prompt_id", "")))}</div>
<div class="text">{escape(bubble.get("original", ""))}</div>
<div class="text"><b>{escape(bubble.get("translated", ""))}</b></div>
{lines_html}
<div class="meta">{escape(report.get("id", ""))}</div>
</div>
</div>"""


def render_html(reports: list[dict]) -> str:
    body = "\n".join(render_report(r) for r in reports) or "<p>Aucun signalement.</p>"
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Phylactère · bulles signalées</title>
<style>{STYLE}</style></head>
<body><h1>Bulles signalées ({len(reports)})</h1>
{body}
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, default=None, help="dossier des signalements")
    parser.add_argument("--open", action="store_true", help="ouvrir le rapport")
    args = parser.parse_args()

    directory = args.dir or reports_dir()
    if not directory.is_dir():
        sys.exit(f"Aucun signalement : {directory} n'existe pas.")
    reports = load_reports(directory)
    out = directory / "index.html"
    out.write_text(render_html(reports), encoding="utf-8")
    print(f"{len(reports)} signalement(s) : {out}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
