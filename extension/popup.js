const $ = (id) => document.getElementById(id);
const status = (msg, cls = "ok") => { $("status").textContent = msg; $("status").className = cls; };
const send = (msg) => chrome.runtime.sendMessage(msg);

const DEFAULTS = { serverUrl: "http://127.0.0.1:8765", token: "", targetLang: "fr", model: "", enabled: true };
// Le serveur n'écoute que sur la machine locale (et le manifeste ne donne accès qu'à elle).
const LOCAL_URL = /^http:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?\/?$/;
// Firefox n'accorde pas toujours les permissions d'hôte des extensions MV3 à l'installation.
const ORIGINS = ["http://127.0.0.1/*", "http://localhost/*", "https://comic.naver.com/*"];

let savedModel = "";
let currentSeries = null; // titleId de l'onglet actif, s'il affiche une page Naver

// L'URL de l'onglet est lisible grâce à la permission d'hôte sur comic.naver.com.
async function activeSeries() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const url = new URL(tab?.url || "");
    return url.hostname === "comic.naver.com" ? url.searchParams.get("titleId") : null;
  } catch {
    return null;
  }
}

async function init() {
  const s = await chrome.storage.local.get(DEFAULTS);
  $("enabled").checked = s.enabled;
  $("serverUrl").value = s.serverUrl;
  $("token").value = s.token;
  $("targetLang").value = s.targetLang;
  savedModel = s.model;
  setModels([], "");
  currentSeries = await activeSeries();
  $("clearSeries").hidden = !currentSeries;
  await checkPermissions();
  await refresh();
  setInterval(refreshHealth, 5000);
}

async function checkPermissions() {
  const granted = await chrome.permissions.contains({ origins: ORIGINS });
  $("grant").hidden = granted;
  return granted;
}

$("grant").addEventListener("click", async () => {
  // Doit être appelé directement depuis le clic (geste de l'utilisateur).
  const granted = await chrome.permissions.request({ origins: ORIGINS });
  $("grant").hidden = granted;
  if (granted) refresh();
});

async function refresh() {
  const health = await refreshHealth();
  if (!health?.ok) return;
  const res = await send({ type: "models" });
  if (res?.ok) {
    setModels((res.data.models || []).map((m) => m.name), health.data.model);
  } else {
    setModels([], health.data.model);
    if (res?.kind === "token") status(res.error, "err");
  }
}

async function refreshHealth() {
  const res = await send({ type: "health" });
  const dot = $("dot");
  if (!res?.ok) {
    dot.className = "dot err";
    $("serverState").textContent = res?.kind === "down"
      ? "Serveur arrêté : lancez scripts\\start.ps1."
      : `Serveur en erreur : ${res?.error ?? "pas de réponse"}`;
    return res;
  }
  const h = res.data;
  const problems = [];
  if (h.ocr === "loading") problems.push("OCR en chargement…");
  if (h.ocr === "error") problems.push("OCR en erreur (voir la console du serveur)");
  if (h.ollama === "down") problems.push("Ollama arrêté");
  dot.className = "dot " + (h.ocr === "error" ? "err" : problems.length ? "warn" : "ok");
  const device = h.device === "auto" ? "" : ` (${h.device.toUpperCase()})`;
  $("serverState").textContent = problems.length
    ? `Serveur v${h.version} : ${problems.join(", ")}`
    : `Serveur v${h.version} prêt${device} · ${h.model}`;
  return res;
}

// Liste des modèles Ollama installés ; le modèle enregistré reste proposé même s'il
// n'y figure pas (serveur arrêté, modèle supprimé).
function setModels(names, serverDefault) {
  const select = $("model");
  const current = select.options.length > 1 ? select.value : savedModel;
  select.replaceChildren(new Option(serverDefault ? `Défaut du serveur (${serverDefault})` : "Défaut du serveur", ""));
  const all = [...new Set([...names, savedModel].filter(Boolean))].sort();
  for (const name of all) {
    const missing = names.length && !names.includes(name) ? " — non installé" : "";
    select.add(new Option(name + missing, name));
  }
  select.value = current;
}

$("enabled").addEventListener("change", async () => {
  await chrome.storage.local.set({ enabled: $("enabled").checked });
  status($("enabled").checked ? "Traduction activée." : "Traduction désactivée.");
});

$("save").addEventListener("click", async () => {
  const serverUrl = $("serverUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.serverUrl;
  if (!LOCAL_URL.test(serverUrl)) {
    return status("L’adresse doit être locale : http://127.0.0.1:<port>.", "err");
  }
  const token = $("token").value.trim();
  savedModel = $("model").value;
  await chrome.storage.local.set({ serverUrl, token, targetLang: $("targetLang").value, model: savedModel });
  $("serverUrl").value = serverUrl;
  status(token ? "Réglages enregistrés." : "Enregistré, mais le jeton est vide.", token ? "ok" : "err");
  refresh();
});

// Les pages déjà affichées gardent leurs bulles : recharger la page pour retraduire.
async function clearCache(seriesId) {
  const res = await send({ type: "clear-cache", seriesId });
  if (!res?.ok) return status(res?.error ?? "Échec.", "err");
  const what = seriesId ? `Cache de la série ${seriesId} vidé` : "Cache vidé";
  status(`${what} (${res.data.removed} entrée(s)). Rechargez la page pour retraduire.`);
}

$("clear").addEventListener("click", () => clearCache());
$("clearSeries").addEventListener("click", () => clearCache(currentSeries));

init();
