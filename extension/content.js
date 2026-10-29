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
      position: absolute; display: inline-flex; align-items: center; gap: 6px; height: 30px;
      padding: 0 10px 0 7px; border: 1px solid #e5e7eb; border-radius: 8px; cursor: pointer; user-select: none;
      background: #ffffff; color: #111827; box-shadow: 0 4px 12px rgba(17,24,39,.12);
      font: 600 13px/1 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    }
    .trigger:hover { border-color: #d1d5db; background: #f9fafb; }
    .trigger:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }
    .trigger img { width: 18px; height: 18px; border-radius: 4px; display: block; flex: none; }
    .card {
      position: absolute; width: ${CARD_WIDTH}px; max-width: calc(100vw - 16px);
      max-height: min(70vh, 560px); overflow: auto; overscroll-behavior: contain;
      padding: 16px; border-radius: 12px; background: #ffffff; border: 1px solid #e5e7eb;
      box-shadow: 0 12px 32px rgba(17,24,39,.14), 0 2px 6px rgba(17,24,39,.06);
    }
    .close { position: absolute; top: 10px; right: 10px; width: 26px; height: 26px; display: grid; place-items: center;
      border: 0; border-radius: 6px; background: none; color: #6b7280; cursor: pointer; padding: 0; }
    .close:hover { color: #111827; background: #f3f4f6; }
    .close:focus-visible { outline: 2px solid #2563eb; outline-offset: 1px; }
    .close svg { width: 12px; height: 12px; }
    @media (prefers-color-scheme: dark) {
      .trigger { background: #17181b; color: #f3f4f6; border-color: #2b2f36; box-shadow: 0 4px 12px rgba(0,0,0,.5); }
      .trigger:hover { background: #1d1f23; border-color: #3a3f47; }
      .card { background: #17181b; border-color: #2b2f36; box-shadow: 0 12px 32px rgba(0,0,0,.55); }
      .close { color: #9ca3af; }
      .close:hover { color: #f3f4f6; background: #24262b; }
    }
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
    xp.setAttribute("stroke-width", "1.8");
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
      trigger.title = "Check this claim against official data (⌘⇧Y)";
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
      const left = Math.min(window.scrollX + end.right - 40, vp.right - 90);
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
