"""Traduit toutes les images d'un épisode via le serveur (critère du jalon 3).

Source : un dossier de samples/ contenant urls.txt (une URL d'image par ligne, dans l'ordre ;
nom du dossier = <titleId>-<no>), ou directement l'URL de l'épisode sur comic.naver.com
(aucune image n'est enregistrée). Le serveur doit tourner. Depuis server/ :

    uv run python ../scripts/translate_samples.py ../samples/729047-66 [--limit 10] [--quiet]
    uv run python ../scripts/translate_samples.py "https://comic.naver.com/webtoon/detail?titleId=729047&no=66"

Le jeton et le port sont lus dans la configuration (la même que le serveur).
"""

import argparse
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from phylactere.__main__ import HOST, use_utf8_output
from phylactere.config import load_settings
from phylactere.main import TOKEN_HEADER
from phylactere.naver import USER_AGENT, episode_image_urls


def episode_images(source: str) -> tuple[str, str, list[str]]:
    """(série, épisode, URL des images dans l'ordre) depuis un dossier ou une URL d'épisode."""
    if source.startswith("https://"):
        query = parse_qs(urlsplit(source).query)
        series_id, episode_id = query["titleId"][0], query["no"][0]
        page = httpx.get(source, headers={"User-Agent": USER_AGENT}, timeout=20)
        page.raise_for_status()
        return series_id, episode_id, episode_image_urls(page.text, source, series_id, episode_id)
    folder = Path(source)
    series_id, _, episode_id = folder.name.partition("-")
    return series_id, episode_id, (folder / "urls.txt").read_text(encoding="utf-8").split()


def main() -> int:
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="dossier de samples/ ou URL de l'épisode")
    parser.add_argument("--limit", type=int, help="nombre maximal d'images")
    parser.add_argument("--lang", help="langue cible (défaut : celle de la config)")
    parser.add_argument("--quiet", action="store_true", help="n'affiche pas les traductions")
    args = parser.parse_args()

    series_id, episode_id, urls = episode_images(args.source)
    if not urls:
        print("Aucune image trouvée (épisode payant, réservé aux adultes, ou page modifiée ?).")
        return 1
    settings = load_settings()
    base = f"http://{HOST}:{settings.server.port}"
    headers = {TOKEN_HEADER: settings.server.token}

    count = min(len(urls), args.limit) if args.limit else len(urls)
    failures, bubbles = 0, 0
    timings: dict[str, list[int]] = {"fetch": [], "ocr": [], "translate": [], "total": []}
    with httpx.Client(base_url=base, headers=headers, timeout=600) as client:
        for index in range(count):
            body = {
                "image_url": urls[index],
                "series_id": series_id,
                "episode_id": episode_id,
                "index": index,
                "priority": "visible",
                "prev_image_url": urls[index - 1] if index > 0 else None,
                "next_image_url": urls[index + 1] if index + 1 < len(urls) else None,
            }
            if args.lang:
                body["target_lang"] = args.lang
            start = time.perf_counter()
            response = client.post("/translate", json=body)
            total = round((time.perf_counter() - start) * 1000)
            if response.status_code != 200:
                failures += 1
                print(f"[{index:03d}] ÉCHEC {response.status_code} : {response.text[:300]}")
                continue
            data = response.json()
            blocks = data["blocks"]
            missing = [b for b in blocks if not b["translated"].strip()]
            failures += bool(missing)
            bubbles += len(blocks)
            if not data["cached"]:
                for step, value in data["timings_ms"].items():
                    timings[step].append(value)
                timings["total"].append(total)
            state = "cache" if data["cached"] else f"{total} ms"
            flag = " — INCOMPLET" if missing else ""
            print(f"[{index:03d}] {len(blocks)} bulle(s), {state}{flag}")
            if not args.quiet:
                for block in blocks:
                    edges = f" [bord : {', '.join(block['edges'])}]" if block["edges"] else ""
                    print(f"      {block['original']}  →  {block['translated']}{edges}")

    print(f"\n{count} image(s), {bubbles} bulle(s), {failures} image(s) en échec ou incomplète(s).")
    if timings["total"]:
        print("Temps (ms, hors cache) :      moyenne   max")
        for step, values in timings.items():
            if values:
                print(f"  {step:<10} {statistics.mean(values):>16.0f} {max(values):>6}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
