# Phylactère

Traduction en temps réel des webtoons de comic.naver.com (coréen → français), entièrement sur ton PC : OCR et traduction tournent en local, sans clé API ni quota.

> Projet en cours de développement : pour l'instant seul le serveur de base existe. Le guide complet arrivera avec le jalon 6.

## Installation sous Windows

Prérequis : [git](https://git-scm.com/download/win).

```powershell
git clone <url-du-dépôt> phylactere
cd phylactere
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

`-ExecutionPolicy Bypass` autorise ce script non signé pour cette exécution uniquement, sans changer la politique de sécurité de Windows.

Le script installe [uv](https://docs.astral.sh/uv/) si besoin, puis Python 3.12 et les dépendances. Relance-le après chaque `git pull`.

## Lancement

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

Le serveur écoute sur `http://127.0.0.1:8765` et affiche son **jeton** (à saisir plus tard dans l'extension). Vérification : ouvrir http://127.0.0.1:8765/health dans le navigateur.

La configuration est dans `%LOCALAPPDATA%\phylactere\config.toml`.
