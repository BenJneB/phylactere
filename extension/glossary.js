// glossary.js — page de glossaire d'une série (ouverte depuis le popup, dans un onglet).
// Paramètres d'URL : series (titleId Naver), episode (no de l'épisode ouvert, facultatif).
// Tous les appels au serveur passent par background.js, comme pour le content script.

const $ = (id) => document.getElementById(id);
// Réponse vide (ou rejet) : le script d'arrière-plan est une ancienne version qui ne
// connaît pas ces messages (extension mise à jour sur le disque mais pas rechargée).
const NO_ANSWER = "L’extension ne répond pas : rechargez-la (chrome://extensions ou " +
  "about:debugging), puis rouvrez cette page.";
async function send(msg) {
  try {
    return (await chrome.runtime.sendMessage(msg)) ?? { ok: false, error: NO_ANSWER };
  } catch {
    return { ok: false, error: NO_ANSWER };
  }
}
const params = new URLSearchParams(location.search);
const seriesId = params.get("series");
const episodeId = params.get("episode");

let dirty = false; // modifications non enregistrées

function setStatus(id, msg, cls = "ok") {
  $(id).textContent = msg;
  $(id).className = "status " + cls;
}

function markDirty() {
  dirty = true;
  setStatus("saveStatus", "Modifications non enregistrées.", "busy");
}

function input(value, label, placeholder = "") {
  const el = document.createElement("input");
  el.type = "text";
  el.value = value;
  el.placeholder = placeholder;
  el.setAttribute("aria-label", label);
  el.spellcheck = false;
  return el;
}

function cell(...children) {
  const td = document.createElement("td");
  td.append(...children);
  return td;
}

// --- Entrées du glossaire ---

function addRow(entry = { ko: "", fr: "", note: "" }, focus = false) {
  const tr = document.createElement("tr");
  tr.className = "entry";
  const ko = input(entry.ko, "Terme coréen", "민수");
  const fr = input(entry.fr, "Traduction", "Min-su");
  const note = input(entry.note || "", "Note", "héros, frère de…");
  ko.lang = "ko";
  ko.dataset.field = "ko";
  fr.dataset.field = "fr";
  note.dataset.field = "note";
  const remove = document.createElement("button");
  remove.className = "icon";
  remove.textContent = "✕";
  remove.title = "Supprimer cette ligne";
  remove.setAttribute("aria-label", "Supprimer cette ligne");
  remove.addEventListener("click", () => {
    tr.remove();
    updateEmpty();
    markDirty();
  });
  const td = cell(remove);
  td.className = "narrow";
  tr.append(cell(ko), cell(fr), cell(note), td);
  for (const el of [ko, fr, note]) el.addEventListener("input", markDirty);
  $("entries").append(tr);
  updateEmpty();
  if (focus) ko.focus();
}

function updateEmpty() {
  $("noEntries").hidden = $("entries").children.length > 0;
}

// Lignes du tableau, sans les lignes entièrement vides ; null si une ligne est invalide.
function readEntries() {
  const entries = [];
  const seen = new Set();
  let error = "";
  for (const tr of $("entries").children) {
    const get = (f) => tr.querySelector(`[data-field=${f}]`);
    const [ko, fr, note] = ["ko", "fr", "note"].map((f) => get(f).value.trim());
    get("ko").classList.remove("invalid");
    get("fr").classList.remove("invalid");
    if (!ko && !fr && !note) continue;
    if (!ko || !fr) {
      get(ko ? "fr" : "ko").classList.add("invalid");
      error ||= "Chaque entrée doit avoir un terme coréen et une traduction.";
      continue;
    }
    if (seen.has(ko)) {
      get("ko").classList.add("invalid");
      error ||= `« ${ko} » figure deux fois.`;
      continue;
    }
    seen.add(ko);
    entries.push({ ko, fr, note });
  }
  if (error) {
    setStatus("saveStatus", error, "err");
    return null;
  }
  return entries;
}

function knownTerms() {
  return new Set([...$("entries").querySelectorAll("[data-field=ko]")].map((el) => el.value.trim()));
}

async function load() {
  setStatus("saveStatus", "Chargement…", "busy");
  const res = await send({ type: "glossary-get", seriesId });
  $("entries").replaceChildren();
  if (!res.ok) {
    updateEmpty();
    setStatus("saveStatus", res.error, "err");
    $("save").disabled = true; // n'écrase pas un glossaire qu'on n'a pas pu lire
    return;
  }
  for (const entry of res.data) addRow(entry);
  updateEmpty();
  dirty = false;
  $("save").disabled = false;
  setStatus("saveStatus", res.data.length ? `${res.data.length} entrée(s).` : "", "ok");
}

async function save() {
  const entries = readEntries();
  if (!entries) return;
  $("save").disabled = true;
  setStatus("saveStatus", "Enregistrement…", "busy");
  const res = await send({ type: "glossary-put", seriesId, entries });
  $("save").disabled = false;
  if (!res.ok) return setStatus("saveStatus", res.error ?? "Échec de l’enregistrement.", "err");
  // Retire les lignes vides et normalise l'affichage.
  $("entries").replaceChildren();
  for (const entry of res.data) addRow(entry);
  dirty = false;
  const tabs = res.notified
    ? ` ${res.notified} onglet(s) de la série en cours de retraduction.`
    : " Les images concernées seront retraduites à leur prochaine ouverture.";
  setStatus("saveStatus", `Glossaire enregistré (${entries.length} entrée(s)).${tabs}`, "ok");
}

// --- Propositions ---

async function suggest() {
  $("suggest").disabled = true;
  $("suggestions").hidden = true;
  setStatus("suggestStatus", "Analyse des bulles traduites… (passe après les images en cours de traduction)", "busy");
  const res = await send({ type: "glossary-suggest", seriesId, episodeId });
  $("suggest").disabled = false;
  if (!res.ok) return setStatus("suggestStatus", res.error, "err");
  // Termes ajoutés au tableau depuis l'ouverture de la page, pas encore enregistrés.
  const known = knownTerms();
  const entries = res.data.entries.filter((e) => !known.has(e.ko));
  showSuggestions(entries);
  setStatus(
    "suggestStatus",
    entries.length
      ? `${entries.length} proposition(s), à partir de ${res.data.bubbles} bulle(s).`
      : `Aucun nouveau terme trouvé dans ${res.data.bubbles} bulle(s).`,
    "ok"
  );
}

function showSuggestions(entries) {
  const rows = $("suggestionRows");
  rows.replaceChildren();
  for (const entry of entries) {
    const tr = document.createElement("tr");
    tr.className = "suggestion";
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = true;
    check.setAttribute("aria-label", `Ajouter ${entry.ko}`);
    const term = document.createElement("div");
    term.className = "term";
    term.lang = "ko";
    term.textContent = entry.ko;
    const fr = input(entry.fr, `Traduction de ${entry.ko}`);
    fr.dataset.field = "fr";
    const note = input(entry.note, `Note pour ${entry.ko}`, "note");
    note.dataset.field = "note";
    const examples = document.createElement("ul");
    examples.className = "examples";
    for (const ex of entry.examples) {
      const li = document.createElement("li");
      const ko = document.createElement("span");
      ko.className = "ko";
      ko.lang = "ko";
      ko.textContent = ex.original;
      li.append(ko, " → ", ex.translated);
      examples.append(li);
    }
    const td = cell(check);
    td.className = "narrow";
    tr.append(td, cell(term), cell(fr), cell(note, examples));
    tr.dataset.ko = entry.ko;
    rows.append(tr);
  }
  $("suggestions").hidden = entries.length === 0;
  updateToggle();
}

function suggestionChecks() {
  return [...$("suggestionRows").querySelectorAll("input[type=checkbox]")];
}

function updateToggle() {
  const checks = suggestionChecks();
  $("toggleAll").textContent = checks.some((c) => c.checked) ? "Tout décocher" : "Tout cocher";
  $("addSelected").disabled = !checks.some((c) => c.checked);
}

function addSelected() {
  let added = 0;
  for (const tr of [...$("suggestionRows").children]) {
    if (!tr.querySelector("input[type=checkbox]").checked) continue;
    const fr = tr.querySelector("[data-field=fr]").value.trim();
    if (!fr) {
      tr.querySelector("[data-field=fr]").classList.add("invalid");
      continue;
    }
    addRow({ ko: tr.dataset.ko, fr, note: tr.querySelector("[data-field=note]").value.trim() });
    tr.remove();
    added++;
  }
  $("suggestions").hidden = $("suggestionRows").children.length === 0;
  updateToggle();
  if (added) {
    markDirty();
    setStatus("suggestStatus", `${added} entrée(s) ajoutée(s) au glossaire : enregistrez pour les appliquer.`, "ok");
  } else {
    setStatus("suggestStatus", "Rien d’ajouté : une traduction est vide.", "err");
  }
}

// --- Initialisation ---

$("addRow").addEventListener("click", () => {
  addRow(undefined, true);
  markDirty();
});
$("save").addEventListener("click", save);
$("suggest").addEventListener("click", suggest);
$("addSelected").addEventListener("click", addSelected);
$("toggleAll").addEventListener("click", () => {
  const checks = suggestionChecks();
  const value = !checks.some((c) => c.checked);
  checks.forEach((c) => (c.checked = value));
  updateToggle();
});
$("suggestionRows").addEventListener("change", updateToggle);
addEventListener("beforeunload", (ev) => {
  if (dirty) ev.preventDefault();
});

if (!seriesId) {
  $("seriesId").textContent = "?";
  setStatus("saveStatus", "Série inconnue : ouvrez cette page depuis le popup, sur une page Naver.", "err");
  $("save").disabled = $("addRow").disabled = $("suggest").disabled = true;
} else {
  $("seriesId").textContent = seriesId;
  document.title = `Glossaire ${seriesId} — Phylactère`;
  load();
}
