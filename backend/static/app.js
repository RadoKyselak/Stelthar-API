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

  const cardOptions = {
    // The full check for one claim of a passage, asked for by the reader.
    onDeeper: (claim) => postJSON("/v2/verify", { claim }),
    onEdit: () => { q.focus(); q.select(); },
  };

  async function check(text) {
    text = (text || "").trim();
    if (!text) return;
    const mine = ++ticket;
    go.disabled = true;
    out.replaceChildren(V.loading());
    try {
      // One claim or a whole passage: each claim in it is answered on its own.
      const result = await postJSON("/v2/check", V.isLink(text) ? { url: text } : { text });
      if (mine !== ticket) return;
      out.replaceChildren(V.renderPassage(result, cardOptions));
      remember(text);
    } catch (e) {
      if (mine === ticket) out.replaceChildren(V.error(e.message, { onRetry: () => check(text) }));
    } finally {
      if (mine === ticket) syncButton();
    }
  }

  // deep: an explicit search (Enter or Find). Only then may a miss go to
  // Claude, which costs a little; never on a keystroke.
  async function find(text, deep) {
    text = (text || "").trim();
    const mine = ++ticket;
    if (text.length < 2) { out.replaceChildren(); return; }
    if (deep) out.replaceChildren(V.loading());
    try {
      const r = await postJSON("/v2/lookup", { query: text.slice(0, 200), deep: !!deep });
      if (mine !== ticket) return;
      out.replaceChildren(V.renderLookup(r, {
        onSuggest: (s) => { q.value = s; syncButton(); find(s, true); },
      }));
      if (r.ok) remember(text);
    } catch (e) {
      if (mine === ticket) out.replaceChildren(V.error(e.message, { onRetry: () => find(text, deep) }));
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    clearTimeout(debounce); // a pending keystroke search must not replace this one
    if (mode === "check") check(q.value); else find(q.value, true);
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

  // Claims to try, each answered from an agency's own data. The last is a
  // passage, to show that every claim in it gets its own answer.
  const TRIES = V.examples.concat([
    "Inflation hit 9.1% in June 2022, and gas averaged $7 a gallon in June 2022. Real GDP grew 2.5% in 2023.",
  ]);
  const tries = $("tries");
  if (tries) {
    tries.replaceChildren(...TRIES.map((text) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = text;
      b.addEventListener("click", () => {
        if (mode !== "check") setMode("check");
        q.value = text;
        syncButton();
        window.scrollTo({ top: 0, behavior: "smooth" });
        check(text);
      });
      li.appendChild(b);
      return li;
    }));
  }

  // Deep links: ?claim=… or ?find=… fill the box but never run on their own.
  // A shared link must not make every visitor's browser send a check (each
  // could cost a paid search); the visitor presses the button.
  const params = new URL(location.href).searchParams;
  if (params.get("find")) {
    setMode("find", { keepText: true });
    q.value = params.get("find").slice(0, 200);
  } else if (params.get("claim")) {
    q.value = params.get("claim").slice(0, 8000);
  }
  // A shared check (/c?claim=…): the server already ran it on free data and
  // put the answer in the page, so it shows at once and nothing is sent.
  const shared = $("shared");
  if (shared) {
    try {
      out.replaceChildren(V.render(JSON.parse(shared.textContent), cardOptions));
    } catch (e) { /* the claim is in the box; the visitor can press Check */ }
  }
  syncButton();
})();
