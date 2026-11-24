"""Rapport HTML des bulles signalées (Alt+clic dans l'extension, jalon 11) et des bulles
corrigées à la main (Ctrl+clic, jalon 14).

Lit les signalements (``<id>.json`` + ``<id>.png``) du dossier des signalements et les
corrections du cache SQLite (en lecture seule : le serveur peut tourner), puis écrit
``index.html`` dans le dossier des signalements (les découpes y sont liées, pas recopiées).
Les corrections sont le meilleur corpus pour régler le prompt. Depuis server/ :

    uv run python ../scripts/reports.py            # dossier et cache par défaut
    uv run python ../scripts/reports.py --open     # et ouvre le rapport dans le navigateur
    uv run python ../scripts/reports.py --dir <dossier> --cache <cache.sqlite3>

Dossier par défaut : Windows %LOCALAPPDATA%\\phylactere\\reports,
Linux ~/.local/share/phylactere/reports (ou $PHYLACTERE_DATA_DIR/reports).
"""

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from html import escape
from pathlib import Path

from phylactere.cache import read_corrections
from phylactere.config import load_settings, reports_dir
from phylactere.naver import episode_page_url

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
.machine { color: #8a5a5a; text-decoration: line-through; }
.fixed { color: #1f7a4a; }
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


def render_correction(row: dict) -> str:
    created = datetime.fromtimestamp(row["created"]).astimezone().isoformat(timespec="seconds")
    box = row["box"]
    episode = episode_page_url(row["series_id"], row["episode_id"])
    machine = row.get("translated")
    machine_html = (
        f'<div class="text machine">{escape(machine)}</div>'
        if machine
        else "<p class='meta'>Traduction du modèle inconnue.</p>"
    )
    return f"""<div class="report">
<div>
<div class="meta">{escape(created)} · <a href="{escape(episode)}">série
{escape(row["series_id"])}, épisode {escape(row["episode_id"])}</a> · image
<a href="{escape(row["image_url"])}">#{row["image_index"]}</a> · {escape(row.get("model") or "")}
· {escape(row["target_lang"])} · boîte {box["x"]}, {box["y"]}, {box["w"]} × {box["h"]}</div>
<div class="text">{escape(row["original"])}</div>
{machine_html}
<div class="text fixed"><b>{escape(row["text"])}</b></div>
</div>
</div>"""


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
    episode = episode_page_url(report["series_id"], report["episode_id"])
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


def render_html(reports: list[dict], corrections: list[dict] = ()) -> str:
    """Un signalement incomplet (JSON écrit à moitié, modifié à la main) est ignoré avec un
    avertissement, sans empêcher le rapport."""
    parts = []
    for report in reports:
        try:
            parts.append(render_report(report))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            name = report.get("id", "?") if isinstance(report, dict) else "?"
            print(f"Ignoré : {name} (champ manquant ou invalide : {exc!r})", file=sys.stderr)
    body = "\n".join(parts) or "<p>Aucun signalement.</p>"
    fixed = "\n".join(render_correction(row) for row in corrections) or (
        "<p>Aucune correction.</p>"
    )
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Phylactère · bulles signalées</title>
<style>{STYLE}</style></head>
<body><h1>Bulles signalées ({len(parts)})</h1>
{body}
<h1>Bulles corrigées ({len(corrections)})</h1>
<p class="meta">Original, traduction du modèle (barrée), correction.</p>
{fixed}
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", type=Path, default=None, help="dossier des signalements")
    parser.add_argument(
        "--cache", type=Path, default=None, help="cache SQLite (corrections des bulles)"
    )
    parser.add_argument("--open", action="store_true", help="ouvrir le rapport")
    args = parser.parse_args()

    directory = args.dir or reports_dir()
    corrections = read_corrections(args.cache or load_settings().cache.resolved_path())
    if not directory.is_dir() and not corrections:
        sys.exit(f"Aucun signalement ({directory} n'existe pas) ni aucune correction.")
    directory.mkdir(parents=True, exist_ok=True)
    reports = load_reports(directory)
    out = directory / "index.html"
    out.write_text(render_html(reports, corrections), encoding="utf-8")
    print(f"{len(reports)} signalement(s), {len(corrections)} correction(s) : {out}")
    if args.open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()
