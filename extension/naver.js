// naver.js — tout ce qui dépend de la structure des pages de comic.naver.com : adresse
// d'un épisode, conteneur et URL des images, script d'état (épisodes précédent et suivant).
// Si Naver change son interface, c'est le seul fichier du content script à modifier.
// Chargé avant content.js (manifest.json) ; expose globalThis.phy.naver.

globalThis.phy ??= {};
globalThis.phy.naver = (() => {
  "use strict";

  const IMG_HOST = "image-comic.pstatic.net";
  // Conteneur des images de l'épisode (<img id="content_image_N">). Ailleurs dans la page,
  // la vignette de l'épisode a une URL du même dossier : elle ne doit pas être traduite.
  const VIEWER = "#sectionContWide";
  // Largeur minimale (px) d'une image de IMG_HOST pour la compter comme image d'épisode
  // non reconnue (jalon 19). Les images d'un épisode font 690 px de large ; les vignettes
  // (épisode, série, autres œuvres), présentes même sur un épisode verrouillé, bien moins.
  const MIN_EPISODE_IMAGE_WIDTH = 400;

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

  // Les images de l'épisode `ep` sont reconnues à leur URL : …/webtoon/<titleId>/<no>/…
  // (pas à leur taille : on peut ainsi les envoyer au serveur avant qu'elles chargent).
  function isEpisodeImage(url, ep) {
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

  // Conteneur des images de l'épisode dans `doc`, ou null si Naver l'a renommé.
  function viewer(doc = document) {
    return doc.querySelector(VIEWER);
  }

  // Images de l'épisode `ep` affichées dans la page, dans l'ordre et sans doublon :
  // [{ url, img }]. Repli sur toute la page si Naver renomme le conteneur.
  function episodeImages(ep) {
    const seen = new Set();
    const list = [];
    const root = viewer() || document;
    for (const img of root.querySelectorAll("img")) {
      const url = imageUrl(img);
      if (seen.has(url) || !isEpisodeImage(url, ep)) continue;
      seen.add(url);
      list.push({ url, img });
    }
    return list;
  }

  // Diagnostic d'une page d'épisode où episodeImages() ne trouve rien (jalon 19) :
  // { unrecognized } nombre de grandes images de IMG_HOST qui ne sont pas reconnues comme
  // images de l'épisode `ep`. Plus de 0 : Naver a sans doute changé le format de ses URL
  // (ou l'adresse de la page). 0 : épisode payant, verrouillé ou pas encore chargé.
  function diagnose(ep) {
    let unrecognized = 0;
    for (const img of document.querySelectorAll("img")) {
      const url = imageUrl(img);
      let u;
      try {
        u = new URL(url);
      } catch {
        continue;
      }
      if (u.hostname !== IMG_HOST || /thumbnail/i.test(u.pathname) || isEpisodeImage(url, ep)) continue;
      // Largeur réelle si l'image est chargée, sinon affichée (ou attribut width).
      if (Math.max(img.naturalWidth, img.width) >= MIN_EPISODE_IMAGE_WIDTH) unrecognized++;
    }
    return { unrecognized };
  }

  // Pages dont l'absence de script d'état a déjà été signalée : une fois par page.
  const stateWarned = new WeakSet();

  // Avertit dans la console si `doc` n'a pas de script d'état (window.__state__) : le
  // préchargement de l'épisode suivant et le résumé « Précédemment… » ne marchent plus.
  // Distinct d'un script d'état sans épisode suivant (dernier épisode, épisode payant).
  function warnMissingState(doc) {
    if (stateWarned.has(doc)) return;
    stateWarned.add(doc);
    console.warn("[Phylactère] Script d'état de Naver (window.__state__) introuvable dans " +
      "la page : pas de préchargement de l'épisode suivant, et l'épisode précédent est " +
      "deviné (no - 1). Naver a sans doute changé sa page.");
  }

  // Vérifie le script d'état de `doc` (page de l'épisode chargée par le navigateur).
  function checkState(doc = document) {
    const found = [...doc.querySelectorAll("script:not([src])")]
      .some((script) => script.textContent.includes("__state__"));
    if (!found) warnMissingState(doc);
    return found;
  }

  // Article du script d'état que Naver met dans la page (window.__state__ = { …,
  // prevArticle: { no: '65', … }, nextArticle: { no: '67', charge: false, … } }). Le content
  // script ne voit pas les variables de la page, mais il lit le texte de ses scripts.
  // Renvoie { block, no } (texte entre accolades et numéro, autre que l'épisode `episodeId`),
  // null si le script d'état n'a pas cet article, undefined s'il n'y a pas de script d'état.
  function stateArticle(doc, name, episodeId) {
    let found;
    const pattern = new RegExp(`${name}\\s*:\\s*\\{([^}]*)\\}`);
    for (const script of doc.querySelectorAll("script:not([src])")) {
      const text = script.textContent;
      if (!text.includes("__state__")) continue;
      found = null;
      const block = pattern.exec(text)?.[1];
      if (block === undefined) continue;
      const no = /\bno\s*:\s*['"]?(\d+)/.exec(block)?.[1];
      return no && no !== episodeId ? { block, no } : null;
    }
    if (found === undefined) warnMissingState(doc);
    return found;
  }

  // Numéro de l'épisode suivant. Épisode payant (charge: true) ou absent (dernier épisode) :
  // pas de préchargement.
  function nextEpisodeNumber(doc, episodeId) {
    const article = stateArticle(doc, "nextArticle", episodeId);
    return article && !/\bcharge\s*:\s*true/.test(article.block) ? article.no : null;
  }

  // Numéro de l'épisode précédent (vérifié sur la vraie page : prevArticle: { no: '65',
  // blindInspection: false }). Script d'état sans prevArticle : premier épisode. Pas de
  // script d'état du tout (Naver a changé sa page) : no - 1.
  function prevEpisodeNumber(doc, episodeId) {
    const article = stateArticle(doc, "prevArticle", episodeId);
    if (article !== undefined) return article?.no ?? null;
    const no = Number(episodeId) - 1;
    return no >= 1 ? String(no) : null;
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

  // URL d'image telle que episodeImages() la verra à l'ouverture de l'épisode (URL absolue,
  // image réelle plutôt qu'image d'attente) : les voisines entrent dans la clé du cache OCR.
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
  // Naver, #sectionContWide) : on applique la même sélection que episodeImages().
  async function episodeImageUrls(ep) {
    const { url, doc } = await fetchEpisodePage(ep.episodeId);
    const root = viewer(doc) || doc;
    const urls = [];
    for (const img of root.querySelectorAll("img")) {
      const src = pageImageUrl(img, url, ep);
      if (src && !urls.includes(src)) urls.push(src);
    }
    return urls;
  }

  return {
    IMG_HOST,
    VIEWER,
    currentEpisode,
    imageUrl,
    viewer,
    episodeImages,
    diagnose,
    checkState,
    nextEpisodeNumber,
    prevEpisodeNumber,
    fetchEpisodePage,
    episodeImageUrls
  };
})();
