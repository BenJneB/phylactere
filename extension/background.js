// background.js — service worker (Chrome) / page d'arrière-plan (Firefox)
// Rôle : seul point de contact avec le serveur local Phylactère. Le content script
// (injecté dans la page Naver) ne peut pas appeler 127.0.0.1 lui-même : la page impose
// ses propres règles (CORS, accès au réseau local). Le script d'arrière-plan, lui,
// a la permission d'hôte "http://127.0.0.1/*" déclarée dans le manifeste.

// Service worker Chrome : charge DEFAULTS (Firefox le charge via background.scripts).
if (typeof importScripts === "function") importScripts("defaults.js");

const TOKEN_HEADER = "X-Phylactere-Token";
// Le serveur attend au plus 180 s Ollama (timeout_s), plus la file d'attente.
const TRANSLATE_TIMEOUT_MS = 280_000;
const SHORT_TIMEOUT_MS = 4_000;

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const handlers = {
    translate: () => translate(msg.request),
    health: () => health(),
    models: () => models(),
    "clear-cache": () => clearCache(msg.seriesId)
  };
  const handler = handlers[msg?.type];
  if (!handler) return false;
  handler().then(sendResponse, (e) => sendResponse(failure("error", String(e?.message || e))));
  return true; // réponse asynchrone
});

async function settings() {
  return chrome.storage.local.get(DEFAULTS);
}

function failure(kind, error, extra = {}) {
  return { ok: false, kind, error, ...extra };
}

// Appel au serveur. Résultat normalisé, jamais d'exception :
//   { ok: true, data }
//   { ok: false, kind, error } avec kind =
//     "down"   serveur injoignable (arrêté, mauvaise URL)
//     "token"  jeton absent ou refusé
//     "busy"   service indisponible pour l'instant (OCR en chargement, Ollama arrêté…) :
//              réessayer après retryAfter secondes
//     "error"  échec propre à cette requête (URL refusée, erreur du modèle…)
// `onTimeout` : genre d'échec quand le délai est dépassé (un serveur lent n'est pas arrêté).
// `s` : réglages déjà lus par l'appelant.
async function call(path, { method = "GET", body, timeout = SHORT_TIMEOUT_MS, auth = true, onTimeout = "down", s } = {}) {
  s ??= await settings();
  if (auth && !s.token) return failure("token", "Jeton manquant : ouvrez le popup de Phylactère.");
  const headers = {};
  if (auth) headers[TOKEN_HEADER] = s.token;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(s.serverUrl.replace(/\/+$/, "") + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeout)
    });
  } catch (e) {
    if (e?.name === "TimeoutError") {
      return failure(onTimeout, "Le serveur ne répond pas (délai dépassé).");
    }
    return failure("down", `Serveur Phylactère injoignable (${s.serverUrl}). Est-il lancé ?`);
  }

  let data = null;
  try {
    data = await response.json();
  } catch {
    // corps vide ou non JSON
  }
  if (response.ok) return { ok: true, data };

  const detail = typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`;
  if (response.status === 401) return failure("token", "Jeton refusé par le serveur : vérifiez-le dans le popup.");
  if (response.status === 503) {
    const retryAfter = Number(response.headers.get("Retry-After")) || 5;
    return failure("busy", detail, { retryAfter });
  }
  return failure("error", detail);
}

async function translate(request) {
  const s = await settings();
  const body = { ...request, target_lang: s.targetLang };
  if (s.model) body.model = s.model;
  return call("/translate", { method: "POST", body, timeout: TRANSLATE_TIMEOUT_MS, onTimeout: "error", s });
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
