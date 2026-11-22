# Phylactère : cahier des charges

Traduction en temps réel, gratuite et illimitée, des webtoons de comic.naver.com (coréen → français par défaut), entièrement exécutée sur le PC Windows de l'utilisateur.

## 1. Objectif

Pendant la lecture d'un épisode sur comic.naver.com, chaque bulle de texte coréen est recouverte par sa traduction, positionnée au même endroit, avant que l'image n'arrive à l'écran. Aucun service cloud payant : OCR et traduction tournent en local.

## 2. Contraintes

- Utilisation : Windows 10/11 x64, PowerShell. Pas de WSL ni Docker obligatoires.
- Développement : Ubuntu (machine sans exigence de GPU). Le code est multiplateforme ; seuls les scripts livrés à l'utilisateur sont spécifiques à Windows.
- Navigateurs : Chrome (et dérivés Chromium) et Firefox 121+, une seule base de code Manifest V3.
- 100 % local, gratuit, sans quota. Aucun appel à une API cloud.
- Le serveur n'écoute que sur 127.0.0.1.
- Usage personnel : aucune image de webtoon n'est versionnée dans le dépôt.

## 3. Architecture

```
Navigateur (extension)                     PC Windows
┌──────────────────────┐   HTTP local   ┌───────────────────────────────┐
│ content.js           │                │ Serveur Python (FastAPI)       │
│  détecte les images  │                │ 127.0.0.1:8765                 │
│  affiche les bulles  │                │  ├─ télécharge l'image (Referer)│
│ background.js        │ ─────────────▶ │  ├─ OCR : PaddleOCR (coréen)    │
│  file d'attente,     │ ◀───────────── │  ├─ regroupe lignes → bulles    │
│  appels au serveur   │                │  ├─ traduit : Ollama (LLM local)│
│ popup                │                │  └─ cache SQLite                │
└──────────────────────┘                └──────────────┬────────────────┘
                                                       │ HTTP
                                              Ollama 127.0.0.1:11434
```

Le serveur télécharge lui-même les images (il ajoute `Referer: https://comic.naver.com/`), ce qui supprime de l'extension la règle declarativeNetRequest et la permission sur le domaine des images.

## 4. Structure du dépôt

```
phylactere/
├─ CLAUDE.md
├─ README.md                 guide d'installation Windows pour l'utilisateur
├─ docs/SPEC.md              ce document
├─ extension/                extension MV3 (JS vanilla, aucun build)
│  ├─ manifest.json
│  ├─ background.js
│  ├─ content.js / content.css
│  └─ popup.html / popup.js
├─ .gitattributes            fins de ligne (voir section 7)
├─ server/
│  ├─ pyproject.toml         géré avec uv, Python 3.12, extras cpu / gpu
│  ├─ config.example.toml
│  ├─ config.dev.toml        réglages de dev (petit modèle, OCR CPU)
│  ├─ phylactere/
│  │  ├─ main.py             app FastAPI, routes
│  │  ├─ fetch.py            téléchargement d'images (allowlist + Referer)
│  │  ├─ ocr.py              PaddleOCR + découpage des images hautes
│  │  ├─ grouping.py         regroupement des lignes en bulles
│  │  ├─ translate.py        client Ollama, prompt, parsing JSON
│  │  ├─ glossary.py         glossaire par série
│  │  ├─ cache.py            SQLite
│  │  └─ queue.py            files de priorité OCR / traduction
│  └─ tests/
├─ scripts/
│  ├─ install.ps1            installation Windows (uv, dépendances, PaddlePaddle GPU/CPU)
│  ├─ start.ps1              lance le serveur (Windows)
│  ├─ start.sh               lance le serveur (Ubuntu, dev)
│  └─ install-autostart.ps1  démarrage auto à l'ouverture de session (optionnel)
└─ samples/                  images de test locales, dans .gitignore
```

## 5. Serveur local

### 5.1 API

`GET /health` → `{ "ok": true, "ocr": "ready|loading|error", "ollama": "ok|down", "model": "…", "device": "gpu|cpu|auto", "version": "…" }`

Le modèle OCR se charge en arrière-plan au démarrage : `ocr` vaut `loading` pendant ce temps, et `device` vaut `auto` tant que l'appareil réellement utilisé n'est pas connu.

`GET /models` → liste des modèles Ollama installés (proxy de `GET /api/tags`).

`POST /translate`
```json
{
  "image_url": "https://image-comic.pstatic.net/…",
  "target_lang": "fr",
  "series_id": "123456",
  "episode_id": "12",
  "index": 7,
  "priority": "visible"
}
```
Réponse :
```json
{
  "blocks": [
    { "box": { "x": 120, "y": 340, "w": 210, "h": 96 }, "original": "…", "translated": "…" }
  ],
  "image_size": { "w": 690, "h": 1600 },
  "cached": false,
  "timings_ms": { "fetch": 80, "ocr": 260, "translate": 4200 }
}
```
Coordonnées en pixels de l'image d'origine.

`GET /glossary/{series_id}` et `PUT /glossary/{series_id}` : lecture et remplacement du glossaire (liste `{ "ko": "…", "fr": "…", "note": "…" }`).

`DELETE /cache` : vide le cache (option `?series_id=`).

`POST /debug/ocr` (outil de mise au point) : formulaire `multipart/form-data` avec soit `image_url` (téléchargée comme pour `/translate`, allowlist comprise), soit `file` (image envoyée). Renvoie un PNG annoté : lignes en rouge, bulles en vert et numérotées, bulles touchant le bord de l'image en orange. Avec `?format=json`, renvoie `{ "image_size": …, "bubbles": [ { "box", "text", "lines", "edges" } ] }`.

### 5.2 Sécurité

- Écoute uniquement sur 127.0.0.1.
- Jeton partagé : généré au premier lancement, stocké dans `config.toml`, affiché dans la console. Toutes les routes sauf `/health` exigent l'en-tête `X-Phylactere-Token`. Le popup de l'extension permet de le saisir.
- CORS : n'accepter que les origines `chrome-extension://*` et `moz-extension://*`.
- `fetch.py` n'accepte que les URL HTTPS dont l'hôte figure dans une allowlist configurable (par défaut `image-comic.pstatic.net`), taille maximale 20 Mo, délai 15 s.

### 5.3 OCR

- PaddleOCR 3.x avec `lang="korean"`, **obligatoire** : le modèle par défaut des versions récentes ne couvre pas le coréen et donne des résultats inutilisables. Désactiver `use_doc_orientation_classify`, `use_doc_unwarping` et `use_textline_orientation`.
- **Piège constaté (PaddleOCR 3.7)** : dès qu'un nom de modèle est fourni, `lang` est ignoré. Imposer un seul nom (par exemple le détecteur) fait charger un modèle de reconnaissance non coréen (`PP-OCRv6_medium_rec`), sans erreur. Les deux noms sont donc toujours imposés (`detection_model`, `recognition_model`), et la configuration refuse une reconnaissance qui n'est pas `korean_*`.
- Modèles par défaut : détection `PP-OCRv5_mobile_det`, reconnaissance `korean_PP-OCRv5_mobile_rec`. Mesuré sur 8 bandes réelles (CPU) : même qualité que `PP-OCRv5_server_det` pour un temps 10 fois moindre (1,3 s contre 13,4 s) ; `PP-OCRv6_small_det` manque des onomatopées.
- Sur CPU, l'accélération oneDNN (`enable_mkldnn`) est désactivée : avec PaddlePaddle 3.3 elle plante sur ces modèles.
- Les modèles sont téléchargés au premier lancement dans le dossier de cache `platformdirs` (`PADDLE_PDX_CACHE_HOME`).
- Les images Naver sont étroites et très hautes. Découper en tranches d'environ 1200 px de haut avec 120 px de chevauchement, lancer l'OCR par tranche, recaler les coordonnées, puis dédoublonner les lignes présentes dans deux tranches (recouvrement de boîtes > 50 % → garder la meilleure confiance).
- Filtrer : ignorer les lignes sans caractère hangul et celles de confiance < 0,5 (seuils configurables).
- Le modèle est chargé une seule fois au démarrage. L'OCR tourne dans un seul worker (thread) pour ne pas saturer le GPU.
- Naver découpe chaque épisode en bandes de 690 × 1600 px : une bulle peut être coupée entre deux images. L'OCR signale les bulles qui touchent le bord haut ou bas (`edges`, marge configurable) ; leur recollage avec l'image voisine relève du jalon 3.
- Appareil : `device = "auto"` (par défaut) utilise le GPU si PaddlePaddle a été installé avec CUDA et qu'une carte répond, sinon le CPU. `python -m phylactere --check-ocr` vérifie l'installation sur une image de test.

### 5.4 Regroupement en bulles (`grouping.py`)

PaddleOCR renvoie des lignes, pas des bulles. Algorithme de base, à couvrir par des tests unitaires sur des boîtes synthétiques :

1. Trier les lignes par y puis x.
2. Fusionner deux lignes dans le même groupe si l'écart vertical est inférieur à 0,8 × la hauteur médiane des lignes et si leurs projections horizontales se recouvrent d'au moins 30 % ou si leurs centres horizontaux sont à moins de 0,6 × la largeur de la plus étroite.
3. Le texte du groupe est la concaténation des lignes dans l'ordre de lecture, séparées par un espace.
4. La boîte du groupe est l'union des boîtes des lignes.

Les seuils sont dans la configuration. Une amélioration ultérieure possible est un détecteur de bulles dédié (hors v1).

### 5.5 Traduction (`translate.py`)

- Ollama via `POST /api/chat`, `stream: false`, `keep_alive: "30m"`, `options.temperature` 0,2.
- Sortie structurée : passer un schéma JSON dans `format`, de la forme `{ "translations": [ { "id": int, "text": string } ] }`. Toutes les bulles d'une image partent dans une seule requête, chacune avec un `id`.
- Prompt système (en français, versionné avec une constante `PROMPT_VERSION`) : traducteur de webtoons coréens, français oral naturel et concis adapté à une bulle, respect du registre (familier, poli, honorifiques), onomatopées adaptées en français, noms propres selon le glossaire, aucune explication ni note.
- Contexte : ajouter les 15 dernières bulles déjà traduites du même épisode (original + traduction) pour la cohérence du ton et des noms.
- Glossaire de la série injecté dans le prompt.
- Si la réponse n'est pas un JSON valide ou si des `id` manquent : une nouvelle tentative, puis traduction bulle par bulle pour les manquantes.
- Le modèle est configurable. Candidats à comparer au jalon 4 : un modèle Qwen3 de taille adaptée à la VRAM (8 Go par défaut, voir section 8), Gemma, translategemma. Le choix par défaut se fait sur la base de mesures, pas a priori.

### 5.6 Cache et files

- Cache SQLite, clé = sha1(image_url) + target_lang + model + PROMPT_VERSION. Le résultat OCR est mis en cache séparément (clé sha1(image_url)) pour pouvoir changer de modèle ou de langue sans refaire l'OCR.
- Deux files asyncio à priorité (OCR, traduction). `visible` passe avant `prefetch`. Les requêtes identiques en cours sont fusionnées.

### 5.7 Configuration (`config.toml`)

Port, jeton, URL Ollama, modèle, langue cible par défaut, appareil OCR (`auto`/`gpu`/`cpu`), modèles OCR, seuils OCR et regroupement, taille des tranches, allowlist d'hôtes, chemin du cache, niveau de log.

Emplacement : dossier utilisateur donné par `platformdirs` (`%LOCALAPPDATA%\phylactere` sous Windows, `~/.config/phylactere` sous Linux), créé au premier lancement à partir de `config.example.toml`. La variable d'environnement `PHYLACTERE_CONFIG_DIR` permet de déplacer ce dossier (tests, CI).

Trois niveaux, du moins au plus prioritaire :
1. `config.example.toml` : valeurs par défaut (une clé absente du fichier utilisateur prend cette valeur) ;
2. `config.toml` du dossier utilisateur : réglages de l'utilisateur et jeton ;
3. le fichier désigné par `PHYLACTERE_CONFIG` (en dev : `config.dev.toml`) : surcharges uniquement. Ce fichier étant versionné, il ne doit jamais contenir de jeton (le serveur refuse de démarrer sinon).

## 6. Extension

Point de départ : l'extension v1 existante (OCR Google Vision + Google Translate) à adapter.

- Supprimer les appels Google, la règle declarativeNetRequest et les permissions associées. Ajouter `http://127.0.0.1/*` dans `host_permissions`.
- `background.js` appelle `POST /translate` avec le jeton, gère les erreurs réseau (serveur arrêté) et remonte un état clair au content script.
- `content.js` : conserver détection, IntersectionObserver, rendu des bulles, Alt+T, clic pour voir l'original, gestion de la navigation SPA. Envoyer `series_id` (`titleId` de l'URL), `episode_id` (`no`), `index` (position de l'image dans l'épisode) et `priority` (`visible` si l'image est dans le viewport, sinon `prefetch`).
- Le badge affiche « Serveur arrêté » quand `/health` ne répond pas, avec un nouvel essai toutes les 5 s.
- Popup : URL du serveur (défaut `http://127.0.0.1:8765`), jeton, langue cible, modèle (liste depuis `/models`), pastille d'état du serveur, bouton vider le cache.
- Compatibilité Firefox : conserver `background.scripts` + `service_worker`, et `browser_specific_settings`.

## 7. Développement sur Ubuntu, utilisation sur Windows

### 7.1 Règles multiplateformes

- Chemins : `pathlib` uniquement, jamais de séparateur ou de chemin écrit en dur. Dossiers de config, cache et logs via `platformdirs`.
- Fichiers texte ouverts avec `encoding="utf-8"` explicite (l'encodage par défaut de Windows n'est pas UTF-8).
- Pas de dépendance propre à Linux (signaux Unix, `fork`, `uvloop`…). Uvicorn sans `--workers` multiples.
- Dépendances Python : uniquement des paquets avec wheel Windows x64 pour Python 3.12.
- PaddlePaddle n'est pas dans les dépendances principales : il est installé via des extras `cpu` / `gpu` de `pyproject.toml`, déclarés en conflit pour uv. **Décision du jalon 2** (écart avec la version initiale, qui voulait un lock sans version GPU) : l'extra `gpu` est figé dans `uv.lock` sur une seule variante, `paddlepaddle-gpu` cu126 de l'index officiel de PaddlePaddle. Il suffit d'un pilote NVIDIA qui supporte CUDA ≥ 12.6 ; les bibliothèques CUDA arrivent en paquets pip (pas de CUDA Toolkit). L'installation est reproductible et évite les conflits de versions (numpy) d'une installation hors lock.
- uv lit les dépendances de `paddlepaddle-gpu` dans sa wheel Linux, où les bibliothèques CUDA sont réservées à Linux : celles de la wheel Windows sont donc déclarées explicitement dans l'extra `gpu` (marqueur `sys_platform == 'win32'`).
- Scripts PowerShell compatibles Windows PowerShell 5.1, encodés en UTF-8 avec BOM. Exécution prévue via `powershell -ExecutionPolicy Bypass -File …` (documenté dans le README) pour éviter le blocage des scripts non signés.
- `.gitattributes` : `* text=auto eol=lf`, sauf `*.ps1 text eol=crlf` et `*.bat text eol=crlf`.

### 7.2 Flux de travail

1. Développement et tests sur Ubuntu : OCR sur CPU, petit modèle Ollama, tests avec Ollama simulé.
2. Chaque jalon (ou fonctionnalité) se développe sur une branche dédiée, poussée sur un dépôt git privé, puis fait l'objet d'une pull request vers `main`. La fusion n'est faite que si la CI est verte (protection de branche sur `main`). Le PC Windows récupère `main` avec `git pull`.
3. Validation sur Windows à chaque jalon terminé : `install.ps1` (la première fois), `start.ps1`, puis le test manuel du jalon.
4. Intégration continue GitHub Actions, en place dès le jalon 1, déclenchée uniquement sur les pull requests vers `main` ou `dev` (`on: pull_request`) et manuellement (`workflow_dispatch`), pas sur les push. Matrice `ubuntu-latest` + `windows-latest` :
   - `uv sync --extra cpu`, `ruff check`, `ruff format --check`, `pytest` ;
   - vérification de syntaxe des scripts `.ps1` (sur le runner Windows, avec Windows PowerShell 5.1) ;
   - démarrage du serveur puis appel de `/health` (test de fumée) ;
   - les tests qui chargent PaddleOCR sont marqués `@pytest.mark.ocr` et utilisent un cache des modèles (`actions/cache`) ; ils tournent sur la CI à partir du jalon 2 ;
   - Ollama n'est jamais appelé en CI (client simulé).
   Le GPU, la vitesse réelle et le site de Naver ne sont pas testables en CI : ils restent couverts par les vérifications manuelles de chaque jalon.

Les mesures de performance du jalon 4 se font sur le PC Windows, qui a le GPU.

### 7.3 Installation et lancement sous Windows

Le README doit couvrir, pas à pas et en français :

1. Installer Ollama (installeur Windows) et télécharger le modèle choisi.
2. Installer git, cloner le dépôt.
3. Lancer `scripts/install.ps1`, qui installe uv si besoin, Python 3.12, les dépendances, puis PaddlePaddle : version GPU (extra `gpu`, index officiel de PaddlePaddle ; le paquet `paddlepaddle-gpu` de PyPI est obsolète et ne convient pas à PaddleOCR 3.x) si `nvidia-smi` indique un pilote qui supporte CUDA ≥ 12.6, sinon version CPU. Le script lance ensuite `python -m phylactere --check-ocr`, qui télécharge les modèles et vérifie que l'OCR fonctionne et sur quel appareil.
4. `scripts/start.ps1` lance le serveur et affiche le jeton.
5. Charger l'extension dans Chrome et Firefox.
6. Optionnel : démarrage automatique avec `install-autostart.ps1`.
7. Dépannage : serveur injoignable, Ollama arrêté, GPU non détecté, premier chargement lent (téléchargement des modèles).

## 8. Performances visées

Le produit doit fonctionner sur une carte NVIDIA de 8 Go comme de 16 Go (le PC de référence a une RTX 3070 portable, 8 Go). L'OCR occupe 1 à 2 Go de mémoire vidéo : le modèle de traduction par défaut doit tenir dans le reste sur une carte de 8 Go, un modèle plus grand restant configurable pour 16 Go.

Objectifs :
- OCR : < 0,5 s par image (GPU), < 4 s (CPU).
- Traduction : < 8 s par image de 10 bulles.
- Première image visible traduite < 15 s après l'ouverture de l'épisode, puis aucune attente en lecture normale grâce au préchargement.

Les mesures sont journalisées (`timings_ms`) et un script `scripts/bench.py` traite le dossier `samples/` et affiche moyenne et maximum par étape.

## 9. Jalons

Chaque jalon se termine par des tests qui passent et une vérification manuelle décrite.

1. **Squelette serveur** : FastAPI, config via platformdirs, jeton, `/health`, `.gitattributes`, `start.sh`, `install.ps1`, `start.ps1`, CI GitHub Actions (Ubuntu + Windows) verte. Critère : `/health` répond sur Ubuntu, puis sur le PC Windows après `install.ps1` et `start.ps1`.
2. **OCR** : fetch avec allowlist, PaddleOCR coréen, découpage en tranches, regroupement en bulles. Route de debug `POST /debug/ocr` qui renvoie une image PNG avec les boîtes dessinées. Critère : les bulles des images de `samples/` sont correctement encadrées.
3. **Traduction** : client Ollama, schéma JSON, contexte, glossaire, cache. Critère : `POST /translate` renvoie des traductions complètes pour toutes les images de `samples/`.
4. **Choix du modèle** (sur le PC Windows) : `bench.py`, comparaison de 2 ou 3 modèles sur vitesse et qualité (relecture humaine), choix du défaut documenté dans le README.
5. **Extension** : adaptation de la v1, popup, gestion serveur arrêté, priorités. Critère : lecture d'un épisode complet dans Chrome puis Firefox.
6. **Finition** : autostart, README complet, dépannage, nettoyage du cache par série.

## 10. Hors périmètre v1

Effacement et redessin du texte d'origine (inpainting), détecteur de bulles par réseau de neurones, autres sites que comic.naver.com, version mobile, publication sur les stores.
