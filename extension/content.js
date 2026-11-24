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
  // Délai dépassé (serveur lent, pas arrêté) : l'image repart en file au plus 2 fois.
  const MAX_TIMEOUT_RETRIES = 2;
  // Mode « episode » : l'épisode suivant est préchargé une fois l'épisode courant traduit
  // et la lecture passée à 80 % de ses images.
  const NEXT_EPISODE_PROGRESS = 0.8;
  const NOTICE_MS = 4000; // durée d'un message temporaire du badge
  const HINT = "Clic ou Alt+T : afficher / masquer les traductions\nAlt+clic sur une bulle : la signaler (ou annuler)";

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
  const loadedEpisodeId = currentEpisode()?.episodeId;
  // Préchargement de l'épisode suivant : null (pas encore demandé), puis
  // { state: "loading" | "none" | "ready", episodeId, urls, pending, done, failed }.
  let upcoming = null;
  let notice = null; // { text, state } : message temporaire du badge (signalement)
  let noticeTimer = 0;

  // --- Badge d'état ---
  const badge = document.createElement("div");
  badge.className = "phy-badge";
  badge.addEventListener("click", toggleHidden);

  settings().then((s) => {
    enabled = s.enabled;
    targetLang = s.targetLang;
    translateMode = s.translateMode;
    lookahead = clampLookahead(s.lookahead);
    reset();
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.enabled) enabled = changes.enabled.newValue;
    if (changes.targetLang) targetLang = changes.targetLang.newValue;
    if (changes.translateMode) translateMode = changes.translateMode.newValue ?? DEFAULTS.translateMode;
    if (changes.lookahead) lookahead = clampLookahead(changes.lookahead.newValue);
    // Serveur, jeton, langue, modèle, activation : on retraduit la page.
    if (Object.keys(DEFAULTS).some((k) => k in changes && !LIVE_SETTINGS.includes(k))) reset();
    // Mode et avance : la fenêtre de lecture change, la file repart (en passant au mode
    // « episode », le préchargement du reste de l'épisode reprend).
    else if (LIVE_SETTINGS.some((k) => k in changes)) pump();
  });

  // Glossaire de la série modifié (page de glossaire, via background.js) : le serveur a
  // effacé les traductions de la série, on retraduit l'épisode avec le nouveau glossaire.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "glossary-changed") return false;
    const mine = !!episode && msg.seriesId === episode.seriesId;
    if (mine) reset();
    // Réponse explicite : sans elle, la promesse de l'expéditeur est rejetée.
    sendResponse({ reset: mine });
    return false;
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
  function isEpisodeImage(url, ep = episode) {
    if (!ep) return false;
    try {
      const u = new URL(url);
      return u.protocol === "https:" && u.hostname === IMG_HOST &&
        u.pathname.includes(`/${ep.seriesId}/${ep.episodeId}/`) &&
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
      const e = entries.get(imageUrl(ent.target));
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
  // envoyer les mêmes.
  function imageRequest(urlAt, i, episodeId) {
    return {
      image_url: urlAt(i),
      series_id: episode.seriesId,
      episode_id: episodeId,
      index: i,
      prev_image_url: urlAt(i - 1),
      next_image_url: urlAt(i + 1)
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
    const request = { ...imageRequest(orderUrl, e.index, episode.episodeId), priority };
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

  // Numéro de l'épisode suivant, lu dans le script d'état que Naver met dans la page
  // (window.__state__ = { …, nextArticle: { no: '67', charge: false, … } }). Le content
  // script ne voit pas les variables de la page, mais il lit le texte de ses scripts.
  // Épisode payant (charge: true) ou absent (dernier épisode) : pas de préchargement.
  function nextEpisodeNumber(doc) {
    for (const script of doc.querySelectorAll("script:not([src])")) {
      const block = /nextArticle\s*:\s*\{([^}]*)\}/.exec(script.textContent)?.[1];
      if (block === undefined) continue;
      const no = /\bno\s*:\s*['"]?(\d+)/.exec(block)?.[1];
      if (!no || no === episode.episodeId || /\bcharge\s*:\s*true/.test(block)) return null;
      return no;
    }
    return null;
  }

  // Page d'un épisode de la série, téléchargée sans l'ouvrir. Sans les cookies de
  // l'utilisateur : Naver pourrait sinon compter l'épisode comme lu (historique, reprise de
  // la lecture). Contrepartie : un épisode réservé aux comptes connectés n'est pas préchargé.
  async function fetchEpisodePage(episodeId) {
    const url = new URL(location.href);
    url.searchParams.set("no", episodeId);
    url.hash = "";
    const res = await fetch(url, { credentials: "omit" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return { url, doc: new DOMParser().parseFromString(await res.text(), "text/html") };
  }

  // URL d'image telle que scan() la verra à l'ouverture de l'épisode (URL absolue, image
  // réelle plutôt qu'image d'attente) : les voisines entrent dans la clé du cache OCR.
  function pageImageUrl(img, base, ep) {
    for (const raw of [img.getAttribute("src"), img.dataset.src]) {
      if (!raw) continue;
      try {
        const url = new URL(raw, base).href;
        if (isEpisodeImage(url, ep)) return url;
      } catch {
        // URL invalide : candidate suivante
      }
    }
    return null;
  }

  // Les URL des images d'un épisode sont dans le HTML de sa page (rendu par le serveur de
  // Naver, #sectionContWide) : on applique la même sélection que scan().
  async function loadUpcoming() {
    const gen = generation;
    let episodeId = null;
    let urls = [];
    try {
      // Après une navigation sans rechargement, les scripts de la page sont ceux du premier
      // épisode chargé : nextArticle est alors relu dans le HTML de l'épisode affiché.
      const current = episode.episodeId === loadedEpisodeId
        ? document
        : (await fetchEpisodePage(episode.episodeId)).doc;
      episodeId = nextEpisodeNumber(current);
      if (episodeId) {
        const { url, doc } = await fetchEpisodePage(episodeId);
        const root = doc.querySelector(VIEWER) || doc;
        const ep = { seriesId: episode.seriesId, episodeId };
        for (const img of root.querySelectorAll("img")) {
          const src = pageImageUrl(img, url, ep);
          if (src && !urls.includes(src)) urls.push(src);
        }
      }
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
    const request = { ...imageRequest((j) => up.urls[j], i, up.episodeId), priority: "prefetch" };
    const res = await ask({ type: "translate", request });
    if (gen !== generation) return;
    inFlight--;
    if (res.ok) {
      up.done++;
    } else if (["down", "token", "busy"].includes(res.kind)) {
      up.pending.unshift(i); // reprise avec la file
      suspend(res);
    } else {
      // Erreur ou délai dépassé : l'image sera traduite à l'ouverture de l'épisode.
      up.failed++;
    }
    pump();
  }

  // --- Signalement d'une bulle (Alt+clic) ---

  // Le serveur enregistre la bulle, ses lignes OCR et une découpe de l'image, pour
  // constituer un corpus d'échecs (scripts/reports.py en fait un rapport HTML).
  // Un second Alt+clic annule le signalement (tant que la page n'a pas été rechargée :
  // l'identifiant n'est gardé que sur la bulle affichée).
  function toggleReport(e, b, el) {
    if (el.dataset.report === "sending") return; // envoi ou annulation en cours
    if (el.dataset.report === "done") cancelReport(el);
    else report(e, b, el);
  }

  async function report(e, b, el) {
    el.dataset.report = "sending";
    const request = {
      ...imageRequest(orderUrl, e.index, episode.episodeId),
      box: b.box,
      original: b.original,
      translated: b.translated
    };
    const res = await ask({ type: "report", request });
    if (!el.isConnected) return; // page remise à zéro entre-temps
    if (res.ok) {
      el.dataset.report = "done";
      el.dataset.reportId = res.data.id;
      showNotice("Bulle signalée (Alt+clic pour annuler)", "");
    } else {
      delete el.dataset.report;
      console.warn("[Phylactère] Signalement impossible :", res.error);
      showNotice("Signalement impossible : " + res.error, "error");
    }
  }

  async function cancelReport(el) {
    el.dataset.report = "sending";
    const res = await ask({ type: "report-cancel", id: el.dataset.reportId });
    if (!el.isConnected) return;
    if (res.ok) {
      delete el.dataset.report;
      delete el.dataset.reportId;
      showNotice("Signalement annulé", "");
    } else {
      el.dataset.report = "done";
      console.warn("[Phylactère] Annulation impossible :", res.error);
      showNotice("Annulation impossible : " + res.error, "error");
    }
  }

  function showNotice(text, state) {
    notice = { text, state };
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice = null; updateBadge(); }, NOTICE_MS);
    updateBadge();
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
      // Fond pris sur l'image par le serveur (narration sur fond coloré) ; sans couleur
      // (fond dessiné, ou traduction en cache d'avant le jalon 9) : blanc de content.css.
      if (b.bg) {
        el.style.backgroundColor = b.bg;
        el.style.color = b.fg || "#000";
        // Ombre floue de la même couleur : adoucit le bord du rectangle sur fond uni.
        el.style.boxShadow = `0 0 6px 3px ${b.bg}`;
      }
      // Clic : masque la bulle pour voir l'original (re-clic pour la remettre).
      // Alt+clic : signale la bulle (mauvaise lecture ou mauvaise traduction).
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (ev.altKey) {
          ev.preventDefault();
          toggleReport(e, b, el);
        } else {
          el.classList.toggle("phy-peek");
        }
      });
      layer.appendChild(el);
    }
    document.body.appendChild(layer);
    e.layer = layer;
    e.lastW = undefined;
    e.rect = null;
    position(e, e.img.getBoundingClientRect());
  }

  // Calque en position absolue dans la page, superposé à l'image (rectangle `r` déjà lu).
  // Rien n'est réécrit si le calque n'a pas bougé : la page de Naver mute souvent
  // (commentaires, publicités), et chaque mutation relance un repositionnement.
  function position(e, r) {
    if (!r?.width || !r.height) {
      e.layer.style.display = "none";
      e.rect = null;
      return;
    }
    const left = r.left + scrollX, top = r.top + scrollY;
    const rect = `${left},${top},${r.width},${r.height}`;
    if (e.rect === rect) return;
    e.rect = rect;
    Object.assign(e.layer.style, {
      display: "",
      left: left + "px",
      top: top + "px",
      width: r.width + "px",
      height: r.height + "px"
    });
    if (e.lastW !== r.width) {
      e.lastW = r.width;
      fitText(e);
    }
  }

  // Plus grande police (au pixel près, entre 9 px et la taille de départ) pour laquelle la
  // traduction tient dans la zone d'origine. Recherche par dichotomie : chaque mesure de
  // scrollHeight force un calcul de mise en page.
  function fitText(e) {
    const scale = e.layer.clientWidth / e.imageSize.w;
    for (const el of e.layer.children) {
      const target = parseFloat(el.dataset.h) * scale * 1.1;
      const fits = (size) => {
        el.style.fontSize = size + "px";
        return el.scrollHeight <= target;
      };
      let hi = Math.round(Math.min(22, Math.max(12, target * 0.3)));
      if (fits(hi)) continue;
      let lo = 9;
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (fits(mid)) lo = mid;
        else hi = mid;
      }
      fits(lo);
    }
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
      layers.forEach((e, i) => position(e, rects[i]));
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
      const next = currentEpisode();
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
    return node === badge || !!node.closest?.(".phy-layer");
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
