# Extension Phylactère

Extension Manifest V3 (Chrome, Edge, Brave, Firefox 121+), en JavaScript sans build ni dépendance. Installation et utilisation : voir le README à la racine du dépôt, section « Extension du navigateur ».

## Fichiers

- `manifest.json` : déclaration de l'extension. `background` contient à la fois `service_worker` (Chrome) et `scripts` (Firefox) : chaque navigateur prend la clé qu'il connaît.
- `content.js` / `content.css` : injectés dans les pages de comic.naver.com. Repèrent les images de l'épisode, les envoient à traduire (images visibles d'abord, puis préchargement de tout l'épisode, ou seulement des `lookahead` images suivantes en mode `scroll`) et superposent les bulles traduites.
- `api.js` : appels au serveur local avec le jeton (`call`, réglages), chargé par `background.js` et par la page de glossaire.
- `background.js` : parle au serveur local pour le content script et le popup (`POST /translate`, `POST` / `DELETE /report`, `/health`, `/models`, `DELETE /cache`, `/glossary`) avec le jeton. Il normalise les erreurs pour le content script : serveur arrêté, jeton refusé, service pas prêt (503), erreur propre à une image. Seule exception : la page de glossaire demande elle-même les propositions (requête qui peut dépasser 5 minutes sur CPU, trop longue pour un service worker). Après l'enregistrement d'un glossaire, il prévient les onglets Naver de la série (message `glossary-changed`), qui se retraduisent.
- `popup.html` / `popup.js` : réglages (adresse du serveur, jeton, langue, modèle, mode de traduction), état du serveur, vidage du cache, ouverture de la page de glossaire.
- `glossary.html` / `glossary.js` : page de glossaire d'une série, ouverte dans un onglet (`?series=<titleId>&episode=<no>`) : un popup se ferme dès qu'il perd le focus. Édition du tableau, propositions du modèle, enregistrement.
- `defaults.js` : valeurs par défaut des réglages, partagées par tous les scripts.
- `icons/` : icônes (16, 32, 48 et 128 px), générées par `scripts/make_icons.py` (depuis `server/` : `uv run python ../scripts/make_icons.py`).

Les réglages sont dans `chrome.storage.local` ; les modifier relance la traduction des pages ouvertes, sauf le mode de traduction et l'avance (`translateMode`, `lookahead`), appliqués à chaud.

## Tests

Pas de test unitaire : des tests de bout en bout (Playwright) chargent l'extension dans Chromium, sur une fausse page d'épisode, face au vrai serveur dont l'OCR et Ollama sont simulés (`server/tests/test_e2e_extension.py`). Sur Ubuntu, dans `server` :

```bash
uv run playwright install chromium   # une fois (environ 200 Mo)
uv run pytest -m e2e
```

Ils couvrent le placement des bulles, la priorité des images visibles, le quota de 3 requêtes, le serveur arrêté puis relancé, le jeton refusé, le changement d'épisode sans rechargement, Alt+T et le clic sur une bulle, dans les deux modes de traduction ; le signalement d'une bulle (Alt+clic, puis annulation), la retraduction d'une bulle (Maj+clic, gardée au rechargement) et d'une image (Maj+Alt+T), la correction d'une bulle (Ctrl+clic, Échap, Entrée, gardée au rechargement, effacée par un texte vide), le délai dépassé (nouvelle tentative, puis erreur), le préchargement de l'épisode suivant (après 80 % de lecture, servi par le cache à l'ouverture, jamais en mode `scroll`) ; puis la fenêtre de lecture du mode `scroll` (saut en fin d'épisode, retour en arrière), le changement de mode à chaud, le popup, et la page de glossaire (propositions, ajout, enregistrement, retraduction de l'onglet de l'épisode). Firefox reste à vérifier à la main : Playwright n'y charge pas d'extension.

## Ajustements

Les constantes en haut de `content.js` (`VIEWER`, `IMG_HOST`, `MAX_IN_FLIGHT`, `PAD`) règlent la détection des images et le rendu. Si Naver change d'hébergeur d'images, modifier aussi `allowed_hosts` dans la configuration du serveur.
