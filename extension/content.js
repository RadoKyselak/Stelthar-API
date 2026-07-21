(() => {
  const MIN_CLAIM_LEN = 12;
  const MAX_CLAIM_LEN = 600;
  const BUBBLE_TIMEOUT_MS = 6000;

  let host = null;
  let shadow = null;
  let bubbleTimer = null;

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
    style.textContent = `
      * { box-sizing: border-box; font-family: 'Courier New', Courier, monospace; }
      .bubble {
        position: absolute; background: #111; color: #fff; padding: 6px 10px;
        border-radius: 6px; font-size: 12px; cursor: pointer; white-space: nowrap;
        box-shadow: 0 2px 8px rgba(0,0,0,0.35); user-select: none;
      }
      .bubble:hover { background: #333; }
      .panel {
        position: absolute; width: 340px; max-width: calc(100vw - 24px);
        background: #fdfaf3; color: #1a1a1a; border: 2px solid #1a1a1a;
        border-radius: 6px; padding: 12px; font-size: 13px; line-height: 1.45;
        box-shadow: 0 4px 16px rgba(0,0,0,0.25);
      }
      .panel-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
      .panel-title { font-weight: bold; font-size: 13px; }
      .panel-close { cursor: pointer; border: 1px solid #1a1a1a; background: #f0eadd; padding: 0 6px; font-size: 13px; line-height: 18px; border-radius: 3px; }
      .panel-claim { color: #555; font-style: italic; font-size: 12px; margin-bottom: 8px; max-height: 60px; overflow: hidden; }
      .verdict { font-weight: bold; font-size: 15px; margin-bottom: 2px; }
      .verdict.supported { color: #008000; }
      .verdict.contradicted { color: #CC0000; }
      .verdict.inconclusive { color: #B8860B; }
      .confidence { color: #666; font-size: 12px; margin-bottom: 6px; }
      .summary { margin-bottom: 8px; }
      .links { list-style: none; padding: 0; margin: 0; }
      .links li { margin-bottom: 4px; }
      .links a { color: #0000EE; font-size: 12px; text-decoration: none; word-break: break-word; }
      .links a:hover { text-decoration: underline; }
      .loading { color: #555; }
      .error { color: #CC0000; font-weight: bold; }
      .cached-note { color: #888; font-size: 11px; margin-top: 6px; }
    `;
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

  const formatConfidence = (conf) => {
    if (conf === null || conf === undefined) return "N/A";
    const num = typeof conf === "string" ? Number(conf.replace("%", "").replace(",", "")) : conf;
    if (Number.isNaN(num)) return String(conf);
    return `${Math.round(num > 0 && num <= 1 ? num * 100 : num)}%`;
  };

  const showPanel = (claim, anchorRect) => {
    const root = ensureShadowHost();
    removeEl("bubble");
    removeEl("panel");

    const panel = document.createElement("div");
    panel.className = "panel";

    const top = anchorRect
      ? window.scrollY + anchorRect.bottom + 8
      : window.scrollY + 80;
    const left = anchorRect
      ? clampLeft(window.scrollX + anchorRect.left, 340)
      : clampLeft(window.scrollX + 80, 340);
    panel.style.top = `${top}px`;
    panel.style.left = `${left}px`;

    const header = document.createElement("div");
    header.className = "panel-header";
    const title = document.createElement("span");
    title.className = "panel-title";
    title.textContent = "Project Mirador";
    const close = document.createElement("button");
    close.className = "panel-close";
    close.textContent = "✕";
    close.onclick = () => panel.remove();
    header.appendChild(title);
    header.appendChild(close);
    panel.appendChild(header);

    const claimEl = document.createElement("div");
    claimEl.className = "panel-claim";
    claimEl.textContent = `"${claim}"`;
    panel.appendChild(claimEl);

    const body = document.createElement("div");
    const loading = document.createElement("div");
    loading.className = "loading";
    loading.textContent = "Checking against official government data…";
    body.appendChild(loading);
    panel.appendChild(body);

    root.appendChild(panel);

    chrome.runtime.sendMessage({ type: "verifyClaim", claim }, (resp) => {
      body.textContent = "";
      if (chrome.runtime.lastError || !resp) {
        const err = document.createElement("div");
        err.className = "error";
        err.textContent = "Verification unavailable. Try the extension popup.";
        body.appendChild(err);
        return;
      }
      if (!resp.ok) {
        const err = document.createElement("div");
        err.className = "error";
        err.textContent = resp.error;
        body.appendChild(err);
        return;
      }

      const r = resp.result;
      const verdictRaw = (r.verdict || "Inconclusive").toLowerCase();
      const verdict = document.createElement("div");
      verdict.className = `verdict ${verdictRaw}`;
      verdict.textContent = `VERDICT: ${verdictRaw.toUpperCase()}`;
      body.appendChild(verdict);

      const conf = document.createElement("div");
      conf.className = "confidence";
      const tier = r.confidence_tier ? ` (${r.confidence_tier})` : "";
      conf.textContent = `Confidence: ${formatConfidence(r.confidence)}${tier}`;
      body.appendChild(conf);

      if (r.summary) {
        const summary = document.createElement("div");
        summary.className = "summary";
        summary.textContent = r.summary;
        body.appendChild(summary);
      }

      if (r.evidence_links?.length) {
        const ul = document.createElement("ul");
        ul.className = "links";
        r.evidence_links.slice(0, 5).forEach((link) => {
          const li = document.createElement("li");
          const a = document.createElement("a");
          a.href = link.source_url;
          a.textContent = `→ ${link.finding}`;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          li.appendChild(a);
          ul.appendChild(li);
        });
        body.appendChild(ul);
      }

      if (r._cached) {
        const note = document.createElement("div");
        note.className = "cached-note";
        note.textContent = "Cached result (checked within the last 12h).";
        body.appendChild(note);
      }
    });
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
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = "✓ Verify with Project Mirador";
      bubble.style.top = `${window.scrollY + rect.top - 34}px`;
      bubble.style.left = `${clampLeft(window.scrollX + rect.left, 210)}px`;
      bubble.onclick = () => showPanel(text, rect);
      root.appendChild(bubble);

      bubbleTimer = setTimeout(() => removeEl("bubble"), BUBBLE_TIMEOUT_MS);
    }, 0);
  });

  document.addEventListener("mousedown", (e) => {
    if (host && e.composedPath?.().includes(host)) return;
    removeEl("bubble");
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "showPanel" && message.claim) {
      const sel = window.getSelection();
      const rect = sel?.rangeCount ? sel.getRangeAt(0).getBoundingClientRect() : null;
      showPanel(message.claim, rect && (rect.width || rect.height) ? rect : null);
    }
  });
})();
