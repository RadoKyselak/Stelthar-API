/* Project Mirador web page: check a claim, or find a number. */
(function () {
  "use strict";
  const V = window.MiradorView;

  // The card's styles, without a <style> tag: the page's security policy
  // allows no inline styles, and a constructed stylesheet is not one.
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(V.css);
    document.adoptedStyleSheets = [sheet, ...document.adoptedStyleSheets];
  } catch (e) { /* a very old browser: the card shows unstyled */ }

  const $ = (id) => document.getElementById(id);
  const form = $("form");
  const q = $("q");
  const go = $("go");
  const out = $("out");
  const MODES = {
    check: { tab: $("mode-check"), label: "Claim to check", button: "Check",
      placeholder: "Unemployment in Texas was 4.1% in 2023" },
    find: { tab: $("mode-find"), label: "Number to find", button: "Find",
      placeholder: "unemployment rate texas 2023" },
  };
  let mode = "check";
  let ticket = 0;
  let debounce = null;

  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) {
      const detail = data && (typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : "");
      throw new Error(r.status === 429 ? "Too many checks from this connection. Try again in a minute."
        : detail || `The service answered with an error (${r.status}).`);
    }
    return data;
  }

  function setMode(next, { keepText = false } = {}) {
    mode = next;
    Object.entries(MODES).forEach(([k, m]) => m.tab.setAttribute("aria-selected", String(k === next)));
    q.placeholder = MODES[next].placeholder;
    $("q-label").textContent = MODES[next].label;
    go.setAttribute("aria-label", MODES[next].button);
    if (!keepText) { q.value = ""; out.replaceChildren(); }
    syncButton();
    q.focus();
  }

  function syncButton() { go.disabled = q.value.trim().length < 2; }

  function remember(text) {
    const url = new URL(location.href);
    url.search = "";
    url.searchParams.set(mode === "check" ? "claim" : "find", text);
    history.replaceState(null, "", url);
  }

  async function check(text) {
    text = (text || "").trim();
    if (!text) return;
    const mine = ++ticket;
    go.disabled = true;
    out.replaceChildren(V.loading());
    try {
      const result = await postJSON("/v2/verify", { claim: text });
      if (mine !== ticket) return;
      out.replaceChildren(V.render(result));
      remember(text);
    } catch (e) {
      if (mine === ticket) out.replaceChildren(V.error(e.message, { onRetry: () => check(text) }));
    } finally {
      if (mine === ticket) syncButton();
    }
  }

  async function find(text) {
    text = (text || "").trim();
    const mine = ++ticket;
    if (text.length < 2) { out.replaceChildren(); return; }
    try {
      const r = await postJSON("/v2/lookup", { query: text.slice(0, 200) });
      if (mine !== ticket) return;
      out.replaceChildren(V.renderLookup(r, {
        onSuggest: (s) => { q.value = s; syncButton(); find(s); },
      }));
      if (r.ok) remember(text);
    } catch (e) {
      if (mine === ticket) out.replaceChildren(V.error(e.message, { onRetry: () => find(text) }));
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (mode === "check") check(q.value); else find(q.value);
  });
  q.addEventListener("input", () => {
    syncButton();
    if (mode === "find") {
      clearTimeout(debounce);
      debounce = setTimeout(() => find(q.value), 300);
    }
  });
  MODES.check.tab.addEventListener("click", () => mode !== "check" && setMode("check"));
  MODES.find.tab.addEventListener("click", () => mode !== "find" && setMode("find"));

  // Deep links: ?claim=… or ?find=… fill the box but never run on their own.
  // A shared link must not make every visitor's browser send a check (each
  // could cost a paid search); the visitor presses the button.
  const params = new URL(location.href).searchParams;
  if (params.get("find")) {
    setMode("find", { keepText: true });
    q.value = params.get("find").slice(0, 200);
  } else if (params.get("claim")) {
    q.value = params.get("claim").slice(0, 1000);
  }
  syncButton();
})();
