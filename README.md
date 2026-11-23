# Phylactère

Traduction en temps réel des webtoons de comic.naver.com (coréen → français), entièrement sur ton PC : OCR et traduction tournent en local, sans clé API ni quota.

> Projet en cours de développement : pour l'instant seul le serveur de base existe. Le guide complet arrivera avec le jalon 6.

## Installation sous Windows

Prérequis :

- [git](https://git-scm.com/download/win) ;
- [Ollama](https://ollama.com/download/windows) : télécharger et lancer `OllamaSetup.exe`, puis télécharger le modèle de traduction (provisoire, environ 2,5 Go) : `ollama pull qwen3:4b`. Ollama démarre ensuite tout seul à l'ouverture de session et n'écoute qu'en local (`127.0.0.1:11434`) ; ne pas définir `OLLAMA_HOST=0.0.0.0`, qui l'exposerait sur le réseau. Vérification : `Invoke-RestMethod http://127.0.0.1:11434/api/tags`.

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
