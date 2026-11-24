// api.js — appels au serveur local, partagés par le script d'arrière-plan (background.js)
// et la page de glossaire (glossary.html). La page de glossaire fait elle-même sa requête
// de propositions, qui peut dépasser 5 minutes sur CPU : Chrome peut arrêter un service
// worker dont un même événement dure plus de 5 minutes, pas une page d'extension ouverte.
// Les deux ont la permission d'hôte "http://127.0.0.1/*" du manifeste.

const TOKEN_HEADER = "X-Phylactere-Token";
// Le serveur attend au plus 180 s Ollama (timeout_s), plus la file d'attente. Au-delà,
// content.js remet l'image en file (le serveur, lui, continue). `let` : les tests e2e le
// raccourcissent depuis le service worker.
let TRANSLATE_TIMEOUT_MS = 280_000;
const SHORT_TIMEOUT_MS = 4_000;
// Propositions de glossaire : le serveur attend Ollama jusqu'à glossary.timeout_s (600 s
// par défaut, plus sur CPU) ; l'extension attend plus longtemps pour recevoir son message
// d'erreur plutôt que d'abandonner avant lui.
const SUGGEST_TIMEOUT_MS = 2_400_000;

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
//     "timeout" délai dépassé, si l'appelant le demande (`onTimeout`)
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

// Corps de requête complété par la langue cible et le modèle choisis dans le popup
// (modèle vide : le serveur prend celui de sa configuration).
function withLangModel(body, s) {
  const out = { ...body, target_lang: s.targetLang };
  if (s.model) out.model = s.model;
  return out;
}

// Propositions d'entrées : le serveur les calcule après les images en attente (priorité
// « background »), en une seule requête longue à Ollama.
async function suggestGlossary(seriesId, episodeId) {
  const s = await settings();
  const body = withLangModel(episodeId ? { episode_id: episodeId } : {}, s);
  return call(`/glossary/${encodeURIComponent(seriesId)}/suggest`, {
    method: "POST", body, timeout: SUGGEST_TIMEOUT_MS, onTimeout: "error", s
  });
}
