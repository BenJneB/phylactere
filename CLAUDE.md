# Phylactère

Extension navigateur + serveur Python local qui traduisent en temps réel les webtoons de comic.naver.com, gratuitement et sans quota. Le cahier des charges complet est dans `docs/SPEC.md` : le lire avant toute tâche importante et le suivre jalon par jalon.

## Environnement : développement sur Ubuntu, utilisation sur Windows

- **Tu travailles sur Ubuntu** (bash). Les commandes que tu lances sont des commandes Linux.
- **Le produit final tourne sur Windows 10/11.** Tout le code doit rester multiplateforme, et les scripts livrés à l'utilisateur sont en PowerShell. Voir « Règles multiplateformes » dans `docs/SPEC.md` (section 7).
- Python 3.12 géré avec uv dans `server/`. Ne pas utiliser le Python système ni pip global.
- Ollama tourne sur 127.0.0.1:11434 sur les deux machines. Sur la machine de dev, un petit modèle suffit (voir `config.dev.toml`) ; les tests n'appellent jamais Ollama (client simulé).
- Sur Ubuntu, PaddleOCR tourne en mode CPU. Les mesures de performance (jalon 4) se font sur le PC Windows, pas ici.
- Le code passe d'une machine à l'autre par git.
- L'utilisateur est développeur mais découvre les extensions navigateur, l'OCR et les LLM locaux : ne pas expliquer les bases de la programmation, mais expliquer en français les notions propres à ces domaines (Manifest V3, content script / service worker, pipeline OCR, Ollama, sortie JSON structurée…) quand elles apparaissent. Préciser sur quelle machine (Ubuntu ou Windows) chaque vérification doit se faire.

## Commandes (Ubuntu, développement)

```bash
cd server
uv sync --extra cpu            # PaddlePaddle CPU pour le dev (extra vide jusqu'au jalon 2)
../scripts/start.sh --reload   # lance le serveur avec config.dev.toml (127.0.0.1, port de la config)
uv run pytest                  # -m "not smoke" pour sauter le test qui lance le vrai serveur
uv run ruff check . && uv run ruff format .

# Vérifier la syntaxe des scripts PowerShell (PowerShell 7 installé via snap)
pwsh -NoProfile -Command '$n = 0; foreach ($f in Get-ChildItem ../scripts/*.ps1) { $e = $null; [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$e) | Out-Null; foreach ($x in $e) { $n++; Write-Host "$($f.Name):$($x.Extent.StartLineNumber) $($x.Message)" } }; exit $n'
```

Configuration en trois niveaux (voir `server/phylactere/config.py`) : `config.example.toml` (défauts) ← `config.toml` du dossier utilisateur (contient le jeton) ← fichier désigné par `PHYLACTERE_CONFIG` (surcharges, jamais de jeton). `PHYLACTERE_CONFIG_DIR` déplace le dossier utilisateur (tests, CI).

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
- PaddleOCR doit toujours être initialisé avec `lang="korean"`.
- Une seule base de code d'extension pour Chrome et Firefox (Manifest V3, `chrome.*`).
- Chemins avec `pathlib`, dossiers de config et de cache via `platformdirs`. Jamais de chemin en dur (`/home/...`, `C:\...`).
- Scripts `.ps1` compatibles Windows PowerShell 5.1 (celui installé par défaut), pas seulement PowerShell 7. Fichiers `.ps1` en UTF-8 **avec BOM**, sinon les accents s'affichent mal sous Windows.
- Ne pas ajouter de dépendance Python qui n'a pas de wheel Windows.
- Code et noms en anglais ; commentaires, messages d'interface et docs en français.
- Écrire des tests pytest pour `grouping.py`, `translate.py` (Ollama simulé) et `cache.py`. Lancer les tests avant de déclarer un jalon terminé.
- Quand un jalon est fini, cocher son statut dans la section suivante et résumer ce que l'utilisateur doit vérifier à la main.

## Avancement

- [x] Jalon 1 : squelette serveur (CI à confirmer sur la première PR)
- [ ] Jalon 2 : OCR
- [ ] Jalon 3 : traduction
- [ ] Jalon 4 : choix du modèle
- [ ] Jalon 5 : extension
- [ ] Jalon 6 : finition
