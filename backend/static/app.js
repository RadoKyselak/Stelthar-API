/* Mirador web page: one box. A sentence with a figure is checked; a measure
   on its own ("unemployment rate texas 2023") is looked up. */
(function () {
  "use strict";
  const V = window.MiradorView;

  // The card's styles, without a <style> tag: the page's security policy
  // allows no inline styles, and a constructed stylesheet is not one.
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(V.css);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch (e) { /* a very old browser: the card shows unstyled */ }

  const $ = (id) => document.getElementById(id);
  const form = $("form");
  const q = $("q");
  const go = $("go");
  const out = $("out");
  let ticket = 0;

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
      throw new Error(r.status === 429 ? "Too many lookups from this connection. Try again in a minute."
        : detail || `The service answered with an error (${r.status}).`);
    }
    return data;
  }

  function syncButton() { go.disabled = q.value.trim().length < 2; }

  function remember(text) {
    const url = new URL(location.href);
    url.search = "";
    url.searchParams.set("q", text);
    history.replaceState(null, "", url);
  }

  async function run(text) {
    text = (text || "").trim();
    if (text.length < 2) return;
    const mine = ++ticket;
    const check = V.intentOf(text) === "check";
    go.disabled = true;
    out.replaceChildren(V.loading());
    try {
      const result = check ? await postJSON("/v2/verify", { claim: text })
        : await postJSON("/v2/lookup", { query: text.slice(0, 200) });
      if (mine !== ticket) return;
      out.replaceChildren(check ? V.render(result) : V.renderLookup(result, {
        onSuggest: (s) => { q.value = s; syncButton(); run(s); },
      }));
      remember(text);
    } catch (e) {
      if (mine === ticket) out.replaceChildren(V.error(e.message, { onRetry: () => run(text) }));
    } finally {
      if (mine === ticket) syncButton();
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); run(q.value); });
  q.addEventListener("input", syncButton);

  // Deep links (?q=…) fill the box but never run on their own. A shared link
  // must not make every visitor's browser send a check (each could cost a
  // paid search); the visitor presses the button.
  const params = new URL(location.href).searchParams;
  const linked = params.get("q") || params.get("claim") || params.get("find") || "";
  q.value = linked.slice(0, 1000);
  syncButton();
})();
