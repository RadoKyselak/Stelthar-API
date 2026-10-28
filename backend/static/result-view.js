/*
 * Project Mirador: the result view.
 *
 * One renderer for every surface: the in-page panel, the extension popup and
 * the web page. It draws the API's `display` block, so wording and layout stay
 * identical everywhere.
 *
 * Safety: results contain text from claims and from web pages, so every
 * string goes in through textContent, never innerHTML, and only http(s)
 * links are ever made clickable.
 *
 * This file is copied to backend/static/result-view.js for the web page; a
 * test fails if the two copies differ.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --mv-card:#ffffff; --mv-sub:#f6f5f2; --mv-ink:#15181d; --mv-muted:#5a6170;
  --mv-faint:#868c98; --mv-line:#e3e1db; --mv-link:#1d4ed8; --mv-focus:#2563eb;
  --mv-fg:#4b5563; --mv-bg:#f1f2f4; --mv-bd:#d7dbe0;
  font: 14px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  color: var(--mv-ink); -webkit-font-smoothing: antialiased; text-align: left; }
.mv *, .mv *::before, .mv *::after { box-sizing: border-box; }
.mv[data-verdict="supported"], .mv [data-verdict="supported"] { --mv-fg:#146c2e; --mv-bg:#e8f5ec; --mv-bd:#b5dcc0; }
.mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --mv-fg:#4f6500; --mv-bg:#f2f6e1; --mv-bd:#d4e2a2; }
.mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --mv-fg:#b42318; --mv-bg:#fdedeb; --mv-bd:#f3c2bc; }
.mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --mv-fg:#9a4a00; --mv-bg:#fff3e3; --mv-bd:#f3d0a1; }
@media (prefers-color-scheme: dark) {
  .mv { --mv-card:#17191e; --mv-sub:#1f2228; --mv-ink:#eceef1; --mv-muted:#a6adb9;
    --mv-faint:#7d8491; --mv-line:#2d3139; --mv-link:#8ab4ff; --mv-focus:#8ab4ff;
    --mv-fg:#c3c9d3; --mv-bg:#262a31; --mv-bd:#3a3f48; }
  .mv[data-verdict="supported"], .mv [data-verdict="supported"] { --mv-fg:#6fd18b; --mv-bg:#14281b; --mv-bd:#24502f; }
  .mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --mv-fg:#c2d66b; --mv-bg:#22280f; --mv-bd:#45501d; }
  .mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --mv-fg:#ff8a7d; --mv-bg:#2f1715; --mv-bd:#5c2a25; }
  .mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --mv-fg:#ffb766; --mv-bg:#2d1f0e; --mv-bd:#5a3d17; }
}
.mv a { color: var(--mv-link); text-decoration: none; }
.mv a:hover { text-decoration: underline; }
.mv a:focus-visible, .mv button:focus-visible { outline: 2px solid var(--mv-focus); outline-offset: 2px; }
.mv-card { background: var(--mv-card); }
.mv-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.mv-badge { display:inline-flex; align-items:center; gap:7px; padding:5px 11px 5px 8px;
  border-radius:999px; background:var(--mv-bg); color:var(--mv-fg); border:1px solid var(--mv-bd);
  font-weight:650; font-size:15px; letter-spacing:-0.01em; }
.mv-badge svg { width:18px; height:18px; flex:none; }
.mv[data-verdict="unavailable"] .mv-badge { border-style:dashed; }
.mv-conf { font-size:12px; color:var(--mv-muted); }
.mv-conf b { color:var(--mv-ink); font-weight:600; }
.mv-meaning { margin:8px 0 0; color:var(--mv-muted); font-size:13px; }
.mv-summary { margin:10px 0 0; font-size:15px; line-height:1.45; font-weight:500; }
.mv-points { margin-top:14px; display:grid; gap:10px; }
.mv-point { border:1px solid var(--mv-line); border-radius:10px; overflow:hidden; }
.mv-point-label { padding:6px 12px; font-size:11px; font-weight:650; letter-spacing:.06em;
  text-transform:uppercase; color:var(--mv-muted); background:var(--mv-sub); border-bottom:1px solid var(--mv-line); }
.mv-cells { display:grid; grid-template-columns:1fr 1fr; }
.mv-cell { padding:10px 12px; min-width:0; }
.mv-cell + .mv-cell { border-left:1px solid var(--mv-line); }
.mv-k { font-size:11px; font-weight:650; letter-spacing:.06em; text-transform:uppercase; color:var(--mv-faint); }
.mv-v { margin-top:2px; font-size:22px; font-weight:680; letter-spacing:-0.02em;
  font-variant-numeric: tabular-nums; overflow-wrap:anywhere; line-height:1.2; }
.mv-cell.mv-official .mv-v { color: var(--mv-ink); }
.mv-cell.mv-claim .mv-v { color: var(--mv-muted); }
.mv-ctx { margin-top:3px; font-size:12px; color:var(--mv-muted); overflow-wrap:anywhere; }
.mv-note { padding:7px 12px; font-size:12.5px; font-weight:600; color:var(--mv-fg);
  background:var(--mv-bg); border-top:1px solid var(--mv-line); }
.mv-reported { margin-top:14px; padding:10px 12px; border:1px solid var(--mv-line); border-radius:10px; }
.mv-reported .mv-v { font-size:15px; font-weight:600; letter-spacing:0; }
.mv-callout { margin-top:12px; padding:10px 12px; border-radius:10px; background:var(--mv-sub);
  font-size:13px; color:var(--mv-ink); }
.mv-callout b { display:block; font-size:12px; color:var(--mv-muted); font-weight:650; margin-bottom:2px; }
.mv-notes { list-style:none; margin:12px 0 0; padding:0; display:grid; gap:6px; }
.mv-notes li { display:flex; gap:8px; font-size:12.5px; color:var(--mv-muted); line-height:1.45; }
.mv-notes svg { width:15px; height:15px; flex:none; margin-top:2px; color:var(--mv-faint); }
.mv-h { margin:16px 0 6px; font-size:11px; font-weight:650; letter-spacing:.06em;
  text-transform:uppercase; color:var(--mv-faint); }
.mv-sources { display:grid; gap:8px; }
.mv-src { display:block; padding:10px 12px; border:1px solid var(--mv-line); border-radius:10px;
  color:inherit !important; text-decoration:none !important; transition:border-color .15s, background .15s; }
.mv-src:hover { border-color: var(--mv-faint); background: var(--mv-sub); }
.mv-src-top { display:flex; align-items:baseline; gap:8px; justify-content:space-between; }
.mv-src-name { font-weight:620; font-size:13.5px; overflow-wrap:anywhere; }
.mv-tag { flex:none; font-size:10.5px; font-weight:650; letter-spacing:.03em; padding:1px 7px;
  border-radius:999px; border:1px solid var(--mv-line); color:var(--mv-muted); white-space:nowrap; }
.mv-tag.mv-official { color:#146c2e; border-color:#b5dcc0; background:#e8f5ec; }
@media (prefers-color-scheme: dark) { .mv-tag.mv-official { color:#6fd18b; border-color:#24502f; background:#14281b; } }
.mv-src-line { margin-top:2px; font-size:12.5px; color:var(--mv-muted); overflow-wrap:anywhere; }
.mv-src-line code { font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color:var(--mv-ink); }
.mv-src-open { margin-top:4px; font-size:12.5px; color:var(--mv-link); font-weight:550; }
.mv-method { margin-top:16px; padding-top:12px; border-top:1px solid var(--mv-line);
  display:flex; gap:10px; font-size:12.5px; color:var(--mv-muted); }
.mv-method svg { width:18px; height:18px; flex:none; color:var(--mv-fg); margin-top:1px; }
.mv[data-verdict] .mv-method svg { color: var(--mv-muted); }
.mv-method b { color:var(--mv-ink); font-weight:620; }
.mv-method p { margin:2px 0 0; }
.mv-chips { display:inline-flex; gap:6px; flex-wrap:wrap; margin-left:4px; }
.mv-chip { font-size:11px; padding:0 7px; border-radius:999px; background:var(--mv-sub);
  border:1px solid var(--mv-line); color:var(--mv-muted); white-space:nowrap; }
.mv-actions { margin-top:14px; display:flex; gap:8px; flex-wrap:wrap; }
.mv-btn { font: inherit; font-size:13px; font-weight:600; padding:7px 12px; border-radius:8px;
  border:1px solid var(--mv-line); background:var(--mv-card); color:var(--mv-ink); cursor:pointer;
  display:inline-flex; align-items:center; gap:6px; }
.mv-btn:hover { background: var(--mv-sub); }
.mv-btn svg { width:15px; height:15px; }
.mv-btn.mv-done { color:#146c2e; border-color:#b5dcc0; }
.mv-meta { margin-top:10px; font-size:11.5px; color:var(--mv-faint); }
.mv-loading { padding:4px 0; }
.mv-bar { height:3px; border-radius:3px; background:var(--mv-sub); overflow:hidden; margin-top:12px; }
.mv-bar i { display:block; height:100%; width:35%; background:var(--mv-focus); border-radius:3px;
  animation: mv-slide 1.1s ease-in-out infinite; }
@keyframes mv-slide { 0% { transform:translateX(-110%); } 100% { transform:translateX(320%); } }
@media (prefers-reduced-motion: reduce) { .mv-bar i { animation:none; width:100%; opacity:.4; } }
.mv-loading-t { font-weight:600; }
.mv-loading-s { margin-top:4px; color:var(--mv-muted); font-size:13px; min-height:1.5em; }
.mv-error { color: var(--mv-ink); }
.mv-error .mv-badge { --mv-fg:#b42318; --mv-bg:#fdedeb; --mv-bd:#f3c2bc; }
`;

  // 24px icon paths (stroke icons).
  const ICONS = {
    "supported": "M5 12.5l4.2 4.2L19 7",
    "mostly-accurate": "M5 9.5c2.3-2 4.7-2 7 0s4.7 2 7 0M5 15.5c2.3-2 4.7-2 7 0s4.7 2 7 0",
    "contradicted": "M6.5 6.5l11 11M17.5 6.5l-11 11",
    "misleading": "M12 7v6.5M12 17.2v.1",
    "no-verdict": "M8 12h8",
    "unavailable": "M12 8v5M12 16.5v.1M10.3 3.9L2.6 17.5A2 2 0 004.3 20.5h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
    "info": "M12 11v5.5M12 7.6v.1M12 21a9 9 0 100-18 9 9 0 000 18z",
    "data": "M4 19h16M7 16V10M12 16V5M17 16v-4",
    "search": "M10.5 18a7.5 7.5 0 100-15 7.5 7.5 0 000 15zM21 21l-5.2-5.2",
    "copy": "M9 9h10v10H9zM5 15V5h10",
    "refresh": "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
    "check": "M5 12.5l4.2 4.2L19 7",
  };
  const RING = new Set(["supported", "mostly-accurate", "contradicted", "misleading", "no-verdict"]);

  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", RING.has(name) ? "2.4" : "1.9");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("aria-hidden", "true");
    if (RING.has(name)) {
      // Verdict icons sit in a filled disc, so they read at a glance.
      const c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("cx", "12"); c.setAttribute("cy", "12"); c.setAttribute("r", "11");
      c.setAttribute("fill", "currentColor"); c.setAttribute("stroke", "none");
      s.appendChild(c);
    }
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", ICONS[name] || ICONS.info);
    if (RING.has(name)) {
      p.style.stroke = "var(--mv-card, #fff)";
      p.setAttribute("transform", "translate(3.6 3.6) scale(0.7)");
    }
    s.appendChild(p);
    return s;
  }

  /** The Mirador mark: bars over a dark tile, green dot for "checked". */
  function logo(size) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 32 32");
    s.setAttribute("width", String(size || 22));
    s.setAttribute("height", String(size || 22));
    s.setAttribute("aria-hidden", "true");
    const r = document.createElementNS(SVGNS, "rect");
    r.setAttribute("width", "32"); r.setAttribute("height", "32"); r.setAttribute("rx", "8");
    r.setAttribute("fill", "#15181d");
    s.appendChild(r);
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", "M8 22V12M13.3 22v-7M18.7 22V9M24 22v-4");
    p.setAttribute("stroke", "#fff"); p.setAttribute("stroke-width", "2.6");
    p.setAttribute("stroke-linecap", "round");
    s.appendChild(p);
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", "24"); c.setAttribute("cy", "10"); c.setAttribute("r", "3.2");
    c.setAttribute("fill", "#3fbf6a");
    s.appendChild(c);
    return s;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null && text !== "") e.textContent = String(text);
    return e;
  }

  function safeUrl(u) {
    try {
      const url = new URL(String(u || ""));
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (e) {
      return "";
    }
  }

  function link(href, cls, text) {
    const url = safeUrl(href);
    const a = el(url ? "a" : "div", cls, text);
    if (url) {
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    return a;
  }

  // Older API responses carry no `display`; build the minimum from v1 fields.
  function displayOf(r) {
    if (r && r.display && r.display.verdict) return r.display;
    const label = (r && (r.verdict_detail || r.verdict)) || "No verdict";
    const key = r && r.degraded ? "unavailable" : label.toLowerCase().replace(/\s+/g, "-");
    return {
      status: r && r.degraded ? "unavailable" : "ok",
      verdict: { key, label: r && r.degraded ? "Couldn't check" : label, meaning: "" },
      summary: (r && (r.headline || r.summary)) || "",
      confidence: { tier: (r && r.confidence_tier) || null, text: "" },
      reason: null, comparisons: [], reported_value: "", notes: [],
      sources: ((r && r.evidence_links) || []).map((l) => ({
        kind: "web", official: true, title: l.finding, domain: "", url: l.source_url,
      })),
      method: { kind: "none", title: "", text: "", model_calls: 0, elapsed_seconds: 0 },
      share_text: (r && r.summary) || "",
    };
  }

  function confidenceEl(d) {
    if (!d.confidence || !d.confidence.tier || d.verdict.key === "no-verdict" ||
        d.verdict.key === "unavailable") return null;
    const c = el("span", "mv-conf");
    c.appendChild(el("b", "", `${d.confidence.tier} confidence`));
    if (d.confidence.text) c.title = d.confidence.text;
    return c;
  }

  function pointEl(p, multi) {
    const box = el("div", "mv-point");
    if (p.verdict_key) box.dataset.verdict = p.verdict_key;
    if (multi && p.label) box.appendChild(el("div", "mv-point-label", p.label));
    const cells = el("div", "mv-cells");
    if (p.claim) {
      const a = el("div", "mv-cell mv-claim");
      a.appendChild(el("div", "mv-k", "Claim"));
      a.appendChild(el("div", "mv-v", p.claim));
      cells.appendChild(a);
    }
    const b = el("div", "mv-cell mv-official");
    b.appendChild(el("div", "mv-k", p.source ? `Official · ${p.source}` : "Official"));
    b.appendChild(el("div", "mv-v", p.official));
    if (p.context) b.appendChild(el("div", "mv-ctx", p.context));
    cells.appendChild(b);
    if (!p.claim) cells.style.gridTemplateColumns = "1fr";
    box.appendChild(cells);
    if (p.note && p.claim) box.appendChild(el("div", "mv-note", p.note));
    return box;
  }

  function dataSourceEl(s) {
    const a = link(s.url, "mv-src");
    const top = el("div", "mv-src-top");
    top.appendChild(el("span", "mv-src-name", s.agency));
    top.appendChild(el("span", "mv-tag mv-official", "Official data"));
    a.appendChild(top);
    const what = [s.dataset, s.measure].filter(Boolean).join(" · ");
    if (what) a.appendChild(el("div", "mv-src-line", what));
    const meta = el("div", "mv-src-line");
    if (s.series_id) {
      const prefix = s.series_prefix === undefined ? "Series" : s.series_prefix;
      if (prefix) meta.appendChild(document.createTextNode(`${prefix} `));
      meta.appendChild(el("code", "", s.series_id));
    }
    const rest = [s.place, s.period, s.value].filter(Boolean).join(" · ");
    if (rest) meta.appendChild(document.createTextNode((s.series_id ? " · " : "") + rest));
    if (meta.childNodes.length) a.appendChild(meta);
    if (safeUrl(s.url)) a.appendChild(el("div", "mv-src-open", "Open the official data ↗"));
    return a;
  }

  function webSourceEl(s) {
    const a = link(s.url, "mv-src");
    const top = el("div", "mv-src-top");
    top.appendChild(el("span", "mv-src-name", s.title || s.domain));
    top.appendChild(el("span", s.official ? "mv-tag mv-official" : "mv-tag",
      s.official ? "Official" : "Other source"));
    a.appendChild(top);
    if (s.domain) a.appendChild(el("div", "mv-src-line", s.domain));
    return a;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      try {
        const t = document.createElement("textarea");
        t.value = text;
        t.style.position = "fixed";
        t.style.opacity = "0";
        document.body.appendChild(t);
        t.select();
        const ok = document.execCommand("copy");
        t.remove();
        return ok;
      } catch (e2) {
        return false;
      }
    }
  }

  function button(label, iconName, onClick) {
    const b = el("button", "mv-btn");
    b.type = "button";
    b.appendChild(icon(iconName));
    b.appendChild(document.createTextNode(label));
    b.addEventListener("click", onClick);
    return b;
  }

  /**
   * Render a verification result.
   * opts: { onRecheck(): void, cached: bool, showSources: bool (default true) }
   */
  function render(result, opts) {
    opts = opts || {};
    const d = displayOf(result);
    const key = d.verdict.key || "no-verdict";
    const root = el("div", "mv mv-card");
    root.dataset.verdict = key;
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", `Verdict: ${d.verdict.label}`);

    const head = el("div", "mv-head");
    const badge = el("span", "mv-badge");
    badge.appendChild(icon(ICONS[key] ? key : "no-verdict"));
    badge.appendChild(document.createTextNode(d.verdict.label));
    head.appendChild(badge);
    const conf = confidenceEl(d);
    if (conf) head.appendChild(conf);
    root.appendChild(head);

    if (d.verdict.meaning) root.appendChild(el("p", "mv-meaning", d.verdict.meaning));
    if (d.summary) root.appendChild(el("p", "mv-summary", d.summary));

    const points = d.comparisons || [];
    if (points.length) {
      const wrap = el("div", "mv-points");
      points.forEach((p) => wrap.appendChild(pointEl(p, points.length > 1)));
      root.appendChild(wrap);
    }

    if (d.reported_value) {
      const r = el("div", "mv-reported");
      r.appendChild(el("div", "mv-k", "Reported official figure"));
      r.appendChild(el("div", "mv-v", d.reported_value));
      root.appendChild(r);
    }

    if (d.reason && (d.reason.next || d.reason.why)) {
      const c = el("div", "mv-callout");
      c.appendChild(el("b", "", d.reason.next ? "What you can try" : "Why"));
      c.appendChild(document.createTextNode(d.reason.next || d.reason.why));
      root.appendChild(c);
    }

    if (d.notes && d.notes.length) {
      const ul = el("ul", "mv-notes");
      d.notes.forEach((n) => {
        const li = el("li");
        li.appendChild(icon("info"));
        li.appendChild(el("span", "", n));
        ul.appendChild(li);
      });
      root.appendChild(ul);
    }

    const sources = (d.sources || []).filter((s) => s && (s.agency || s.title || s.domain));
    if (opts.showSources !== false && sources.length) {
      const data = sources.filter((s) => s.kind === "data");
      const web = sources.filter((s) => s.kind !== "data");
      root.appendChild(el("div", "mv-h", data.length > 1 || web.length > 1 ? "Sources" : "Source"));
      const list = el("div", "mv-sources");
      data.forEach((s) => list.appendChild(dataSourceEl(s)));
      web.slice(0, 6).forEach((s) => list.appendChild(webSourceEl(s)));
      root.appendChild(list);
    }

    if (d.method && d.method.title) {
      const m = el("div", "mv-method");
      m.appendChild(icon(d.method.kind === "web_search" ? "search" : "data"));
      const body = el("div");
      const t = el("div");
      t.appendChild(el("b", "", d.method.title));
      const chips = el("span", "mv-chips");
      if (d.method.kind === "official_data") chips.appendChild(el("span", "mv-chip", "No AI"));
      if (d.method.kind === "web_search") chips.appendChild(el("span", "mv-chip", "AI search"));
      if (d.method.elapsed_seconds) chips.appendChild(el("span", "mv-chip", `${d.method.elapsed_seconds}s`));
      if (chips.childNodes.length) t.appendChild(chips);
      body.appendChild(t);
      if (d.method.text) body.appendChild(el("p", "", d.method.text));
      if (d.confidence && d.confidence.text && conf) {
        body.appendChild(el("p", "", `${d.confidence.tier} confidence: ${d.confidence.text}`));
      }
      m.appendChild(body);
      root.appendChild(m);
    }

    const actions = el("div", "mv-actions");
    if (d.share_text) {
      const b = button("Copy result", "copy", async () => {
        const ok = await copyText(d.share_text);
        b.lastChild.textContent = ok ? "Copied" : "Copy failed";
        b.classList.toggle("mv-done", ok);
        setTimeout(() => { b.lastChild.textContent = "Copy result"; b.classList.remove("mv-done"); }, 1600);
      });
      actions.appendChild(b);
    }
    if (typeof opts.onRecheck === "function") {
      actions.appendChild(button("Check again", "refresh", () => opts.onRecheck()));
    }
    if (actions.childNodes.length) root.appendChild(actions);
    if (opts.cached) root.appendChild(el("div", "mv-meta", "Saved result from the last 12 hours."));
    return root;
  }

  /** A loading state that says what is happening, honestly. */
  function loading() {
    const root = el("div", "mv mv-card mv-loading");
    root.setAttribute("role", "status");
    root.appendChild(el("div", "mv-loading-t", "Checking against official U.S. data…"));
    const s = el("div", "mv-loading-s", "Looking for the exact published figure.");
    root.appendChild(s);
    const bar = el("div", "mv-bar");
    bar.appendChild(el("i"));
    root.appendChild(bar);
    setTimeout(() => {
      if (root.isConnected) s.textContent = "Still working: searching published sources can take up to 15 seconds.";
    }, 2500);
    return root;
  }

  function error(message, opts) {
    opts = opts || {};
    const root = el("div", "mv mv-card mv-error");
    root.dataset.verdict = "unavailable";
    const head = el("div", "mv-head");
    const badge = el("span", "mv-badge");
    badge.appendChild(icon("unavailable"));
    badge.appendChild(document.createTextNode("Couldn't check"));
    head.appendChild(badge);
    root.appendChild(head);
    root.appendChild(el("p", "mv-summary", message || "Something went wrong."));
    if (typeof opts.onRetry === "function") {
      const a = el("div", "mv-actions");
      a.appendChild(button("Try again", "refresh", () => opts.onRetry()));
      root.appendChild(a);
    }
    return root;
  }

  /** Short label and key for a stored verdict string, for history lists. */
  function verdictKey(label) {
    return String(label || "No verdict").toLowerCase().replace(/\s+/g, "-");
  }

  global.MiradorView = { css: CSS, render, loading, error, icon, logo, safeUrl, copyText, verdictKey };
})(typeof window !== "undefined" ? window : this);
