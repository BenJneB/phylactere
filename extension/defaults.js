// defaults.js — réglages (valeurs par défaut, lecture), partagés par background.js,
// content.js, le popup et la page de glossaire.
// Chargé avant eux : manifeste (content script, page d'arrière-plan Firefox),
// importScripts (service worker Chrome) et balise <script> du popup.
const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765",
  token: "",
  targetLang: "fr",
  model: "", // vide = modèle de la configuration du serveur
  // Modèle pour retraduire une bulle ou une image (jalon 13) ; vide = `model`.
  retranslateModel: "",
  enabled: true,
  // "episode" : tout l'épisode part dès l'ouverture (images visibles d'abord) ;
  // "scroll" : seulement les images visibles et les `lookahead` suivantes.
  translateMode: "episode",
  lookahead: 4 // images d'avance en mode "scroll" (de 1 à 20)
};

// Réglages appliqués à chaud par content.js, sans retraduire la page.
const LIVE_SETTINGS = ["translateMode", "lookahead", "retranslateModel"];

// Réglages enregistrés, complétés par les valeurs par défaut.
function settings() {
  return chrome.storage.local.get(DEFAULTS);
}

function clampLookahead(value) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) ? Math.min(20, Math.max(1, n)) : DEFAULTS.lookahead;
}
