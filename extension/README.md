# Webtoon Translator – Naver

Extension (Chrome et Firefox, Manifest V3) qui traduit les bulles des webtoons de comic.naver.com pendant la lecture.

## Fonctionnement

Le texte des webtoons est dessiné dans les images, il n'y a donc pas de texte HTML à traduire. Pour chaque image de l'épisode, l'extension :

1. la télécharge depuis le script d'arrière-plan (avec le Referer `comic.naver.com` exigé par le serveur d'images) ;
2. l'envoie à Google Cloud Vision (`DOCUMENT_TEXT_DETECTION`), qui renvoie chaque bloc de texte coréen avec ses coordonnées ;
3. traduit les blocs en une seule requête Google Translate ;
4. superpose une bulle blanche contenant la traduction exactement à l'emplacement du texte original.

Les images sont traitées un peu avant d'apparaître à l'écran (IntersectionObserver avec marge de 1500 px), deux à la fois, et le résultat est mis en cache : relire un épisode ne coûte rien.

## 1. Préparer une clé Google Cloud

1. Crée un projet sur https://console.cloud.google.com et associe-lui un compte de facturation (obligatoire, même pour le quota gratuit).
2. Active **Cloud Vision API** et **Cloud Translation API**.
3. Dans « API et services › Identifiants », crée une clé API, puis restreins-la à ces deux API.
4. Colle la clé dans le popup de l'extension.

Les deux API ont un quota gratuit mensuel (Vision en compte environ un millier d'images). Un épisode fait souvent 40 à 80 images ; vérifie la page de tarifs Google Cloud et pose une alerte budgétaire.

## 2. Installer

**Chrome / Edge / Brave** : `chrome://extensions` → active le mode développeur → « Charger l'extension non empaquetée » → choisis ce dossier.

**Firefox (121+)** : `about:debugging#/runtime/this-firefox` → « Charger un module complémentaire temporaire » → choisis `manifest.json`. Ensuite, dans `about:addons` › l'extension › Permissions, autorise l'accès aux sites (Firefox ne les accorde pas automatiquement en MV3). Pour une installation permanente, il faut signer l'extension via addons.mozilla.org (distribution « non listée » possible).

## 3. Utiliser

Ouvre un épisode (`comic.naver.com/webtoon/detail?...`). Un badge en bas à droite indique la progression.

- **Alt+T** ou clic sur le badge : afficher/masquer toutes les traductions.
- Clic sur une bulle : la masquer pour voir l'original (re-clic pour la remettre). Survoler une bulle affiche le texte coréen reconnu.
- Changer de langue dans le popup retraduit la page.

## Ajustements possibles

Les constantes en haut de `content.js` (`MIN_SIZE`, `PRELOAD_MARGIN`, `MAX_CONCURRENT`, `PAD`) règlent la détection et le rendu. Si Naver change d'hébergeur d'images, modifie `IMG_HOST` dans `content.js`, le domaine de la règle dans `background.js` et `host_permissions` dans `manifest.json`.

Pour changer de moteur, seules les fonctions `ocr()` et `translateTexts()` de `background.js` sont à remplacer (par exemple DeepL pour la traduction, ou un modèle de vision qui fait OCR + traduction en une requête).

## Limites connues

- Les onomatopées stylisées et le texte vertical sont mal reconnus ; ils seront parfois ignorés ou mal traduits.
- La bulle blanche recouvre le dessin quand le texte est hors bulle (narration sur fond coloré, effets sonores).
- Vision peut fusionner deux bulles proches en un bloc, ou couper une bulle en deux.
- La traduction automatique ne connaît pas le contexte de l'histoire : noms propres et tutoiement/vouvoiement varient.

Usage personnel : les webtoons restent la propriété de leurs auteurs, ne redistribue pas les traductions.
