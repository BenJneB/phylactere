// content.js — injecté sur comic.naver.com
// Repère les images de l'épisode, les fait traduire par le serveur local (via
// background.js) et superpose les bulles traduites à l'emplacement du texte d'origine.

(() => {
  "use strict";

  // --- Réglages (à ajuster si Naver change son interface) ---
  const IMG_HOST = "image-comic.pstatic.net";
  // Conteneur des images de l'épisode (<img id="content_image_N">). Ailleurs dans la page,
  // la vignette de l'épisode a une URL du même dossier : elle ne doit pas être traduite.
  const VIEWER = "#sectionContWide";
  // Nombre maximal de requêtes en cours. Le navigateur n'ouvre que 6 connexions par
  // serveur : au-delà, une image devenue visible attendrait derrière le préchargement
  // sans même atteindre le serveur (qui, lui, sait faire passer « visible » devant).
  const MAX_IN_FLIGHT = 3;
  const HEALTH_RETRY_MS = 5000; // nouvel essai quand le serveur est arrêté
  const PAD = 6; // marge autour du texte détecté (px de l'image d'origine)
  const HINT = "Clic ou Alt+T : afficher / masquer les traductions";

  const entries = new Map(); // url -> entrée (voir track())
  let order = []; // entrées dans l'ordre de l'épisode
  let episode = null; // { seriesId, episodeId }
  let generation = 0; // incrémenté à chaque remise à zéro : ignore les réponses obsolètes
  let inFlight = 0;
  let pause = null; // { kind: "down" | "token" | "busy", error } : file suspendue
  let pauseTimer = 0;
  let enabled = true;
  let hidden = false;
  let targetLang = "fr";
  let lastHref = location.href;

  // --- Badge d'état ---
  const badge = document.createElement("div");
  badge.className = "phy-badge";
  badge.addEventListener("click", toggleHidden);

  chrome.storage.local.get({ enabled: true, targetLang: "fr" }).then((s) => {
    enabled = s.enabled;
    targetLang = s.targetLang;
    reset();
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.enabled) enabled = changes.enabled.newValue;
    if (changes.targetLang) targetLang = changes.targetLang.newValue;
    // Tout réglage utile (serveur, jeton, langue, modèle) relance la traduction de la page.
    if (["enabled", "targetLang", "model", "serverUrl", "token"].some((k) => k in changes)) reset();
  });

  document.addEventListener("keydown", (e) => {
    if (e.altKey && e.code === "KeyT" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) {
      e.preventDefault();
      toggleHidden();
    }
  });

  // --- Épisode et images ---

  // Page de lecture : /webtoon/detail?titleId=…&no=… (ou /bestChallenge/detail, etc.)
  function currentEpisode() {
    if (!/\/detail/.test(location.pathname)) return null;
    const q = new URLSearchParams(location.search);
    const seriesId = q.get("titleId"), episodeId = q.get("no");
    return seriesId && episodeId ? { seriesId, episodeId } : null;
  }

  function imageUrl(img) {
    return img.currentSrc || img.src || img.dataset.src || "";
  }

  // Les images de l'épisode sont reconnues à leur URL : …/webtoon/<titleId>/<no>/…
  // (pas à leur taille : on peut ainsi les envoyer au serveur avant qu'elles chargent).
  function isEpisodeImage(url) {
    if (!episode) return false;
    try {
      const u = new URL(url);
      return u.protocol === "https:" && u.hostname === IMG_HOST &&
        u.pathname.includes(`/${episode.seriesId}/${episode.episodeId}/`) &&
        !/\/thumbnail[^/]*$/.test(u.pathname);
    } catch {
      return false;
    }
  }

  function scan() {
    if (!enabled || !episode) return;
    const seen = new Set();
    const list = [];
    // Repli sur toute la page si Naver renomme le conteneur.
    const root = document.querySelector(VIEWER) || document;
    for (const img of root.querySelectorAll("img")) {
      const url = imageUrl(img);
      if (seen.has(url) || !isEpisodeImage(url)) continue;
      seen.add(url);
      list.push(track(url, img));
    }
    if (!list.length) return;
    list.forEach((e, i) => (e.index = i));
    order = list;
    updateBadge();
    pump();
  }

  // Entrée : { url, img, index, status: "queued" | "working" | "done" | "error",
  //            visible, sentVisible, blocks, imageSize, layer, error }
  function track(url, img) {
    let e = entries.get(url);
    if (!e) {
      e = { url, img, index: 0, status: "queued", visible: false, sentVisible: false };
      entries.set(url, e);
    } else if (e.img !== img) {
      visibility.unobserve(e.img);
      e.img = img;
      e.lastW = undefined;
    }
    visibility.observe(img);
    return e;
  }

  // Images dans le viewport : traduites en priorité (« visible »), les autres en
  // préchargement (« prefetch »).
  const visibility = new IntersectionObserver((list) => {
    for (const ent of list) {
      const e = entries.get(imageUrl(ent.target));
      if (!e || e.img !== ent.target) continue;
      e.visible = ent.isIntersecting;
      if (e.visible && e.status === "working" && !e.sentVisible) send(e, "visible", false);
    }
    pump();
  });

  // --- File d'attente ---

  function nextEntry() {
    const queued = order.filter((e) => e.status === "queued");
    if (!queued.length) return null;
    const visible = queued.find((e) => e.visible);
    if (visible) return visible;
    // Préchargement : d'abord la suite de l'épisode à partir de ce qu'on lit, puis le début.
    const anchor = order.find((e) => e.visible)?.index ?? 0;
    return queued.find((e) => e.index >= anchor) || queued[0];
  }

  function pump() {
    while (!pause && inFlight < MAX_IN_FLIGHT) {
      const e = nextEntry();
      if (!e) break;
      send(e, e.visible ? "visible" : "prefetch", true);
    }
    updateBadge();
  }

  // `counted` : requête qui occupe une place de la file. Une image déjà envoyée en
  // préchargement qui devient visible est renvoyée en « visible » (hors quota) : le serveur
  // fusionne les deux requêtes et fait passer la tâche devant.
  async function send(e, priority, counted) {
    const gen = generation;
    if (counted) {
      inFlight++;
      e.status = "working";
    }
    if (priority === "visible") e.sentVisible = true;
    const request = {
      image_url: e.url,
      series_id: episode.seriesId,
      episode_id: episode.episodeId,
      index: e.index,
      priority,
      prev_image_url: order[e.index - 1]?.url,
      next_image_url: order[e.index + 1]?.url
    };
    let res;
    try {
      res = await chrome.runtime.sendMessage({ type: "translate", request });
    } catch (err) {
      res = { ok: false, kind: "down", error: "Extension indisponible : " + (err?.message || err) };
    }
    if (gen !== generation) return; // page remise à zéro entre-temps
    if (counted) inFlight--;
    handle(e, res || { ok: false, kind: "error", error: "Réponse vide" });
    pump();
  }

  function handle(e, res) {
    if (e.status === "done") return; // l'autre requête (visible / prefetch) a déjà répondu
    if (res.ok) {
      e.status = "done";
      e.blocks = res.data.blocks;
      e.imageSize = res.data.image_size;
      render(e);
      return;
    }
    if (res.kind === "error") {
      e.status = "error";
      e.error = res.error;
      console.warn("[Phylactère]", res.error, e.url);
      return;
    }
    // Serveur arrêté, jeton refusé ou service pas prêt : l'image reste en file
    // et toute la file est suspendue.
    e.status = "queued";
    e.sentVisible = false;
    suspend(res);
  }

  function suspend(res) {
    pause = { kind: res.kind, error: res.error };
    clearTimeout(pauseTimer);
    if (res.kind === "down") pauseTimer = setTimeout(checkHealth, HEALTH_RETRY_MS);
    if (res.kind === "busy") pauseTimer = setTimeout(resume, (res.retryAfter || 5) * 1000);
    // "token" : on attend que le jeton change dans le popup (storage.onChanged → reset).
    updateBadge();
  }

  function resume() {
    pause = null;
    pump();
  }

  async function checkHealth() {
    const gen = generation;
    let res;
    try {
      res = await chrome.runtime.sendMessage({ type: "health" });
    } catch {
      res = null;
    }
    if (gen !== generation || pause?.kind !== "down") return;
    if (res?.ok) resume();
    else pauseTimer = setTimeout(checkHealth, HEALTH_RETRY_MS);
  }

  // --- Rendu des bulles ---

  function render(e) {
    e.layer?.remove();
    const { w: nw, h: nh } = e.imageSize;
    const layer = document.createElement("div");
    layer.className = "phy-layer" + (hidden ? " phy-hidden" : "");

    for (const b of e.blocks) {
      // Une bulle coupée entre deux images peut déborder en bas (y + h > hauteur).
      const x = Math.max(0, b.box.x - PAD);
      const y = Math.max(0, b.box.y - PAD);
      const w = Math.min(nw - x, b.box.w + PAD * 2);
      const h = b.box.h + PAD * 2;
      const el = document.createElement("div");
      el.className = "phy-bubble";
      el.lang = targetLang;
      el.textContent = b.translated;
      el.title = b.original;
      el.dataset.h = h;
      el.style.left = (x / nw) * 100 + "%";
      el.style.top = (y / nh) * 100 + "%";
      el.style.width = (w / nw) * 100 + "%";
      el.style.minHeight = (h / nh) * 100 + "%";
      // Clic : masque la bulle pour voir l'original (re-clic pour la remettre).
      el.addEventListener("click", (ev) => { ev.stopPropagation(); el.classList.toggle("phy-peek"); });
      layer.appendChild(el);
    }
    document.body.appendChild(layer);
    e.layer = layer;
    e.lastW = undefined;
    position(e);
  }

  // Calque en position absolue dans la page, superposé à l'image.
  function position(e) {
    if (!e.layer) return;
    const r = e.img.getBoundingClientRect();
    if (!r.width || !r.height) {
      e.layer.style.display = "none";
      return;
    }
    Object.assign(e.layer.style, {
      display: "",
      left: r.left + scrollX + "px",
      top: r.top + scrollY + "px",
      width: r.width + "px",
      height: r.height + "px"
    });
    if (e.lastW !== r.width) {
      e.lastW = r.width;
      fitText(e);
    }
  }

  // Réduit la police jusqu'à ce que la traduction tienne dans la zone d'origine.
  function fitText(e) {
    const scale = e.layer.clientWidth / e.imageSize.w;
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
      for (const e of entries.values()) {
        if (e.img.isConnected) position(e);
        else if (e.layer) e.layer.style.display = "none";
      }
    });
  }
  addEventListener("resize", scheduleReposition);
  new ResizeObserver(scheduleReposition).observe(document.body);
  // Une image qui finit de charger change de hauteur : on recale les calques.
  document.addEventListener("load", (ev) => { if (ev.target.tagName === "IMG") scheduleReposition(); }, true);

  // --- Navigation et changements du DOM ---

  let scanPending = false;
  function scheduleScan() {
    if (scanPending) return;
    scanPending = true;
    setTimeout(() => { scanPending = false; scan(); }, 200);
  }

  new MutationObserver((muts) => {
    if (location.href !== lastHref) { // navigation sans rechargement (SPA)
      lastHref = location.href;
      const next = currentEpisode();
      if (next?.seriesId !== episode?.seriesId || next?.episodeId !== episode?.episodeId) {
        reset();
        return;
      }
    }
    if (muts.some((m) => m.type === "attributes" || m.addedNodes.length)) scheduleScan();
    scheduleReposition();
  }).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset", "data-src"]
  });

  // --- Utilitaires ---

  // Repart de zéro : autre épisode, réglages modifiés, extension (dés)activée.
  function reset() {
    generation++;
    inFlight = 0;
    pause = null;
    clearTimeout(pauseTimer);
    visibility.disconnect();
    for (const e of entries.values()) e.layer?.remove();
    entries.clear();
    order = [];
    episode = currentEpisode();
    updateBadge();
    scan();
  }

  function toggleHidden() {
    hidden = !hidden;
    document.querySelectorAll(".phy-layer").forEach((l) => l.classList.toggle("phy-hidden", hidden));
    updateBadge();
  }

  function updateBadge() {
    if (!enabled || !episode || !order.length) {
      badge.remove();
      return;
    }
    if (!badge.isConnected) document.body.appendChild(badge);
    let done = 0, errors = 0, lastError = "";
    for (const e of order) {
      if (e.status === "done") done++;
      if (e.status === "error") { errors++; lastError = e.error; }
    }
    let text = `${hidden ? "Original" : "Traduit"} · ${done}/${order.length}`;
    let title = HINT;
    let state = "";
    if (pause?.kind === "down") {
      text = "Serveur arrêté";
      title = `${pause.error}\nNouvel essai toutes les 5 s.\n${HINT}`;
      state = "error";
    } else if (pause?.kind === "token") {
      text = "Jeton à saisir dans le popup";
      title = `${pause.error}\n${HINT}`;
      state = "error";
    } else if (pause?.kind === "busy") {
      text += " · en attente du serveur";
      title = `${pause.error}\n${HINT}`;
      state = "wait";
    } else if (errors) {
      text += ` · ${errors} erreur(s)`;
      title = `${lastError}\n${HINT}`;
      state = "error";
    }
    badge.textContent = "Phylactère · " + text;
    badge.title = title;
    badge.dataset.state = state;
  }
})();
