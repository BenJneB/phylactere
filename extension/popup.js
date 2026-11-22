const $ = (id) => document.getElementById(id);
const status = (msg, cls = "ok") => { $("status").textContent = msg; $("status").className = cls; };

chrome.storage.local.get({ apiKey: "", targetLang: "fr", enabled: true }).then((s) => {
  $("apiKey").value = s.apiKey;
  $("targetLang").value = s.targetLang;
  $("enabled").checked = s.enabled;
});

$("enabled").addEventListener("change", async () => {
  await chrome.storage.local.set({ enabled: $("enabled").checked });
  status($("enabled").checked ? "Traduction activée." : "Traduction désactivée.");
});

$("save").addEventListener("click", async () => {
  const apiKey = $("apiKey").value.trim();
  if (!apiKey) return status("Ajoute une clé API pour lancer la traduction.", "err");
  await chrome.storage.local.set({ apiKey, targetLang: $("targetLang").value });
  status("Réglages enregistrés.");
});

$("clear").addEventListener("click", async () => {
  const r = await chrome.runtime.sendMessage({ type: "clear-cache" });
  status(`Cache vidé (${r?.removed ?? 0} image(s)).`);
});
