// bubbles.js — actions sur une bulle affichée : clic (masquer), Alt+clic (signaler),
// Maj+clic (retraduire), Ctrl+clic (corriger), et Maj+Alt+T (retraduire l'image).
// Chargé avant content.js (manifest.json) ; expose globalThis.phy.bubbleActions(app), où
// `app` donne accès à l'état de content.js :
//   ask(msg)          message au script d'arrière-plan (résultat normalisé)
//   requestFor(e)     désignation de l'image de l'entrée `e` pour le serveur
//   showNotice(text, state)   message temporaire du badge
//   render(e)         redessine le calque de l'entrée `e`
//   generation()      numéro de la remise à zéro en cours
//   order()           entrées de l'épisode, dans l'ordre

globalThis.phy ??= {};
globalThis.phy.bubbleActions = (app) => {
  "use strict";

  const { fillBubble, fitText } = globalThis.phy.render;

  // Clic sur la bulle i de l'entrée `e`. Clic : masque la bulle pour voir l'original
  // (re-clic pour la remettre). Alt+clic : signale la bulle (mauvaise lecture ou mauvaise
  // traduction). Maj+clic : la retraduit. Ctrl+clic : la corrige à la main. `e.blocks[i]`
  // : la bulle a pu être retraduite ou corrigée depuis le dessin du calque.
  function onClick(e, i, el, ev) {
    ev.stopPropagation();
    if (el.isContentEditable) return; // correction en cours : place le curseur
    if (ev.ctrlKey) {
      ev.preventDefault();
      startCorrection(e, i, el);
    } else if (ev.altKey) {
      ev.preventDefault();
      toggleReport(e, e.blocks[i], el);
    } else if (ev.shiftKey) {
      ev.preventDefault();
      retranslateBubble(e, i, el);
    } else {
      el.classList.toggle("phy-peek");
    }
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
      ...app.requestFor(e),
      box: b.box,
      original: b.original,
      translated: b.translated
    };
    const res = await app.ask({ type: "report", request });
    if (!el.isConnected) return; // page remise à zéro entre-temps
    if (res.ok) {
      el.dataset.report = "done";
      el.dataset.reportId = res.data.id;
      app.showNotice("Bulle signalée (Alt+clic pour annuler)", "");
    } else {
      delete el.dataset.report;
      console.warn("[Phylactère] Signalement impossible :", res.error);
      app.showNotice("Signalement impossible : " + res.error, "error");
    }
  }

  async function cancelReport(el) {
    el.dataset.report = "sending";
    const res = await app.ask({ type: "report-cancel", id: el.dataset.reportId });
    if (!el.isConnected) return;
    if (res.ok) {
      delete el.dataset.report;
      delete el.dataset.reportId;
      app.showNotice("Signalement annulé", "");
    } else {
      el.dataset.report = "done";
      console.warn("[Phylactère] Annulation impossible :", res.error);
      app.showNotice("Annulation impossible : " + res.error, "error");
    }
  }

  // --- Retraduction (jalon 13) ---

  // Maj+clic : le serveur retraduit la bulle seule (avec le modèle « pour retraduire » du
  // popup) et la remplace dans son cache, sous la clé du modèle affiché : la nouvelle
  // traduction reste au rechargement de la page.
  async function retranslateBubble(e, i, el) {
    if (el.dataset.retranslate) return; // déjà en cours
    if (e.blocks[i].corrected) {
      return app.showNotice("Bulle corrigée : effacez la correction d'abord", "error");
    }
    el.dataset.retranslate = "sending";
    const request = { ...app.requestFor(e), box: e.blocks[i].box };
    const res = await app.ask({ type: "retranslate", request });
    if (!el.isConnected) return; // page remise à zéro ou image retraduite entre-temps
    delete el.dataset.retranslate;
    if (!res.ok) {
      console.warn("[Phylactère] Retraduction impossible :", res.error);
      app.showNotice("Retraduction impossible : " + res.error, "error");
      return;
    }
    e.blocks[i] = res.data;
    fillBubble(el, res.data);
    fitText(e);
    app.showNotice("Bulle retraduite" + (res.data.model ? ` (${res.data.model})` : ""), "");
  }

  // Image dont la plus grande hauteur est à l'écran.
  function mostVisibleEntry() {
    let best = null, bestHeight = 0;
    for (const e of app.order()) {
      if (!e.visible || !e.img.isConnected) continue;
      const r = e.img.getBoundingClientRect();
      const height = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
      if (height > bestHeight) { best = e; bestHeight = height; }
    }
    return best;
  }

  // Maj+Alt+T : toute l'image la plus visible est retraduite (`force` : le serveur ignore
  // sa traduction en cache puis la remplace ; l'OCR en cache est réutilisé). Requête hors
  // quota, comme un renvoi « visible » : l'utilisateur attend.
  async function retranslateImage() {
    const e = mostVisibleEntry();
    if (!e) return;
    if (e.status !== "done" || !e.layer) return app.showNotice("Image pas encore traduite", "error");
    if (!e.blocks.length) return app.showNotice("Aucune bulle sur cette image", "");
    if (e.retranslating) return;
    const gen = app.generation();
    e.retranslating = true;
    e.layer.classList.add("phy-retranslating");
    const request = { ...app.requestFor(e), priority: "visible", force: true };
    const res = await app.ask({ type: "translate", request });
    if (gen !== app.generation()) return;
    e.retranslating = false;
    e.layer?.classList.remove("phy-retranslating");
    if (!res.ok) {
      console.warn("[Phylactère] Retraduction impossible :", res.error);
      app.showNotice("Retraduction impossible : " + res.error, "error");
      return;
    }
    e.blocks = res.data.blocks;
    e.imageSize = res.data.image_size;
    app.render(e);
    const model = res.data.blocks[0]?.model;
    app.showNotice("Image retraduite" + (model ? ` (${model})` : ""), "");
  }

  // --- Correction à la main (jalon 14) ---

  // Ctrl+clic : la bulle devient modifiable, son texte sélectionné. Entrée valide,
  // Maj+Entrée ajoute un retour à la ligne, Échap (ou un clic ailleurs) annule. Le serveur
  // garde la correction par image et langue, quel que soit le modèle : elle survit au
  // rechargement, au vidage du cache et aux changements de modèle.
  function startCorrection(e, i, el) {
    if (el.isContentEditable || el.dataset.correct || el.dataset.retranslate) return;
    el.classList.remove("phy-peek");
    el.contentEditable = "true";
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);

    const stop = (ev) => ev.stopPropagation(); // raccourcis clavier de la page de Naver
    const finish = (commit) => {
      el.removeEventListener("keydown", onKey);
      el.removeEventListener("keyup", stop);
      el.removeEventListener("keypress", stop);
      el.removeEventListener("blur", onBlur);
      const text = el.innerText.replace(/ /g, " ").trim();
      el.contentEditable = "false";
      el.removeAttribute("contenteditable");
      getSelection().removeAllRanges();
      fillBubble(el, e.blocks[i]); // texte d'avant : remplacé si le serveur accepte
      if (commit && text !== e.blocks[i].translated.trim()) saveCorrection(e, i, el, text);
      else fitText(e);
    };
    const onKey = (ev) => {
      ev.stopPropagation();
      if (ev.key === "Escape") {
        ev.preventDefault();
        finish(false);
      } else if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        finish(true);
      }
    };
    const onBlur = () => finish(false);
    el.addEventListener("keydown", onKey);
    el.addEventListener("keyup", stop);
    el.addEventListener("keypress", stop);
    el.addEventListener("blur", onBlur);
  }

  async function saveCorrection(e, i, el, text) {
    const b = e.blocks[i];
    // Traduction du modèle : celle d'avant la première correction.
    const machine = b.corrected ? (b.machine ?? b.translated) : b.translated;
    // Rien à effacer, ou texte identique à celui du modèle : pas de correction.
    if (!b.corrected && !text) return;
    el.dataset.correct = "sending";
    const request = {
      ...app.requestFor(e),
      box: b.box,
      original: b.original,
      text,
      translated: machine
    };
    const res = await app.ask({ type: "correct", request });
    if (!el.isConnected) return; // page remise à zéro ou image retraduite entre-temps
    delete el.dataset.correct;
    if (!res.ok) {
      console.warn("[Phylactère] Correction impossible :", res.error);
      app.showNotice("Correction impossible : " + res.error, "error");
      return;
    }
    e.blocks[i] = text
      ? { ...b, translated: text, corrected: true, machine }
      : { ...b, translated: machine, corrected: false, machine: null };
    fillBubble(el, e.blocks[i]);
    fitText(e);
    app.showNotice(text ? "Bulle corrigée" : "Correction effacée", "");
  }

  return { onClick, retranslateImage };
};
