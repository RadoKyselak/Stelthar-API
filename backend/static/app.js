/* Project Mirador web page: check a claim, look up a figure, show coverage. */
(function () {
  "use strict";
  const V = window.MiradorView;
  const AI_CHAR_LIMIT = 300;

  const style = document.createElement("style");
  style.textContent = V.css;
  document.head.appendChild(style);

  const $ = (id) => document.getElementById(id);
  const form = $("check-form");
  const input = $("claim");
  const btn = $("check-btn");
  const count = $("count");
  const out = $("result");

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  async function postJSON(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* non-JSON error page */ }
    if (!r.ok) {
      const detail = data && (typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : "");
      throw new Error(r.status === 429
        ? "Too many checks from this connection. Please wait a moment."
        : detail || `The service answered with an error (${r.status}).`);
    }
    return data;
  }

  // ------------------------------------------------------------------ check
  function updateCount() {
    const n = input.value.trim().length;
    count.textContent = n ? `${n} / ${AI_CHAR_LIMIT}` : "";
    count.classList.toggle("over", n > AI_CHAR_LIMIT);
    btn.disabled = n === 0;
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
  }

  let running = 0;
  async function check(claim) {
    claim = (claim || "").trim();
    if (!claim) return;
    const ticket = ++running;
    btn.disabled = true;
    out.replaceChildren(V.loading());
    try {
      const result = await postJSON("/v2/verify", { claim });
      if (ticket !== running) return;
      const head = el("p", "result-claim");
      head.appendChild(document.createTextNode("Claim checked: "));
      head.appendChild(el("q", "", claim));
      const card = V.render(result, { onRecheck: () => check(claim) });
      card.prepend(head);
      out.replaceChildren(card);
      const url = new URL(location.href);
      url.searchParams.set("claim", claim);
      history.replaceState(null, "", url);
    } catch (e) {
      if (ticket !== running) return;
      out.replaceChildren(V.error(e.message, { onRetry: () => check(claim) }));
    } finally {
      if (ticket === running) updateCount();
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); check(input.value); });
  input.addEventListener("input", () => { updateCount(); autosize(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); check(input.value); }
  });
  document.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => {
    input.value = b.textContent;
    updateCount(); autosize();
    check(b.textContent);
  }));

  // ------------------------------------------------------------------ lookup
  const q = $("lookup-q");
  const lookOut = $("lookup-out");
  let debounce = null;
  let lookTicket = 0;

  function copyButton(label, getText) {
    const b = el("button", "lk-btn", label);
    b.type = "button";
    b.addEventListener("click", async () => {
      const ok = await V.copyText(getText());
      b.textContent = ok ? "Copied" : "Copy failed";
      b.classList.toggle("done", ok);
      setTimeout(() => { b.textContent = label; b.classList.remove("done"); }, 1400);
    });
    return b;
  }

  function showLookup(r) {
    if (!r.ok) {
      const box = el("div", "lk-miss", r.headline || "No official series matches that yet.");
      const s = el("div", "lk-sugg");
      (r.suggestions || []).slice(0, 6).forEach((x) => {
        const b = el("button", "ex", x.example);
        b.type = "button";
        b.addEventListener("click", () => { q.value = x.example; runLookup(x.example); });
        s.appendChild(b);
      });
      if (s.childNodes.length) box.appendChild(s);
      lookOut.replaceChildren(box);
      return;
    }
    const c = r.citations || {};
    const box = el("div", "lk");
    box.appendChild(el("div", "lk-v", c.value || r.value));
    box.appendChild(el("div", "lk-a", [r.label, c.attribution].filter(Boolean).join(" · ")));
    if (c.caveat) box.appendChild(el("div", "lk-c", `Basis: ${c.caveat}`));
    box.appendChild(el("div", "lk-cite", c.note || c.inline || ""));
    const row = el("div", "lk-row");
    row.appendChild(copyButton("Copy citation", () => c.note || c.inline || ""));
    row.appendChild(copyButton("Copy inline", () => c.inline || ""));
    row.appendChild(copyButton("Copy Markdown", () => c.markdown || c.inline || ""));
    const url = V.safeUrl(c.url);
    if (url) {
      const a = el("a", "lk-btn", "Open source ↗");
      a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
      row.appendChild(a);
    }
    box.appendChild(row);
    lookOut.replaceChildren(box);
  }

  async function runLookup(query) {
    query = (query || "").trim();
    const ticket = ++lookTicket;
    if (query.length < 2) { lookOut.replaceChildren(); return; }
    try {
      const r = await postJSON("/v2/lookup", { query });
      if (ticket === lookTicket) showLookup(r);
    } catch (e) {
      if (ticket === lookTicket) lookOut.replaceChildren(el("div", "lk-miss", e.message));
    }
  }

  q.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => runLookup(q.value), 280);
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { clearTimeout(debounce); runLookup(q.value); }
  });

  // ---------------------------------------------------------------- coverage
  async function loadCoverage() {
    const grid = $("coverage-grid");
    try {
      const r = await fetch("/v2/coverage").then((x) => x.json());
      grid.replaceChildren(...r.agencies.map((a) => {
        const card = el("div", "cov");
        card.appendChild(el("h3", "", a.agency));
        const ul = el("ul");
        a.metrics.forEach((m) => {
          const li = el("li", "", m.label);
          const extra = [m.places];
          if (m.groups && m.groups.length) extra.push(`also by group: ${m.groups.join(", ")}`);
          li.appendChild(el("small", "", extra.join(" · ")));
          ul.appendChild(li);
        });
        card.appendChild(ul);
        return card;
      }));
      $("coverage-sub").textContent =
        `${r.metric_count} measures, answered with the agency's own published figure and no AI. ` +
        "Anything else is checked by searching published sources, official ones first.";
    } catch (e) {
      grid.replaceChildren(el("p", "sub", "Coverage could not be loaded."));
    }
  }

  // ------------------------------------------------------------------ start
  updateCount();
  loadCoverage();
  const initial = new URL(location.href).searchParams.get("claim");
  if (initial) {
    input.value = initial.slice(0, 1000);
    updateCount(); autosize();
    check(initial);
  }
})();
