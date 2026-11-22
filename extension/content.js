// content.js — injecté sur comic.naver.com
// Détecte les images de l'épisode, les traduit à l'approche du viewport
// et superpose des bulles traduites positionnées sur le texte original.

(() => {
  "use strict";

  // --- Réglages (à ajuster si Naver change son interface) ---
  const IMG_HOST = "image-comic.pstatic.net";
  const MIN_SIZE = 300;            // ignore miniatures et icônes (px naturels)
  const PRELOAD_MARGIN = "1500px"; // traduit les images un peu avant qu'elles soient visibles
  const MAX_CONCURRENT = 2;        // requêtes parallèles vers l'API
  const PAD = 6;                   // marge autour du texte détecté (px naturels)

  const entries = new Map();       // img -> { src, status, blocks, layer, lastW }
  const queue = [];
  let active = 0;
  let generation = 0;
  let enabled = true;
  let hidden = false;
  let targetLang = "fr";
  let lastHref = location.href;
  let io = makeIO();

  // --- Badge d'état ---
  const badge = document.createElement("div");
  badge.className = "wtt-badge";
  badge.title = "Clic ou Alt+T : afficher / masquer les traductions";
  badge.addEventListener("click", toggleHidden);

  chrome.storage.local.get({ enabled: true, targetLang: "fr" }).then((s) => {
    enabled = s.enabled;
    targetLang = s.targetLang;
    scanAll();
  });

  chrome.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    if (ch.enabled) {
      enabled = ch.enabled.newValue;
      if (!enabled) clearAll(); else scanAll();
    }
    if (ch.targetLang) {
      targetLang = ch.targetLang.newValue;
      clearAll();
      scanAll();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.altKey && e.code === "KeyT" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) {
      e.preventDefault();
      toggleHidden();
    }
  });

  // --- Détection des images ---
  function isViewerPage() {
    return /\/detail/.test(location.pathname);
  }

  function consider(img) {
    if (!enabled || !isViewerPage()) return;
    const src = img.currentSrc || img.src || "";
    if (!src.includes(IMG_HOST)) return;
    const prev = entries.get(img);
    if (prev && prev.src === src) return;
    if (prev) removeEntry(img);
    if (!img.complete || !img.naturalWidth) {
      img.addEventListener("load", () => consider(img), { once: true });
      return;
    }
    if (img.naturalWidth < MIN_SIZE || img.naturalHeight < MIN_SIZE) return;
    entries.set(img, { src, status: "pending" });
    io.observe(img);
    updateBadge();
  }

  function scanAll() {
    if (!enabled || !isViewerPage()) { updateBadge(); return; }
    document.querySelectorAll("img").forEach(consider);
  }

  function makeIO() {
    return new IntersectionObserver((list) => {
      for (const ent of list) {
        if (!ent.isIntersecting) continue;
        io.unobserve(ent.target);
        queue.push(ent.target);
      }
      pump();
    }, { rootMargin: `${PRELOAD_MARGIN} 0px` });
  }

  new MutationObserver((muts) => {
    if (location.href !== lastHref) {         // navigation SPA vers un autre épisode
      lastHref = location.href;
      clearAll();
      setTimeout(scanAll, 300);
      return;
    }
    for (const m of muts) {
      if (m.type === "attributes" && m.target.tagName === "IMG") consider(m.target);
      for (const n of m.addedNodes || []) {
        if (n.nodeType !== 1) continue;
        if (n.tagName === "IMG") consider(n);
        else n.querySelectorAll?.("img").forEach(consider);
      }
    }
    scheduleReposition();
  }).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"]
  });

  // --- File d'attente ---
  function pump() {
    while (active < MAX_CONCURRENT && queue.length) {
      const img = queue.shift();
      if (!entries.has(img)) continue;
      active++;
      process(img).finally(() => { active--; pump(); updateBadge(); });
    }
  }

  async function process(img) {
    const e = entries.get(img);
    const gen = generation;
    e.status = "working";
    updateBadge();
    let res;
    try {
      res = await chrome.runtime.sendMessage({ type: "translate-image", url: e.src });
    } catch (err) {
      res = { ok: false, error: err.message };
    }
    if (gen !== generation || entries.get(img) !== e) return; // obsolète
    if (!res?.ok) {
      e.status = "error";
      e.error = res?.error || "Erreur inconnue";
      console.warn("[WTT]", e.error, e.src);
      return;
    }
    e.status = "done";
    e.blocks = res.blocks;
    render(img, e);
  }

  // --- Rendu des bulles ---
  function render(img, e) {
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const layer = document.createElement("div");
    layer.className = "wtt-layer" + (hidden ? " wtt-hidden" : "");

    for (const b of e.blocks) {
      const x = Math.max(0, b.box.x - PAD);
      const y = Math.max(0, b.box.y - PAD);
      const w = Math.min(nw - x, b.box.w + PAD * 2);
      const h = Math.min(nh - y, b.box.h + PAD * 2);
      const el = document.createElement("div");
      el.className = "wtt-bubble";
      el.lang = targetLang;
      el.textContent = b.translated;
      el.title = b.original;
      el.dataset.h = h;
      el.style.left = (x / nw) * 100 + "%";
      el.style.top = (y / nh) * 100 + "%";
      el.style.width = (w / nw) * 100 + "%";
      el.style.minHeight = (h / nh) * 100 + "%";
      el.addEventListener("click", (ev) => { ev.stopPropagation(); el.classList.toggle("wtt-peek"); });
      layer.appendChild(el);
    }
    document.body.appendChild(layer);
    e.layer = layer;
    position(img, e);
  }

  function position(img, e) {
    if (!e.layer) return;
    const r = img.getBoundingClientRect();
    if (!r.width) { e.layer.style.display = "none"; return; }
    Object.assign(e.layer.style, {
      display: "",
      left: r.left + scrollX + "px",
      top: r.top + scrollY + "px",
      width: r.width + "px",
      height: r.height + "px"
    });
    if (e.lastW !== r.width) { e.lastW = r.width; fitText(img, e); }
  }

  // Réduit la police jusqu'à ce que la traduction tienne dans la zone d'origine.
  function fitText(img, e) {
    const scale = e.layer.clientWidth / img.naturalWidth;
    for (const el of e.layer.children) {
      const target = parseFloat(el.dataset.h) * scale;
      let size = Math.min(22, Math.max(12, target * 0.3));
      el.style.fontSize = size + "px";
      while (el.scrollHeight > target * 1.1 && size > 9) {
        size -= 1;
        el.style.fontSize = size + "px";
      }
    }
  }

  let rafPending = false;
  function scheduleReposition() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      for (const [img, e] of entries) {
        if (!img.isConnected) removeEntry(img);
        else position(img, e);
      }
    });
  }
  addEventListener("resize", scheduleReposition);
  new ResizeObserver(scheduleReposition).observe(document.body);

  // --- Utilitaires ---
  function removeEntry(img) {
    const e = entries.get(img);
    e?.layer?.remove();
    entries.delete(img);
    io.unobserve(img);
  }

  function clearAll() {
    generation++;
    queue.length = 0;
    io.disconnect();
    io = makeIO();
    for (const e of entries.values()) e.layer?.remove();
    entries.clear();
    updateBadge();
  }

  function toggleHidden() {
    hidden = !hidden;
    document.querySelectorAll(".wtt-layer").forEach((l) => l.classList.toggle("wtt-hidden", hidden));
    updateBadge();
  }

  function updateBadge() {
    if (!enabled || !isViewerPage() || entries.size === 0) { badge.remove(); return; }
    if (!badge.isConnected) document.body.appendChild(badge);
    let done = 0, errors = 0, lastError = "";
    for (const e of entries.values()) {
      if (e.status === "done") done++;
      if (e.status === "error") { errors++; lastError = e.error; }
    }
    badge.textContent = `${hidden ? "Original" : "Traduit"} · ${done}/${entries.size}` + (errors ? ` · ${errors} erreur(s)` : "");
    badge.classList.toggle("wtt-badge-error", errors > 0);
    badge.title = (lastError ? lastError + "\n" : "") + "Clic ou Alt+T : afficher / masquer les traductions";
  }
})();
