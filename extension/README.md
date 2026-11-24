# Extension Phylactère

Extension Manifest V3 (Chrome, Edge, Brave, Firefox 121+), en JavaScript sans build ni dépendance. Installation et utilisation : voir le README à la racine du dépôt, section « Extension du navigateur ».

## Fichiers

- `manifest.json` : déclaration de l'extension. `background` contient à la fois `service_worker` (Chrome) et `scripts` (Firefox) : chaque navigateur prend la clé qu'il connaît.
- `content.js` / `content.css` : injectés dans les pages de comic.naver.com. Repèrent les images de l'épisode, les envoient à traduire (images visibles d'abord, puis préchargement) et superposent les bulles traduites.
- `background.js` : seul à parler au serveur local (`POST /translate`, `/health`, `/models`, `DELETE /cache`) avec le jeton. Il normalise les erreurs pour le content script : serveur arrêté, jeton refusé, service pas prêt (503), erreur propre à une image.
- `popup.html` / `popup.js` : réglages (adresse du serveur, jeton, langue, modèle), état du serveur, vidage du cache.

Les réglages sont dans `chrome.storage.local` ; les modifier relance la traduction des pages ouvertes.

## Tests

Pas de test unitaire : des tests de bout en bout (Playwright) chargent l'extension dans Chromium, sur une fausse page d'épisode, face au vrai serveur dont l'OCR et Ollama sont simulés (`server/tests/test_e2e_extension.py`). Sur Ubuntu, dans `server` :

```bash
uv run playwright install chromium   # une fois (environ 200 Mo)
uv run pytest -m e2e
```

Ils couvrent le placement des bulles, la priorité des images visibles, le quota de 3 requêtes, le serveur arrêté puis relancé, le jeton refusé, le changement d'épisode sans rechargement, Alt+T et le clic sur une bulle. Firefox reste à vérifier à la main : Playwright n'y charge pas d'extension.

## Ajustements

Les constantes en haut de `content.js` (`VIEWER`, `IMG_HOST`, `MAX_IN_FLIGHT`, `PAD`) règlent la détection des images et le rendu. Si Naver change d'hébergeur d'images, modifier aussi `allowed_hosts` dans la configuration du serveur.
