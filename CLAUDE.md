# Phylactère

Extension navigateur + serveur Python local qui traduisent en temps réel les webtoons de comic.naver.com, gratuitement et sans quota. Le cahier des charges complet est dans `docs/SPEC.md` : le lire avant toute tâche importante et le suivre jalon par jalon.

## Environnement : développement sur Ubuntu, utilisation sur Windows

- **Tu travailles sur Ubuntu** (bash). Les commandes que tu lances sont des commandes Linux.
- **Le produit final tourne sur Windows 10/11.** Tout le code doit rester multiplateforme, et les scripts livrés à l'utilisateur sont en PowerShell. Voir « Règles multiplateformes » dans `docs/SPEC.md` (section 7).
- Python 3.12 géré avec uv dans `server/`. Ne pas utiliser le Python système ni pip global.
- Ollama tourne sur 127.0.0.1:11434 sur les deux machines. Modèle de traduction : `qwen3.5:9b` par défaut (Windows, choisi au jalon 5, voir README), `qwen3.5:4b` sur Ubuntu via `config.dev.toml` (pas de GPU) ; `qwen3:1.7b` ne traduit pas, il romanise ; les tests n'appellent jamais Ollama (client simulé).
- Sur Ubuntu, PaddleOCR tourne en mode CPU. Les mesures de performance (jalon 4) se font sur le PC Windows, pas ici.
- Le code passe d'une machine à l'autre par git.
- L'utilisateur est développeur mais découvre les extensions navigateur, l'OCR et les LLM locaux : ne pas expliquer les bases de la programmation, mais expliquer en français les notions propres à ces domaines (Manifest V3, content script / service worker, pipeline OCR, Ollama, sortie JSON structurée…) quand elles apparaissent. Préciser sur quelle machine (Ubuntu ou Windows) chaque vérification doit se faire.

## Commandes (Ubuntu, développement)

```bash
cd server
uv sync --extra cpu            # PaddlePaddle CPU pour le dev (l'extra gpu est pour Windows)
../scripts/start.sh --reload   # lance le serveur avec config.dev.toml (127.0.0.1, port de la config)
uv run pytest                  # -m "not smoke and not ocr" : sans le vrai serveur ni le vrai PaddleOCR
uv run playwright install chromium && uv run pytest -m e2e   # extension dans Chromium (exclus par défaut)
uv run python -m phylactere --check-ocr   # charge PaddleOCR et lit tests/data/korean_sample.png
# Critère du jalon 3 (serveur lancé) : traduit un épisode de samples/ et vérifie chaque bulle
PHYLACTERE_CONFIG=config.dev.toml uv run python ../scripts/translate_samples.py ../samples/729047-66 --limit 20
# Banc d'essai (jalon 4, serveur arrêté) : rapport HTML dans samples/bench/
uv run python ../scripts/bench.py ../samples/729047-66 --models qwen3.5:4b --variants base,sans-contexte --limit 6
# Rapport HTML des bulles signalées (Alt+clic) : ~/.local/share/phylactere/reports/index.html
uv run python ../scripts/reports.py --open
uv run ruff check . && uv run ruff format .

# Vérifier la syntaxe des scripts PowerShell (PowerShell 7 installé via snap)
pwsh -NoProfile -Command '$n = 0; foreach ($f in Get-ChildItem ../scripts/*.ps1) { $e = $null; [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$e) | Out-Null; foreach ($x in $e) { $n++; Write-Host "$($f.Name):$($x.Extent.StartLineNumber) $($x.Message)" } }; exit $n'
```

Configuration en trois niveaux (voir `server/phylactere/config.py`) : `config.example.toml` (défauts) ← `config.toml` du dossier utilisateur (contient le jeton) ← fichier désigné par `PHYLACTERE_CONFIG` (surcharges, jamais de jeton). `PHYLACTERE_CONFIG_DIR`, `PHYLACTERE_CACHE_DIR` et `PHYLACTERE_DATA_DIR` déplacent les dossiers de config, de cache et de données (signalements) (tests, CI). Cache SQLite : `~/.cache/phylactere/cache.sqlite3`. Les modèles PaddleOCR sont dans le dossier de cache `platformdirs` (`~/.cache/phylactere/paddlex`), ou dans `PADDLE_PDX_CACHE_HOME` s'il est défini.

Images de test : `samples/<titleId>-<no>/` (non versionné). `samples/fetch_samples.py` (local, non versionné) télécharge un épisode : `uv run python ../samples/fetch_samples.py "<url de l'épisode>"`.

## Commandes (Windows, utilisation)

```powershell
.\scripts\install.ps1    # première installation : uv, dépendances, PaddlePaddle GPU ou CPU
.\scripts\start.ps1      # lance le serveur
```

L'extension (`extension`) est en JavaScript vanilla, sans build ni dépendance npm. Pour la tester : `chrome://extensions` › Charger l'extension non empaquetée, ou `about:debugging` dans Firefox.

## Règles

- Tout reste local : aucun appel à une API cloud, aucune clé API.
- Le serveur écoute uniquement sur 127.0.0.1 et exige le jeton `X-Phylactere-Token` (sauf `/health`).
- Ne jamais versionner d'image de webtoon : `samples/` est dans `.gitignore`.
- PaddleOCR doit toujours être initialisé avec `lang="korean"` **et** les deux noms de modèle (`detection_model`, `recognition_model` de la config) : dès qu'un nom est fourni, `lang` est ignoré, et un seul nom fait charger une reconnaissance non coréenne. La config refuse une reconnaissance qui n'est pas `korean_*`.
- Une seule base de code d'extension pour Chrome et Firefox (Manifest V3, `chrome.*`).
- Chemins avec `pathlib`, dossiers de config et de cache via `platformdirs`. Jamais de chemin en dur (`/home/...`, `C:\...`).
- Scripts `.ps1` compatibles Windows PowerShell 5.1 (celui installé par défaut), pas seulement PowerShell 7. Fichiers `.ps1` en UTF-8 **avec BOM**, sinon les accents s'affichent mal sous Windows.
- Ne pas ajouter de dépendance Python qui n'a pas de wheel Windows. Après tout changement de dépendances, vérifier dans `uv.lock` que chaque paquet utilisé sous Windows a une wheel `win_amd64` cp312 ou `none-any` (uv suppose que toutes les wheels d'un paquet ont les mêmes dépendances : c'est faux pour `paddlepaddle-gpu`, voir SPEC 7.1).
- Code et noms en anglais ; commentaires, messages d'interface et docs en français.
- Après tout changement du prompt ou du format de réponse de `translate.py`, incrémenter `PROMPT_VERSION` (invalide les traductions en cache).
- Écrire des tests pytest pour `grouping.py`, `translate.py` (Ollama simulé) et `cache.py`. Lancer les tests avant de déclarer un jalon terminé.
- Quand un jalon est fini, cocher son statut dans la section suivante et résumer ce que l'utilisateur doit vérifier à la main.

## Avancement

- [x] Jalon 1 : squelette serveur
- [x] Jalon 2 : OCR
- [x] Jalon 3 : traduction
- [x] Jalon 4 : choix du modèle
- [x] Jalon 5 : extension
- [x] Jalon 6 : finition
- [x] Jalon 7 : tests de bout en bout de l'extension (Playwright)
- [x] Jalon 8 : mode de traduction au défilement
- [x] Jalon 9 : couleur des bulles prise sur l'image
- [x] Jalon 10 : glossaire assisté (page d'édition, propositions par le modèle)
- [x] Jalon 11 : confort (signaler une bulle, préchargement de l'épisode suivant, délai dépassé côté extension)
- [x] Jalon 12 : cache borné et statistiques
- [ ] Jalon 13 : retraduire une bulle ou une image
- [ ] Jalon 14 : corriger une bulle à la main
- [ ] Jalon 15 : résumé « Précédemment… »
- [ ] Jalon 16 : brouillon rapide puis version améliorée (sous réserve de mesure)
- [ ] Jalon 17 : export d'un épisode traduit en PDF
