// render.js — dessin des bulles traduites : calque superposé à une image, texte et
// couleurs d'une bulle, position du calque, taille du texte, encart « Précédemment… ».
// Aucun état : content.js garde les entrées et appelle ces fonctions.
// Chargé avant content.js (manifest.json) ; expose globalThis.phy.render.

globalThis.phy ??= {};
globalThis.phy.render = (() => {
  "use strict";

  const PAD = 6; // marge autour du texte détecté (px de l'image d'origine)

  // Texte, infobulle (original, et modèle qui a traduit s'il est connu) et liseré des
  // bulles corrigées.
  function fillBubble(el, b) {
    el.textContent = b.translated;
    if (b.corrected) {
      el.dataset.corrected = "";
      el.title = `${b.original}\n— corrigée à la main` +
        (b.machine ? `\nTraduction du modèle : ${b.machine}` : "");
    } else {
      delete el.dataset.corrected;
      el.title = b.model ? `${b.original}\n— ${b.model}` : b.original;
    }
  }

  // Calque des bulles de l'entrée `e` (`e.blocks`, `e.imageSize`), pas encore inséré dans
  // la page. `onClick(ev, i, el)` : clic sur la bulle i.
  function buildLayer(e, { hidden, lang, onClick }) {
    const { w: nw, h: nh } = e.imageSize;
    const layer = document.createElement("div");
    layer.className = "phy-layer" + (hidden ? " phy-hidden" : "");

    e.blocks.forEach((b, i) => {
      // Une bulle coupée entre deux images peut déborder en bas (y + h > hauteur).
      const x = Math.max(0, b.box.x - PAD);
      const y = Math.max(0, b.box.y - PAD);
      const w = Math.min(nw - x, b.box.w + PAD * 2);
      const h = b.box.h + PAD * 2;
      const el = document.createElement("div");
      el.className = "phy-bubble";
      el.lang = lang;
      fillBubble(el, b);
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
      el.addEventListener("click", (ev) => onClick(ev, i, el));
      layer.appendChild(el);
    });
    return layer;
  }

  // Calque en position absolue dans la page, superposé à l'image (rectangle `r` déjà lu).
  // Rien n'est réécrit si le calque n'a pas bougé : la page de Naver mute souvent
  // (commentaires, publicités), et chaque mutation relance un repositionnement. Renvoie
  // true si la largeur a changé : le texte des bulles est alors à réajuster (fitText).
  function position(e, r) {
    if (!r?.width || !r.height) {
      e.layer.style.display = "none";
      e.rect = null;
      return false;
    }
    const left = r.left + scrollX, top = r.top + scrollY;
    const rect = `${left},${top},${r.width},${r.height}`;
    if (e.rect === rect) return false;
    e.rect = rect;
    Object.assign(e.layer.style, {
      display: "",
      left: left + "px",
      top: top + "px",
      width: r.width + "px",
      height: r.height + "px"
    });
    if (e.lastW === r.width) return false;
    e.lastW = r.width;
    return true;
  }

  // Plus grande police (au pixel près, entre 9 px et la taille de départ) pour laquelle la
  // traduction tient dans la zone d'origine, pour toutes les bulles des entrées données.
  // Recherche par dichotomie, menée pour toutes les bulles à la fois : chaque étape écrit
  // toutes les tailles puis lit toutes les hauteurs, soit une mise en page par étape au
  // lieu d'une par bulle et par étape (redimensionnement d'un épisode entier).
  function fitText(...list) {
    const items = [];
    for (const e of list) {
      const scale = e.layer.clientWidth / e.imageSize.w;
      for (const el of e.layer.children) {
        const target = parseFloat(el.dataset.h) * scale * 1.1;
        items.push({ el, target, lo: 9, hi: Math.round(Math.min(22, Math.max(12, target * 0.3))) });
      }
    }
    for (const it of items) it.el.style.fontSize = it.hi + "px";
    const searching = items.filter((it) => it.el.scrollHeight > it.target);
    for (let step = searching; step.length; step = step.filter((it) => it.hi - it.lo > 1)) {
      for (const it of step) {
        it.mid = (it.lo + it.hi) >> 1;
        it.el.style.fontSize = it.mid + "px";
      }
      for (const it of step) {
        if (it.el.scrollHeight <= it.target) it.lo = it.mid;
        else it.hi = it.mid;
      }
    }
    for (const it of searching) it.el.style.fontSize = it.lo + "px";
  }

  // Encart repliable « Précédemment… » (jalon 15), pas encore inséré dans la page.
  // `onClose` : clic sur la croix.
  function buildPreviously(data, lang, onClose) {
    const box = document.createElement("details");
    box.className = "phy-previously";
    box.open = true;
    box.lang = lang;
    const head = document.createElement("summary");
    const title = document.createElement("span");
    title.textContent = "Précédemment…" + (data.partial ? " (épisode lu en partie)" : "");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "phy-close";
    close.textContent = "×";
    close.title = "Fermer le résumé";
    close.setAttribute("aria-label", "Fermer le résumé");
    close.addEventListener("click", (ev) => {
      ev.preventDefault(); // pas de repli : l'encart disparaît
      onClose();
    });
    head.append(title, close);
    const text = document.createElement("p");
    text.textContent = data.summary;
    text.title = `Résumé par ${data.model}, à partir de ${data.bubbles} bulle(s) traduite(s)`;
    box.append(head, text);
    // Les clics ne remontent pas à la page (barre d'outils de la visionneuse de Naver).
    box.addEventListener("click", (ev) => ev.stopPropagation());
    return box;
  }

  return { fillBubble, buildLayer, position, fitText, buildPreviously };
})();
