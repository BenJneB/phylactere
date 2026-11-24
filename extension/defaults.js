// defaults.js — réglages par défaut, partagés par background.js, content.js et le popup.
// Chargé avant eux : manifeste (content script, page d'arrière-plan Firefox),
// importScripts (service worker Chrome) et balise <script> du popup.
const DEFAULTS = {
  serverUrl: "http://127.0.0.1:8765",
  token: "",
  targetLang: "fr",
  model: "", // vide = modèle de la configuration du serveur
  enabled: true
};
