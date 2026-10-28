/*
 * Project Mirador: the result view.
 *
 * One renderer for every surface: the in-page panel, the extension popup and
 * the web app. It draws the API's `display` block (for a claim) or a lookup
 * result (for a single figure), so wording and layout match everywhere.
 *
 * Safety: results contain text from claims and from web pages, so every
 * string goes in through textContent and only http(s) links are clickable.
 *
 * This file is copied to backend/static/result-view.js for the web app; a
 * test fails if the two copies differ.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --mv-card:#ffffff; --mv-ink:#121314; --mv-muted:#6b6f76; --mv-faint:#9a9ea5; --mv-line:#ebebe8;
  --mv-sub:#f6f6f4; --mv-link:#121314; --mv-focus:#2563eb;
  --mv-fg:#5b6068; --mv-bg:#f1f2f3;
  font: 14px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--mv-ink); -webkit-font-smoothing: antialiased; text-align: left; }
.mv *, .mv *::before, .mv *::after { box-sizing: border-box; }
.mv[data-verdict="supported"], .mv [data-verdict="supported"] { --mv-fg:#12813a; --mv-bg:#e9f6ee; }
.mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --mv-fg:#5f7700; --mv-bg:#f3f7e2; }
.mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --mv-fg:#c4281c; --mv-bg:#fdeeec; }
.mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --mv-fg:#b25600; --mv-bg:#fff3e4; }
@media (prefers-color-scheme: dark) {
  .mv { --mv-card:#161718; --mv-ink:#f1f1f0; --mv-muted:#a3a7ae; --mv-faint:#71757c; --mv-line:#2a2b2e;
    --mv-sub:#1e1f21; --mv-link:#f1f1f0; --mv-focus:#8ab4ff; --mv-fg:#b7bbc2; --mv-bg:#25272a; }
  .mv[data-verdict="supported"], .mv [data-verdict="supported"] { --mv-fg:#5fd38a; --mv-bg:#12261a; }
  .mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --mv-fg:#c6da6b; --mv-bg:#23280f; }
  .mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --mv-fg:#ff8378; --mv-bg:#2f1614; }
  .mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --mv-fg:#ffb35e; --mv-bg:#2d1f0e; }
}
.mv a { color: var(--mv-link); }
.mv a:focus-visible, .mv button:focus-visible, .mv summary:focus-visible { outline: 2px solid var(--mv-focus); outline-offset: 2px; border-radius: 4px; }
.mv-sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }

/* verdict stamp */
.mv-stamp { display:inline-flex; align-items:center; gap:7px; color:var(--mv-fg);
  border:2px solid currentColor; border-radius:7px; padding:3px 10px 3px 7px;
  font-weight:800; font-size:13px; letter-spacing:.07em; text-transform:uppercase;
  transform-origin: 30% 60%; animation: mv-stamp .32s cubic-bezier(.2,1.5,.45,1) both; }
.mv-stamp svg { width:17px; height:17px; flex:none; }
.mv[data-verdict="unavailable"] .mv-stamp, .mv[data-verdict="no-verdict"] .mv-stamp { border-style:dashed; }
@keyframes mv-stamp { from { opacity:0; transform: scale(1.35) rotate(-6deg); } to { opacity:1; transform:none; } }

/* the two numbers */
.mv-nums { margin-top:16px; display:grid; gap:14px; }
.mv-row { display:grid; grid-template-columns: minmax(0,auto) auto minmax(0,1fr); align-items:baseline; column-gap:14px; row-gap:2px; }
.mv-row.mv-solo { grid-template-columns: minmax(0,1fr); }
.mv-row-label { grid-column: 1 / -1; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
  color:var(--mv-faint); margin-bottom:2px; }
.mv-k { font-size:11px; font-weight:650; letter-spacing:.07em; text-transform:uppercase; color:var(--mv-faint); }
.mv-n.mv-claim { font-size:20px; font-weight:600; color:var(--mv-muted); font-variant-numeric:tabular-nums;
  line-height:1.15; overflow-wrap:anywhere; }
[data-verdict="contradicted"] .mv-n.mv-claim { text-decoration: line-through; text-decoration-thickness: 2px;
  text-decoration-color: var(--mv-fg); }
.mv-arrow { color:var(--mv-faint); font-size:18px; }
.mv-n.mv-official { font-size:34px; font-weight:800; letter-spacing:-0.025em; line-height:1.05;
  font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
.mv-ctx { margin-top:4px; font-size:12.5px; color:var(--mv-muted); }
.mv-note { margin-top:10px; display:inline-flex; align-items:center; gap:6px; font-size:13px; font-weight:650;
  color:var(--mv-fg); background:var(--mv-bg); border-radius:999px; padding:3px 10px; }
.mv-single .mv-n.mv-official { font-size:40px; }
.mv-ctx { grid-column: -2 / -1; }

.mv-summary { margin:14px 0 0; font-size:14.5px; line-height:1.5; }
.mv-try { margin:10px 0 0; font-size:13.5px; color:var(--mv-muted); }
.mv-try b { color:var(--mv-ink); font-weight:650; }
.mv-notes { list-style:none; margin:12px 0 0; padding:0; display:grid; gap:4px; }
.mv-notes li { font-size:12.5px; color:var(--mv-muted); line-height:1.45; padding-left:14px; position:relative; }
.mv-notes li::before { content:"i"; position:absolute; left:0; top:0; font: italic 700 11px Georgia, serif; color:var(--mv-faint); }

/* footer: source + actions */
.mv-foot { margin-top:16px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.mv-src { flex:1 1 auto; min-width:0; font-size:12.5px; color:var(--mv-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.mv-src a { color:var(--mv-muted); text-decoration:underline; text-decoration-color:var(--mv-line); text-underline-offset:3px; }
.mv-src a:hover { color:var(--mv-ink); text-decoration-color:currentColor; }
.mv-btn { font:inherit; font-size:12.5px; font-weight:650; padding:6px 11px; border-radius:999px; cursor:pointer;
  border:1px solid var(--mv-line); background:var(--mv-card); color:var(--mv-ink); display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
.mv-btn:hover { border-color: var(--mv-faint); }
.mv-btn.mv-primary { background:var(--mv-ink); border-color:var(--mv-ink); color:var(--mv-card); }
.mv-btn.mv-primary:hover { opacity:.88; }
.mv-btn svg { width:14px; height:14px; }
.mv-btn.mv-done { background:var(--mv-bg); border-color:transparent; color:var(--mv-fg); }

/* how this was checked */
.mv-how { margin-top:14px; border-top:1px solid var(--mv-line); }
.mv-how summary { cursor:pointer; list-style:none; padding:10px 0 0; font-size:12.5px; font-weight:600; color:var(--mv-muted); }
.mv-how summary::-webkit-details-marker { display:none; }
.mv-how summary::after { content:" +"; color:var(--mv-faint); }
.mv-how[open] summary::after { content:" \\2212"; }
.mv-how-body { padding-top:8px; display:grid; gap:10px; font-size:12.5px; color:var(--mv-muted); }
.mv-how-body b { color:var(--mv-ink); font-weight:650; }
.mv-how-body p { margin:0; }
.mv-list { list-style:none; margin:0; padding:0; display:grid; gap:6px; }
.mv-list a { color:var(--mv-ink); text-decoration:none; font-weight:600; }
.mv-list a:hover { text-decoration:underline; }
.mv-list span { display:block; color:var(--mv-muted); font-weight:400; overflow-wrap:anywhere; }
.mv-tag { display:inline-block; margin-left:6px; font-size:10.5px; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; color:#12813a; }
.mv-cite { padding:8px 10px; border-radius:8px; background:var(--mv-sub); color:var(--mv-ink);
  font: 11.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap:anywhere; white-space:pre-wrap; }
.mv-meta { margin-top:8px; font-size:11.5px; color:var(--mv-faint); }
.mv-meta button { font:inherit; color:var(--mv-muted); background:none; border:0; padding:0; text-decoration:underline; cursor:pointer; }

/* loading */
.mv-skel { display:grid; gap:12px; }
.mv-skel i { display:block; height:14px; border-radius:6px; background: linear-gradient(90deg, var(--mv-sub), var(--mv-line), var(--mv-sub));
  background-size: 200% 100%; animation: mv-shim 1.1s linear infinite; }
.mv-skel i:nth-child(1) { width:132px; height:26px; }
.mv-skel i:nth-child(2) { width:58%; height:38px; }
.mv-skel i:nth-child(3) { width:40%; }
@keyframes mv-shim { from { background-position: 200% 0; } to { background-position: -200% 0; } }
.mv-loading-t { margin-top:12px; font-size:13px; color:var(--mv-muted); }
@media (prefers-reduced-motion: reduce) { .mv-stamp, .mv-skel i { animation:none; } }
`;

  const ICONS = {
    "supported": "M5 12.5l4.2 4.2L19 7",
    "mostly-accurate": "M5 9.5c2.3-2 4.7-2 7 0s4.7 2 7 0M5 15.5c2.3-2 4.7-2 7 0s4.7 2 7 0",
    "contradicted": "M6.5 6.5l11 11M17.5 6.5l-11 11",
    "misleading": "M12 6v8M12 18v.1",
    "no-verdict": "M7 12h10",
    "unavailable": "M12 7v6M12 17v.1",
    "copy": "M9 9h10v10H9zM5 15V5h10",
    "check": "M5 12.5l4.2 4.2L19 7",
  };

  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "2.6");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", ICONS[name] || ICONS["no-verdict"]);
    s.appendChild(p);
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

  function link(href, text, cls) {
    const url = safeUrl(href);
    const a = el(url ? "a" : "span", cls, text);
    if (url) {
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    return a;
  }

  const reducedMotion = () =>
    typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

  // "$6.75 trillion" -> { pre: "$", num: 6.75, decimals: 2, grouped: false, post: " trillion" }
  function parseNum(text) {
    const m = /^(.*?)(-?\d[\d,]*\.?\d*)(.*)$/.exec(String(text || ""));
    if (!m) return null;
    return { pre: m[1], num: parseFloat(m[2].replace(/,/g, "")),
      decimals: (m[2].split(".")[1] || "").length, grouped: m[2].includes(","), post: m[3] };
  }

  function fmt(p, x) {
    const s = p.grouped
      ? x.toLocaleString("en-US", { minimumFractionDigits: p.decimals, maximumFractionDigits: p.decimals })
      : x.toFixed(p.decimals);
    return p.pre + s + p.post;
  }

  /** A number element that counts to its value: from the claim's figure, or from zero. */
  function numberEl(finalText, fromText) {
    const wrap = el("div", "mv-n");
    const shown = el("span", "", finalText);
    shown.setAttribute("aria-hidden", "true");
    wrap.append(shown, el("span", "mv-sr", finalText));
    const to = parseNum(finalText);
    if (!to || reducedMotion()) return wrap;
    const from = parseNum(fromText);
    const start = from && from.post === to.post ? from.num : 0;
    if (start === to.num) return wrap;
    const t0 = performance.now();
    const dur = 700;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - k, 3);
      shown.textContent = k < 1 ? fmt(to, start + (to.num - start) * e) : finalText;
      if (k < 1) requestAnimationFrame(step);
    };
    shown.textContent = fmt(to, start);
    requestAnimationFrame(step);
    return wrap;
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

  function copyButton(label, text, primary) {
    const b = el("button", primary ? "mv-btn mv-primary" : "mv-btn");
    b.type = "button";
    const ic = icon("copy");
    const t = el("span", "", label);
    b.append(ic, t);
    b.addEventListener("click", async () => {
      const ok = await copyText(text);
      b.classList.toggle("mv-done", ok);
      b.classList.toggle("mv-primary", !ok && primary);
      ic.firstChild.setAttribute("d", ok ? ICONS.check : ICONS.copy);
      t.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(() => {
        b.classList.remove("mv-done");
        if (primary) b.classList.add("mv-primary");
        ic.firstChild.setAttribute("d", ICONS.copy);
        t.textContent = label;
      }, 1600);
    });
    return b;
  }

  function stamp(key, label) {
    const s = el("span", "mv-stamp");
    s.append(icon(ICONS[key] ? key : "no-verdict"), document.createTextNode(label));
    return s;
  }

  // Older API responses carry no `display`; build the minimum from v1 fields.
  function displayOf(r) {
    if (r && r.display && r.display.verdict) return r.display;
    const label = (r && (r.verdict_detail || r.verdict)) || "No verdict";
    const key = r && r.degraded ? "unavailable" : label.toLowerCase().replace(/\s+/g, "-");
    return {
      verdict: { key, label: r && r.degraded ? "Couldn't check" : label, meaning: "" },
      summary: (r && (r.headline || r.summary)) || "", comparisons: [], notes: [], sources: [],
      reason: null, citation: "", share_text: (r && r.summary) || "", method: null, confidence: null,
    };
  }

  // Claim and official figure on one baseline: labels, then numbers, then context.
  function rowEl(c, multi) {
    const row = el("div", c.claim ? "mv-row" : "mv-row mv-solo");
    row.dataset.verdict = c.verdict_key || "";
    if (multi && c.label) row.appendChild(el("div", "mv-row-label", c.label));
    const officialK = el("div", "mv-k mv-official", c.source ? `Official · ${c.source}` : "Official");
    const officialN = numberEl(c.official, c.claim);
    officialN.classList.add("mv-official");
    if (c.claim) {
      const claimN = el("div", "mv-n mv-claim", c.claim);
      row.append(el("div", "mv-k", "Claim"), el("div"), officialK,
        claimN, el("div", "mv-arrow", "→"), officialN);
      if (c.context) row.append(el("div"), el("div"), el("div", "mv-ctx", c.context));
    } else {
      row.append(officialK, officialN);
      if (c.context) row.appendChild(el("div", "mv-ctx", c.context));
    }
    return row;
  }

  function howEl(d, sources) {
    const how = el("details", "mv-how");
    how.appendChild(el("summary", "", "How this was checked"));
    const body = el("div", "mv-how-body");
    if (d.method && d.method.title) {
      const p = el("p");
      p.appendChild(el("b", "", d.method.title + ". "));
      p.appendChild(document.createTextNode(d.method.text || ""));
      if (d.method.elapsed_seconds) p.appendChild(document.createTextNode(` Took ${d.method.elapsed_seconds}s.`));
      body.appendChild(p);
    }
    if (d.confidence && d.confidence.tier) {
      const p = el("p");
      p.appendChild(el("b", "", `${d.confidence.tier} confidence. `));
      p.appendChild(document.createTextNode(d.confidence.text || ""));
      body.appendChild(p);
    }
    if (sources.length) {
      const ul = el("ul", "mv-list");
      sources.slice(0, 8).forEach((s) => {
        const li = el("li");
        if (s.kind === "data") {
          li.appendChild(link(s.url, s.agency));
          li.appendChild(el("span", "", [s.dataset, s.identifier, s.measure].filter(Boolean).join(" · ")));
        } else {
          li.appendChild(link(s.url, s.title || s.domain));
          const meta = el("span", "", s.domain);
          if (s.official) meta.appendChild(el("b", "mv-tag", "official"));
          li.appendChild(meta);
        }
        ul.appendChild(li);
      });
      body.appendChild(ul);
    }
    if (d.citation) body.appendChild(el("div", "mv-cite", d.citation));
    how.appendChild(body);
    return how;
  }

  /**
   * Render a claim check.
   * opts: { onRecheck(): void, cached: bool }
   */
  function render(result, opts) {
    opts = opts || {};
    const d = displayOf(result);
    const key = (d.verdict && d.verdict.key) || "no-verdict";
    const root = el("div", "mv");
    root.dataset.verdict = key;
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", `Verdict: ${d.verdict.label}`);

    const st = stamp(key, d.verdict.label);
    if (d.verdict.meaning) st.title = d.verdict.meaning;
    root.appendChild(st);

    const comps = (d.comparisons || []).filter((c) => c.official);
    if (comps.length) {
      const nums = el("div", comps.length === 1 ? "mv-nums mv-single" : "mv-nums");
      comps.forEach((c) => nums.appendChild(rowEl(c, comps.length > 1)));
      root.appendChild(nums);
      comps.forEach((c) => {
        if (!c.note) return;
        const label = comps.length > 1 && c.label ? `${c.label}: ` : "";
        const n = el("div", "mv-note", label + c.note);
        n.dataset.verdict = c.verdict_key || "";
        root.append(n, document.createTextNode(" "));
      });
    } else if (d.reported_value) {
      const nums = el("div", "mv-nums mv-single");
      nums.appendChild(rowEl({ official: d.reported_value, context: d.reported_context,
        source: "", verdict_key: key }, false));
      nums.querySelector(".mv-k").textContent = "Figure found";
      root.appendChild(nums);
    }

    // Words only where the numbers do not already say it.
    if (!comps.length && d.summary) root.appendChild(el("p", "mv-summary", d.summary));
    if (d.reason && d.reason.next) {
      const p = el("p", "mv-try");
      p.append(el("b", "", "Try: "), document.createTextNode(d.reason.next));
      root.appendChild(p);
    }

    if (d.notes && d.notes.length) {
      const ul = el("ul", "mv-notes");
      d.notes.forEach((n) => ul.appendChild(el("li", "", n)));
      root.appendChild(ul);
    }

    const sources = (d.sources || []).filter((s) => s && safeUrl(s.url));
    const foot = el("div", "mv-foot");
    const first = sources[0];
    if (first) {
      const src = el("div", "mv-src");
      src.appendChild(document.createTextNode("Source: "));
      const text = first.kind === "data"
        ? [first.agency_short, first.identifier].filter(Boolean).join(", ")
        : first.domain;
      src.appendChild(link(first.url, `${text} ↗`));
      const more = sources.length - 1;
      if (more > 0) src.appendChild(document.createTextNode(` +${more} more`));
      foot.appendChild(src);
    }
    if (d.citation) foot.appendChild(copyButton("Copy citation", d.citation, true));
    if (d.share_text) foot.appendChild(copyButton("Copy result", d.share_text, false));
    if (foot.childNodes.length) root.appendChild(foot);

    if ((d.method && d.method.title) || sources.length) root.appendChild(howEl(d, sources));

    if (opts.cached && typeof opts.onRecheck === "function") {
      const meta = el("div", "mv-meta", "Saved result from earlier today. ");
      const again = el("button", "", "Check again");
      again.type = "button";
      again.addEventListener("click", () => opts.onRecheck());
      meta.appendChild(again);
      root.appendChild(meta);
    }
    return root;
  }

  /** Render a lookup ("find a number") result. opts: { onSuggest(query) } */
  function renderLookup(r, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    if (!r || !r.ok) {
      root.appendChild(el("p", "mv-summary", (r && r.headline) || "No official series matches that yet."));
      const sugg = (r && r.suggestions) || [];
      if (sugg.length && typeof opts.onSuggest === "function") {
        const p = el("div", "mv-foot");
        sugg.slice(0, 5).forEach((s) => {
          const b = el("button", "mv-btn", s.example);
          b.type = "button";
          b.addEventListener("click", () => opts.onSuggest(s.example));
          p.appendChild(b);
        });
        root.appendChild(p);
      }
      return root;
    }
    const c = r.citations || {};
    const nums = el("div", "mv-nums mv-single");
    nums.style.marginTop = "0";
    nums.appendChild(rowEl({ official: c.value || r.value, context: c.attribution, source: "" }, false));
    nums.querySelector(".mv-k").textContent = r.label || "Official figure";
    root.appendChild(nums);
    if (c.caveat) {
      const ul = el("ul", "mv-notes");
      ul.appendChild(el("li", "", c.caveat));
      root.appendChild(ul);
    }
    const foot = el("div", "mv-foot");
    if (safeUrl(c.url)) {
      const src = el("div", "mv-src");
      src.appendChild(document.createTextNode("Source: "));
      src.appendChild(link(c.url, `${c.agency || "official data"} ↗`));
      foot.appendChild(src);
    }
    if (c.note) foot.appendChild(copyButton("Copy citation", c.note, true));
    if (c.value) foot.appendChild(copyButton("Copy number", c.value, false));
    root.appendChild(foot);
    if (c.note) {
      const how = el("details", "mv-how");
      how.appendChild(el("summary", "", "Citation formats"));
      const body = el("div", "mv-how-body");
      [["Full", c.note], ["Inline", c.inline], ["Markdown", c.markdown]].forEach(([k, v]) => {
        if (!v) return;
        const p = el("div");
        p.appendChild(el("b", "", k));
        const box = el("div", "mv-cite", v);
        box.style.marginTop = "4px";
        p.appendChild(box);
        body.appendChild(p);
      });
      how.appendChild(body);
      root.appendChild(how);
    }
    return root;
  }

  function loading(text) {
    const root = el("div", "mv");
    root.setAttribute("role", "status");
    const sk = el("div", "mv-skel");
    sk.append(el("i"), el("i"), el("i"));
    root.appendChild(sk);
    const t = el("div", "mv-loading-t", text || "Pulling the official figure…");
    root.appendChild(t);
    setTimeout(() => {
      if (root.isConnected) t.textContent = "No direct data feed for this one, so searching published sources (up to 15s)…";
    }, 2500);
    return root;
  }

  function error(message, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    root.dataset.verdict = "unavailable";
    root.appendChild(stamp("unavailable", "Couldn't check"));
    root.appendChild(el("p", "mv-summary", message || "Something went wrong."));
    if (typeof opts.onRetry === "function") {
      const f = el("div", "mv-foot");
      const b = el("button", "mv-btn", "Try again");
      b.type = "button";
      b.addEventListener("click", () => opts.onRetry());
      f.appendChild(b);
      root.appendChild(f);
    }
    return root;
  }

  function verdictKey(label) {
    return String(label || "No verdict").toLowerCase().replace(/\s+/g, "-");
  }

  // Real claims and queries for "Surprise me". Each is answered from an
  // agency's own data (no AI search) and its verdict is computed live.
  const EXAMPLES = {
    claims: [
      "Unemployment in Texas was 4.1% in 2023",
      "A dozen eggs cost $4.82 in January 2023",
      "Inflation hit 9.1% in June 2022",
      "The government spent over $7 trillion in 2024",
      "The national debt is over $40 trillion",
      "Unemployment fell from 8.1% in 2020 to 3.6% in 2023",
      "The federal deficit was $1.8 trillion in 2024",
      "Real GDP grew at a 2.2% annual rate in Q2 2026",
      "Teen unemployment was 11% in 2023",
      "Electricity costs 17 cents per kWh",
      "Unemployment in Ohio was 10% in 2023",
      "U.S. population was about 335 million in 2023",
      "Federal revenue was $4.9 trillion in 2024",
      "Unemployment hit 14.8% in April 2020",
    ],
    lookups: [
      "unemployment rate texas 2023",
      "price of eggs january 2023",
      "national debt",
      "federal deficit 2024",
      "inflation rate june 2022",
      "population california 2023",
      "median household income 2023",
      "price of gas 2023",
      "teen unemployment rate 2023",
      "real gdp growth 2023",
    ],
  };

  function pick(list, avoid) {
    const pool = list.filter((x) => x !== avoid);
    return pool[Math.floor(Math.random() * pool.length)];
  }

  global.MiradorView = { css: CSS, render, renderLookup, loading, error, icon, safeUrl, copyText,
    verdictKey, examples: EXAMPLES, pick };
})(typeof window !== "undefined" ? window : this);
