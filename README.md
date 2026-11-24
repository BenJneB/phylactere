# Phylactère

Traduction en temps réel des webtoons de comic.naver.com (coréen → français par défaut), entièrement sur ton PC : la lecture du texte (OCR) et la traduction tournent en local, sans clé API ni quota.

Pendant la lecture d'un épisode, chaque bulle de texte coréen est recouverte par sa traduction, au même endroit.

## Fonctionnement

Deux éléments travaillent ensemble :

- **l'extension du navigateur** (Chrome, Edge, Brave, Firefox) repère les images de l'épisode et affiche les bulles traduites par-dessus ;
- **le serveur local** (Python, sur `127.0.0.1:8765`, inaccessible depuis le réseau) télécharge chaque image, y lit le texte coréen avec PaddleOCR, regroupe les lignes en bulles, puis les fait traduire par un modèle de langue local via [Ollama](https://ollama.com).

Les images visibles passent en premier, puis l'extension fait traduire la suite de l'épisode à l'avance : en lecture normale, les traductions sont prêtes avant d'arriver à l'écran. Tout est mis en cache : relire un épisode est instantané.

Configuration conseillée : Windows 10 ou 11 (64 bits), carte NVIDIA avec 8 Go de mémoire vidéo (une carte plus petite ou le processeur seul fonctionnent, plus lentement), une quinzaine de Go d'espace disque (modèles et bibliothèques).

## Installation sous Windows

### 1. Ollama et le modèle de traduction

Télécharger et lancer [`OllamaSetup.exe`](https://ollama.com/download/windows), puis, dans un terminal PowerShell :

```powershell
ollama pull qwen3.5:9b
```

Ollama démarre ensuite tout seul à l'ouverture de session et n'écoute qu'en local (`127.0.0.1:11434`) ; ne pas définir `OLLAMA_HOST=0.0.0.0`, qui l'exposerait sur le réseau. Vérification : `Invoke-RestMethod http://127.0.0.1:11434/api/tags` liste le modèle.

Carte de moins de 8 Go : prendre plutôt `qwen3.5:4b` (voir « Modèle de traduction »).

### 2. Le serveur

Installer [git](https://git-scm.com/download/win), puis :

```powershell
git clone <url-du-dépôt> phylactere
cd phylactere
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`-ExecutionPolicy Bypass` autorise ce script non signé pour cette exécution uniquement, sans changer la politique de sécurité de Windows.

Le script installe [uv](https://docs.astral.sh/uv/) si besoin, puis Python 3.12 et les dépendances. Pour l'OCR, il installe PaddlePaddle :

- **version GPU** si une carte NVIDIA est détectée et que son pilote supporte CUDA 12.6 ou plus (ligne `CUDA Version` de `nvidia-smi`). Il suffit d'un [pilote NVIDIA](https://www.nvidia.com/fr-fr/drivers/) récent : pas besoin d'installer CUDA, les bibliothèques nécessaires sont téléchargées avec les dépendances (plusieurs Go) ;
- **version CPU** sinon (plus lente).

Il termine par une vérification : chargement de l'OCR (les modèles sont téléchargés la première fois) et lecture d'une image de test. L'appareil utilisé (GPU ou CPU) est affiché ; la même vérification se relance à tout moment avec :

```powershell
uv --directory server run --no-sync python -m phylactere --check-ocr
```

### 3. Lancer le serveur

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Le serveur affiche son adresse (`http://127.0.0.1:8765`) et son **jeton**, à saisir dans l'extension. Il tourne tant que la fenêtre reste ouverte (arrêt : Ctrl+C). Vérification : ouvrir http://127.0.0.1:8765/health dans le navigateur.

Pour ne plus avoir à le lancer à la main, voir « Démarrage automatique ».

### 4. L'extension du navigateur

Le dossier `extension` contient l'extension. Elle n'a besoin d'aucune installation de paquet.

**Chrome / Edge / Brave** : `chrome://extensions` › activer le **mode développeur** › « Charger l'extension non empaquetée » › choisir le dossier `extension`.

**Firefox (121 ou plus)** : `about:debugging#/runtime/this-firefox` › « Charger un module complémentaire temporaire » › choisir `extension\manifest.json`. Le module est retiré à la fermeture de Firefox : il faut le recharger à chaque démarrage (une installation permanente demande de signer l'extension sur addons.mozilla.org, en distribution « non listée »). Si le popup affiche « Autoriser l'accès au serveur et à Naver », cliquer dessus : Firefox n'accorde pas toujours ces permissions à l'installation.

Ouvrir ensuite le popup de l'extension (icône dans la barre d'outils ; l'épingler pour l'avoir sous la main), coller le **jeton**, puis **Enregistrer**. La pastille passe au vert quand le serveur est prêt.

## Utilisation

Ouvrir un épisode sur comic.naver.com. Le badge en bas à droite indique la progression (`Traduit · 12/81 · en cours`).

- **Alt+T** ou clic sur le badge : afficher / masquer toutes les traductions ;
- clic sur une bulle : la masquer pour voir l'original (re-clic pour la remettre) ; survoler une bulle affiche le texte coréen reconnu ;
- **Alt+clic** sur une bulle mal lue ou mal traduite : la signaler (elle s'entoure d'un pointillé rouge). Le serveur garde la bulle, sa traduction et une découpe de l'image dans `%LOCALAPPDATA%\phylactere\reports\` ; `uv run python ..\scripts\reports.py --open` (depuis `server\`) en fait un rapport HTML. Ces signalements servent à régler le prompt et la lecture des bulles ;
- en mode « Tout l'épisode », une fois l'épisode traduit et la lecture passée à 80 %, l'épisode suivant est traduit en arrière-plan (badge `épisode suivant 5/81`) : il s'affiche traduit dès son ouverture. Pas de préchargement pour un épisode payant ;
- popup : langue cible, modèle de traduction, activation de l'extension. Changer la langue ou le modèle retraduit la page (l'OCR, déjà en cache, n'est pas refait) ;
- popup › **Quand traduire** : « Tout l'épisode dès l'ouverture » (défaut : images à l'écran d'abord, puis le reste en arrière-plan) ou « Au fil de la lecture » (seulement les images à l'écran et les quelques suivantes, 4 par défaut, de 1 à 20). Le second mode évite de traduire tout un épisode qu'on ne fait que survoler ; le badge affiche alors `en attente du défilement` quand la suite sera traduite à l'approche. Le changement s'applique tout de suite, sans recharger la page ;
- popup › **Vider le cache de cette série** (sur une page Naver) ou **Vider tout le cache** : les traductions seront refaites à la prochaine lecture (recharger la page). Les glossaires sont conservés.

## Démarrage automatique (optionnel)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

Le script crée un raccourci dans le dossier Démarrage de Windows (aucun droit administrateur requis) et lance le serveur tout de suite. Il démarre ensuite à chaque ouverture de session, **sans fenêtre** : ses messages vont dans le journal `%LOCALAPPDATA%\phylactere\Logs\server.log`.

- Arrêter le serveur : `powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1` ;
- retirer le démarrage automatique : `powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1 -Remove`.

Lancer `start.ps1` alors que le serveur tourne déjà n'a pas d'effet : il l'indique et s'arrête.

## Glossaire d'une série

Les noms propres et les surnoms se traduisent mal sans contexte, et peuvent changer d'une bulle à l'autre. Le glossaire d'une série fixe leur traduction.

Sur une page d'épisode Naver : popup › **Glossaire de cette série**. La page s'ouvre dans un onglet :

1. lire d'abord un épisode (ou plusieurs) avec la traduction activée : les propositions partent des bulles déjà traduites ;
2. **Proposer des entrées** : le modèle relève les noms, surnoms, titres et termes d'adresse récurrents (jusqu'à 200 bulles, en partant de l'épisode ouvert ; réglage `[glossary] suggest_bubbles`). Chaque proposition montre une ou deux bulles où le terme apparaît, avec leur traduction actuelle, pour juger sans lire le coréen. Les termes inventés par le modèle (absents des bulles) sont écartés. La demande passe après les images en cours de traduction ;
3. cocher les propositions utiles, corriger la traduction si besoin, **Ajouter la sélection**, puis **Enregistrer**.

L'enregistrement remplace le glossaire et efface les traductions en cache des seules images dont une bulle contient un terme ajouté, retiré ou modifié : les autres restent en cache. Les onglets ouverts sur la série se retraduisent tout de suite (les images inchangées reviennent instantanément du cache), les autres épisodes à leur prochaine ouverture. La colonne « Traduction » contient la traduction dans la langue cible, quelle qu'elle soit.

Sans l'extension, en PowerShell (l'identifiant de la série est le `titleId` de l'URL de l'épisode : `…/detail?titleId=729047&no=66` → `729047`) :

```powershell
$headers = @{ 'X-Phylactere-Token' = '<jeton>' }
$glossary = @(
    @{ ko = '김독자'; fr = 'Kim Dokja'; note = 'héros' },
    @{ ko = '형'; fr = 'grand frère' }
)
$body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $glossary))
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:8765/glossary/729047 -Headers $headers `
    -ContentType 'application/json; charset=utf-8' -Body $body
```

Le glossaire est remplacé en entier à chaque envoi. Lecture : `Invoke-RestMethod http://127.0.0.1:8765/glossary/729047 -Headers $headers`.

## Modèle de traduction

Le modèle par défaut est **`qwen3.5:9b`**. Les candidats ont été départagés au jalon 4 par comparaison de 8 modèles sur 40 images d'un épisode (57 bulles), avec relecture à l'aveugle (2 points par bulle juste, 1 par bulle approximative, 0 si fausse ; maximum 114). Les mesures ont été faites sur un PC portable avec une RTX 3070 Laptop (8 Go), OCR chargé en même temps. Le choix final s'est fait au jalon 5, en lisant de vrais épisodes avec l'extension sur ce même PC : `qwen3.5:9b` y traduit mieux, et ses traductions plus concises tiennent dans les bulles avec une police lisible.

| Modèle | Qualité (sur 114) | Vitesse (s/image) | Mémoire vidéo, OCR compris |
|---|---|---|---|
| **`qwen3.5:9b`** (défaut) | **64** | 1,4 (6,1 lors d'une série) | 6,5 Go |
| `qwen3.5:4b` | 57 à 59 | 1,2 à 1,5 | 4 Go |
| `qwen3:8b` | 50 à 53 | 1,6 | 5,6 Go |
| `gemma3:4b` | 47 | 1,8 | |
| `translategemma:4b` | 41 | 1,6 | |
| `qwen3:4b` | 38 | 0,9 | |
| `exaone3.5:7.8b` | 26 | 5,5 | |

Les vitesses viennent de plusieurs séries, faites dans des modes d'alimentation Windows différents ; elles indiquent un ordre de grandeur. Sur un PC portable, le mode **« Meilleures performances »** (Paramètres › Système › Alimentation) et le chargeur branché peuvent diviser le temps de traduction par 2 à 3.

- **Carte de 8 Go** : `qwen3.5:9b` tient avec l'OCR (6,5 Go), mais sans grande marge. Si quelque chose d'autre occupe la mémoire vidéo, Windows déborde en silence dans la mémoire vive (« mémoire GPU partagée » dans le Gestionnaire des tâches) et la traduction devient plusieurs fois plus lente : c'est l'explication probable de la série à 6,1 s/image. Dans ce cas, passer à `qwen3.5:4b`.
- **Carte de moins de 8 Go** : `qwen3.5:4b`.

Pour changer de modèle : `ollama pull <modèle>`, puis le choisir dans le popup de l'extension, ou le fixer pour toutes les requêtes dans la configuration (voir ci-dessous) :

```toml
[ollama]
model = "qwen3.5:4b"
```

Le banc d'essai (`scripts/bench.py`) permet de refaire la comparaison sur un autre épisode.

## Configuration

Fichiers, tous dans `%LOCALAPPDATA%\phylactere\` :

| Emplacement | Contenu |
|---|---|
| `config.toml` | réglages personnels et jeton |
| `Cache\cache.sqlite3` | traductions, résultats de l'OCR, glossaires |
| `Cache\paddlex\` | modèles de l'OCR |
| `reports\` | bulles signalées (Alt+clic) : un `.json` et une découpe `.png` par signalement |
| `Logs\server.log` | journal du serveur (4 fichiers de 2 Mo au plus) |

`config.toml` est créé au premier lancement avec seulement le jeton. Toutes les valeurs possibles, commentées, sont dans `server\config.example.toml` (à ne pas modifier) : copier dans `config.toml` uniquement celles à changer, dans leur section. Exemples : port (`[server] port`), langue cible par défaut (`[translation] target_lang`), appareil de l'OCR (`[ocr] device = "cpu"`). Redémarrer le serveur ensuite (`stop.ps1` puis `start.ps1`). Après un changement de port, mettre à jour l'adresse du serveur dans le popup.

## Mise à jour

```powershell
git pull
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Puis recharger l'extension : bouton ↻ sur sa carte dans `chrome://extensions`, ou « Recharger » dans `about:debugging` (Firefox).

## Dépannage

Commencer par le popup de l'extension : sa pastille donne l'état du serveur, de l'OCR et d'Ollama. Le journal `%LOCALAPPDATA%\phylactere\Logs\server.log` donne le détail des erreurs.

**Badge « Serveur arrêté »** : le serveur ne répond pas. Le lancer (`start.ps1`) ; l'extension réessaie toutes les 5 s et reprend seule. S'il est lancé, vérifier http://127.0.0.1:8765/health et l'adresse dans le popup (port modifié ?). Si `start.ps1` s'arrête avec une erreur de port déjà utilisé, un autre programme occupe le port 8765 : choisir un autre port (`[server] port` dans `config.toml`, et la même adresse dans le popup).

**Badge « Jeton à saisir dans le popup »** : le jeton est absent ou faux. Il est affiché par `start.ps1`, et écrit dans `config.toml` (`[server] token`).

**Pastille orange « Ollama arrêté »** : lancer Ollama depuis le menu Démarrer (il doit apparaître dans la zone de notification), puis vérifier `ollama list`. Le badge affiche « en attente du serveur » et reprend seul.

**« Modèle absent d'Ollama »** (survol du badge ou journal) : le modèle choisi n'est pas téléchargé. Lancer la commande `ollama pull <modèle>` indiquée, ou choisir un modèle installé dans le popup.

**Le GPU n'est pas utilisé** (popup : « prêt (CPU) », ou `--check-ocr` affiche CPU) :

1. vérifier que `nvidia-smi` répond et affiche `CUDA Version` 12.6 ou plus ; sinon, mettre à jour le [pilote NVIDIA](https://www.nvidia.com/fr-fr/drivers/) ;
2. relancer `install.ps1`, qui choisit la version GPU de PaddlePaddle s'il détecte la carte ;
3. relancer `--check-ocr` (commande ci-dessus) : il indique l'appareil utilisé et avertit si une carte est présente mais pas utilisée.

Ollama utilise le GPU de son côté, indépendamment de l'OCR : `ollama ps` affiche la part du modèle chargée sur le GPU.

**Premier lancement lent** : normal. `install.ps1` télécharge plusieurs Go (PaddlePaddle GPU et bibliothèques CUDA), le premier lancement de l'OCR télécharge ses modèles, et Ollama charge le modèle de traduction en mémoire au démarrage du serveur (jusqu'à une minute). Pendant ce temps, la pastille indique « OCR en chargement » et les traductions attendent.

**Traduction lente en cours de lecture** : regarder dans le Gestionnaire des tâches (Performances › GPU) la « mémoire GPU partagée ». Si elle augmente pendant la traduction, la mémoire vidéo est saturée : fermer les applications gourmandes, désactiver l'accélération matérielle du navigateur, ou passer à `qwen3.5:4b`. Sur un portable : brancher le chargeur et choisir le mode d'alimentation « Meilleures performances ».

**Une image reste sans traduction, ou le badge compte des erreurs** : survoler le badge affiche la dernière erreur. Une image qui dépasse le délai (280 s, serveur très chargé) est redemandée deux fois avant de passer en erreur. Recharger la page relance les images en erreur. Une image sans texte coréen n'a simplement aucune bulle.

**Bulles mal placées, ratées ou mal traduites** : voir « Limites connues ». Le glossaire corrige les noms propres ; vider le cache de la série puis recharger la page refait les traductions.

**Plus rien ne se passe sur Naver** (pas de badge) : vérifier que la traduction est activée dans le popup, et que l'extension est chargée (Firefox la retire à chaque fermeture). Si Naver a changé la structure de ses pages, les réglages en haut de `extension\content.js` sont à adapter (voir `extension\README.md`).

## Désinstallation

1. `install-autostart.ps1 -Remove`, puis `stop.ps1` ;
2. retirer l'extension du navigateur ;
3. supprimer le dossier du dépôt et `%LOCALAPPDATA%\phylactere` (configuration, cache, modèles de l'OCR, journaux) ;
4. facultatif : `ollama rm qwen3.5:9b`, puis désinstaller Ollama et uv.

## Limites connues

- Les onomatopées stylisées et le texte vertical sont souvent mal lus par l'OCR : ignorés ou mal traduits.
- La bulle traduite est un rectangle de couleur unie, prise sur l'image autour du texte d'origine (narration sur fond coloré). Sur un fond dessiné ou en dégradé, elle reste blanche et recouvre le dessin (bruitages). Les traductions mises en cache avant cette version restent blanches jusqu'à « Vider le cache ».
- Deux bulles proches peuvent être fusionnées, ou une bulle coupée en deux.
- Un modèle local de cette taille ne vaut pas un traducteur humain : au banc d'essai, le meilleur obtient 64 points sur 114 (2 par bulle juste, 1 par bulle approximative). Contresens et tournures maladroites restent fréquents.
- Seul comic.naver.com est pris en charge, sur ordinateur.

Usage personnel : les webtoons restent la propriété de leurs auteurs, ne redistribue pas les traductions.

## Développement

Le cahier des charges et les décisions techniques sont dans `docs/SPEC.md`. Le développement se fait sur Ubuntu (`scripts/start.sh`, tests `uv run pytest` dans `server`), l'utilisation sur Windows. Les tests de bout en bout de l'extension (Chromium piloté par Playwright) sont exclus par défaut : `uv run playwright install chromium` une fois, puis `uv run pytest -m e2e` (voir `extension/README.md`).
