# Phylactère

Traduction en temps réel des webtoons de comic.naver.com (coréen → français), entièrement sur ton PC : OCR et traduction tournent en local, sans clé API ni quota.

> Projet en cours de développement : pour l'instant seul le serveur de base existe. Le guide complet arrivera avec le jalon 6.

## Installation sous Windows

Prérequis :

- [git](https://git-scm.com/download/win) ;
- [Ollama](https://ollama.com/download/windows) : télécharger et lancer `OllamaSetup.exe`, puis télécharger le modèle de traduction (environ 3,3 Go) : `ollama pull qwen3.5:4b`. Ollama démarre ensuite tout seul à l'ouverture de session et n'écoute qu'en local (`127.0.0.1:11434`) ; ne pas définir `OLLAMA_HOST=0.0.0.0`, qui l'exposerait sur le réseau. Vérification : `Invoke-RestMethod http://127.0.0.1:11434/api/tags`.

```powershell
git clone <url-du-dépôt> phylactere
cd phylactere
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`-ExecutionPolicy Bypass` autorise ce script non signé pour cette exécution uniquement, sans changer la politique de sécurité de Windows.

Le script installe [uv](https://docs.astral.sh/uv/) si besoin, puis Python 3.12 et les dépendances. Relance-le après chaque `git pull`.

Pour l'OCR (lecture du texte coréen), il installe PaddlePaddle :

- **version GPU** si une carte NVIDIA est détectée et que son pilote supporte CUDA 12.6 ou plus (ligne `CUDA Version` de `nvidia-smi`). Il suffit d'un [pilote NVIDIA](https://www.nvidia.com/fr-fr/drivers/) récent : pas besoin d'installer CUDA, les bibliothèques nécessaires sont téléchargées avec les dépendances (plusieurs Go) ;
- **version CPU** sinon (plus lente).

Il termine par une vérification : chargement de l'OCR (les modèles sont téléchargés la première fois) et lecture d'une image de test. L'appareil utilisé (GPU ou CPU) est affiché ; la même vérification se relance à tout moment avec :

```powershell
uv --directory server run --no-sync python -m phylactere --check-ocr
```

## Lancement

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Le serveur écoute sur `http://127.0.0.1:8765` et affiche son **jeton** (à saisir plus tard dans l'extension). Vérification : ouvrir http://127.0.0.1:8765/health dans le navigateur.

La configuration est dans `%LOCALAPPDATA%\phylactere\config.toml`.

## Modèle de traduction

Le modèle par défaut est **`qwen3.5:4b`**, choisi au jalon 4 après comparaison de 8 modèles sur 40 images d'un épisode (57 bulles), avec relecture à l'aveugle (2 points par bulle juste, 1 par bulle approximative, 0 si fausse ; maximum 114). Les mesures ont été faites sur un PC portable avec une RTX 3070 Laptop (8 Go), OCR chargé en même temps.

| Modèle | Qualité (sur 114) | Vitesse (s/image) | Mémoire vidéo, OCR compris |
|---|---|---|---|
| **`qwen3.5:4b`** (défaut) | 57 à 59 | **1,5** | 4 Go |
| `qwen3.5:9b` | **64** | 6,1 | 6,5 Go |
| `qwen3:8b` | 50 à 53 | 1,6 | 5,6 Go |
| `gemma3:4b` | 47 | 1,8 | |
| `translategemma:4b` | 41 | 1,6 | |
| `qwen3:4b` | 38 | 0,9 | |
| `exaone3.5:7.8b` | 26 | 5,5 | |

Les vitesses viennent de plusieurs séries, faites dans des modes d'alimentation Windows différents ; elles indiquent un ordre de grandeur. Sur un PC portable, le mode **« Meilleures performances »** (Paramètres › Système › Alimentation) peut diviser le temps de traduction par 2 à 3.

- **Carte de 8 Go** : garder `qwen3.5:4b`. `qwen3.5:9b` traduit un peu mieux mais occupe presque toute la mémoire vidéo et devient environ 4 fois plus lent.
- **Carte de 12 Go ou plus** : `qwen3.5:9b` devrait tenir avec de la marge et vaut la peine d'être essayé (non mesuré, faute de machine de test).

Pour changer de modèle : `ollama pull <modèle>`, puis dans `%LOCALAPPDATA%\phylactere\config.toml` :

```toml
[ollama]
model = "qwen3.5:9b"
```

Les noms propres et surnoms se traduisent mal sans contexte : le glossaire de la série (`PUT /glossary/{series_id}`) corrige ce point. Le banc d'essai (`scripts/bench.py`) permet de refaire la comparaison sur un autre épisode.
