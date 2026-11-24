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
│  ├─ defaults.js            réglages par défaut partagés (popup, content script, arrière-plan)
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
│  │  ├─ errors.py           erreurs métier → code HTTP (un seul gestionnaire)
│  │  └─ queue.py            files de priorité OCR / traduction
│  └─ tests/
├─ bench/references/         traductions de référence pour le banc d'essai (texte seul)
├─ scripts/
│  ├─ install.ps1            installation Windows (uv, dépendances, PaddlePaddle GPU/CPU)
│  ├─ start.ps1              lance le serveur (Windows)
│  ├─ start.sh               lance le serveur (Ubuntu, dev)
│  ├─ install-autostart.ps1  démarrage auto à l'ouverture de session (optionnel)
│  └─ stop.ps1               arrête le serveur (lancé sans fenêtre par le démarrage auto)
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
  "priority": "visible",
  "model": "qwen3.5:4b",
  "prev_image_url": "https://image-comic.pstatic.net/…",
  "next_image_url": "https://image-comic.pstatic.net/…"
}
```
`target_lang` et `model` (modèle Ollama choisi dans le popup) sont facultatifs (défaut : la configuration). `prev_image_url` et `next_image_url` (facultatifs) désignent les images voisines dans l'épisode : Naver découpe les épisodes en bandes de 1600 px et une bulle, voire une ligne, peut être coupée entre deux images (cas réel : épisode 729047/66, images 14-15). Le serveur lit l'image prolongée par `neighbor_margin` px de chaque voisine de même largeur ; une bulle appartient à l'image qui contient son haut, et sa boîte peut déborder en bas sur l'image suivante (qui l'ignore). Une voisine illisible est ignorée.
Réponse :
```json
{
  "blocks": [
    { "box": { "x": 120, "y": 340, "w": 210, "h": 96 }, "original": "…", "translated": "…", "edges": [] }
  ],
  "image_size": { "w": 690, "h": 1600 },
  "cached": false,
  "timings_ms": { "fetch": 80, "ocr": 260, "translate": 4200 }
}
```
Coordonnées en pixels de l'image d'origine (`y + h` peut dépasser la hauteur de l'image : bulle qui déborde sur la suivante). `edges` (`top`, `bottom`) signale une bulle qui touche un bord sans voisine pour la compléter. Erreurs : 400 (URL refusée), 502/504 (serveur d'images, Ollama), 503 (OCR en chargement, Ollama arrêté ou modèle absent, avec un message qui donne la commande `ollama pull`).

Chaque bloc porte aussi `model` (jalon 13, facultatif) : le modèle qui l'a traduit. Avec `"force": true` (jalon 13), la traduction en cache est ignorée puis remplacée (l'OCR en cache est réutilisé), et `retranslate_model` (facultatif) choisit le modèle qui retraduit ; le résultat reste rangé sous la clé de `model`, le modèle affiché.

`POST /retranslate` (jalon 13) : `{ "image_url", "series_id", "episode_id", "index", "prev_image_url"?, "next_image_url"?, "target_lang"?, "model"?, "retranslate_model"?, "box" }`. Retraduit une seule bulle (retrouvée par sa boîte dans la traduction en cache) et la remplace dans le cache. Réponse : le bloc mis à jour. Erreurs : 404 (traduction ou bulle absente du cache), 409 (bulle corrigée à la main, jalon 14), puis celles de `/translate` pour Ollama.

`PUT /correction` (jalon 14) : `{ "image_url", "series_id", "episode_id", "index", "target_lang"?, "model"?, "box", "original", "text", "translated"? }`. Enregistre le texte saisi pour une bulle (`translated` : traduction du modèle remplacée, gardée pour le corpus) ; remplace la correction qui s'appliquait déjà à cette bulle. Réponse : `{ "box", "original", "text" }`. `text` vide ou blanc : 422. `DELETE /correction` (même corps sans `text` ni `translated`) efface la correction de la bulle ; réponse `{ "removed": n }`. Dans la réponse de `/translate`, une bulle corrigée a `translated` = la correction, `corrected: true` et `machine` = la traduction du modèle.

`GET /glossary/{series_id}` et `PUT /glossary/{series_id}` : lecture et remplacement du glossaire (liste `{ "ko": "…", "fr": "…", "note": "…" }` ; `fr` est la traduction dans la langue cible). Remplacer le glossaire efface les traductions en cache des images de la série dont une bulle contient un terme ajouté, retiré ou modifié (traduction ou note), comparaison faite sans les espaces ; elles seront refaites avec lui. Jusqu'au jalon 9, toute la série était effacée.

`POST /glossary/{series_id}/suggest` (jalon 10) : `{ "target_lang"?, "model"?, "episode_id"? }`. Propose des entrées à partir des bulles traduites en cache de la série, sans rien enregistrer. Réponse : `{ "entries": [ { "ko", "fr", "note", "examples": [ { "original", "translated" } ] } ], "bubbles": n }` (`bubbles` : nombre de bulles envoyées au modèle). Erreurs : 404 (aucune bulle en cache pour la série et la langue), 502 (réponse invalide deux fois), 503/504 (Ollama).

`POST /report` (jalon 11) : `{ "image_url", "series_id", "episode_id", "index", "prev_image_url"?, "next_image_url"?, "target_lang"?, "model"?, "box", "original", "translated" }` (bulle telle que l'extension l'affiche). Écrit `<id>.json` et `<id>.png` (découpe) dans le dossier des signalements (`reports/` du dossier de données `platformdirs`). Réponse : `{ "id", "matched" }` (`matched` : bulle retrouvée dans le résultat OCR, dont les lignes sont enregistrées). Erreurs : celles de `/translate` pour l'OCR (400, 502, 503). `DELETE /report/{id}` annule un signalement (supprime ses deux fichiers) ; 404 si l'identifiant est inconnu ou n'a pas la forme d'un identifiant de signalement.

`DELETE /cache` : vide le cache OCR et traductions (option `?series_id=`) ; les glossaires et les corrections sont conservés. Renvoie `{ "removed": n }`.

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

- Ollama via `POST /api/chat`, `stream: false`, `keep_alive: "30m"`, `options.temperature` 0,2, `think: false` : Qwen3 « réfléchit » par défaut avant de répondre, ce qui multiplie le temps par 7 à 10 (mesuré : 27,7 s contre 3,7 s) sans intérêt pour une bulle. Si le modèle ne connaît pas l'option, elle n'est plus envoyée.
- Préchargement : au démarrage, le serveur demande à Ollama de charger le modèle (requête `/api/chat` sans message), en tâche de fond. Sans ça, la première image attendait le chargement à froid (58 s mesurées sur le PC de référence, pour un objectif de 15 s). Un échec (Ollama arrêté, modèle absent) n'est qu'un avertissement dans la console.
- Sortie structurée : passer un schéma JSON dans `format`, de la forme `{ "translations": [ { "id": int, "original": string, "text": string } ] }`. Toutes les bulles d'une image partent dans une seule requête, chacune avec un `id`. Le modèle recopie le texte coréen (`original`) avant de le traduire : sans cette recopie, qwen3:4b mélangeait ou oubliait des bulles (0 réponse correcte sur 4 essais sur une image de 3 bulles, 4 sur 4 avec). Le serveur n'utilise pas la recopie.
- Prompt système (en français, versionné avec une constante `PROMPT_VERSION`) : traducteur de webtoons coréens, français oral naturel et concis adapté à une bulle, respect du registre (familier, poli, honorifiques), onomatopées adaptées en français, noms propres selon le glossaire, aucune explication ni note.
- Contexte : ajouter les 15 dernières bulles déjà traduites du même épisode (original + traduction) pour la cohérence du ton et des noms. « Dernières » dans l'ordre de lecture : bulles des images d'index inférieur, même langue et même modèle (avec le préchargement, les images ne sont pas traduites dans l'ordre).
- Glossaire de la série injecté dans le prompt.
- Si la réponse n'est pas un JSON valide ou si des `id` manquent : une nouvelle tentative, puis traduction bulle par bulle pour les manquantes.
- Le modèle est configurable. Candidats à comparer au jalon 4 : un modèle Qwen3 de taille adaptée à la VRAM (8 Go par défaut, voir section 8), Gemma, translategemma. Le choix par défaut se fait sur la base de mesures, pas a priori.

### 5.6 Cache et files

- Cache SQLite (dossier de cache `platformdirs`, ou `[cache] path`), clé = sha1(image_url) + target_lang + model + PROMPT_VERSION. Le résultat OCR est mis en cache séparément pour pouvoir changer de modèle ou de langue sans refaire l'OCR ; sa clé couvre l'URL, les URL des voisines et une empreinte des réglages OCR et de regroupement (changer de modèle OCR ou de seuil invalide le cache). Une traduction en échec n'est pas mise en cache.
- Deux files asyncio à priorité (OCR, traduction), chacune traitée par un seul worker. `visible` passe avant `prefetch` ; une demande `visible` fait passer devant une tâche déjà en file en `prefetch`. Les requêtes identiques en cours sont fusionnées, de même que les téléchargements d'images (une image sert aussi de voisine aux deux images adjacentes ; les 64 dernières sont gardées en mémoire).

### 5.7 Configuration (`config.toml`)

Port, jeton, URL Ollama, modèle, `keep_alive` et délai d'Ollama, taille du contexte, température, langue cible par défaut, appareil OCR (`auto`/`gpu`/`cpu`), modèles OCR, seuils OCR et regroupement, taille des tranches, allowlist d'hôtes, chemin du cache, niveau de log.

Emplacement : dossier utilisateur donné par `platformdirs` (`%LOCALAPPDATA%\phylactere` sous Windows, `~/.config/phylactere` sous Linux), créé au premier lancement avec **seulement le jeton** : les autres valeurs viennent de `config.example.toml`, et l'utilisateur n'y écrit que celles qu'il change. (Une copie complète de l'exemple, comme au jalon 1, figeait les valeurs par défaut : un nouveau défaut, comme `device = "auto"`, n'atteignait plus les installations existantes.) Les variables d'environnement `PHYLACTERE_CONFIG_DIR` et `PHYLACTERE_CACHE_DIR` permettent de déplacer les dossiers de configuration et de cache (tests, CI).

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

Réalisation (jalon 5) :
- Les images de l'épisode sont reconnues à leur URL (`image-comic.pstatic.net/…/<titleId>/<no>/`) dans le conteneur `#sectionContWide`, la vignette de l'épisode (même dossier, `thumbnail_…`) étant exclue. Toutes sont envoyées dès l'ouverture, sans attendre leur chargement : les images visibles d'abord (`visible`), puis le reste de l'épisode à partir de la position de lecture (`prefetch`). `index`, `prev_image_url` et `next_image_url` suivent l'ordre du DOM.
- Au plus 3 requêtes en cours : le navigateur n'ouvre que 6 connexions par serveur, et une image devenue visible resterait sinon bloquée dans le navigateur derrière le préchargement, sans atteindre la file à priorité du serveur. Une image déjà envoyée en `prefetch` qui devient visible est renvoyée en `visible` (hors quota) : le serveur fusionne les deux requêtes.
- Serveur injoignable, jeton refusé (401) ou service indisponible (503) suspendent toute la file : badge « Serveur arrêté » et nouvel essai via `/health` toutes les 5 s, « Jeton à saisir » jusqu'au changement du jeton dans le popup, ou nouvel essai après `Retry-After`. Les autres erreurs (400, 502, 504) ne concernent que leur image.
- Le popup vérifie les permissions d'hôte (`chrome.permissions.contains`) et propose de les accorder : Firefox ne les donne pas toujours à l'installation d'une extension MV3.

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
   - `uv sync --extra cpu`, `pytest` ;
   - démarrage du serveur puis appel de `/health` (test de fumée) ;
   - les tests qui chargent PaddleOCR sont marqués `@pytest.mark.ocr` et utilisent un cache des modèles (`actions/cache`) ; ils tournent sur la CI à partir du jalon 2 ;
   - Ollama n'est jamais appelé en CI (client simulé).
   Vérifications statiques dans un workflow séparé, `quality-checks.yml` (mêmes déclencheurs), sans dépendances lourdes : `ruff check` et `ruff format --check` (outils de dev seuls, `uv sync --only-dev`), syntaxe des scripts de l'extension (`node --check`), fichiers référencés par `manifest.json` et `web-ext lint` (erreurs seulement : l'avertissement sur `background.service_worker`, ignoré par Firefox, est attendu), syntaxe des scripts `.ps1` (runner Windows, Windows PowerShell 5.1).
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
3. **Traduction** : client Ollama, schéma JSON, contexte, glossaire, cache, files à priorité, lecture des bulles coupées entre deux images. Critère : `POST /translate` renvoie des traductions complètes pour toutes les images de `samples/` (`scripts/translate_samples.py`). Modèle provisoire du jalon 3 : `qwen3:4b` (`qwen3:1.7b` romanise le coréen au lieu de le traduire), remplacé au jalon 4.
4. **Choix du modèle** (sur le PC Windows) : `bench.py`, comparaison de 2 ou 3 modèles sur vitesse et qualité (relecture humaine), choix du défaut documenté dans le README.
   Méthode retenue :
   - candidats qui tiennent dans 8 Go avec l'OCR : `qwen3:4b`, `qwen3:8b`, `qwen3.5:4b`, `gemma3:4b`, `translategemma:4b`, `exaone3.5:7.8b` (plus `qwen3.5:9b`, ajouté au tour 2 ; pour 12 ou 16 Go, une option plus grosse est documentée sans mesure : pas de machine pour la vérifier) ;
   - `scripts/bench.py` fait l'OCR une fois, puis chaque modèle traduit l'épisode dans l'ordre avec son propre contexte ; il mesure chargement à froid, part du modèle sur le GPU, temps par image (moyenne, maximum) et produit un rapport HTML local (`samples/bench/`) ;
   - relecture à l'aveugle (traductions regroupées, ordre aléatoire, modèles masqués) en deux tours : les 6 modèles avec le prompt actuel, puis les meilleurs avec les variantes de prompt (`context_mode`, `examples`) ;
   - l'utilisateur ne lit pas le coréen : une traduction de référence rédigée par Claude (`bench/references/<titleId>-<no>.json`, texte seul) sert d'appui pour juger le sens. Elle signale aussi les erreurs d'OCR (onomatopées stylisées mal lues).

   Résultats (épisode 729047/66, 40 images, 57 bulles, RTX 3070 Laptop 8 Go) :
   - la relecture a été faite par Claude, à l'aveugle (2 points par bulle juste, 1 si approximative, 0 si fausse ; maximum 114). Contrôle par l'utilisateur sur 10 bulles où les deux finalistes diffèrent : aucun choix opposé ;
   - tour 1 (prompt actuel) : `qwen3.5:4b` 59, `qwen3:8b` 53, `gemma3:4b` 47, `translategemma:4b` 41, `qwen3:4b` 38, `exaone3.5:7.8b` 26 ;
   - tour 2 (variantes) : `qwen3.5:9b` 64, `qwen3.5:4b` 57, `qwen3:8b` 50 avec le prompt actuel. Les exemples d'onomatopées et l'absence de contexte dégradent toujours le résultat ; le contexte en coréen seul ne fait jamais mieux. Le prompt reste donc inchangé (`context_mode = "pairs"`, `examples = false`) ; ces deux variantes ont ensuite été retirées de la configuration et du banc d'essai, qui ne garde que `base` et `sans-contexte` ;
   - vitesse en mode « Meilleures performances » : `qwen3.5:4b` 1,5 s/image, `qwen3.5:9b` 6,1 s/image (6,5 Go de mémoire vidéo avec l'OCR : il frôle la limite de 8 Go et ralentit fortement). Les autres séries ont été faites en mode « Meilleure efficacité », et deux séries dans ce même mode ont donné des temps très différents (1,9 et 4,2 s/image pour `qwen3.5:4b`) sans explication établie. Le banc mesure depuis les débits en jetons par seconde (génération et lecture du prompt, d'après les compteurs d'Ollama), les jetons par bulle et les nouvelles tentatives : ils distinguent une carte bridée de réponses plus longues ;
   - décision du jalon 4 : `qwen3.5:4b` par défaut, `qwen3.5:9b` en alternative pour les cartes de 12 Go ou plus ;
   - **révisée au jalon 5 : `qwen3.5:9b` par défaut**. Une nouvelle série sur le même PC donne `qwen3.5:9b` à 1,4 s/image (55,7 jetons/s, 100 % sur le GPU) et `qwen3.5:4b` à 1,2 s/image : les 6,1 s/image du jalon 4 venaient d'une carte saturée ou bridée, pas du modèle (explication probable : mémoire vidéo occupée par une autre application, débordement silencieux dans la mémoire partagée de Windows). En lecture réelle avec l'extension, `qwen3.5:9b` traduit mieux et ses traductions, plus concises, tiennent dans les bulles avec une police lisible (celles de `qwen3.5:4b` font tomber la police au minimum). `qwen3.5:4b` reste l'alternative pour les cartes de moins de 8 Go ou une mémoire vidéo saturée. La machine de dev Ubuntu (sans GPU) garde `qwen3.5:4b` dans `config.dev.toml`.
5. **Extension** : adaptation de la v1, popup, gestion serveur arrêté, priorités. Critère : lecture d'un épisode complet dans Chrome puis Firefox.
6. **Finition** : autostart, README complet, dépannage, nettoyage du cache par série.

   **Réalisation :**

   - `install-autostart.ps1` crée un raccourci dans le dossier Démarrage de l'utilisateur (pas de tâche planifiée : un déclencheur à l'ouverture de session demande souvent les droits d'administrateur), qui lance `start.ps1` sans fenêtre, puis démarre le serveur ; `-Remove` retire le raccourci. `stop.ps1` arrête les processus `python -m phylactere`.
   - Console cachée : le serveur écrit aussi son journal dans le dossier de logs `platformdirs` (`%LOCALAPPDATA%\phylactere\Logs\server.log`, fichier tournant de 2 Mo, 3 sauvegardes ; `PHYLACTERE_LOG_DIR` le déplace). Le jeton n'y est jamais écrit.
   - Un second lancement (démarrage auto puis `start.ps1`) détecte le serveur qui répond sur `/health` et s'arrête sans erreur.
   - Popup : « Vider le cache de cette série » (série de l'onglet actif, `DELETE /cache?series_id=`) à côté de « Vider tout le cache ».

### Évolutions après la v1

Ordre choisi : d'abord les tests de l'extension, qui n'en a aucun alors que les jalons 8 à 11 la modifient, puis du plus petit au plus gros chantier. Chaque jalon reste livrable seul.

7. **Tests de bout en bout de l'extension**. `content.js` dépend du DOM de Naver, d'un IntersectionObserver et d'une file à quota : c'est la partie la plus fragile, et aucun test ne la couvre.
   - Playwright pour Python (wheels Windows disponibles), dans un groupe de dépendances de développement de `server/`, marqueur pytest `e2e` exclu par défaut comme `smoke` et `ocr`. Chromium chargé avec l'extension non empaquetée (`--load-extension` ; le mode sans fenêtre récent accepte les extensions, à vérifier sur la version installée). Firefox reste en vérification manuelle : Playwright ne charge pas d'extension dans Firefox.
   - Aucun accès réseau : Playwright intercepte `https://comic.naver.com/…` (page d'épisode de test, structure minimale reprise du vrai DOM : `#sectionContWide`, images, vignette) et `https://image-comic.pstatic.net/…` (images synthétiques). Le serveur Phylactère tourne pour de vrai dans le test, avec le détecteur OCR et le client Ollama simulés de `tests/helpers.py`.
   - Scénarios : bulles posées au bon endroit après le défilement ; ordre `visible` avant `prefetch` ; jamais plus de 3 requêtes en cours ; serveur arrêté → badge « Serveur arrêté », puis reprise au redémarrage ; jeton refusé → « Jeton à saisir » ; navigation vers l'épisode suivant sans rechargement (SPA) ; Alt+T et clic sur une bulle.
   - CI : un job Ubuntu séparé, qui installe Chromium (`playwright install --with-deps chromium`).
   - Critère (Ubuntu) : `uv run pytest -m e2e` passe ; un test volontairement cassé (sélecteur faux dans `content.js`) échoue.

   **Réalisation :**

   - `server/tests/test_e2e_extension.py` : serveur uvicorn dans un thread (`create_app` avec images, OCR et Ollama simulés ; `neighbor_margin = 0` et images de 690 × 1000 px pour une seule tranche). La couleur de chaque image code l'épisode et l'index, que l'OCR simulé relit : chaque bulle traduite se rattache à son image. Le journal des requêtes `/translate` (index, priorité, requêtes simultanées) est pris dans le serveur.
   - Chromium complet (`channel="chromium"`) en mode sans fenêtre : le « headless shell » de Playwright ignore les extensions. Les réglages sont écrits dans `chrome.storage.local` depuis le service worker de l'extension.
   - Les 3 premières requêtes partent ensemble et arrivent au serveur dans un ordre variable : le test vérifie leur contenu (0 en `visible`, 1 et 2 en `prefetch`), pas leur ordre.
   - Deux défauts trouvés et corrigés dans `content.js` :
     - à l'ouverture, la file partait avant le premier rappel de l'IntersectionObserver : l'image visible était envoyée en `prefetch`, puis une seconde fois en `visible`. Une image attend désormais que sa visibilité soit connue (`visible: null`) ;
     - changement d'épisode quand la page réutilise ses `<img>` (seul `src` change) : `currentSrc` garde l'ancienne URL jusqu'au chargement de la nouvelle image, le scan l'écartait et rien ne le relançait. Le chargement d'une image relance maintenant un scan. On ne sait pas si le vrai lecteur Naver réutilise ses `<img>` ou les recrée : le test couvre le cas le plus difficile.
   - Vérifié : `MAX_IN_FLIGHT = 4` ou une marge `PAD` fausse font échouer les tests ; chaque correctif retiré fait échouer son test.
   - CI : job `extension` (Ubuntu) qui installe Chromium et lance `pytest -m e2e`.
8. **Mode de traduction au défilement** (extension seule). Aujourd'hui, tout l'épisode part dès l'ouverture : c'est inutile si on survole un épisode ou si on le ferme tôt, et ça occupe le GPU (et la machine Ubuntu, sur CPU, pendant de longues minutes).
   - Nouveau réglage `translateMode` dans `defaults.js` et le popup : `"episode"` (défaut, comportement actuel) ou `"scroll"`. En mode `"scroll"`, réglage `lookahead` (images d'avance, défaut 4, de 1 à 20) : à environ 1,4 s par image sur le PC de référence, 4 images laissent quelques secondes de marge en lecture normale.
   - En mode `"scroll"`, `nextEntry()` ne renvoie qu'une image en file dont l'index est compris entre la première image visible et la dernière image visible + `lookahead`. Les images déjà dépassées ne sont traduites que si elles redeviennent visibles. La fenêtre est recalculée à chaque événement de l'IntersectionObserver (qui appelle déjà `pump()`).
   - Pas d'annulation : une requête partie reste au serveur, qui n'a pas de mécanisme d'annulation. Le gaspillage reste borné par `MAX_IN_FLIGHT` (3).
   - Un changement de mode s'applique tout de suite via `storage.onChanged`, sans recharger la page. Passer de `"scroll"` à `"episode"` relance le préchargement du reste de l'épisode.
   - Le badge distingue « en attente du défilement » de « en cours », pour qu'une image non traduite en bas de l'épisode ne passe pas pour une panne.
   - Critère (Windows, Chrome puis Firefox) : en mode `"scroll"`, le journal du serveur ne montre que les images visibles et les `lookahead` suivantes. Une lecture normale ne fait pas attendre. Un saut en fin d'épisode traduit l'endroit atteint sans traiter tout ce qui précède. Le mode `"episode"` se comporte comme au jalon 5. Les tests du jalon 7 couvrent les deux modes.

   **Réalisation :**

   - `readingWindow()` (`content.js`) calcule la fenêtre `{ from, to }`, ou `null` en mode `"episode"`. Sans aucune image visible (en-tête de la page à l'écran, ou défilement au-delà de l'épisode), la fenêtre part de la première image située sous l'écran (`lookahead` images), ou est vide si toutes sont au-dessus.
   - `translateMode` et `lookahead` sont listés dans `LIVE_SETTINGS` (`defaults.js`) : leur changement relance seulement `pump()`, les autres réglages remettent la page à zéro. `clampLookahead()` borne l'avance à 1-20 dans le popup comme dans le content script. Le popup enregistre ces deux réglages dès leur changement, sans passer par « Enregistrer ».
   - Badge : `· en cours` tant qu'une image est en traitement ou en file dans la fenêtre (dans les deux modes), sinon `· en attente du défilement` s'il reste des images hors fenêtre (détail au survol).
   - Tests : les tests communs du jalon 7 sont paramétrés sur les deux modes ; nouveaux tests de la fenêtre (ouverture, saut à l'image 10, retour à l'image 5, ensemble exact des images demandées), du changement à chaud (`lookahead` puis `"episode"`, sans requête en double) et du popup. Vérifié : sans le filtre de fenêtre, ou si le changement de mode remet la page à zéro, les tests concernés échouent.

9. **Couleur des bulles prise sur l'image**. Le rectangle blanc recouvre le dessin quand le texte d'origine est hors bulle (narration sur fond coloré). Ce jalon ne fait pas d'inpainting : il pose un fond uni de la bonne couleur.
   - Calcul côté serveur, qui a déjà l'image décodée (côté extension, le canvas serait « contaminé » : les images de `pstatic.net` ne sont pas servies avec CORS). Le calcul se fait dans l'étape OCR, sur l'image prolongée par ses voisines, puisque la boîte peut déborder.
   - Méthode : pixels de la boîte de la bulle élargie de quelques pixels, **hors** boîtes des lignes OCR (sinon le texte compte). Couleurs quantifiées (4 bits par canal) ; la classe la plus fréquente donne la couleur de fond (moyenne de ses pixels). Si elle couvre moins d'une part configurable des pixels (défaut 60 %, fond dessiné ou dégradé), `bg = null` et l'extension garde le fond blanc actuel.
   - Couleur du texte : noir ou blanc, selon le meilleur contraste (luminance relative WCAG) avec le fond.
   - Format : `Bubble` et `TranslatedBlock` gagnent `bg` et `fg` (`"#rrggbb"` ou `null`), renvoyés par `/translate` et `/debug/ocr?format=json`. Une version de format entre dans `ocr_fingerprint` pour invalider le cache OCR. Les traductions déjà en cache n'ont pas ces champs : elles s'affichent en blanc, comme avant, jusqu'au prochain « Vider le cache ». On ne touche pas à `PROMPT_VERSION`, puisque le prompt ne change pas.
   - Extension : `background-color` et `color` en style en ligne, plus une ombre floue de la même couleur (`box-shadow`) pour adoucir le bord du rectangle sur fond uni.
   - Tests pytest sur images synthétiques : bulle blanche à texte noir (blanc et noir), narration bleu nuit à texte blanc (bleu nuit et blanc), fond bruité (`null`), bulle qui déborde sur la voisine.
   - Critère (Windows) : sur `samples/729047-66`, les narrations sur fond coloré gardent leur couleur et les bulles blanches ne changent pas. Contrôle visuel avec `/debug/ocr`, qui affiche la couleur calculée.

   **Réalisation :**

   - `colors.py` : `color_bubbles()` est appelé à la fin de `run_pipeline()`, donc sur l'image prolongée par ses voisines ; `bg`/`fg` suivent la bulle quand ses coordonnées sont ramenées à l'image. Le masque retire les boîtes de **toutes** les lignes OCR (le texte d'une bulle voisine peut tomber dans la marge), élargies de 2 px pour l'anticrénelage.
   - Réglages dans `[ocr]` : `bg_margin` (6 px, comme la marge `PAD` de l'extension : on échantillonne la zone que le rectangle recouvre) et `bg_min_share` (0,6). Ils entrent dans `ocr_fingerprint` comme les autres réglages OCR, plus `OCR_FORMAT_VERSION = 2`.
   - `/debug/ocr` : pastille à droite du numéro de bulle, remplie de la couleur de fond avec un « A » de la couleur du texte ; pastille blanche barrée si `bg = null`.
   - Extension : `background-color`, `color` et `box-shadow: 0 0 6px 3px <bg>` en style en ligne, seulement si `bg` est présent ; sinon le blanc de `content.css`.
   - Essai (Ubuntu, PaddleOCR CPU) sur les 83 images de `samples/729047-66` : 104 bulles, 89 blanches (`#fdfdfd` à `#ffffff`, texte noir), 3 sur fond sombre uni (2 noires, 1 narration gris-bleu `#364454`, texte blanc), 14 sans couleur : bruitages sur le dessin et narrations sur fond rayé ou en dégradé, qui gardent le fond blanc comme avant.
   - Correctif (pendant le jalon 10) : un cartouche ajusté au texte (« 스네이크홀 » en blanc sur un bandeau noir arrondi, 729047/66 image 5) sortait blanc, puisque la marge de 6 px tombe surtout hors du cartouche, sur la page blanche. Le fond entre les lettres (intérieur des boîtes des lignes) l'emporte désormais s'il est uni sur au moins `bg_text_share` (0,7) de ces boîtes. Mesure sur les 124 bulles de l'épisode : le texte et son anticrénelage couvrent 35 à 58 % de la boîte d'une ligne (aucun fond intérieur au-delà de 65 % hors de ce cartouche, qui atteint 78 %) ; seul ce cartouche change. Limite connue : un texte très épais (plus de 70 % de sa boîte) prendrait sa propre couleur comme fond. Les tests synthétiques dessinaient un « texte » couvrant 75 % de la boîte, irréaliste : ramené à 50 %. Les traductions déjà en cache gardent l'ancienne couleur jusqu'à « Vider le cache de cette série ».
   - Tests : `tests/test_colors.py` (cas de la spécification, plus masque du texte, texte d'une bulle voisine, fond légèrement bruité de type JPEG, contraste) ; `bg`/`fg` vérifiés dans `/translate` et `/debug/ocr?format=json` ; e2e : la bulle prend la couleur unie de l'image et un texte blanc. Vérifié : sans le masque du texte, ou sans le style en ligne dans `content.js`, les tests concernés échouent.

10. **Glossaire assisté**. Le glossaire existe (section 5.1), mais il ne s'édite qu'en PowerShell, et l'utilisateur ne lit pas le coréen : il ne peut pas repérer seul les noms à fixer. L'essentiel du gain vient donc des propositions automatiques.
    - **Page de glossaire** de l'extension (`glossary.html`, ouverte dans un onglet depuis le bouton « Glossaire de cette série » du popup : un popup se ferme dès qu'il perd le focus, ce qui gêne l'édition d'un tableau). Tableau `ko` / `fr` / `note` éditable, ajout et suppression de lignes, enregistrement par `PUT /glossary/{series_id}`. Un avertissement rappelle que l'enregistrement efface les traductions en cache de la série.
    - Après l'enregistrement, `background.js` prévient les onglets Naver ouverts sur cette série (`chrome.tabs.query` sur l'URL, couvert par les permissions d'hôte) ; `content.js` remet l'épisode à zéro et le retraduit avec le nouveau glossaire.
    - **Propositions** : `POST /glossary/{series_id}/suggest` `{ "target_lang", "model"?, "episode_id"? }`. Le serveur rassemble les bulles déjà traduites de la série depuis le cache (original et traduction), en partant de l'épisode donné ou du plus récent, dans une limite configurable (défaut 200 bulles). Il demande ensuite au modèle, avec une sortie JSON structurée `{ "entries": [ { "ko", "fr", "note" } ] }`, les noms propres, surnoms, titres et termes d'adresse récurrents.
    - Garde-fous : on ne garde que les `ko` présents tels quels dans au moins un original (le modèle invente parfois) et absents du glossaire actuel, et on renvoie au plus 30 entrées. Rien n'est enregistré : la page affiche les propositions avec des cases à cocher et un `fr` modifiable, puis « Ajouter la sélection ». Pour chaque proposition, une ou deux bulles où le terme apparaît (avec leur traduction actuelle) aident à juger.
    - La requête passe par la file de traduction avec une priorité inférieure à `prefetch` (nouvelle priorité `background`), pour ne pas ralentir la lecture ni charger deux modèles à la fois. Sans aucune traduction en cache pour la série : 404 avec un message clair. JSON invalide deux fois de suite : 502.
    - Tests pytest (Ollama simulé) : filtrage des `ko` inventés, entrées déjà au glossaire écartées, limite de 30, aucune bulle en cache (404), JSON invalide (nouvelle tentative puis 502), priorité `background` servie après `prefetch` dans `queue.py`.
    - Critère (Windows) : après la lecture d'un épisode, « Proposer des entrées » donne en moins de 30 s une liste où figurent les noms des personnages principaux. Une fois la sélection enregistrée, l'onglet de l'épisode se retraduit et les noms restent constants d'une bulle à l'autre.

    **Réalisation :**

    - Serveur : `glossary.py` (prompt, schéma `{ "entries": … }`, garde-fous, une nouvelle tentative puis 502), `Pipeline.suggest_glossary()` (file de traduction, priorité `background`, clé de fusion par série, langue, modèle et épisode), `Cache.series_bubbles()`. `queue.py` distingue `ImagePriority` (`visible`, `prefetch`, seules acceptées par `/translate`) et `Priority` (plus `background`).
    - Bulles envoyées : celles de l'épisode demandé d'abord, puis des autres épisodes du plus récemment traduit au plus ancien, sans doublon (une image traduite par deux modèles ne compte qu'une fois), filtrées sur la langue cible mais pas sur le modèle.
    - Nouvelle section `[glossary]` : `suggest_bubbles` (200) et `num_ctx` (16384). Le contexte par défaut d'Ollama sur une petite carte (4096 jetons) ne contient pas 200 bulles, et Ollama coupe le texte en trop sans erreur. Contrepartie : Ollama recharge le modèle quand `num_ctx` change (à la demande de propositions, puis à la traduction suivante). À mesurer sur Windows (critère des 30 s).
    - Délai propre aux propositions : `glossary.timeout_s` (600 s ; 1800 s et 80 bulles dans `config.dev.toml`). Le `timeout_s` d'Ollama (180 s) faisait échouer la demande sur CPU (~10 jetons/s de lecture du prompt avec `qwen3.5:4b`). La page de glossaire appelle le serveur elle-même (`extension/api.js`, partagé avec `background.js`) : Chrome peut arrêter un service worker dont un événement dure plus de 5 minutes.
    - Le prompt de proposition n'entre pas dans la clé du cache des traductions : `PROMPT_VERSION` ne change pas. `OllamaTranslator._chat` devient `chat()` (schéma et `num_ctx` en paramètres) ; la requête de traduction est identique.
    - Essai réel (Ubuntu, `qwen3.5:4b` sur CPU, 30 bulles de 729047/66) : le premier prompt donnait 17 propositions dont une majorité de bruit (interjections, phrases, crédits de l'épisode) et recopiait son propre exemple de nom dans les notes (« ami de Min-su »). Prompt revu : exemples neutres, exclusions explicites (interjections, phrases, mots courants, crédits), une seule forme par terme, « peu d'entrées sûres ». Résultat : 6 propositions (꼬랑지, 크리쳐, 스네이크홀, 재웅님… plus deux erreurs : les crédits et une onomatopée), 194 jetons générés au lieu de 1 473. La qualité avec `qwen3.5:9b` reste à juger sur Windows.
    - Enregistrement ciblé (idée de l'utilisateur pendant le jalon) : au lieu d'effacer toutes les traductions de la série, `Cache.put_glossary()` compare l'ancien et le nouveau glossaire et n'efface que les images dont une bulle contient un terme ajouté, retiré ou modifié (traduction ou note ; ordre des entrées indifférent), espaces ignorées puisque l'OCR en perd ou en ajoute entre deux lignes. Le grain reste l'image (clé du cache) : les autres bulles d'une image concernée sont retraduites aussi. Remplacer le texte dans les traductions en cache a été écarté : on ne sait pas comment l'ancien terme a été rendu dans chaque bulle, et l'accord du français autour (élision, genre, registre) ne se corrige pas par substitution.
    - Exemples de chaque proposition : les deux premières bulles (dans l'ordre ci-dessus) dont l'original contient le terme.
    - Extension : `glossary.html` / `glossary.js` (tableau éditable, lignes vides ignorées, terme ou traduction manquant et doublon refusés, avertissement de sortie si modifications non enregistrées ; enregistrement désactivé si le glossaire n'a pas pu être lu, pour ne pas l'écraser). Les propositions déjà ajoutées au tableau sans être enregistrées sont aussi écartées. `background.js` prévient les onglets via `chrome.tabs.sendMessage` ; `content.js` répond explicitement (sans réponse, la promesse de l'expéditeur est rejetée) et compte les onglets retraduits, affichés par la page.
    - Tests : `tests/test_glossary.py` (garde-fous, 404, 502, nouvelle tentative, langue et modèle, `num_ctx`, rien d'enregistré), `test_queue.py` (`background` après `prefetch`), `test_cache.py` (ordre des bulles). e2e : propositions (terme inventé écarté), ajout, enregistrement, retraduction de l'onglet de l'épisode. Vérifié : sans le message `glossary-changed` dans `content.js`, le test e2e échoue.

11. **Confort d'utilisation**.
    - **Signaler une bulle** : Alt+clic sur une bulle → `POST /report`. Le serveur écrit dans le dossier de données `platformdirs` (`reports/`, hors du dépôt, puisqu'on ne versionne jamais d'image) un JSON par signalement : URL de l'image, série, épisode, index, lignes OCR, bulle, traduction, modèle, `PROMPT_VERSION`, et la découpe de la bulle en PNG. `scripts/reports.py` en fait un rapport HTML local. But : constituer un corpus de vrais échecs pour régler le prompt et les seuils de regroupement, au lieu d'un seul épisode de référence au banc d'essai.
    - **Préchargement de l'épisode suivant** (mode `"episode"` seulement) : une fois l'épisode courant entièrement traduit et la lecture passée aux 80 %, l'extension envoie en `prefetch` les images de l'épisode suivant. **À vérifier en premier**, sans certitude : comment obtenir les URL de ses images (lien « épisode suivant » dans la page, puis HTML de l'épisode ou API JSON appelée par le lecteur de Naver). Si elles ne s'obtiennent qu'en ouvrant l'épisode, on abandonne ce point.
    - **Délai dépassé côté extension** : `background.js` abandonne une requête `/translate` après 280 s et l'image passe en erreur (« 1 erreur(s) » au badge), alors que le serveur continue et met le résultat en cache (cas constaté sur Ubuntu, CPU chargé : 354 s pour une image). Sur un délai dépassé, remettre l'image en file (avec une limite de tentatives) au lieu de l'afficher en erreur : la nouvelle demande est fusionnée avec la tâche en cours au serveur, ou servie par le cache.
    - Pas de mode « original / traduction côte à côte » : survoler une bulle montre déjà le coréen, et l'utilisateur ne le lit pas.
    - Critère (Windows) : un signalement apparaît dans le rapport avec sa découpe ; en lecture continue, le premier écran de l'épisode suivant est déjà traduit à l'ouverture (si le préchargement est retenu).

    **Réalisation :**

    - Vérification préalable (Ubuntu, `curl` sur 729047/66) : la page d'un épisode est rendue par le serveur de Naver. Le HTML contient les URL des images dans `#sectionContWide` (comme `samples/fetch_samples.py` les lit déjà) et, dans un script inline, `window.__state__ = { …, nextArticle: { no: '67', charge: false, … } }`. Le préchargement est donc retenu, sans ouvrir l'épisode suivant.
    - **Signalement** : `reports.py` (requête, recherche de la bulle dans le résultat OCR, découpe, écriture), `Pipeline.report()`. Le résultat OCR vient du cache (même clé que `/translate` : l'extension envoie les mêmes voisines) ; s'il a été vidé, il est recalculé en priorité `visible`. La bulle est retrouvée par sa boîte (égalité, sinon plus grand recouvrement) ; non retrouvée, le JSON garde ce que l'extension a envoyé, sans lignes (`matched: false`). Découpe : boîte + 24 px, prolongée sur l'image suivante si la bulle déborde. Le JSON contient aussi `prompt_id` (version du prompt, telle qu'elle entre dans la clé du cache) et les couleurs `bg`/`fg`. Identifiant `AAAAMMJJ-HHMMSS-<6 hex>` : les fichiers se trient par date, sans caractère interdit sous Windows.
    - Dossier : `config.data_dir()` (`platformdirs.user_data_dir`, `%LOCALAPPDATA%\phylactere` sous Windows, `~/.local/share/phylactere` sous Linux), déplaçable par `PHYLACTERE_DATA_DIR` (tests). `scripts/reports.py` écrit `index.html` dans le dossier des signalements (découpes liées, pas recopiées), du plus récent au plus ancien ; `--open` l'ouvre, `--dir` choisit un autre dossier.
    - Extension : Alt+clic sur une bulle (le clic simple masque toujours la bulle), message `report` à `background.js`, qui ajoute langue et modèle. Contour pointillé gris pendant l'envoi, rouge une fois enregistrée. Le badge affiche « Bulle signalée » (ou l'erreur) pendant 4 s.
    - Annulation (demande de l'utilisateur pendant le jalon) : un second Alt+clic sur une bulle signalée appelle `DELETE /report/{id}` (identifiant gardé sur la bulle, donc perdu au rechargement de la page : il reste alors la suppression des deux fichiers à la main). Le serveur n'accepte qu'un identifiant de la forme `AAAAMMJJ-HHMMSS-<6 hex>` : aucun autre fichier ne peut être visé.
    - **Épisode suivant** (mode `"episode"` seulement) : déclenché quand plus aucune image de l'épisode n'est en file ni en cours (une image en erreur ne bloque pas) et que 80 % des images ont leur haut au-dessus du bas de l'écran (couvre aussi la lecture des commentaires, où aucune image n'est visible). `content.js` lit `nextArticle` dans le texte des scripts de la page (le content script ne voit pas les variables de la page) ; rien si l'épisode est payant (`charge: true`), absent, ou porte le numéro de l'épisode affiché (script d'une page quittée sans rechargement). Il télécharge ensuite le HTML de l'épisode suivant (`fetch` même origine, avec les cookies), le lit avec `DOMParser` et applique la même sélection que pour la page (`#sectionContWide`, vignette exclue).
    - Les images partent en `prefetch`, dans l'ordre, dans le même quota de 3 requêtes, avec les mêmes voisines que lorsque l'épisode sera ouvert : la clé du cache OCR en dépend. À l'ouverture, tout vient du cache. Badge : `· épisode suivant 5/81` pendant le préchargement. Serveur arrêté ou occupé : l'image reprend avec la file ; erreur ou délai dépassé : ignorée (traduite à l'ouverture). Un passage en mode `"scroll"` arrête l'envoi.
    - **Délai dépassé** : `api.js` renvoie un échec `timeout` (au lieu de `error`) pour `/translate`. `content.js` remet l'image en file au plus 2 fois (`MAX_TIMEOUT_RETRIES`), puis l'affiche en erreur avec le nombre d'essais. Si c'est le renvoi `visible` qui expire alors que la requête de la file court encore, on attend celle-ci. `TRANSLATE_TIMEOUT_MS` est un `let` pour que les tests e2e le raccourcissent.
    - Tests : `tests/test_reports.py` (JSON et découpe, lignes prises dans le cache, OCR recalculé après vidage, bulle non retrouvée, URL refusée et jeton, débordement sur l'image suivante, rapport HTML échappé). `DELETE /report` (fichiers supprimés, autres signalements intacts, nom étranger refusé). e2e : Alt+clic (fichiers écrits), second Alt+clic (annulation), nouveau signalement, délai dépassé puis réussite (une seule traduction au serveur), trois délais dépassés puis erreur, préchargement après 80 % seulement puis épisode suivant servi sans OCR ni traduction, aucun préchargement en mode `"scroll"`. Vérifié : avec un seuil de 0 % ou sans nouvelle tentative, les tests concernés échouent.

### Deuxième série d'évolutions (jalons 12 à 17)

Ordre choisi : d'abord l'hygiène (cache borné, mesures), puis la retraduction, qui pose la mécanique « remplacer une bulle affichée » dont dépendent les corrections et la version améliorée. Ensuite les fonctions qui réutilisent le cache (corrections, résumé). La version améliorée venait après parce que sa faisabilité dépend d'une mesure sur le PC Windows ; elle est hors périmètre pour le moment (section 10), et l'export a repris son numéro, 16. L'export vient en dernier parce qu'il est indépendant et le moins utile. Chaque jalon reste livrable seul. La prétraduction en lot de plusieurs épisodes a été écartée.

12. **Cache borné et statistiques**. Aujourd'hui, `cache.sqlite3` grossit sans limite : chaque image lue y laisse son OCR et ses traductions, par modèle et par langue. Rien ne montre non plus ce que fait le serveur, à part les journaux de la console.
    - Taille maximale `[cache] max_mb` (défaut : 500, 0 = sans limite). Au-delà, on supprime les entrées OCR et traductions les moins récemment **utilisées**, pas les plus anciennes. Il faut une colonne `used` (horodatage), mise à jour à chaque lecture et ajoutée par `ALTER TABLE` aux bases existantes. Les glossaires (et les corrections du jalon 14) ne sont jamais purgés.
    - Une purge tourne au démarrage, puis toutes les 100 écritures, et descend à 90 % du maximum pour ne pas se relancer à chaque écriture. Taille mesurée par `page_count × page_size`. SQLite ne rend pas la place libérée au disque sans `VACUUM`. **À vérifier :** `auto_vacuum = INCREMENTAL` avec `PRAGMA incremental_vacuum` après une purge ; une base existante doit d'abord être convertie par un `VACUUM` complet (une fois, au démarrage, journalisé).
    - `GET /stats` → taille du cache et nombre d'entrées (OCR, traductions, glossaires), puis, depuis le démarrage : images servies, part venue du cache, temps moyens et maximaux de téléchargement, d'OCR et de traduction (sur les 50 dernières images calculées), tâches en file par priorité, modèle et appareil.
    - Popup : section repliable « Statistiques », rafraîchie à l'ouverture, avec la taille du cache à côté du bouton « Vider tout le cache ».
    - Tests : purge au-delà du maximum (ordre par utilisation, glossaires intacts), colonne `used` ajoutée à une base de l'ancien format, `/stats` (compteurs, jeton exigé).
    - Critère (Ubuntu puis Windows) : avec `max_mb` réglé bas, la taille du fichier redescend sous le maximum après la lecture d'un épisode ; le popup affiche des temps cohérents avec les journaux du serveur.

    **Réalisation :**

    - `auto_vacuum = INCREMENTAL` vérifié (Ubuntu, SQLite de Python 3.12) : sans lui, le fichier ne rapetisse jamais. Une base neuve le prend avant la création des tables ; une base existante est réécrite une fois par `VACUUM` au démarrage (message dans le journal). Après une purge ou un `DELETE /cache`, `PRAGMA incremental_vacuum` puis `wal_checkpoint(TRUNCATE)` rendent la place au disque. `incremental_vacuum` renvoie des lignes : sans `fetchall()`, il ne s'exécute qu'en partie.
    - Purge : la taille « vivante » (pages moins pages libres) est remesurée après **chaque** suppression, pas par lot. Les pages libérées apparaissent dans `freelist_count` avant même le commit. Un premier essai par lots de 20 vidait tout un petit cache. Les écritures OCR et traductions précisent désormais leurs colonnes, puisque `used` est ajoutée en fin de table par `ALTER TABLE` (reprise de `created` pour les anciennes lignes).
    - `used` est mise à jour à chaque `get_ocr` et `get_translation`, pas par les lectures du contexte et des propositions de glossaire : une image relue reste, une image seulement citée en contexte peut partir.
    - Purge lancée en tâche de fond au démarrage (réglage abaissé depuis le dernier lancement), puis toutes les 100 écritures, sous le verrou du cache.
    - `GET /stats` : les temps sont ceux des images où l'étape a vraiment tourné (une image dont l'OCR vient du cache ne compte pas pour l'OCR), attente en file comprise. Files : tâches en attente comptées à leur meilleure priorité demandée, plus `running`.
    - Popup : taille du cache sur le bouton « Vider tout le cache », section repliable « Statistiques » remplie à l'ouverture et après un vidage ; cachée si le serveur ne répond pas.
    - Tests : purge par ordre d'utilisation (la plus ancienne relue survit), glossaires intacts, fichier réellement réduit, purge toutes les N écritures, pas de limite à 0, base d'avant le jalon 12 (colonne `used` ajoutée, `auto_vacuum` activé), `DELETE /cache` rend la place, `/stats` (compteurs, temps, files, jeton), `snapshot()` des files. e2e : taille sur le bouton et statistiques dans le popup. Vérifié sur une copie du cache Ubuntu (174 entrées) : conversion, puis purge sous un maximum de 50 ko.

13. **Retraduire une bulle ou une image**. Quand une bulle est absurde, la seule issue aujourd'hui est de vider le cache de la série.
    - Maj+clic sur une bulle : retraduit **cette bulle**. `POST /retranslate` `{ image_url, series_id, episode_id, index, prev_image_url?, next_image_url?, target_lang?, model?, box }`. Le serveur retrouve la bulle dans l'OCR en cache par sa boîte (même recherche que `/report`), la traduit seule avec le contexte habituel, plus les autres bulles de l'image déjà traduites, et remplace son texte dans l'entrée en cache. Réponse : le bloc mis à jour. Priorité `visible`.
    - Maj+Alt+T : retraduit **toute l'image** la plus visible, via `POST /translate` avec `force: true` (le cache de traduction est ignoré puis remplacé ; l'OCR en cache est réutilisé).
    - Modèle de retraduction : choix du popup « Modèle pour retraduire » (défaut : le même que pour traduire). Le résultat est rangé sous la clé du modèle **affiché**, sinon il disparaîtrait au rechargement. Le nom du modèle qui a produit la bulle est gardé dans le bloc (`model`, facultatif) et montré au survol.
    - Pendant la retraduction, contour pointillé sur la bulle (comme le signalement), puis remplacement du texte et nouveau `fitText`. Mise à jour du texte d'aide du badge.
    - Tests : bulle retrouvée et seule changée dans le cache, `force` (le client Ollama simulé est rappelé), modèle de retraduction différent mais clé d'affichage conservée. e2e : Maj+clic remplace le texte de la bulle, le clic simple masque toujours.
    - Critère (Windows) : une bulle retraduite avec un plus gros modèle garde sa nouvelle traduction après rechargement de la page.

    **Réalisation :**

    - `POST /retranslate` cherche la bulle dans la **traduction** en cache (même recherche par boîte que `/report`, mise en commun dans `schemas.match_box`), pas dans l'OCR : c'est elle qu'on remplace, et elle porte déjà l'original. Traduction absente (cache vidé, glossaire modifié) ou bulle introuvable : 404, « rechargez la page ». `retranslate_model` (facultatif) désigne le modèle qui retraduit ; `model` reste le modèle affiché, celui de la clé.
    - Les autres bulles de l'image partent dans une section à part du prompt (« Autres bulles de la même image »), présente seulement pour une retraduction de bulle. Le prompt d'une image entière ne change donc pas, mais `PROMPT_VERSION` passe quand même à 3 (règle : tout changement du prompt l'incrémente) : les traductions en cache seront refaites, l'OCR en cache reste valable.
    - `force: true` sur `/translate` ignore la traduction en cache, réutilise l'OCR en cache et remplace l'entrée. La tâche a sa propre clé dans la file : fusionnée avec une traduction ordinaire en cours de la même image, elle en aurait reçu le résultat sans rien retraduire.
    - Chaque bloc porte `model` (modèle qui l'a traduit) ; l'infobulle affiche l'original puis « — modèle ». Un glossaire enregistré pendant une retraduction de bulle : le résultat est renvoyé mais pas mis en cache.
    - Extension : réglage `retranslateModel` (popup « Modèle pour retraduire », enregistré à chaque changement, sans retraduire la page). Maj+clic : contour pointillé bleu sur la bulle, puis texte, infobulle et `fitText` mis à jour ; le clic garde l'indice de la bulle (`e.blocks[i]`) pour signaler ensuite la version retraduite. Maj+Alt+T : image dont la plus grande hauteur est à l'écran, pointillés sur toutes ses bulles, requête hors quota. `user-select: none` sur les bulles : Maj+clic sélectionnait du texte. Le badge affiche « Bulle retraduite (modèle) », « Image retraduite » ou l'erreur pendant 4 s.
    - Tests : bloc avec son modèle, `force` (Ollama rappelé, OCR en cache, entrée remplacée), `force` avec un autre modèle rangé sous la clé affichée, bulle seule changée (autres bulles en contexte, une seule bulle envoyée), contexte des images précédentes, boîte décalée retrouvée, 404 (pas de traduction, boîte inconnue), jeton. e2e : Maj+clic (texte, infobulle, badge), clic simple qui masque toujours, nouvelle traduction gardée au rechargement sans rappeler Ollama ; Maj+Alt+T retraduit l'image visible seule, sans OCR ni masquage.

14. **Corriger une bulle à la main**. Le signalement dit « c'est faux » ; la correction dit « voici la bonne version », et elle sert ensuite au modèle.
    - Ctrl+clic sur une bulle : elle devient modifiable (`contenteditable`, texte sélectionné). Entrée valide, Échap annule, Maj+Entrée ajoute un retour à la ligne. Valider un texte vide supprime la correction : la traduction du modèle revient.
    - Serveur : table `correction` (empreinte de l'URL de l'image, langue cible, boîte, original, texte, date). Elle ne dépend ni du modèle ni de `PROMPT_VERSION` : une correction humaine survit aux changements de prompt et de modèle. `DELETE /cache` ne la vide pas (comme les glossaires), la purge du jalon 12 non plus. Routes `PUT /correction` et `DELETE /correction`.
    - Application : `/translate` remplace le texte des bulles corrigées (boîte égale, sinon plus grand recouvrement ≥ 50 % avec le même original sans espaces, puisque l'OCR peut avoir changé) et les marque `corrected: true`. Le contexte des images suivantes prend le texte corrigé, et les propositions de glossaire (jalon 10) aussi.
    - Retraduire (jalon 13) une bulle corrigée est refusé : le badge affiche « Bulle corrigée : effacez la correction d'abord ».
    - Affichage : petit liseré sur les bulles corrigées. `scripts/reports.py` montre aussi les corrections : c'est le meilleur corpus pour régler le prompt.
    - Tests : correction appliquée quel que soit le modèle, survit à `DELETE /cache` et à un changement de `PROMPT_VERSION`, retrouvée après un léger décalage de boîte, utilisée dans le contexte. e2e : Ctrl+clic, saisie, Entrée, rechargement → texte corrigé ; Échap ne change rien.
    - Critère (Windows) : une bulle corrigée reste corrigée après « Vider tout le cache » et un changement de modèle dans le popup.

    **Réalisation :**

    - Table `correction` dans `cache.sqlite3` : empreinte de l'URL, langue, boîte (JSON), original, texte, date, plus l'URL, la série, l'épisode, l'index, le modèle affiché et la traduction du modèle remplacée (pour `scripts/reports.py`). Ni `clear` ni la purge n'y touchent (elle n'est pas dans `PURGEABLE`).
    - Les corrections sont appliquées **à la lecture** (`cache.apply_corrections`), jamais écrites dans les traductions en cache : effacer une correction fait revenir la traduction du modèle sans rien recalculer. Elles s'appliquent à la réponse de `/translate` (cache ou calcul, `force` compris), au contexte des images suivantes et aux propositions de glossaire (l'empreinte de l'image est en tête de la clé de traduction).
    - Correspondance (`match_correction`) : boîte égale, sinon même original sans espaces et plus grand recouvrement, s'il atteint 50 % de la **plus grande** des deux boîtes. Une correction ne s'applique qu'à une bulle. Une nouvelle correction (ou `DELETE`) remplace celle qui s'appliquait à la bulle affichée, même si sa boîte a un peu bougé.
    - `/retranslate` applique les corrections avant de chercher la bulle : bulle corrigée → 409, et les bulles voisines passent au modèle sous leur forme corrigée. L'extension refuse déjà le Maj+clic sans appeler le serveur.
    - Extension : Ctrl+clic → `contenteditable`, texte sélectionné, contour vert. Entrée valide, Maj+Entrée ajoute un retour à la ligne (`white-space: pre-line` sur les bulles), Échap **ou un clic ailleurs** annulent. Les touches ne remontent pas à la page (raccourcis de Naver), Alt+T est ignoré pendant la saisie. Texte inchangé : rien n'est envoyé ; texte vide : `DELETE /correction`. Liseré vert à gauche (`data-corrected`), infobulle « corrigée à la main » avec la traduction du modèle. Badge : « Bulle corrigée », « Correction effacée » ou l'erreur.
    - `scripts/reports.py` lit les corrections dans le cache en lecture seule (`cache.read_corrections`, le serveur peut tourner ; `--cache` pour une autre base) et les ajoute au rapport : original, traduction du modèle barrée, correction.
    - Limite connue : un Alt+clic sur une bulle corrigée envoie le texte corrigé comme `translated` du signalement, qui passe alors pour la sortie du modèle.
    - Tests : `test_cache.py` (texte et `machine`, autre langue ou image sans effet, boîte décalée et seuil de 50 %, original sans espaces, une bulle par correction, remplacement, effacement, survie à la purge et à `clear`, contexte et propositions, lecture pour le rapport) ; `test_translate_api.py` (correction quel que soit le modèle, survie à `DELETE /cache` et à un changement de `PROMPT_ID`, boîte décalée, contexte de l'image suivante, `DELETE /correction`, 409 à la retraduction, texte vide refusé, URL étrangère et jeton) ; `test_reports.py` (section des corrections, texte échappé). e2e : Échap ne change rien, Ctrl+clic, saisie sur deux lignes, Entrée, liseré, Maj+clic refusé, clic simple qui masque toujours, rechargement → correction sans retraduire, texte vide → traduction du modèle.

15. **Résumé « Précédemment… »**. En reprenant une série après des semaines, on a oublié l'intrigue.
    - Encart repliable au-dessus de la première image de l'épisode : résumé de l'épisode précédent en 3 à 6 phrases, dans la langue cible. Option du popup pour l'activer (activée par défaut). Bouton pour fermer l'encart.
    - `POST /summary` `{ series_id, episode_id, target_lang?, model? }` : résume l'épisode `episode_id` à partir des bulles en cache, dans l'ordre de lecture (corrections comprises). Rien en cache → 404 et pas d'encart : l'épisode n'a pas été lu avec Phylactère. Le résumé est gardé dans une table `summary` (série, épisode, langue, modèle, `SUMMARY_VERSION`). Un épisode lu seulement en partie est résumé quand même, avec la mention « lu en partie ».
    - Quand : dès que l'épisode courant est entièrement traduit (même déclencheur que le préchargement du jalon 11), l'extension demande son résumé en tâche de fond, pour qu'il soit prêt à l'ouverture du suivant. À l'ouverture, si le résumé du précédent manque, il est demandé en priorité `prefetch`.
    - Épisode précédent : **à vérifier en premier**, `window.__state__` contient probablement `prevArticle` comme il contient `nextArticle` ; à défaut, `no - 1`.
    - Taille : un épisode fait environ 100 à 200 bulles, qui tiennent dans `num_ctx = 16384`. **À mesurer** sur un vrai épisode. Si elles ne tiennent pas, on résume par tranches puis on résume les résumés.
    - Tests : ordre des bulles dans le prompt, résumé en cache (un seul appel au client simulé), 404 sans bulles, mention « lu en partie ». e2e : encart affiché à l'ouverture de l'épisode suivant.
    - Critère (Windows) : après la lecture d'un épisode, l'ouverture du suivant montre un résumé fidèle (relecture humaine).

    **Réalisation :**

    - Vérifié sur la vraie page (729047/66) : `window.__state__` contient `prevArticle: { no: '65', blindInspection: false }`. L'extension le lit comme `nextArticle` ; script d'état sans `prevArticle` : premier épisode, pas d'encart ; pas de script d'état du tout : `no - 1`.
    - Mesuré sur 729047/66 (cache Ubuntu) : 81 images, 107 bulles, 3 400 caractères traduits, de l'ordre de 1 000 jetons : large marge sous `num_ctx = 16384`. Le découpage par tranches existe quand même (`[summary] chunk_chars`, 20 000 caractères par défaut, sans couper une image), puis un résumé des résumés.
    - Seules les **traductions** partent au modèle (pas le coréen), numérotées par image (`Image n :`) : c'est ce que le lecteur a lu, et le prompt est deux fois plus court. Sortie JSON `{"summary": …}`, une nouvelle tentative si elle est invalide ou vide, puis 502. Module `summary.py`, `SUMMARY_VERSION = "1"` (indépendant de `PROMPT_VERSION`). Réglages `[summary]` : `num_ctx`, `timeout_s`, `chunk_chars`.
    - Bulles : celles du modèle demandé ; à défaut (épisode lu avec un autre modèle), celles du modèle qui a traduit le plus d'images de l'épisode. Corrections appliquées, une seule version du prompt par image (la plus récente).
    - « Lu en partie » : trou dans les index des images en cache, ou moins d'images que `image_count`, envoyé par l'extension (longueur de l'épisode à la fin de la lecture ; à l'ouverture du suivant, compté dans la page de l'épisode précédent, téléchargée sans cookies comme pour le préchargement).
    - Table `summary` (série, épisode, langue, modèle → texte, version, empreinte des bulles résumées, `partial`, nombres d'images et de bulles). Le résumé est refait si la version ou l'empreinte change (images traduites depuis, correction, retraduction), ou si `image_count` contredit `partial`. Ni `DELETE /cache` ni la purge n'y touchent : plus aucune bulle en cache, le résumé gardé reste servi. Compté dans `/stats` (`summaries`) et le popup.
    - File de traduction, clé `summary:…:<empreinte>` : la demande « background » de fin de lecture et la demande « prefetch » de l'ouverture se rejoignent, la seconde fait passer la tâche devant.
    - Extension : déclencheur de fin de lecture = épisode entièrement traduit et 80 % atteints, dans les deux modes (pas seulement « episode » comme le préchargement), une fois par épisode, réponse ignorée. À l'ouverture : délai dépassé → nouvelle demande (2 fois au plus) ; serveur arrêté ou pas prêt → redemandé à la reprise de la file ; 404 → pas d'encart. Encart `<details class="phy-previously">` inséré avant `#sectionContWide`, dans le flux (les calques des bulles suivent via le `ResizeObserver`), ignoré par le `MutationObserver`, clics arrêtés. Réglage `summary` (défaut : activé) appliqué à chaud.
    - Tests : `test_summary.py` (ordre de lecture, traductions seules, cache, 404, lu en partie par `image_count` et par trou, résumé refait quand les bulles changent, gardé sans `image_count`, corrections, survie à `DELETE /cache`, autre modèle, jeton, réponse vide → 502, tranches). e2e : résumé calculé en fin de lecture puis affiché à l'ouverture du suivant sans rappeler Ollama, sans recouvrir la première image, repli et fermeture ; épisode lu en partie ; option du popup.

16. **Exporter un épisode traduit en PDF** (numéroté 17 jusqu'au 2026-11-24). Usage strictement personnel : l'export n'est jamais publié ni partagé, de même que les images de `samples/` ne sont jamais versionnées. Il permet de relire un épisode hors ligne, par exemple sur un téléphone.
    - `scripts/export.py <series_id> <episode_id> [--lang fr] [--model …] [--open]`, que le serveur tourne ou non. Le script lit le cache (OCR, traductions, corrections), retélécharge les images (même allowlist que `fetch.py`) et écrit `config.data_dir()/exports/<series_id>-<episode_id>-<lang>.pdf`, jamais dans le dépôt. Il ne lance ni OCR ni traduction : une image sans traduction en cache est exportée sans bulles, avec un avertissement en console.
    - Mise en page : une page par image Naver, aux proportions de l'image (690 × 1600 px). L'image d'origine est posée en fond. **À vérifier :** `fpdf2` doit embarquer le JPEG tel quel, sans le recompresser.
    - Bulles : un rectangle plein de couleur `bg`, puis la traduction en **vrai texte** de couleur `fg`, pas en pixels : il reste net à tous les zooms, sélectionnable et recherchable. Taille de police cherchée comme dans `fitText` (dichotomie, avec la mesure de largeur de `fpdf2`). Une bulle à cheval sur deux images a son fond dessiné sur les deux pages, et son texte posé sur la page qui en porte la plus grande part.
    - Police : DejaVu Sans (licence libre), versionnée avec sa licence dans `server/phylactere/fonts/`. Pas de police système, dont le chemin varie selon l'OS. Les polices standard du PDF ne couvrent que le latin‑1 : `’` et `…` n'y sont pas.
    - Dépendances : `fpdf2` (pur Python), et `pypdf` en dépendance de développement pour relire le texte dans les tests. Vérifier dans `uv.lock` que chacune et ses dépendances ont une wheel `none-any` ou `win_amd64` cp312.
    - Tests : une page par image et aux bonnes dimensions, texte des bulles retrouvé par `pypdf`, bulle à cheval sur deux pages, correction prioritaire sur la traduction, image sans traduction exportée sans bulles.
    - Critère (Windows, puis téléphone) : le PDF d'un épisode lu s'ouvre dans le lecteur PDF de Windows et sur un téléphone en défilement continu, avec des bulles lisibles et bien placées ; la recherche d'un mot d'une bulle le trouve.

    **Réalisation :**

    - Vérifié : `fpdf2` (2.9) embarque le JPEG tel quel, ses octets se retrouvent dans le PDF (image de 89 893 o → PDF de 91 023 o). Les PNG des tests, eux, sont recompressés.
    - URL des images : le cache ne garde que leur empreinte, et une image jamais traduite doit figurer aussi. Le script lit donc la page de l'épisode (`phylactere/naver.py`, partagé avec `translate_samples.py`, `bench.py` et `reports.py`) (sans cookies, comme le préchargement de l'extension) et applique la sélection de `content.js` : HTTPS, `image-comic.pstatic.net`, dossier `/<série>/<épisode>/`, sans la vignette. Sur la vraie page de 729047/66 : 81 images plus la vignette, écartée. Téléchargement par `fetch_bytes` (allowlist, Referer, taille maximale), 4 à la fois.
    - Traductions lues en lecture seule (`cache.read_translations`) par l'empreinte de l'URL : la plus récente du modèle demandé, sinon la plus récente d'un autre modèle, corrections appliquées. Aucune traduction pour tout l'épisode : erreur, pas de PDF.
    - Mise en page (`phylactere/export.py`) : 1 px de l'image = 1 pt, page aux dimensions de l'image. Bulles comme dans `content.js` et `content.css` : marge de 6 px, rectangle arrondi (14), padding 4 × 2, interligne 1,12, police de 9 à 22 cherchée par dichotomie (`multi_cell` en `dry_run`), texte centré verticalement. Couleur du texte : `fg` sur un fond pris sur l'image, noir sur le blanc par défaut. Une bulle à cheval a son rectangle dessiné sur les deux pages (le bord de page le coupe), et son texte sur la page qui en porte la plus grande part, dans la partie visible.
    - Police : DejaVu Sans **Bold** (les bulles de l'extension sont en gras), copiée du paquet Ubuntu `fonts-dejavu-core` avec sa licence (`server/phylactere/fonts/`), `*.ttf binary` dans `.gitattributes`. fpdf2 n'embarque que les glyphes utilisés. Les glyphes absents de DejaVu (hangul resté dans une traduction) ne s'affichent pas.
    - Dépendances : `fpdf2` 2.9.0 (`none-any`), qui tire `fonttools` (wheel `win_amd64` cp312) et Pillow (déjà là) ; `pypdf` 7.1 en dépendance de développement (`none-any`).
    - Essai réel (Ubuntu) : 729047/66, 81 pages, 107 bulles, 12 Mo (taille des images d'origine), en 3,5 s ; rendu vérifié avec `pdftoppm` (bulles placées, fond sombre et texte blanc, bulle à cheval). Limite : un mot trop long pour une bulle étroite est coupé n'importe où (« Mâc/hoir/e »), comme le fait l'extension (`overflow-wrap: anywhere`).
    - Tests (`test_export.py`) : une page par image aux bonnes dimensions, texte retrouvé par `pypdf` (hors latin-1 compris), vignette ignorée, page sans cookies et images avec le Referer, JPEG non recompressé, image sans traduction exportée sans bulles, aucune traduction → erreur, correction prioritaire, modèle demandé puis un autre, bulle à cheval (texte sur la page qui en porte le plus, fond sur les deux), couleurs par défaut, police réduite pour un long texte, sélection des URL comme l'extension.

17. **Cache : URL et nombre d'images, une seule règle de choix, corrections à la lecture**. Issu de la revue `/simplify` du jalon 16 : le résumé et l'export contournaient chacun des manques du cache.
    - La table `translation` garde l'URL de l'image (`image_url`, ajoutée par `ALTER TABLE`, vide pour les lignes plus anciennes). Une table `episode` garde le nombre d'images de chaque épisode, que l'extension envoie avec chaque image (`image_count` de `/translate`).
    - Le résumé lit ce nombre dans le cache : `SummaryRequest.image_count` disparaît, et l'extension ne télécharge plus la page de l'épisode précédent à chaque ouverture.
    - L'export prend l'URL et le nombre des images dans le cache, et ne lit la page de Naver que s'il en manque.
    - Une seule règle de choix entre plusieurs traductions d'une image, pour le résumé et l'export : prompt actuel d'abord, puis modèle demandé, puis la plus récente.
    - Les corrections sont appliquées par `get_translation` lui-même : plus d'appel séparé après chaque lecture.
    - Bouton « Exporter l'épisode en PDF » dans le popup (demande de l'utilisateur) : grisé tant que l'épisode n'est pas entièrement traduit, avancement affiché dessous ; le PDF arrive dans Téléchargements.
    - Critère (Windows) : après la lecture d'un épisode avec cette version, son export PDF se fait sans lire la page de Naver (pas de message « URL des images lues dans la page de Naver »), et le résumé d'un épisode lu en partie porte la mention « lu en partie ».

    **Réalisation :**

    - `cache.py` : colonne `translation.image_url` (helper `_add_column`, partagé avec la colonne `used`), table `episode (series_id, episode_id, image_count)`, gardée en mémoire et écrite seulement quand le nombre change, conservée par « Vider le cache » et la purge. `put_translation(..., image_url=)`.
    - `ImageRequest.image_count` (facultatif) : retenu par `/translate`, y compris quand l'image vient du cache (un épisode lu avant ce jalon apprend ainsi son nombre d'images, et son résumé est refait avec la bonne mention). L'extension l'envoie pour l'épisode courant (`order.length`) et pour l'épisode suivant préchargé.
    - Règle de choix : `prompt_rank` (prompt actuel, puis modèle demandé), puis la plus récente ; `Cache.episode_images` l'applique et rend, par index, le modèle, l'URL et la traduction corrigée. `episode_bubbles` (résumé) en est la projection ; `read_translations` (lecture seule, par URL) disparaît. Le prompt actuel passe avant le modèle demandé : un ancien prompt a été invalidé. Le résumé choisit donc désormais image par image, comme l'export.
    - `get_translation` applique les corrections (langue lue dans la ligne) ; `/translate` et `/retranslate` ne rappellent plus `corrected`, gardé pour le résultat qui vient d'être calculé.
    - Export : traductions par index (et non plus par empreinte d'URL), URL du cache si elles y sont toutes, sinon page de Naver (`ExportReport.page_read`, signalé par le script). L'export ouvre le cache normalement (SQLite gère la connexion du serveur en parallèle).
    - Bouton d'export : `POST /export` (`ExportRequest`, identifiants en chiffres seulement puisqu'ils entrent dans l'URL de la page) renvoie le PDF, avec `Content-Disposition` et `X-Phylactere-Missing` (images sans bulles). Même code que le script (`export.render_episode`), avec les images gardées en mémoire par le serveur (`Pipeline.download`) ; `ExportError` devient une `ServiceError` (404 sans traduction, 502 page ou image illisible). Popup : bouton visible sur une page d'épisode, état demandé chaque seconde au content script (`episode-status` : images traduites, épisode fini), grisé tant que l'épisode n'est pas fini (une image en erreur ne bloque pas, elle est exportée sans bulles). Le script d'arrière-plan reçoit le PDF (`call(..., binary: true)`) et l'enregistre par `chrome.downloads` dans `Téléchargements/phylactere/` : URL `blob:` là où elle existe (page d'arrière-plan de Firefox, qui refuse les URL `data:`), URL `data:` dans le service worker de Chrome (sans `URL.createObjectURL`). Nouvelle permission `downloads` (Chrome et Firefox redemandent l'autorisation à la mise à jour).
    - Tests : migration (`image_url` vide pour les anciennes lignes), corrections appliquées par `get_translation`, règle unique (export et résumé font le même choix), nombre d'images conservé à la réouverture et par `clear`, `/translate` garde URL et nombre, résumé refait quand le nombre est appris plus tard, export sans lire la page, page lue quand une image n'a jamais été traduite ; `/export` (PDF, en-têtes, pas de page de Naver, 404, identifiants et jeton). e2e : bouton grisé avec l'avancement en mode défilement, activé une fois l'épisode traduit, clic → vrai PDF téléchargé.

## 10. Hors périmètre v1

Effacement et redessin du texte d'origine (inpainting), détecteur de bulles par réseau de neurones, autres sites que comic.naver.com, version mobile, publication sur les stores.

### Brouillon rapide, puis version améliorée (ancien jalon 16)

Mis hors périmètre le 2026-11-24, détails conservés tels quels. Son numéro, 16, a été repris par l'export PDF : en cas de reprise, il prendra le numéro suivant.

Un petit modèle traduit vite ce qu'on lit, et un plus gros repasse derrière quand le GPU est libre.
- **À vérifier en premier, sur le PC Windows, sans certitude :** le modèle par défaut y est déjà `qwen3.5:9b`. Un 4b pour le brouillon et le 9b ensemble ne tiennent sans doute pas dans 8 Go avec PaddleOCR, et chaque basculement de modèle dans Ollama coûte un chargement (58 s à froid mesurées au jalon 3). Mesurer : mémoire GPU avec les deux modèles chargés (`OLLAMA_MAX_LOADED_MODELS`), et durée d'un basculement à chaud (fichiers dans le cache disque du système).
- Selon la mesure, une seule des deux variantes :
  - **A. Deux modèles** (s'ils tiennent ensemble, ou si un basculement coûte moins de 10 s) : brouillon avec `[translation] draft_model`, version améliorée avec le modèle habituel.
  - **B. Un seul modèle, second passage** : le même modèle retraduit l'épisode une fois qu'il est entièrement traduit, avec en contexte les bulles **suivantes** en plus des précédentes. Il gagne la cohérence (noms, répliques qui se répondent d'une image à l'autre), pas la puissance d'un plus gros modèle.
  - Si ni A ni B n'apporte de différence visible au banc d'essai (`bench.py`, relecture humaine), on abandonne ce jalon et on le note ici.
- Mécanique commune : nouvelle priorité `background`, sous `prefetch`, et toute demande `visible` passe devant. L'extension envoie les images déjà affichées en `background` une fois la file vide, dans l'ordre de lecture, en un seul lot (un seul basculement de modèle par épisode en variante A). Les bulles se mettent à jour en place, par la mécanique du jalon 13, avec un fondu discret. Les bulles corrigées ne bougent pas. Un clic sur une bulle n'est pas perturbé par une mise à jour (elle attend que la bulle soit remise).
- La version améliorée est rangée sous sa propre clé de cache. À la réouverture d'un épisode, elle est servie en priorité si elle existe.
- Tests : priorité `background` (servie en dernier, doublée par `visible`), clé de cache de la version améliorée, contexte « bulles suivantes » (variante B). e2e : texte remplacé après le passage en `background`, bulle corrigée intacte.
- Critère (Windows) : en lisant normalement, les premières bulles s'affichent aussi vite qu'avant, et le reste de l'épisode est amélioré sans qu'on attende davantage les images visibles.
