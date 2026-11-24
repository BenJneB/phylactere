// background.js — service worker (Chrome) / page d'arrière-plan (Firefox)
// Rôle : seul point de contact avec le serveur local Phylactère. Le content script
// (injecté dans la page Naver) ne peut pas appeler 127.0.0.1 lui-même : la page impose
// ses propres règles (CORS, accès au réseau local). Le script d'arrière-plan, lui,
// a la permission d'hôte "http://127.0.0.1/*" déclarée dans le manifeste.

// Service worker Chrome : charge DEFAULTS et les appels au serveur (Firefox les charge via
// background.scripts).
if (typeof importScripts === "function") importScripts("defaults.js", "api.js");

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handlers = {
    translate: () => translate(msg.request),
    retranslate: () => retranslate(msg.request),
    correct: () => correct(msg.request),
    summary: () => summary(msg.request),
    report: () => report(msg.request),
    "report-cancel": () => call(`/report/${encodeURIComponent(msg.id)}`, { method: "DELETE" }),
    health: () => health(),
    models: () => models(),
    stats: () => call("/stats"),
    "clear-cache": () => clearCache(msg.seriesId),
    "glossary-get": () => call(`/glossary/${encodeURIComponent(msg.seriesId)}`),
    "glossary-put": () => putGlossary(msg.seriesId, msg.entries)
  };
  const handler = handlers[msg?.type];
  if (!handler) return false;
  handler().then(sendResponse, (e) => sendResponse(failure("error", String(e?.message || e))));
  return true; // réponse asynchrone
});

// Requête longue au serveur (OCR et modèle, file d'attente), avec le délai de /translate.
// `retranslate` : modèle « pour retraduire » du popup ; `onTimeout` : voir call().
async function post(path, request, { onTimeout, retranslate = false }) {
  const s = await settings();
  const body = retranslate ? withRetranslateModel(request, s) : withLangModel(request, s);
  return call(path, { method: "POST", body, timeout: TRANSLATE_TIMEOUT_MS, onTimeout, s });
}

// Délai dépassé : le serveur est lent (CPU chargé), pas arrêté ; content.js remet l'image
// en file, et la nouvelle demande est fusionnée avec la tâche en cours ou servie par le cache.
// Maj+Alt+T (`force`) : retraduction de l'image avec le modèle choisi pour retraduire.
function translate(request) {
  return post("/translate", request, { onTimeout: "timeout", retranslate: !!request.force });
}

// Signalement d'une bulle (Alt+clic) : le serveur enregistre la bulle, ses lignes OCR et
// sa découpe. Il peut devoir refaire l'OCR si le cache a été vidé.
function report(request) {
  return post("/report", request, { onTimeout: "error" });
}

// Maj+clic sur une bulle : le serveur la retraduit seule et la remplace dans son cache.
function retranslate(request) {
  return post("/retranslate", request, { onTimeout: "error", retranslate: true });
}

// Résumé « Précédemment… » d'un épisode (jalon 15), calculé par le serveur à partir des
// bulles en cache. Délai dépassé : le serveur continue et garde le résumé, qu'une nouvelle
// demande retrouve (fusionnée avec la tâche en cours, ou en cache).
function summary(request) {
  return post("/summary", request, { onTimeout: "timeout" });
}

// Ctrl+clic puis Entrée (jalon 14) : texte saisi → PUT /correction ; texte vide → la
// correction est effacée (DELETE /correction) et la traduction du modèle revient.
async function correct(request) {
  const s = await settings();
  const { text, translated, ...target } = request;
  const erase = !text.trim();
  const body = withLangModel(erase ? target : { ...target, text, translated }, s);
  return call("/correction", { method: erase ? "DELETE" : "PUT", body, s });
}

function health() {
  return call("/health", { auth: false });
}

function models() {
  return call("/models");
}

function clearCache(seriesId) {
  const query = seriesId ? `?series_id=${encodeURIComponent(seriesId)}` : "";
  return call("/cache" + query, { method: "DELETE" });
}

// Enregistre le glossaire (le serveur efface les traductions en cache des images qui
// mentionnent un terme ajouté, retiré ou modifié), puis
// prévient les onglets Naver ouverts sur cette série : ils se retraduisent avec lui.
async function putGlossary(seriesId, entries) {
  const res = await call(`/glossary/${encodeURIComponent(seriesId)}`, { method: "PUT", body: entries });
  if (res.ok) res.notified = await notifySeriesTabs(seriesId);
  return res;
}

// L'URL des onglets Naver est lisible grâce à la permission d'hôte sur comic.naver.com.
async function notifySeriesTabs(seriesId) {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: "https://comic.naver.com/*" });
  } catch {
    return 0;
  }
  let notified = 0;
  for (const tab of tabs) {
    let titleId = null;
    try {
      titleId = new URL(tab.url).searchParams.get("titleId");
    } catch {
      continue;
    }
    if (titleId !== seriesId) continue;
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "glossary-changed", seriesId });
      notified++;
    } catch {
      // onglet sans content script (page en chargement, extension rechargée depuis)
    }
  }
  return notified;
}

