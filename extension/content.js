// content.js — injecté sur comic.naver.com
// Repère les images de l'épisode, les fait traduire par le serveur local (via
// background.js) et superpose les bulles traduites à l'emplacement du texte d'origine.
// Chargé après naver.js (structure des pages de Naver), render.js (dessin des bulles) et
// bubbles.js (actions sur une bulle) : voir manifest.json.

(() => {
  "use strict";

  const { naver, render: view, bubbleActions } = globalThis.phy;

  // Nombre maximal de requêtes en cours. Le navigateur n'ouvre que 6 connexions par
  // serveur : au-delà, une image devenue visible attendrait derrière le préchargement
  // sans même atteindre le serveur (qui, lui, sait faire passer « visible » devant).
  const MAX_IN_FLIGHT = 3;
  const HEALTH_RETRY_MS = 5000; // nouvel essai quand le serveur est arrêté
  // Délai dépassé (serveur lent, pas arrêté) : l'image repart en file au plus 2 fois.
  const MAX_TIMEOUT_RETRIES = 2;
  // Mode « episode » : l'épisode suivant est préchargé une fois l'épisode courant traduit
  // et la lecture passée à 80 % de ses images.
  const NEXT_EPISODE_PROGRESS = 0.8;
  const NOTICE_MS = 4000; // durée d'un message temporaire du badge
  // Échecs qui suspendent la file (serveur arrêté, jeton refusé, service pas prêt) : la
  // demande est refaite à la reprise.
  const SUSPENDING = ["down", "token", "busy"];
  const HINT = "Clic ou Alt+T : afficher / masquer les traductions\n" +
    "Maj+clic sur une bulle : la retraduire\n" +
    "Ctrl+clic sur une bulle : la corriger (Entrée valide, Échap annule, vide : efface)\n" +
    "Maj+Alt+T : retraduire l'image la plus visible\n" +
    "Alt+clic sur une bulle : la signaler (ou annuler)";

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
  let translateMode = DEFAULTS.translateMode;
  let lookahead = DEFAULTS.lookahead;
  let lastHref = location.href;
  // Épisode du HTML chargé par le navigateur : le content script n'est injecté qu'au
  // chargement d'une page, pas lors d'une navigation sans rechargement.
  const loadedEpisodeId = naver.currentEpisode()?.episodeId;
  // Préchargement de l'épisode suivant : null (pas encore demandé), puis
  // { state: "loading" | "none" | "ready", episodeId, urls, pending, done, failed }.
  let upcoming = null;
  let currentDoc = null; // promesse du document de l'épisode affiché (voir currentEpisodeDoc)
  let notice = null; // { text, state } : message temporaire du badge (signalement)
  let noticeTimer = 0;
  // Résumé « Précédemment… » (jalon 15) : encart affiché, état de la demande du résumé de
  // l'épisode précédent (null, "loading" ou "retry" : serveur arrêté, redemandé à la reprise
  // de la file) et résumé de l'épisode courant déjà demandé d'avance.
  let summaryEnabled = DEFAULTS.summary;
  let previously = null;
  let previouslyState = null;
  let summaryRequested = false;

  const bubbles = bubbleActions({
    ask,
    requestFor: (e) => imageRequest(orderUrl, e.index, episode.episodeId, order.length),
    showNotice,
    render,
    generation: () => generation,
    order: () => order
  });

  // --- Badge d'état ---
  const badge = document.createElement("div");
  badge.className = "phy-badge";
  badge.addEventListener("click", toggleHidden);

  settings().then((s) => {
    enabled = s.enabled;
    targetLang = s.targetLang;
    translateMode = s.translateMode;
    lookahead = clampLookahead(s.lookahead);
    summaryEnabled = s.summary;
    reset();
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.enabled) enabled = changes.enabled.newValue;
    if (changes.targetLang) targetLang = changes.targetLang.newValue;
    if (changes.translateMode) translateMode = changes.translateMode.newValue ?? DEFAULTS.translateMode;
    if (changes.lookahead) lookahead = clampLookahead(changes.lookahead.newValue);
    if (changes.summary) {
      summaryEnabled = changes.summary.newValue ?? DEFAULTS.summary;
      if (summaryEnabled) loadPreviously();
      else removePreviously();
    }
    // Serveur, jeton, langue, modèle, activation : on retraduit la page.
    if (Object.keys(DEFAULTS).some((k) => k in changes && !LIVE_SETTINGS.includes(k))) reset();
    // Mode et avance : la fenêtre de lecture change, la file repart (en passant au mode
    // « episode », le préchargement du reste de l'épisode reprend).
    else if (LIVE_SETTINGS.some((k) => k in changes)) pump();
  });

  // Glossaire de la série modifié (page de glossaire, via background.js) : le serveur a
  // effacé les traductions de la série, on retraduit l'épisode avec le nouveau glossaire.
  // État de l'épisode pour le popup (bouton « Exporter en PDF », grisé tant que la
  // traduction n'est pas finie).
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "episode-status") return false;
    sendResponse(episode && enabled ? {
      seriesId: episode.seriesId,
      episodeId: episode.episodeId,
      total: order.length,
      done: order.filter((e) => e.status === "done").length,
      finished: episodeFinished()
    } : null);
    return false;
  });

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "glossary-changed") return false;
    const mine = !!episode && msg.seriesId === episode.seriesId;
    if (mine) reset();
    // Réponse explicite : sans elle, la promesse de l'expéditeur est rejetée.
    sendResponse({ reset: mine });
    return false;
  });

  document.addEventListener("keydown", (e) => {
    if (e.altKey && e.code === "KeyT" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) &&
        !e.target.isContentEditable) {
      e.preventDefault();
      if (e.shiftKey) bubbles.retranslateImage();
      else toggleHidden();
    }
  });

  // --- Épisode et images ---

  function scan() {
    if (!enabled || !episode) return;
    const list = naver.episodeImages(episode).map(({ url, img }) => track(url, img));
    if (!list.length) return;
    list.forEach((e, i) => (e.index = i));
    order = list;
    updateBadge();
    pump();
  }

  // Entrée : { url, img, index, status: "queued" | "working" | "done" | "error",
  //            visible, sentVisible, timeouts, blocks, imageSize, layer, error }
  // `visible` vaut null tant que l'IntersectionObserver n'a pas répondu pour l'image.
  function track(url, img) {
    let e = entries.get(url);
    if (!e) {
      e = { url, img, index: 0, status: "queued", visible: null, sentVisible: false, timeouts: 0 };
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
      const e = entries.get(naver.imageUrl(ent.target));
      if (!e || e.img !== ent.target) continue;
      e.visible = ent.isIntersecting;
      if (e.visible && e.status === "working" && !e.sentVisible) send(e, "visible", false);
    }
    pump();
  });

  // --- File d'attente ---

  // Fenêtre de lecture du mode « scroll » : { from, to } (index inclus), ou null en mode
  // « episode » (pas de limite). De la première image visible à la dernière visible +
  // `lookahead`. Sans image visible (en-tête de la page encore à l'écran), les `lookahead`
  // premières images situées sous l'écran ; si toutes sont au-dessus (commentaires), aucune.
  function readingWindow() {
    if (translateMode !== "scroll") return null;
    const visible = order.filter((e) => e.visible);
    if (visible.length) {
      return { from: visible[0].index, to: visible[visible.length - 1].index + lookahead };
    }
    const below = order.find((e) => e.img.isConnected && e.img.getBoundingClientRect().top >= 0);
    return below ? { from: below.index, to: below.index + lookahead - 1 } : { from: 0, to: -1 };
  }

  const inWindow = (e, win) => !win || (e.index >= win.from && e.index <= win.to);

  function nextEntry() {
    // Une image dont on ne sait pas encore si elle est visible attend le premier rappel de
    // l'IntersectionObserver : sinon, à l'ouverture, l'image visible partait en
    // « prefetch » puis devait être renvoyée en « visible ».
    if (order.some((e) => e.visible === null)) return null;
    // Mode « scroll » : les images hors de la fenêtre attendent le défilement (celles déjà
    // dépassées ne repartent que si elles redeviennent visibles).
    const win = readingWindow();
    const queued = order.filter((e) => e.status === "queued" && inWindow(e, win));
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
      if (e) send(e, e.visible ? "visible" : "prefetch", true);
      else if (!sendUpcoming()) break;
    }
    maybeLoadUpcoming();
    maybeSummarize();
    updateBadge();
  }

  // Message au script d'arrière-plan, avec un résultat normalisé comme ceux d'api.js :
  // rejet (extension rechargée entre-temps) → "down", réponse vide → "error".
  async function ask(msg) {
    try {
      return (await chrome.runtime.sendMessage(msg)) ??
        { ok: false, kind: "error", error: "Réponse vide" };
    } catch (err) {
      return { ok: false, kind: "down", error: "Extension indisponible : " + (err?.message || err) };
    }
  }

  // Image `i` d'un épisode et ses voisines (`urlAt(j)` : URL de l'image j). Les voisines
  // entrent dans la clé du cache OCR : traduction, préchargement et signalement doivent
  // envoyer les mêmes. `count` : nombre d'images de l'épisode, retenu par le serveur
  // (épisode lu en partie pour le résumé, export PDF sans relire la page de Naver).
  function imageRequest(urlAt, i, episodeId, count) {
    return {
      image_url: urlAt(i),
      series_id: episode.seriesId,
      episode_id: episodeId,
      index: i,
      prev_image_url: urlAt(i - 1),
      next_image_url: urlAt(i + 1),
      image_count: count
    };
  }

  const orderUrl = (j) => order[j]?.url;

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
    const request = { ...imageRequest(orderUrl, e.index, episode.episodeId, order.length), priority };
    const res = await ask({ type: "translate", request });
    if (gen !== generation) return; // page remise à zéro entre-temps
    if (counted) inFlight--;
    handle(e, res, counted);
    pump();
  }

  function handle(e, res, counted) {
    if (e.status === "done") return; // l'autre requête (visible / prefetch) a déjà répondu
    if (res.ok) {
      e.status = "done";
      e.blocks = res.data.blocks;
      e.imageSize = res.data.image_size;
      render(e);
      return;
    }
    if (res.kind === "timeout") {
      // Le renvoi « visible » a expiré alors que la requête de la file court encore :
      // c'est elle qui conclura.
      if (!counted && e.status === "working") return;
      // Le serveur continue la tâche et la met en cache : une nouvelle demande est
      // fusionnée avec elle, ou servie par le cache. Au-delà de la limite : erreur.
      if (++e.timeouts <= MAX_TIMEOUT_RETRIES) {
        e.status = "queued";
        e.sentVisible = false;
        console.info("[Phylactère] Délai dépassé, image remise en file :", e.url);
        return;
      }
      res = { ...res, error: `${res.error} (${e.timeouts} essais)` };
    }
    if (res.kind === "error" || res.kind === "timeout") {
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
    if (previouslyState === "retry") loadPreviously();
    pump();
  }

  async function checkHealth() {
    const gen = generation;
    const res = await ask({ type: "health" });
    if (gen !== generation || pause?.kind !== "down") return;
    if (res.ok) resume();
    else pauseTimer = setTimeout(checkHealth, HEALTH_RETRY_MS);
  }

  // --- Préchargement de l'épisode suivant (mode « episode ») ---

  // Épisode courant terminé : plus aucune image en file ni en cours (une image en erreur
  // ne bloque pas le préchargement).
  function episodeFinished() {
    return order.length > 0 && order.every((e) => e.status === "done" || e.status === "error");
  }

  // Part de l'épisode atteinte : images dont le haut est passé sous le bas de l'écran.
  function readingProgress() {
    const reached = order.filter((e) => e.img.isConnected &&
      e.img.getBoundingClientRect().top < innerHeight).length;
    return reached / order.length;
  }

  function maybeLoadUpcoming() {
    if (upcoming || translateMode !== "episode" || !episode || pause) return;
    if (!episodeFinished() || readingProgress() < NEXT_EPISODE_PROGRESS) return;
    upcoming = { state: "loading" };
    loadUpcoming();
  }

  // Document de l'épisode affiché. Après une navigation sans rechargement, les scripts de
  // la page sont ceux du premier épisode chargé : le script d'état est alors relu dans le
  // HTML de l'épisode affiché.
  // Téléchargé une seule fois par épisode (résumé, puis préchargement du suivant).
  function currentEpisodeDoc() {
    if (episode.episodeId === loadedEpisodeId) return Promise.resolve(document);
    currentDoc ??= naver.fetchEpisodePage(episode.episodeId).then((page) => page.doc);
    currentDoc.catch(() => { currentDoc = null; }); // échec : nouvel essai la fois suivante
    return currentDoc;
  }

  async function loadUpcoming() {
    const gen = generation;
    let episodeId = null;
    let urls = [];
    try {
      episodeId = naver.nextEpisodeNumber(await currentEpisodeDoc(), episode.episodeId);
      if (episodeId) urls = await naver.episodeImageUrls({ seriesId: episode.seriesId, episodeId });
    } catch (err) {
      console.warn("[Phylactère] Épisode suivant illisible :", err?.message || err);
      urls = [];
    }
    if (gen !== generation) return;
    upcoming = urls.length
      ? { state: "ready", episodeId, urls, pending: urls.map((_, i) => i), done: 0, failed: 0 }
      : { state: "none" };
    pump();
  }

  // Envoie une image de l'épisode suivant en « prefetch », seulement quand l'épisode
  // courant n'a plus rien à traduire. Mêmes voisines que lorsque l'épisode sera ouvert :
  // elles entrent dans la clé du cache OCR.
  function sendUpcoming() {
    const up = upcoming;
    if (up?.state !== "ready" || !up.pending.length) return false;
    if (translateMode !== "episode" || !episodeFinished()) return false;
    const i = up.pending.shift();
    sendUpcomingImage(up, i);
    return true;
  }

  async function sendUpcomingImage(up, i) {
    const gen = generation;
    inFlight++;
    const request = { ...imageRequest((j) => up.urls[j], i, up.episodeId, up.urls.length), priority: "prefetch" };
    const res = await ask({ type: "translate", request });
    if (gen !== generation) return;
    inFlight--;
    if (res.ok) {
      up.done++;
    } else if (SUSPENDING.includes(res.kind)) {
      up.pending.unshift(i); // reprise avec la file
      suspend(res);
    } else {
      // Erreur ou délai dépassé : l'image sera traduite à l'ouverture de l'épisode.
      up.failed++;
    }
    pump();
  }

  // --- Résumé « Précédemment… » (jalon 15) ---

  // À l'ouverture d'un épisode : résumé de l'épisode précédent, en priorité « prefetch ».
  // Le plus souvent, il a été calculé d'avance à la fin de sa lecture (maybeSummarize) et
  // le serveur le renvoie tout de suite. Rien en cache pour cet épisode (404) : pas d'encart.
  async function loadPreviously() {
    if (!enabled || !summaryEnabled || !episode || previously || previouslyState === "loading") return;
    const gen = generation;
    previouslyState = "loading";
    let episodeId = null;
    try {
      episodeId = naver.prevEpisodeNumber(await currentEpisodeDoc(), episode.episodeId);
    } catch (err) {
      console.warn("[Phylactère] Épisode précédent introuvable :", err?.message || err);
    }
    if (gen !== generation) return;
    if (!episodeId) {
      previouslyState = null;
      return;
    }
    // Le serveur connaît le nombre d'images de l'épisode précédent (envoyé avec ses images) :
    // il en déduit s'il a été lu en entier.
    const request = { series_id: episode.seriesId, episode_id: episodeId, priority: "prefetch" };
    let res;
    // Délai dépassé : le serveur continue, une nouvelle demande rejoint sa tâche.
    for (let attempt = 0; attempt <= MAX_TIMEOUT_RETRIES; attempt++) {
      res = await ask({ type: "summary", request });
      if (gen !== generation) return;
      if (res.kind !== "timeout") break;
    }
    if (res.ok) {
      previouslyState = null;
      if (summaryEnabled) showPreviously(res.data);
      return;
    }
    // Serveur arrêté ou pas prêt : redemandé quand la file reprend.
    previouslyState = SUSPENDING.includes(res.kind) ? "retry" : null;
    console.info("[Phylactère] Pas de résumé de l'épisode précédent :", res.error);
  }

  // Encart repliable au-dessus des images de l'épisode, dans le flux de la page.
  function showPreviously(data) {
    removePreviously();
    const anchor = naver.viewer() || order[0]?.img;
    if (!anchor?.parentNode) return;
    const box = view.buildPreviously(data, targetLang, removePreviously);
    anchor.parentNode.insertBefore(box, anchor);
    previously = box;
  }

  function removePreviously() {
    previously?.remove();
    previously = null;
  }

  // Fin de lecture de l'épisode (même déclencheur que le préchargement de l'épisode
  // suivant, dans les deux modes) : son résumé est demandé d'avance, en priorité
  // « background », pour être prêt à l'ouverture du suivant. La réponse est ignorée.
  function maybeSummarize() {
    if (summaryRequested || !summaryEnabled || !episode || pause) return;
    if (!episodeFinished() || readingProgress() < NEXT_EPISODE_PROGRESS) return;
    summaryRequested = true;
    const request = {
      series_id: episode.seriesId, episode_id: episode.episodeId, priority: "background"
    };
    ask({ type: "summary", request }).then((res) => {
      if (!res.ok && res.kind !== "timeout") {
        console.info("[Phylactère] Résumé de l'épisode impossible :", res.error);
      }
    });
  }

  // --- Message temporaire du badge ---

  function showNotice(text, state) {
    notice = { text, state };
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice = null; updateBadge(); }, NOTICE_MS);
    updateBadge();
  }

  // --- Rendu des bulles ---

  // Calque de l'entrée `e`, redessiné (nouvelle traduction, image retraduite).
  function render(e) {
    e.layer?.remove();
    const layer = view.buildLayer(e, {
      hidden,
      lang: targetLang,
      onClick: (ev, i, el) => bubbles.onClick(e, i, el, ev)
    });
    document.body.appendChild(layer);
    e.layer = layer;
    e.lastW = undefined;
    e.rect = null;
    if (view.position(e, e.img.getBoundingClientRect())) view.fitText(e);
  }

  let rafPending = false;
  function scheduleReposition() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      // Toutes les lectures d'abord, puis toutes les écritures.
      const layers = [...entries.values()].filter((e) => e.layer);
      const rects = layers.map((e) => e.img.isConnected ? e.img.getBoundingClientRect() : null);
      const resized = layers.filter((e, i) => view.position(e, rects[i]));
      if (resized.length) view.fitText(...resized);
    });
  }
  addEventListener("resize", scheduleReposition);
  new ResizeObserver(scheduleReposition).observe(document.body);
  // Une image qui finit de charger change de hauteur : on recale les calques. Son
  // currentSrc change aussi (sans mutation d'attribut) : quand la page réutilise ses <img>
  // pour un autre épisode, currentSrc garde l'ancienne URL jusqu'au chargement de la nouvelle.
  document.addEventListener("load", (ev) => {
    if (ev.target.tagName !== "IMG") return;
    scheduleReposition();
    scheduleScan();
  }, true);

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
      const next = naver.currentEpisode();
      if (next?.seriesId !== episode?.seriesId || next?.episodeId !== episode?.episodeId) {
        reset();
        return;
      }
    }
    // Nos propres éléments (badge, calques) ne relancent rien : sans ce filtre, la mise à
    // jour du badge déclenchait un nouveau scan, qui remettait le badge à jour, etc.
    const external = muts.filter((m) => !isOurs(m.target) &&
      !(m.addedNodes.length && [...m.addedNodes].every(isOurs)));
    if (!external.length) return;
    const addsImage = (m) => [...m.addedNodes].some((n) => n.tagName === "IMG" || n.querySelector?.("img"));
    if (external.some((m) => m.type === "attributes" || addsImage(m))) scheduleScan();
    scheduleReposition();
  }).observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset", "data-src"]
  });

  // --- Utilitaires ---

  function isOurs(node) {
    return node === badge || !!node.closest?.(".phy-layer, .phy-previously");
  }

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
    upcoming = null;
    currentDoc = null;
    removePreviously();
    previouslyState = null;
    summaryRequested = false;
    episode = naver.currentEpisode();
    updateBadge();
    scan();
    loadPreviously();
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
    const win = readingWindow();
    let done = 0, errors = 0, lastError = "", working = 0, waiting = 0;
    for (const e of order) {
      if (e.status === "done") done++;
      if (e.status === "error") { errors++; lastError = e.error; }
      if (e.status === "working" || (e.status === "queued" && inWindow(e, win))) working++;
      if (e.status === "queued" && !inWindow(e, win)) waiting++;
    }
    let text = `${hidden ? "Original" : "Traduit"} · ${done}/${order.length}`;
    let title = HINT;
    let state = "";
    // Mode « scroll » : une image non traduite plus bas n'est pas une panne.
    const up = upcoming?.state === "ready" ? upcoming : null;
    if (working) {
      text += " · en cours";
    } else if (up && translateMode === "episode" && up.done + up.failed < up.urls.length) {
      text += ` · épisode suivant ${up.done}/${up.urls.length}`;
      title = `Préchargement de l'épisode ${up.episodeId} pendant la lecture.\n${HINT}`;
    } else if (waiting) {
      text += " · en attente du défilement";
      title = `Mode défilement : ${waiting} image(s) seront traduites à l'approche ` +
        `(${lookahead} d'avance).\n${HINT}`;
    }
    if (pause?.kind === "down") {
      text = "Serveur arrêté";
      title = `${pause.error}\nNouvel essai toutes les ${HEALTH_RETRY_MS / 1000} s.\n${HINT}`;
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
    if (notice && !pause) {
      text += " · " + notice.text;
      state = notice.state || state;
    }
    // N'écrire que ce qui change : chaque écriture est une mutation du DOM.
    text = "Phylactère · " + text;
    if (badge.textContent !== text) badge.textContent = text;
    if (badge.title !== title) badge.title = title;
    if (badge.dataset.state !== state) badge.dataset.state = state;
  }
})();
