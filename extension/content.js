(() => {
  const MIN_CLAIM_LEN = 12;
  const MAX_CLAIM_LEN = 600;
  const BUBBLE_TIMEOUT_MS = 6000;
  const PANEL_WIDTH = 380;
  const View = window.MiradorView;

  let host = null;
  let shadow = null;
  let bubbleTimer = null;

  // The Mirador logo, from the extension's own icons.
  const logo = (size) => {
    const img = document.createElement("img");
    img.src = chrome.runtime.getURL("icons/icon48.png");
    img.alt = "";
    img.width = size;
    img.height = size;
    img.className = "logo";
    return img;
  };

  const PANEL_CSS = `
    :host { all: initial; }
    .logo { border-radius: 5px; display: block; flex: none; }
    .bubble {
      position: absolute; display: inline-flex; align-items: center; gap: 7px;
      background: #121314; color: #fff; padding: 5px 13px 5px 5px; border-radius: 999px; border: 0;
      font: 650 13px/1.2 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      cursor: pointer; white-space: nowrap; user-select: none;
      box-shadow: 0 6px 20px rgba(0,0,0,.22), 0 1px 3px rgba(0,0,0,.2);
      animation: pop .16s cubic-bezier(.2,1.4,.45,1);
    }
    .bubble:hover { background: #2a2c30; }
    .bubble:focus-visible { outline: 2px solid #8ab4ff; outline-offset: 2px; }
    @keyframes pop { from { opacity: 0; transform: translateY(4px) scale(.94); } to { opacity: 1; transform: none; } }
    .panel {
      --p-card: #ffffff; --p-line: #ebebe8; --p-muted: #6b6f76; --p-ink: #121314;
      position: absolute; width: ${PANEL_WIDTH}px; max-width: calc(100vw - 24px);
      max-height: min(80vh, 640px); overflow: auto; overscroll-behavior: contain;
      background: var(--p-card); color: var(--p-ink); border: 1px solid var(--p-line); border-radius: 16px;
      box-shadow: 0 20px 50px rgba(0,0,0,.16), 0 2px 8px rgba(0,0,0,.06);
      font: 14px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      animation: pop .18s ease-out;
    }
    @media (prefers-color-scheme: dark) {
      .panel { --p-card: #161718; --p-line: #2a2b2e; --p-muted: #a3a7ae; --p-ink: #f1f1f0;
        box-shadow: 0 20px 50px rgba(0,0,0,.55); }
    }
    @media (prefers-reduced-motion: reduce) { .bubble, .panel { animation: none; } }
    .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 8px;
      padding: 12px 12px 0 16px; }
    .panel-brand { display: flex; align-items: center; gap: 8px; font-weight: 750; font-size: 13.5px; }
    .panel-close { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 8px;
      border: 0; background: transparent; color: var(--p-muted); cursor: pointer; font-size: 15px; }
    .panel-close:hover { background: rgba(127,127,127,.14); color: var(--p-ink); }
    .panel-close:focus-visible { outline: 2px solid #2563eb; outline-offset: 1px; }
    .panel-claim { margin: 10px 16px 0; color: var(--p-muted); font-size: 13px; font-style: italic;
      display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
    .panel-body { padding: 14px 16px 16px; }
  `;

  const ensureShadowHost = () => {
    if (host && document.documentElement.contains(host)) return shadow;
    host = document.createElement("div");
    host.id = "stelthar-root";
    host.style.all = "initial";
    host.style.position = "absolute";
    host.style.top = "0";
    host.style.left = "0";
    host.style.zIndex = "2147483647";
    shadow = host.attachShadow({ mode: "closed" });

    const style = document.createElement("style");
    style.textContent = PANEL_CSS + View.css;
    shadow.appendChild(style);
    document.documentElement.appendChild(host);
    return shadow;
  };

  const removeEl = (cls) => {
    if (!shadow) return;
    shadow.querySelectorAll(`.${cls}`).forEach((el) => el.remove());
  };

  const clampLeft = (left, width) =>
    Math.max(8, Math.min(left, window.scrollX + document.documentElement.clientWidth - width - 8));

  const closePanel = () => removeEl("panel");

  const showPanel = (claim, anchorRect) => {
    const root = ensureShadowHost();
    removeEl("bubble");
    removeEl("panel");

    const panel = document.createElement("div");
    panel.className = "panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Project Mirador fact check");

    const top = anchorRect ? window.scrollY + anchorRect.bottom + 10 : window.scrollY + 80;
    const left = clampLeft(window.scrollX + (anchorRect ? anchorRect.left : 80), PANEL_WIDTH);
    panel.style.top = `${top}px`;
    panel.style.left = `${left}px`;

    const header = document.createElement("div");
    header.className = "panel-head";
    const brand = document.createElement("div");
    brand.className = "panel-brand";
    brand.appendChild(logo(20));
    brand.appendChild(document.createTextNode("Mirador"));
    const close = document.createElement("button");
    close.className = "panel-close";
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.textContent = "✕";
    close.onclick = closePanel;
    header.appendChild(brand);
    header.appendChild(close);
    panel.appendChild(header);

    const claimEl = document.createElement("div");
    claimEl.className = "panel-claim";
    claimEl.textContent = `“${claim}”`;
    panel.appendChild(claimEl);

    const body = document.createElement("div");
    body.className = "panel-body";
    panel.appendChild(body);
    root.appendChild(panel);
    close.focus({ preventScroll: true });

    const run = (bypassCache) => {
      body.replaceChildren(View.loading());
      chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
        if (chrome.runtime.lastError || !resp) {
          body.replaceChildren(View.error(
            "The extension's background service did not answer. Reload the page and try again.",
            { onRetry: () => run(true) }));
          return;
        }
        if (!resp.ok) {
          body.replaceChildren(View.error(resp.error, { onRetry: () => run(true) }));
          return;
        }
        body.replaceChildren(View.render(resp.result, {
          cached: !!resp.result._cached,
          onRecheck: () => run(true),
        }));
      });
    };
    run(false);
  };

  const isEditableTarget = (node) => {
    const el = node instanceof Element ? node : node?.parentElement;
    if (!el) return false;
    return !!el.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']");
  };

  document.addEventListener("mouseup", (e) => {
    // Ignore interactions with our own UI
    if (host && e.composedPath?.().includes(host)) return;

    // Defer so the selection reflects this mouseup
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel?.toString().trim() || "";
      removeEl("bubble");
      clearTimeout(bubbleTimer);

      if (
        text.length < MIN_CLAIM_LEN ||
        text.length > MAX_CLAIM_LEN ||
        !sel.rangeCount ||
        isEditableTarget(sel.anchorNode)
      ) {
        return;
      }

      const rect = sel.getRangeAt(0).getBoundingClientRect();
      if (!rect.width && !rect.height) return;

      const root = ensureShadowHost();
      const bubble = document.createElement("button");
      bubble.type = "button";
      bubble.className = "bubble";
      bubble.appendChild(logo(18));
      bubble.appendChild(document.createTextNode("Check"));
      bubble.style.top = `${window.scrollY + rect.top - 38}px`;
      bubble.style.left = `${clampLeft(window.scrollX + rect.left, 100)}px`;
      bubble.onclick = () => showPanel(text, rect);
      root.appendChild(bubble);

      bubbleTimer = setTimeout(() => removeEl("bubble"), BUBBLE_TIMEOUT_MS);
    }, 0);
  });

  document.addEventListener("mousedown", (e) => {
    if (host && e.composedPath?.().includes(host)) return;
    removeEl("bubble");
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && shadow && shadow.querySelector(".panel")) closePanel();
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "showPanel" && message.claim) {
      const sel = window.getSelection();
      const rect = sel?.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;
      showPanel(message.claim, rect && (rect.width || rect.height) ? rect : null);
    }
  });
})();
