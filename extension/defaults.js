// defaults.js — réglages par défaut, partagés par background.js, content.js et le popup.
// Chargé avant eux : manifeste (content script, page d'arrière-plan Firefox),
// importScripts (service worker Chrome) et balise <script> du popup.
const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765",
  token: "",
  targetLang: "fr",
  model: "", // vide = modèle de la configuration du serveur
  enabled: true,
  // "episode" : tout l'épisode part dès l'ouverture (images visibles d'abord) ;
  // "scroll" : seulement les images visibles et les `lookahead` suivantes.
  translateMode: "episode",
  lookahead: 4 // images d'avance en mode "scroll" (de 1 à 20)
};

// Réglages appliqués à chaud par content.js, sans retraduire la page.
const LIVE_SETTINGS = ["translateMode", "lookahead"];

function clampLookahead(value) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) ? Math.min(20, Math.max(1, n)) : DEFAULTS.lookahead;
}
