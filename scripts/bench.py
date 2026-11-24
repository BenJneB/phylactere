"""Banc d'essai des modèles de traduction (jalon 4, voir docs/SPEC.md sections 8 et 9).

Fait l'OCR d'un épisode une seule fois, puis le fait traduire par chaque modèle et chaque
variante de prompt, dans l'ordre de lecture et avec le contexte, comme en usage réel.
Mesure les temps (moyenne et maximum par étape) et la part du modèle chargée sur le GPU,
puis produit un rapport HTML local pour la relecture humaine, à l'aveugle.

À lancer sur le PC qui a le GPU, serveur Phylactère arrêté (il occuperait la mémoire vidéo),
Ollama démarré et les modèles installés (ollama pull …). Depuis server/ :

    uv run --no-sync python ../scripts/bench.py "https://comic.naver.com/webtoon/detail?titleId=729047&no=66"
    uv run --no-sync python ../scripts/bench.py ../samples/729047-66 --models qwen3:4b,qwen3:8b \\
        --variants base,sans-contexte --limit 40
    uv run --no-sync python ../scripts/bench.py ../samples/729047-66 --models qwen3.5:9b \\
        --variants base,suivantes   # mesure du second passage (jalon 20)

Résultats dans samples/bench/<série>-<épisode>/ (non versionné) : rapport-<date>.html et
resultats-<date>.json.
"""

import argparse
import asyncio
import base64
import contextlib
import html
import json
import logging
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image
from translate_samples import episode_images

from phylactere.__main__ import use_utf8_output
from phylactere.config import load_settings
from phylactere.fetch import decode_image, fetch_bytes
from phylactere.ocr import PaddleDetector, run_pipeline_with_neighbors
from phylactere.reports import crop_bubble
from phylactere.schemas import compact
from phylactere.translate import OllamaTranslator, TranslationError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = "qwen3:4b,qwen3:8b,qwen3.5:4b,gemma3:4b,translategemma:4b,exaone3.5:7.8b"
# Variantes de prompt. "base" : comportement du serveur (bulles précédentes en contexte).
# "sans-contexte" : aucune bulle précédente. "suivantes" : simule le second passage du
# jalon 21 ; chaque image est retraduite avec les bulles précédentes (déjà retraduites) et
# les bulles suivantes (traduction de "base"), qui est donc lancée avant elle.
VARIANTS = ("base", "sans-contexte", "suivantes")


def gpu_name() -> str:
    if shutil.which("nvidia-smi") is None:
        return "aucune carte NVIDIA"
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        return output.strip().splitlines()[0]
    except (subprocess.SubprocessError, IndexError):
        return "carte NVIDIA (nom inconnu)"


def stats(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "max": None}
    return {"mean": round(statistics.mean(values)), "max": round(max(values))}


async def download(urls: list[str], settings) -> list[bytes]:
    images = []
    async with httpx.AsyncClient() as client:
        for url in urls:
            images.append(await fetch_bytes(client, url, settings.fetch))
            await asyncio.sleep(0.2)
    return images


def run_ocr(images: list[Image.Image], count: int, settings) -> tuple[list, list[float]]:
    print("Chargement de PaddleOCR…", flush=True)
    detector = PaddleDetector(settings.ocr)
    print(f"OCR sur {detector.device.upper()}", flush=True)
    results, timings = [], []
    for index in range(count):
        start = time.perf_counter()
        result = run_pipeline_with_neighbors(
            images[index],
            images[index - 1] if index > 0 else None,
            images[index + 1] if index + 1 < len(images) else None,
            detector,
            settings.ocr,
            settings.grouping,
        )
        timings.append((time.perf_counter() - start) * 1000)
        results.append(result)
    return results, timings


async def ollama_gpu_ratio(client: httpx.AsyncClient, model: str) -> float | None:
    """Part du modèle chargée en mémoire vidéo (1 = entièrement sur le GPU)."""
    try:
        models = (await client.get("/api/ps", timeout=10)).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return None
    for entry in models:
        if entry.get("name") == model or entry.get("model") == model:
            size = entry.get("size") or 0
            return round(entry.get("size_vram", 0) / size, 2) if size else None
    return None


async def unload(client: httpx.AsyncClient, model: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        await client.post("/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)


async def run_model(client, settings, model, variants, ocr_results, lang) -> list[dict]:
    ollama_cfg = settings.ollama.model_copy(update={"model": model})
    runs = []
    loader = OllamaTranslator(client, ollama_cfg, settings.translation)
    # Mesure un chargement à froid : le modèle peut déjà être en mémoire (serveur, essai
    # précédent).
    await unload(client, model)
    start = time.perf_counter()
    loaded = await loader.preload()
    load_s = round(time.perf_counter() - start, 1)
    gpu = await ollama_gpu_ratio(client, model)
    if not loaded:
        print(f"  {model} : chargement impossible (modèle installé ?), ignoré.", flush=True)
        return [{"model": model, "variant": v, "skipped": True} for v in variants]
    gpu_text = f"{gpu:.0%} sur le GPU" if gpu is not None else "part GPU inconnue"
    print(f"  {model} : chargé en {load_s} s, {gpu_text}", flush=True)

    first_pass: list[list[str]] | None = None
    for variant in variants:
        calls: list[dict] = []  # statistiques Ollama de chaque requête de l'image en cours
        translator = OllamaTranslator(
            client, ollama_cfg, settings.translation, on_stats=calls.append
        )
        texts = [[bubble.text for bubble in result.bubbles] for result in ocr_results]
        episode = await translate_episode(
            translator,
            texts,
            lang,
            settings.translation.context_size,
            calls,
            use_context=variant != "sans-contexte",
            first_pass=first_pass if variant == "suivantes" else None,
        )
        if variant == "base":
            first_pass = episode["translations"]
        times = episode["times"]
        translated_indexes, per_image = episode["indexes"], episode["per_image"]
        errors, translations = episode["errors"], episode["translations"]
        bubbles = sum(len(r.bubbles) for r in ocr_results)
        run = {
            "model": model,
            "variant": variant,
            "load_s": load_s,
            "gpu_ratio": gpu,
            "translate_ms": stats(times),
            # Temps de chaque image traduite (images sans bulle exclues) : repère les pics.
            "times_ms": {
                str(i): round(ms) for i, ms in zip(translated_indexes, times, strict=True)
            },
            "per_bubble_ms": round(sum(times) / bubbles) if bubbles else None,
            # Vitesse propre de la carte, indépendante de la longueur des réponses.
            "tokens": token_summary(per_image),
            "per_image": per_image,
            "errors": errors,
            "translations": translations,
        }
        runs.append(run)
        ms, tok = run["translate_ms"], run["tokens"]
        print(
            f"    {variant:<14} {ms['mean']} ms/image (max {ms['max']}), {len(errors)} erreur(s)"
            f" | génération {tok['generation_tps']} jetons/s, lecture {tok['prompt_tps']} jetons/s,"
            f" {tok['tokens_per_bubble']} jetons/bulle, {tok['extra_requests']} requête(s) en plus",
            flush=True,
        )

    await unload(client, model)  # libère la mémoire vidéo avant le modèle suivant
    return runs


STAT_KEYS = ("eval_count", "eval_ms", "prompt_eval_count", "prompt_eval_ms")


def following_window(
    texts: list[list[str]], first_pass: list[list[str]], index: int, size: int
) -> list[tuple[str, str]]:
    """Bulles des images qui suivent ``index``, avec leur traduction du premier passage,
    dans l'ordre de lecture : les ``size`` premières. Les bulles que le premier passage n'a
    pas su traduire sont sautées."""
    window: list[tuple[str, str]] = []
    for later in range(index + 1, len(texts)):
        window.extend(
            (original, translated)
            for original, translated in zip(texts[later], first_pass[later], strict=True)
            if translated
        )
        if len(window) >= size:
            break
    return window[:size]


async def translate_episode(
    translator: OllamaTranslator,
    texts: list[list[str]],
    lang: str,
    context_size: int,
    calls: list[dict],
    use_context: bool = True,
    first_pass: list[list[str]] | None = None,
) -> dict:
    """Traduit un épisode image par image, dans l'ordre de lecture, comme le serveur.

    ``texts`` : textes des bulles de chaque image. ``calls`` : liste remplie par le
    ``on_stats`` du traducteur. ``first_pass`` (variante « suivantes ») : traductions d'un
    premier passage ; chaque image reçoit alors aussi les bulles suivantes en contexte, et
    les bulles précédentes sont celles de ce second passage.
    """
    context: list[tuple[str, str]] = []
    translations: list[list[str]] = []
    times: list[float] = []
    errors: list[dict] = []
    indexes: list[int] = []
    per_image: dict[str, dict] = {}
    for index, image_texts in enumerate(texts):
        if not image_texts:
            translations.append([])
            continue
        window = context[-context_size:] if use_context and context_size else []
        following = (
            following_window(texts, first_pass, index, context_size)
            if first_pass is not None and context_size
            else None
        )
        calls.clear()
        start = time.perf_counter()
        try:
            out = await translator.translate(image_texts, lang, window, following=following)
        except TranslationError as exc:
            errors.append({"index": index, "error": str(exc)})
            out = [""] * len(image_texts)
        times.append((time.perf_counter() - start) * 1000)
        per_image[str(index)] = {
            "ms": round(times[-1]),
            "bubbles": len(image_texts),
            # Plus d'une requête : réponse invalide ou incomplète, nouvelle tentative.
            "requests": len(calls),
            **{key: round(sum(c[key] for c in calls)) for key in STAT_KEYS},
        }
        indexes.append(index)
        translations.append(out)
        context.extend(zip(image_texts, out, strict=True))
    return {
        "translations": translations,
        "times": times,
        "errors": errors,
        "indexes": indexes,
        "per_image": per_image,
    }


def token_summary(per_image: dict[str, dict]) -> dict:
    """Débits en jetons par seconde : séparent « carte lente » de « réponses longues ».

    generation_tps : vitesse d'écriture de la réponse ; prompt_tps : vitesse de lecture du
    prompt ; tokens_per_bubble : longueur des réponses ; extra_requests : nouvelles
    tentatives (réponse invalide ou incomplète).
    """
    total = {key: sum(image[key] for image in per_image.values()) for key in STAT_KEYS}
    bubbles = sum(image["bubbles"] for image in per_image.values())
    requests = sum(image["requests"] for image in per_image.values())

    def rate(count: str, duration: str) -> float | None:
        return round(total[count] / total[duration] * 1000, 1) if total[duration] else None

    return {
        "generation_tps": rate("eval_count", "eval_ms"),
        "prompt_tps": rate("prompt_eval_count", "prompt_eval_ms"),
        "tokens_per_bubble": round(total["eval_count"] / bubbles, 1) if bubbles else None,
        "extra_requests": requests - len(per_image),
    }


def load_reference(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        (b["index"], compact(b["original"])): (b["reference"], b.get("note", ""))
        for b in data["bubbles"]
    }


def crop_data_uri(images: list[Image.Image], index: int, box) -> str:
    """Vignette de la case autour de la bulle (qui peut déborder sur l'image suivante)."""
    following = images[index + 1] if index + 1 < len(images) else None
    crop = crop_bubble(images[index], following, box, margin=90)
    crop.thumbnail((360, 360))
    out = BytesIO()
    crop.convert("RGB").save(out, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def render_report(meta: dict, images, ocr_results, runs, reference) -> str:
    esc = html.escape
    active = [r for r in runs if not r.get("skipped")]
    labels = [f"{r['model']} / {r['variant']}" for r in active]

    speed_rows = []
    for run in runs:
        if run.get("skipped"):
            speed_rows.append(
                f"<tr><td>{esc(run['model'])}</td><td>{esc(run['variant'])}</td>"
                "<td colspan=10>non chargé (modèle absent d'Ollama ?)</td></tr>"
            )
            continue
        gpu = f"{run['gpu_ratio']:.0%}" if run["gpu_ratio"] is not None else "?"
        ms, tok = run["translate_ms"], run["tokens"]
        speed_rows.append(
            f"<tr><td>{esc(run['model'])}</td><td>{esc(run['variant'])}</td>"
            f"<td>{run['load_s']}</td><td>{gpu}</td><td>{ms['mean']}</td><td>{ms['max']}</td>"
            f"<td>{run['per_bubble_ms']}</td><td>{tok['generation_tps']}</td>"
            f"<td>{tok['prompt_tps']}</td><td>{tok['tokens_per_bubble']}</td>"
            f"<td>{tok['extra_requests']}</td><td>{len(run['errors'])}</td></tr>"
        )

    cards = []
    for index, result in enumerate(ocr_results):
        for position, bubble in enumerate(result.bubbles):
            bubble_id = f"{index}-{position}"
            candidates: dict[str, list[int]] = {}
            for run_index, run in enumerate(active):
                text = run["translations"][index][position] or "(aucune traduction)"
                candidates.setdefault(text, []).append(run_index)
            order = list(candidates.items())
            random.Random(bubble_id).shuffle(order)  # ordre aléatoire mais stable : à l'aveugle
            ref = reference.get((index, compact(bubble.text)))
            ref_html = (
                f"<p class=ref><b>Référence :</b> {esc(ref[0])}"
                + (f"<br><small>{esc(ref[1])}</small>" if ref[1] else "")
                + "</p>"
                if ref
                else "<p class=ref><i>Pas de référence pour cette bulle.</i></p>"
            )
            rows = []
            for k, (text, run_ids) in enumerate(order):
                names = ", ".join(labels[i] for i in run_ids)
                rows.append(
                    f"<li data-runs='{json.dumps(run_ids)}'>"
                    f"<label><input type=checkbox data-ok name='ok-{bubble_id}-{k}'>"
                    " correcte</label> "
                    f"<label><input type=radio data-best name='best-{bubble_id}' value={k}>"
                    " meilleure</label> "
                    f"<span class=text>{esc(text)}</span>"
                    f"<span class=who> — {esc(names)}</span></li>"
                )
            cards.append(
                f"<section class=card data-id='{bubble_id}'>"
                f"<img src='{crop_data_uri(images, index, bubble.box)}' alt=''>"
                f"<div><p class=orig>Image {index} — <b>{esc(bubble.text)}</b></p>{ref_html}"
                f"<ol>{''.join(rows)}</ol></div></section>"
            )

    meta_html = "".join(f"<li>{esc(k)} : {esc(str(v))}</li>" for k, v in meta.items())
    return f"""<!doctype html>
<html lang=fr><head><meta charset=utf-8><title>Banc d'essai Phylactère</title>
<style>
body {{ font: 15px/1.45 system-ui, sans-serif; margin: 24px; color: #1d2a44; background: #f6f7fb; }}
table {{ border-collapse: collapse; margin: 8px 0 20px; background: #fff; }}
td, th {{ border: 1px solid #cfd6e4; padding: 4px 8px; text-align: left; }}
.card {{ display: flex; gap: 16px; background: #fff; border: 1px solid #cfd6e4;
         border-radius: 8px; padding: 12px; margin: 12px 0; }}
.card img {{ max-width: 360px; align-self: flex-start; border-radius: 4px; }}
.ref {{ background: #eef7ee; padding: 6px 8px; border-radius: 4px; }}
ol {{ padding-left: 20px; }} li {{ margin: 6px 0; }}
.text {{ font-weight: 600; }} .who {{ color: #5d6a85; display: none; }}
body.reveal .who {{ display: inline; }}
.done {{ border-color: #1f7a4d; }}
#scores td:nth-child(n+2) {{ text-align: right; }}
.bar {{ position: sticky; top: 0; background: #f6f7fb; padding: 8px 0; z-index: 1; }}
</style></head><body>
<h1>Banc d'essai des modèles de traduction</h1>
<ul>{meta_html}</ul>
<h2>Vitesse</h2>
<table><tr><th>Modèle</th><th>Variante</th><th>Chargement (s)</th><th>Part GPU</th>
<th>Traduction moy. (ms/image)</th><th>max</th><th>ms/bulle</th>
<th>Génération (jetons/s)</th><th>Lecture du prompt (jetons/s)</th><th>Jetons/bulle</th>
<th>Requêtes en plus</th><th>Erreurs</th></tr>
{"".join(speed_rows)}</table>
<p>Les débits en jetons par seconde mesurent la vitesse propre de la carte : s'ils baissent
d'une série à l'autre, la carte était bridée (mode d'alimentation, chauffe) ; si ce sont les
jetons par bulle qui montent, les réponses étaient plus longues. « Requêtes en plus » compte
les nouvelles tentatives après une réponse invalide ou incomplète.</p>
<h2>Qualité (relecture)</h2>
<p>Pour chaque bulle : cochez <b>correcte</b> pour chaque traduction juste et naturelle (même
sens que la référence, français fluide), et choisissez la <b>meilleure</b>. Les traductions
identiques sont regroupées et l'ordre est aléatoire : on ne sait pas quel modèle a produit
quoi tant que les noms ne sont pas révélés. Les choix sont gardés dans le navigateur.</p>
<div class=bar><button id=reveal>Révéler les modèles</button>
<button id=export>Exporter les notes (JSON)</button> <span id=progress></span></div>
<table id=scores><tr><th>Modèle / variante</th><th>Correctes</th><th>Meilleures</th></tr></table>
{"".join(cards)}
<script>
const LABELS = {json.dumps(labels)};
const KEY = "phylactere-bench-" + {json.dumps(meta["date"])};
const saved = JSON.parse(localStorage.getItem(KEY) || "{{}}");
const inputs = [...document.querySelectorAll("input")];
inputs.forEach((el) => {{
  const id = el.name + (el.type === "radio" ? "=" + el.value : "");
  if (saved[id]) el.checked = true;
  el.addEventListener("change", update);
}});
function update() {{
  const state = {{}};
  inputs.forEach((el) => {{
    if (el.checked) state[el.name + (el.type === "radio" ? "=" + el.value : "")] = 1;
  }});
  localStorage.setItem(KEY, JSON.stringify(state));
  const ok = LABELS.map(() => 0), best = LABELS.map(() => 0);
  let rated = 0;
  document.querySelectorAll(".card").forEach((card) => {{
    const touched = card.querySelector("input:checked") !== null;
    card.classList.toggle("done", touched);
    if (touched) rated++;
    card.querySelectorAll("li").forEach((li) => {{
      const runs = JSON.parse(li.dataset.runs);
      if (li.querySelector("[data-ok]").checked) runs.forEach((r) => ok[r]++);
      if (li.querySelector("[data-best]").checked) runs.forEach((r) => best[r]++);
    }});
  }});
  const reveal = document.body.classList.contains("reveal");
  document.querySelector("#scores").innerHTML =
    "<tr><th>Modèle / variante</th><th>Correctes</th><th>Meilleures</th></tr>" +
    LABELS.map((l, i) => `<tr><td>${{reveal ? l : "candidat " + (i + 1) + " (masqué)"}}</td>` +
      `<td>${{ok[i]}} / ${{rated}}</td><td>${{best[i]}}</td></tr>`).join("");
  document.querySelector("#progress").textContent =
    `${{rated}} / ${{document.querySelectorAll(".card").length}} bulles relues`;
}}
document.querySelector("#reveal").onclick = () => {{
  document.body.classList.toggle("reveal");
  update();
}};
document.querySelector("#export").onclick = () => {{
  const blob = new Blob([localStorage.getItem(KEY) || "{{}}"], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "notes-bench.json"; a.click();
}};
update();
</script></body></html>"""


async def main() -> int:
    use_utf8_output()
    # paddlex active la journalisation globale : on coupe celle des requêtes HTTP.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="dossier de samples/ ou URL de l'épisode")
    parser.add_argument("--models", default=DEFAULT_MODELS, help="modèles Ollama, séparés par ,")
    parser.add_argument(
        "--variants", default="base", help=f"variantes (séparées par ,) : {', '.join(VARIANTS)}"
    )
    parser.add_argument("--limit", type=int, default=40, help="nombre d'images (défaut 40)")
    parser.add_argument("--lang", default="fr")
    parser.add_argument("--reference", type=Path, help="traduction de référence (JSON)")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        parser.error(
            f"variante(s) inconnue(s) : {', '.join(unknown)} (connues : {', '.join(VARIANTS)})"
        )
    if "suivantes" in variants and "base" not in variants:
        variants.append("base")  # premier passage dont « suivantes » a besoin
    variants.sort(key=VARIANTS.index)  # « base » avant « suivantes »

    settings = load_settings()
    series_id, episode_id, urls = episode_images(args.source)
    count = min(args.limit, len(urls))
    if not count:
        print(
            "Aucune image trouvée : vérifie le dossier ou l'URL (entre guillemets sous "
            "PowerShell, à cause du &) ; épisode payant, réservé aux adultes, ou page de "
            "Naver modifiée ?"
        )
        return 1
    out_dir = ROOT / "samples" / "bench" / f"{series_id}-{episode_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    episode = f"{series_id}-{episode_id}"
    reference_path = args.reference or ROOT / "bench" / "references" / f"{episode}.json"

    print(f"Épisode {episode} : {count} image(s), {len(models)} modèle(s)", flush=True)
    raw = await download(urls[: count + 1], settings)  # +1 : voisine de la dernière image
    images = [decode_image(data) for data in raw]
    ocr_results, ocr_times = run_ocr(images, count, settings)
    bubbles = sum(len(r.bubbles) for r in ocr_results)
    print(f"OCR : {bubbles} bulle(s), {stats(ocr_times)['mean']} ms/image en moyenne", flush=True)
    if not bubbles:
        print("Aucune bulle lue : rien à traduire.")
        return 1

    runs = []
    async with httpx.AsyncClient(base_url=settings.ollama.url) as client:
        for model in models:
            runs.extend(await run_model(client, settings, model, variants, ocr_results, args.lang))

    date = datetime.now().strftime("%Y-%m-%d_%H%M")
    meta = {
        "date": date,
        "épisode": f"{series_id}/{episode_id}",
        "images": count,
        "bulles": bubbles,
        "carte graphique": gpu_name(),
        "système": f"{platform.system()} {platform.release()}",
        "OCR": "{mean} ms/image en moyenne, {max} ms au maximum".format(**stats(ocr_times)),
        "langue": args.lang,
        "référence": reference_path.name if reference_path.exists() else "aucune",
    }
    results = {
        "meta": meta,
        "images": [
            {"index": i, "bubbles": [b.text for b in r.bubbles]} for i, r in enumerate(ocr_results)
        ],
        "runs": runs,
    }
    (out_dir / f"resultats-{date}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    report = out_dir / f"rapport-{date}.html"
    report.write_text(
        render_report(meta, images, ocr_results, runs, load_reference(reference_path)),
        encoding="utf-8",
    )
    print(f"\nRapport : {report}\nRésultats : {report.with_name(f'resultats-{date}.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
