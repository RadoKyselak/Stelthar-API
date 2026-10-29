(() => {
  // On-page checking: highlight a sentence with a number in it, and a small
  // button appears where the selection ends. Clicking it opens a card right
  // there. Right-click "Verify with Project Mirador" and Cmd/Ctrl+Shift+Y open
  // the same card. Nothing appears for text without a figure in it.
  const MIN_CLAIM_LEN = 12;
  const MAX_CLAIM_LEN = 600;
  const TRIGGER_TIMEOUT_MS = 6000;
  const CARD_WIDTH = 340;
  const View = window.MiradorView;

  let host = null;
  let shadow = null;
  let triggerTimer = null;

  const CSS = `
    :host { all: initial; }
    .trigger {
      position: absolute; display: inline-flex; align-items: center; height: 30px; padding: 3px;
      border: 0; border-radius: 999px; cursor: pointer; user-select: none;
      background: rgba(255,255,255,.92); color: #1d1d1f;
      -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px);
      box-shadow: 0 0 0 .5px rgba(0,0,0,.12), 0 4px 14px rgba(0,0,0,.16);
      font: 500 12.5px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, Arial, sans-serif;
      animation: appear .18s cubic-bezier(.2,1.3,.4,1);
    }
    .trigger img { width: 24px; height: 24px; border-radius: 50%; display: block; flex: none; }
    .trigger span { max-width: 0; overflow: hidden; white-space: nowrap; opacity: 0;
      transition: max-width .2s ease, opacity .15s ease, padding .2s ease; }
    .trigger:hover span, .trigger:focus-visible span { max-width: 60px; opacity: 1; padding: 0 8px 0 6px; }
    .trigger:focus-visible { outline: 2px solid #0071e3; outline-offset: 2px; }
    .card {
      position: absolute; width: ${CARD_WIDTH}px; max-width: calc(100vw - 16px);
      max-height: min(70vh, 520px); overflow: auto; overscroll-behavior: contain;
      padding: 14px 16px 12px; border-radius: 14px;
      background: rgba(255,255,255,.86);
      -webkit-backdrop-filter: saturate(180%) blur(24px); backdrop-filter: saturate(180%) blur(24px);
      box-shadow: 0 0 0 .5px rgba(0,0,0,.14), 0 14px 40px rgba(0,0,0,.18);
      animation: appear .16s ease-out;
    }
    .card.above { transform-origin: bottom left; }
    .close { position: absolute; top: 9px; right: 9px; width: 22px; height: 22px; display: grid; place-items: center;
      border: 0; border-radius: 50%; background: rgba(0,0,0,.06); color: #6e6e73; cursor: pointer; padding: 0; }
    .close:hover { background: rgba(0,0,0,.1); color: #1d1d1f; }
    .close:focus-visible { outline: 2px solid #0071e3; outline-offset: 1px; }
    .close svg { width: 10px; height: 10px; }
    @media (prefers-color-scheme: dark) {
      .trigger { background: rgba(44,44,46,.92); color: #f5f5f7; box-shadow: 0 0 0 .5px rgba(255,255,255,.16), 0 4px 14px rgba(0,0,0,.5); }
      .card { background: rgba(30,30,32,.86); box-shadow: 0 0 0 .5px rgba(255,255,255,.14), 0 14px 40px rgba(0,0,0,.55); }
      .close { background: rgba(255,255,255,.1); color: #a1a1a6; }
      .close:hover { background: rgba(255,255,255,.16); color: #f5f5f7; }
    }
    /* Scale only: if a browser pauses the animation, the card is still fully visible. */
    @keyframes appear { from { transform: scale(.97); } to { transform: none; } }
    @media (prefers-reduced-motion: reduce) { .trigger, .card { animation: none; } }
  `;

  const ensureHost = () => {
    if (host && document.documentElement.contains(host)) return shadow;
    host = document.createElement("div");
    host.style.all = "initial";
    host.style.position = "absolute";
    host.style.top = "0";
    host.style.left = "0";
    host.style.zIndex = "2147483647";
    shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS + View.css;
    shadow.appendChild(style);
    document.documentElement.appendChild(host);
    return shadow;
  };

  const remove = (cls) => {
    if (shadow) shadow.querySelectorAll(`.${cls}`).forEach((n) => n.remove());
  };
  const closeCard = () => remove("card");

  const viewport = () => ({
    left: window.scrollX + 8,
    right: window.scrollX + document.documentElement.clientWidth - 8,
    top: window.scrollY + 8,
    bottom: window.scrollY + window.innerHeight - 8,
  });

  // The last line box of the selection: where the reader's eye ends up.
  const selectionEnd = (range) => {
    const rects = Array.from(range.getClientRects()).filter((r) => r.width || r.height);
    return rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
  };

  const showCard = (claim, anchor) => {
    const root = ensureHost();
    remove("trigger");
    closeCard();

    const card = document.createElement("div");
    card.className = "card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "Fact check");
    card.tabIndex = -1;

    const close = document.createElement("button");
    close.className = "close";
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    const x = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    x.setAttribute("viewBox", "0 0 10 10");
    x.setAttribute("aria-hidden", "true");
    const xp = document.createElementNS("http://www.w3.org/2000/svg", "path");
    xp.setAttribute("d", "M1.5 1.5l7 7M8.5 1.5l-7 7");
    xp.setAttribute("stroke", "currentColor");
    xp.setAttribute("stroke-width", "1.6");
    xp.setAttribute("stroke-linecap", "round");
    x.appendChild(xp);
    close.appendChild(x);
    close.onclick = closeCard;
    const body = document.createElement("div");
    card.append(close, body);

    // Below the selection; above it when there is no room below.
    const vp = viewport();
    const a = anchor || { left: 80, right: 80, top: 80, bottom: 80 };
    const left = Math.max(vp.left, Math.min(window.scrollX + a.left, vp.right - CARD_WIDTH));
    const below = window.scrollY + a.bottom + 8;
    card.style.left = `${left}px`;
    if (anchor && below + 260 > vp.bottom && window.scrollY + a.top - vp.top > 260) {
      card.classList.add("above");
      card.style.top = `${window.scrollY + a.top - 8}px`;
      card.style.transform = "translateY(-100%)";
      card.style.animation = "none";
    } else {
      card.style.top = `${anchor ? below : window.scrollY + 80}px`;
    }
    root.appendChild(card);
    card.focus({ preventScroll: true });

    const run = (bypassCache) => {
      body.replaceChildren(View.loading());
      chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
        if (!card.isConnected) return;
        if (chrome.runtime.lastError || !resp) {
          body.replaceChildren(View.error("The extension didn't respond. Reload the page and try again.",
            { onRetry: () => run(true) }));
          return;
        }
        if (!resp.ok) {
          body.replaceChildren(View.error(resp.error, { onRetry: () => run(true) }));
          return;
        }
        body.replaceChildren(View.render(resp.result, {
          onRecheck: resp.result._cached ? () => run(true) : null,
        }));
      });
    };
    run(false);
  };

  const isEditable = (node) => {
    const el = node instanceof Element ? node : node && node.parentElement;
    return !!(el && el.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']"));
  };

  const ours = (e) => host && e.composedPath && e.composedPath().includes(host);

  document.addEventListener("mouseup", (e) => {
    if (ours(e)) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = (sel ? sel.toString() : "").replace(/\s+/g, " ").trim();
      remove("trigger");
      clearTimeout(triggerTimer);
      // Only text that states a figure can be checked against official data.
      if (text.length < MIN_CLAIM_LEN || text.length > MAX_CLAIM_LEN || !/\d/.test(text)
          || !sel.rangeCount || isEditable(sel.anchorNode)) {
        return;
      }
      const end = selectionEnd(sel.getRangeAt(0));
      if (!end.width && !end.height) return;

      const root = ensureHost();
      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "trigger";
      trigger.title = "Check this claim (⌘⇧Y)";
      trigger.setAttribute("aria-label", "Check this claim");
      const icon = document.createElement("img");
      icon.src = chrome.runtime.getURL("icons/icon48.png");
      icon.alt = "";
      const label = document.createElement("span");
      label.textContent = "Check";
      trigger.append(icon, label);

      // Just above where the selection ends, so it never covers the text;
      // below it when the selection is at the top of the window.
      const vp = viewport();
      let top = window.scrollY + end.top - 36;
      if (top < vp.top) top = window.scrollY + end.bottom + 6;
      const left = Math.min(window.scrollX + end.right - 28, vp.right - 90);
      trigger.style.top = `${top}px`;
      trigger.style.left = `${Math.max(vp.left, left)}px`;
      const anchor = sel.getRangeAt(0).getBoundingClientRect();
      trigger.onclick = () => showCard(text, anchor);
      root.appendChild(trigger);
      triggerTimer = setTimeout(() => remove("trigger"), TRIGGER_TIMEOUT_MS);
    }, 0);
  });

  document.addEventListener("mousedown", (e) => {
    if (ours(e)) return;
    remove("trigger");
    closeCard();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && shadow && shadow.querySelector(".card")) closeCard();
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "showPanel" && message.claim) {
      const sel = window.getSelection();
      const rect = sel && sel.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;
      showCard(message.claim, rect && (rect.width || rect.height) ? rect : null);
    }
  });
})();
