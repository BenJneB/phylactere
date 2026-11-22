// background.js — service worker (Chrome) / event page (Firefox)
// Rôle : télécharger l'image du webtoon, l'envoyer à Google Vision (OCR),
// traduire les blocs de texte avec Google Translate, et mettre le résultat en cache.

const RULE_ID = 1;
const DEFAULTS = { apiKey: "", targetLang: "fr", enabled: true };
const HANGUL = /[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]/;
const inflight = new Map(); // évite de traiter deux fois la même image en parallèle

// Les images de Naver refusent les requêtes sans Referer "comic.naver.com".
// Cette règle de session ajoute le Referer uniquement aux requêtes de l'extension
// (tabIds: [-1] = requêtes qui ne viennent d'aucun onglet), pas à celles de la page.
async function setupRefererRule() {
  try {
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds: [RULE_ID],
      addRules: [{
        id: RULE_ID,
        priority: 1,
        action: {
          type: "modifyHeaders",
          requestHeaders: [{ header: "Referer", operation: "set", value: "https://comic.naver.com/" }]
        },
        condition: { requestDomains: ["image-comic.pstatic.net"], tabIds: [-1] }
      }]
    });
  } catch (e) {
    console.warn("[WTT] Règle Referer non installée :", e);
  }
}
const ready = setupRefererRule();

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "translate-image") {
    translateImage(msg.url)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((e) => sendResponse({ ok: false, error: String(e?.message || e) }));
    return true; // réponse asynchrone
  }
  if (msg?.type === "clear-cache") {
    clearCache().then((n) => sendResponse({ ok: true, removed: n }));
    return true;
  }
});

async function translateImage(url) {
  const { apiKey, targetLang } = await chrome.storage.local.get(DEFAULTS);
  if (!apiKey) throw new Error("Clé API Google manquante : ouvre le popup de l'extension.");

  const key = `c:${targetLang}:${url}`;
  const cached = (await chrome.storage.local.get(key))[key];
  if (cached) return cached;

  if (inflight.has(key)) return inflight.get(key);
  const job = (async () => {
    await ready;
    const b64 = await fetchImageBase64(url);
    const blocks = await ocr(b64, apiKey);
    if (blocks.length) {
      const translations = await translateTexts(blocks.map((b) => b.original), targetLang, apiKey);
      blocks.forEach((b, i) => (b.translated = translations[i] ?? ""));
    }
    const result = { blocks };
    await chrome.storage.local.set({ [key]: result });
    return result;
  })();
  inflight.set(key, job);
  try {
    return await job;
  } finally {
    inflight.delete(key);
  }
}

async function fetchImageBase64(url) {
  const r = await fetch(url, { credentials: "omit" });
  if (!r.ok) throw new Error(`Image inaccessible (${r.status})`);
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

async function ocr(b64, apiKey) {
  const r = await fetch("https://vision.googleapis.com/v1/images:annotate", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Goog-Api-Key": apiKey },
    body: JSON.stringify({
      requests: [{
        image: { content: b64 },
        features: [{ type: "DOCUMENT_TEXT_DETECTION" }],
        imageContext: { languageHints: ["ko"] }
      }]
    })
  });
  const j = await r.json();
  if (!r.ok) throw new Error("Vision API : " + (j.error?.message || r.status));
  const resp = j.responses?.[0] || {};
  if (resp.error) throw new Error("Vision API : " + resp.error.message);

  const out = [];
  for (const page of resp.fullTextAnnotation?.pages || []) {
    for (const block of page.blocks || []) {
      const text = blockText(block);
      if (!text || !HANGUL.test(text)) continue;          // ignore crédits latins, chiffres…
      if ((block.confidence ?? 1) < 0.4) continue;         // ignore le bruit
      out.push({ original: text, box: bbox(block.boundingBox) });
    }
  }
  return out;
}

// Reconstruit le texte d'un bloc avec les espaces et sauts de ligne détectés.
function blockText(block) {
  let s = "";
  for (const p of block.paragraphs || []) {
    for (const w of p.words || []) {
      for (const sym of w.symbols || []) {
        s += sym.text;
        const br = sym.property?.detectedBreak?.type;
        if (br === "SPACE" || br === "SURE_SPACE" || br === "EOL_SURE_SPACE" || br === "LINE_BREAK") s += " ";
      }
    }
  }
  return s.replace(/\s+/g, " ").trim();
}

function bbox(poly) {
  const xs = (poly?.vertices || []).map((v) => v.x ?? 0);
  const ys = (poly?.vertices || []).map((v) => v.y ?? 0);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

async function translateTexts(texts, target, apiKey) {
  const out = [];
  for (let i = 0; i < texts.length; i += 100) {
    const r = await fetch("https://translation.googleapis.com/language/translate/v2", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Goog-Api-Key": apiKey },
      body: JSON.stringify({ q: texts.slice(i, i + 100), source: "ko", target, format: "text" })
    });
    const j = await r.json();
    if (!r.ok) throw new Error("Translation API : " + (j.error?.message || r.status));
    out.push(...j.data.translations.map((t) => t.translatedText));
  }
  return out;
}

async function clearCache() {
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((k) => k.startsWith("c:"));
  await chrome.storage.local.remove(keys);
  return keys.length;
}
