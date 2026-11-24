const $ = (id) => document.getElementById(id);
const status = (msg, cls = "ok") => { $("status").textContent = msg; $("status").className = cls; };
const send = (msg) => chrome.runtime.sendMessage(msg);

// Le serveur n'écoute que sur la machine locale (et le manifeste ne donne accès qu'à elle).
const LOCAL_URL = /^http:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?\/?$/;
// Firefox n'accorde pas toujours les permissions d'hôte des extensions MV3 à l'installation.
const ORIGINS = chrome.runtime.getManifest().host_permissions;

let savedModel = "";
let currentSeries = null; // titleId de l'onglet actif, s'il affiche une page Naver
let currentEpisode = null; // no de l'épisode affiché (page de lecture seulement)

// L'URL de l'onglet est lisible grâce à la permission d'hôte sur comic.naver.com.
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const url = new URL(tab?.url || "");
    if (url.hostname !== "comic.naver.com") return {};
    return { series: url.searchParams.get("titleId"), episode: url.searchParams.get("no") };
  } catch {
    return {};
  }
}

async function init() {
  const s = await settings();
  $("enabled").checked = s.enabled;
  $("serverUrl").value = s.serverUrl;
  $("token").value = s.token;
  $("targetLang").value = s.targetLang;
  $("translateMode").value = s.translateMode;
  $("lookahead").value = clampLookahead(s.lookahead);
  showMode();
  savedModel = s.model;
  setModels([], "");
  await updateSeries();
  await checkPermissions();
  await refresh();
  setInterval(refreshHealth, 5000);
}

// Les boutons propres à la série n'apparaissent que sur une page Naver.
async function updateSeries() {
  const tab = await activeTab();
  currentSeries = tab.series || null;
  currentEpisode = tab.episode || null;
  $("clearSeries").hidden = !currentSeries;
  $("glossary").hidden = !currentSeries;
}

async function checkPermissions() {
  $("grant").hidden = await chrome.permissions.contains({ origins: ORIGINS });
}

$("grant").addEventListener("click", async () => {
  // Doit être appelé directement depuis le clic (geste de l'utilisateur).
  const granted = await chrome.permissions.request({ origins: ORIGINS });
  $("grant").hidden = granted;
  if (!granted) return;
  await updateSeries(); // l'URL de l'onglet n'est lisible qu'avec la permission
  refresh();
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
  if (h.ocr === "error") problems.push("OCR en erreur (voir le journal %LOCALAPPDATA%\\phylactere\\Logs\\server.log)");
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

// Mode de traduction : enregistré tout de suite et appliqué sans retraduire la page.
function showMode() {
  const scroll = $("translateMode").value === "scroll";
  $("lookaheadRow").hidden = !scroll;
  $("modeHint").textContent = scroll
    ? "Seules les images à l’écran et les suivantes sont traduites : moins de calcul si vous ne lisez pas tout l’épisode."
    : "Les images à l’écran d’abord, puis le reste de l’épisode en arrière-plan.";
}

$("translateMode").addEventListener("change", async () => {
  showMode();
  await chrome.storage.local.set({ translateMode: $("translateMode").value });
  status("Mode de traduction enregistré.");
});

$("lookahead").addEventListener("change", async () => {
  const lookahead = clampLookahead($("lookahead").value);
  $("lookahead").value = lookahead;
  await chrome.storage.local.set({ lookahead });
  status(`${lookahead} image(s) d’avance.`);
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

// Page de glossaire dans un onglet : un popup se ferme dès qu'il perd le focus.
$("glossary").addEventListener("click", async () => {
  const query = new URLSearchParams({ series: currentSeries });
  if (currentEpisode) query.set("episode", currentEpisode);
  await chrome.tabs.create({ url: chrome.runtime.getURL("glossary.html?" + query) });
  window.close();
});

init();
