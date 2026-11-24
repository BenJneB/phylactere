const $ = (id) => document.getElementById(id);
const status = (msg, cls = "ok") => { $("status").textContent = msg; $("status").className = cls; };
const send = (msg) => chrome.runtime.sendMessage(msg);

// Le serveur n'écoute que sur la machine locale (et le manifeste ne donne accès qu'à elle).
const LOCAL_URL = /^http:\/\/(127\.0\.0\.1|localhost)(:\d{1,5})?\/?$/;
// Firefox n'accorde pas toujours les permissions d'hôte des extensions MV3 à l'installation.
const ORIGINS = chrome.runtime.getManifest().host_permissions;

let savedModel = "";
let savedRetranslateModel = "";
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
  savedRetranslateModel = s.retranslateModel;
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
  refreshStats();
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
  fillModels($("model"), names, savedModel,
    serverDefault ? `Défaut du serveur (${serverDefault})` : "Défaut du serveur");
  fillModels($("retranslateModel"), names, savedRetranslateModel, "Le même que pour traduire");
}

// Première option (valeur vide) : `emptyLabel` ; sélection en cours gardée.
function fillModels(select, names, saved, emptyLabel) {
  const current = select.options.length > 1 ? select.value : saved;
  select.replaceChildren(new Option(emptyLabel, ""));
  const all = [...new Set([...names, saved].filter(Boolean))].sort();
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

// Modèle pour retraduire : enregistré tout de suite, sans retraduire la page.
$("retranslateModel").addEventListener("change", async () => {
  savedRetranslateModel = $("retranslateModel").value;
  await chrome.storage.local.set({ retranslateModel: savedRetranslateModel });
  status(savedRetranslateModel
    ? `Retraduction avec ${savedRetranslateModel}.`
    : "Retraduction avec le modèle de traduction.");
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

// --- Statistiques (GET /stats) ---

const number = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const megabytes = (bytes) => `${number.format(bytes / 1_000_000)} Mo`;
const seconds = (ms) => `${number.format(ms / 1000)} s`;

function duration(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
}

// Taille du cache sur le bouton de vidage, détail dans la section repliable.
async function refreshStats() {
  const res = await send({ type: "stats" });
  const box = $("stats");
  if (!res?.ok) {
    $("clear").textContent = "Vider tout le cache";
    box.replaceChildren();
    $("statsBox").hidden = true;
    return;
  }
  const st = res.data;
  $("statsBox").hidden = false;
  $("clear").textContent = `Vider tout le cache (${megabytes(st.cache.size_bytes)})`;

  const rows = [];
  const limit = st.cache.max_bytes ? ` sur ${megabytes(st.cache.max_bytes)}` : " (sans limite)";
  rows.push(["Cache", megabytes(st.cache.size_bytes) + limit]);
  rows.push(["En cache", `${st.cache.ocr} image(s) lue(s), ${st.cache.translations} traduction(s), ${st.cache.glossaries} glossaire(s)`]);
  const share = st.images.served ? Math.round((100 * st.images.cached) / st.images.served) : 0;
  rows.push([`Depuis ${duration(st.uptime_s)}`, `${st.images.served} image(s) servie(s), ${share} % depuis le cache`]);
  const labels = { fetch: "Téléchargement", ocr: "OCR", translate: "Traduction" };
  for (const [phase, label] of Object.entries(labels)) {
    const t = st.timings_ms[phase];
    rows.push([label, t?.count ? `${seconds(t.avg_ms)} en moyenne, ${seconds(t.max_ms)} au plus (${t.count} image(s))` : "—"]);
  }
  for (const [name, label] of [["ocr", "File OCR"], ["translation", "File traduction"]]) {
    const q = st.queues[name];
    const waiting = q.visible + q.prefetch + q.background;
    const detail = waiting ? ` (${q.visible} visible(s), ${q.prefetch} en avance, ${q.background} de fond)` : "";
    rows.push([label, `${q.running ? "1 en cours, " : ""}${waiting} en attente${detail}`]);
  }
  rows.push(["Modèle", `${st.model} · ${st.device.toUpperCase()}`]);

  box.replaceChildren(...rows.flatMap(([term, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    return [dt, dd];
  }));
}

// Les pages déjà affichées gardent leurs bulles : recharger la page pour retraduire.
async function clearCache(seriesId) {
  const res = await send({ type: "clear-cache", seriesId });
  if (!res?.ok) return status(res?.error ?? "Échec.", "err");
  const what = seriesId ? `Cache de la série ${seriesId} vidé` : "Cache vidé";
  status(`${what} (${res.data.removed} entrée(s)). Rechargez la page pour retraduire.`);
  refreshStats();
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
