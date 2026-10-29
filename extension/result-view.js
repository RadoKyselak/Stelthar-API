/*
 * Project Mirador: the result card.
 *
 * One renderer for both surfaces, the in-page card and the toolbar popup. It
 * draws the API's `display` block (a claim check) or a lookup result (one
 * official figure).
 *
 * The card shows as little as possible: the verdict, the official number,
 * one line saying what that number is, the claim only when it differs, and
 * the source with a one-click citation. No branding, no labels.
 *
 * Safety: results contain text from claims and web pages, so every string
 * goes in through textContent and only http(s) links are clickable.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --ink:#1d1d1f; --ink2:#6e6e73; --ink3:#86868b; --line:rgba(0,0,0,.1); --fill:rgba(0,0,0,.05);
  --fill2:rgba(0,0,0,.08); --link:#0066cc; --focus:#0071e3; --tone:#6e6e73;
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--ink); -webkit-font-smoothing: antialiased; text-align: left; letter-spacing: -0.005em; }
.mv *, .mv *::before, .mv *::after { box-sizing: border-box; }
.mv[data-verdict="supported"], .mv [data-verdict="supported"] { --tone:#248a3d; }
.mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --tone:#5f7700; }
.mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --tone:#d70015; }
.mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --tone:#c93400; }
@media (prefers-color-scheme: dark) {
  .mv { --ink:#f5f5f7; --ink2:#a1a1a6; --ink3:#7c7c80; --line:rgba(255,255,255,.14); --fill:rgba(255,255,255,.07);
    --fill2:rgba(255,255,255,.12); --link:#2997ff; --focus:#2997ff; --tone:#98989d; }
  .mv[data-verdict="supported"], .mv [data-verdict="supported"] { --tone:#30d158; }
  .mv[data-verdict="mostly-accurate"], .mv [data-verdict="mostly-accurate"] { --tone:#c6da6b; }
  .mv[data-verdict="contradicted"], .mv [data-verdict="contradicted"] { --tone:#ff453a; }
  .mv[data-verdict="misleading"], .mv [data-verdict="misleading"] { --tone:#ff9f0a; }
}
.mv button { font-family: inherit; color: inherit; }
.mv a:focus-visible, .mv button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }
.mv-sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }

.mv-verdict { display:flex; align-items:center; gap:6px; color:var(--tone); font-size:14px; font-weight:600; }
.mv-verdict svg { width:17px; height:17px; flex:none; animation: mv-pop .38s cubic-bezier(.3,1.6,.5,1) both; }
@keyframes mv-pop { from { transform: scale(.7); } to { transform:none; } }

.mv-fig { display:block; margin:6px 0 0; padding:0; border:0; background:none; cursor:copy; text-align:left;
  font-size:34px; font-weight:600; letter-spacing:-0.03em; line-height:1.1; font-variant-numeric:tabular-nums;
  overflow-wrap:anywhere; }
.mv-fig.mv-long { font-size:22px; letter-spacing:-0.015em; line-height:1.2; }
.mv-copied { margin-left:8px; font-size:12px; font-weight:500; letter-spacing:0; color:var(--ink3); vertical-align:middle; }
.mv-what { margin-top:3px; color:var(--ink2); font-size:13px; }
.mv-claim { margin-top:8px; font-size:13px; color:var(--tone); font-weight:500; }
.mv-claim s { text-decoration-thickness:1.5px; }

.mv-rows { margin-top:8px; display:grid; grid-template-columns:auto auto 1fr; column-gap:14px; row-gap:2px; align-items:baseline; }
.mv-rk { color:var(--ink2); font-size:13px; font-variant-numeric:tabular-nums; }
.mv-rv { font-size:24px; font-weight:600; letter-spacing:-0.02em; font-variant-numeric:tabular-nums; }
.mv-rd { font-size:13px; color:var(--tone); font-weight:500; }

.mv-text { margin:8px 0 0; font-size:13.5px; line-height:1.45; color:var(--ink); }
.mv-try { margin:6px 0 0; font-size:13px; color:var(--ink2); }
.mv-note { margin:8px 0 0; font-size:12px; line-height:1.4; color:var(--ink3); }

.mv-bar { margin-top:12px; padding-top:10px; border-top:.5px solid var(--line); display:flex; align-items:center; gap:10px; }
.mv-src { flex:1 1 auto; min-width:0; display:flex; align-items:center; gap:6px; font-size:13px; color:var(--ink2);
  white-space:nowrap; overflow:hidden; }
.mv-src a { color:var(--link); text-decoration:none; overflow:hidden; text-overflow:ellipsis; }
.mv-src a:hover { text-decoration:underline; }
.mv-tag { flex:none; font-size:11px; color:var(--ink3); }
.mv-btn { flex:none; display:inline-flex; align-items:center; gap:5px; border:0; border-radius:999px; cursor:pointer;
  padding:5px 11px; font-size:12.5px; font-weight:500; background:var(--fill2); color:var(--ink); white-space:nowrap; }
.mv-btn:hover { background: var(--line); }
.mv-btn svg { width:13px; height:13px; }
.mv-btn.mv-done { color:#248a3d; }
.mv-icon-btn { flex:none; border:0; background:none; cursor:pointer; color:var(--ink3); padding:2px; display:grid; place-items:center; }
.mv-icon-btn:hover { color:var(--ink); }
.mv-icon-btn svg { width:15px; height:15px; }

.mv-skel { display:grid; gap:9px; padding:2px 0; }
.mv-skel i { display:block; height:12px; border-radius:6px; background:var(--fill2); animation: mv-pulse 1.2s ease-in-out infinite; }
.mv-skel i:nth-child(1) { width:96px; }
.mv-skel i:nth-child(2) { width:132px; height:30px; border-radius:8px; }
.mv-skel i:nth-child(3) { width:62%; }
@keyframes mv-pulse { 50% { opacity:.45; } }
.mv-wait { margin-top:10px; font-size:12px; color:var(--ink3); min-height:16px; }
@media (prefers-reduced-motion: reduce) { .mv-verdict svg, .mv-skel i { animation:none; } }
`;

  // Glyphs drawn inside a filled circle, like SF Symbols' ".circle.fill".
  const GLYPHS = {
    "supported": "M7.5 12.4l3 3 6-6.4",
    "mostly-accurate": "M7 10.3c1.6-1.3 3.4-1.3 5 0s3.4 1.3 5 0M7 14.3c1.6-1.3 3.4-1.3 5 0s3.4 1.3 5 0",
    "contradicted": "M8.5 8.5l7 7M15.5 8.5l-7 7",
    "misleading": "M12 7.5v5.5M12 16.4v.1",
    "no-verdict": "M8 12h8",
    "unavailable": "M12 7.5v5.5M12 16.4v.1",
  };
  const LINES = {
    copy: "M9 9h10v10H9zM5 15V5h10",
    check: "M5 12.5l4.2 4.2L19 7",
    refresh: "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
  };
  const SVGNS = "http://www.w3.org/2000/svg";

  function svg(d, filled) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    if (filled) {
      const c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("cx", "12"); c.setAttribute("cy", "12"); c.setAttribute("r", "11");
      c.setAttribute("fill", "currentColor");
      s.appendChild(c);
    }
    const p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", filled ? "#fff" : "currentColor");
    p.setAttribute("stroke-width", filled ? "2.2" : "1.9");
    p.setAttribute("stroke-linecap", "round");
    p.setAttribute("stroke-linejoin", "round");
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

  function link(href, text, title) {
    const url = safeUrl(href);
    const a = el(url ? "a" : "span", "", text);
    if (url) {
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    if (title) a.title = title;
    return a;
  }

  /** Copy plain text, plus HTML (with a live link) where the browser allows. */
  async function copyText(plain, html) {
    try {
      if (html && global.ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({
          "text/plain": new Blob([plain], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        })]);
        return true;
      }
      await navigator.clipboard.writeText(plain);
      return true;
    } catch (e) {
      try {
        const t = document.createElement("textarea");
        t.value = plain;
        t.setAttribute("readonly", "");
        t.style.cssText = "position:fixed;top:0;left:0;opacity:0;";
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

  function citeButton(plain, html) {
    const b = el("button", "mv-btn");
    b.type = "button";
    const ic = svg(LINES.copy);
    const t = el("span", "", "Copy citation");
    b.append(ic, t);
    b.title = plain;
    b.addEventListener("click", async () => {
      const ok = await copyText(plain, html);
      b.classList.toggle("mv-done", ok);
      ic.lastChild.setAttribute("d", ok ? LINES.check : LINES.copy);
      t.textContent = ok ? "Copied" : "Couldn't copy";
      setTimeout(() => {
        b.classList.remove("mv-done");
        ic.lastChild.setAttribute("d", LINES.copy);
        t.textContent = "Copy citation";
      }, 1500);
    });
    return b;
  }

  // The big number, always the official figure exactly as published (never
  // animated through other values). Clicking it copies just the number.
  function figure(text) {
    const b = el("button", text.length > 16 ? "mv-fig mv-long" : "mv-fig");
    b.type = "button";
    b.title = "Copy number";
    const shown = el("span", "", text);
    b.appendChild(shown);
    b.addEventListener("click", async () => {
      if (b.querySelector(".mv-copied")) return;
      const ok = await copyText(text);
      const tip = el("span", "mv-copied", ok ? "Copied" : "Couldn't copy");
      b.appendChild(tip);
      setTimeout(() => tip.remove(), 1200);
    });
    return b;
  }

  function verdictLine(key, label, meaning) {
    const v = el("div", "mv-verdict");
    v.append(svg(GLYPHS[key] || GLYPHS["no-verdict"], true), document.createTextNode(label));
    if (meaning) v.title = meaning;
    return v;
  }

  function what(c) {
    const when = [c.period, c.basis].filter(Boolean).join(" ");
    return [c.measure, c.place, when].filter(Boolean).join(" · ");
  }

  function claimLine(c, key) {
    if (!c.claim || c.relation === "exact") return null;
    const line = el("div", "mv-claim");
    line.appendChild(document.createTextNode("Claim: "));
    // Struck through only when the claim is wrong, not when it is close.
    line.appendChild(key === "contradicted" ? el("s", "", c.claim) : document.createTextNode(c.claim));
    if (c.relation === "off" && c.gap_short) line.appendChild(document.createTextNode(` · off by ${c.gap_short}`));
    if (c.relation === "within") line.appendChild(document.createTextNode(" · within margin"));
    return line;
  }

  function sourceBar(d, opts) {
    const bar = el("div", "mv-bar");
    const src = el("div", "mv-src");
    const sources = (d.sources || []).filter((s) => s && safeUrl(s.url));
    const data = sources.filter((s) => s.kind === "data");
    const web = sources.filter((s) => s.kind !== "data");
    const shown = new Set();
    data.forEach((s) => {
      if (shown.has(s.url)) return;
      shown.add(s.url);
      const detail = [s.agency, s.dataset, s.identifier].filter(Boolean).join(", ");
      src.appendChild(link(s.url, `${s.agency_short} ↗`, detail));
    });
    if (!data.length && web.length) {
      const first = web.find((s) => s.official) || web[0];
      src.appendChild(link(first.url, `${first.domain} ↗`, first.title));
      if (web.length > 1) {
        const more = el("span", "mv-tag", `+${web.length - 1}`);
        more.title = web.filter((s) => s !== first).map((s) => s.domain).join(", ");
        src.appendChild(more);
      }
      src.appendChild(el("span", "mv-tag", "· web search"));
    }
    bar.appendChild(src);
    if (opts.onRecheck) {
      const r = el("button", "mv-icon-btn");
      r.type = "button";
      r.title = "Saved result. Check again";
      r.setAttribute("aria-label", "Check again");
      r.appendChild(svg(LINES.refresh));
      r.addEventListener("click", () => opts.onRecheck());
      bar.appendChild(r);
    }
    if (d.citation) bar.appendChild(citeButton(d.citation, d.citation_html));
    return src.childNodes.length || d.citation ? bar : null;
  }

  // Older API responses carry no `display`; build the minimum from v1 fields.
  function displayOf(r) {
    if (r && r.display && r.display.verdict) return r.display;
    const label = (r && (r.verdict_detail || r.verdict)) || "Can't verify";
    const key = r && r.degraded ? "unavailable" : label.toLowerCase().replace(/\s+/g, "-");
    return { verdict: { key, label: r && r.degraded ? "Couldn't check" : label },
      summary: (r && (r.headline || r.summary)) || "", comparisons: [], notes: [], sources: [] };
  }

  /**
   * A claim check.
   * opts: { onRecheck(): void } shows a small "check again" control (for saved results).
   */
  function render(result, opts) {
    opts = opts || {};
    const d = displayOf(result);
    const key = d.verdict.key || "no-verdict";
    const root = el("div", "mv");
    root.dataset.verdict = key;
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", d.verdict.label);
    root.appendChild(verdictLine(key, d.verdict.label, d.verdict.meaning));

    const comps = (d.comparisons || []).filter((c) => c.official);
    if (comps.length === 1) {
      const c = comps[0];
      root.appendChild(figure(c.official));
      root.appendChild(el("div", "mv-what", what(c)));
      const cl = claimLine(c, key);
      if (cl) root.appendChild(cl);
    } else if (comps.length > 1) {
      const rows = el("div", "mv-rows");
      comps.forEach((c) => {
        const k = el("span", "mv-rk", c.label || c.period);
        const v = el("span", "mv-rv", c.official);
        const delta = el("span", "mv-rd");
        delta.dataset.verdict = c.verdict_key || "";
        if (c.relation === "off") delta.textContent = `claim ${c.claim}`;
        else if (c.relation === "within") delta.textContent = `claim ${c.claim}, within margin`;
        else if (c.relation === "exact") {
          const tick = svg(LINES.check);
          tick.style.cssText = "width:14px;height:14px;vertical-align:-2px";
          tick.setAttribute("aria-label", "matches");
          delta.appendChild(tick);
        }
        rows.append(k, v, delta);
      });
      root.appendChild(rows);
      const first = comps[0];
      root.appendChild(el("div", "mv-what", [first.measure, first.place, first.basis].filter(Boolean).join(" · ")));
    } else if (d.reported_value) {
      root.appendChild(figure(d.reported_value));
      if (d.reported_context) root.appendChild(el("div", "mv-what", d.reported_context));
    }

    if (!comps.length && d.summary) root.appendChild(el("p", "mv-text", d.summary));
    if (d.reason && d.reason.next) root.appendChild(el("p", "mv-try", d.reason.next));

    (d.notes || []).slice(0, 2).forEach((n) => root.appendChild(el("p", "mv-note", n)));

    const bar = sourceBar(d, opts);
    if (bar) root.appendChild(bar);
    return root;
  }

  /** One official figure (a lookup). opts: { onSuggest(query) } */
  function renderLookup(r, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    if (!r || !r.ok) {
      root.dataset.verdict = "no-verdict";
      root.appendChild(el("p", "mv-text", (r && r.headline) || "No official figure matches that."));
      const sugg = ((r && r.suggestions) || []).slice(0, 3);
      if (sugg.length && typeof opts.onSuggest === "function") {
        const p = el("p", "mv-try", "Try ");
        sugg.forEach((s, i) => {
          const a = el("button", "", s.example);
          a.type = "button";
          a.style.cssText = "border:0;background:none;padding:0;color:var(--link);cursor:pointer;";
          a.addEventListener("click", () => opts.onSuggest(s.example));
          p.appendChild(a);
          if (i < sugg.length - 1) p.appendChild(document.createTextNode(", "));
        });
        root.appendChild(p);
      }
      return root;
    }
    const c = r.citations || {};
    // attribution reads "BLS, Texas, 2023 annual average": agency, then context.
    const attr = c.attribution || "";
    const cut = attr.indexOf(", ");
    const agency = cut > 0 ? attr.slice(0, cut) : "Source";
    const context = cut > 0 ? attr.slice(cut + 2) : "";
    root.appendChild(figure(c.value || r.value));
    root.appendChild(el("div", "mv-what", [r.label, context].filter(Boolean).join(" · ")));
    if (c.caveat) root.appendChild(el("p", "mv-note", c.caveat));
    const bar = el("div", "mv-bar");
    const src = el("div", "mv-src");
    if (safeUrl(c.url)) src.appendChild(link(c.url, `${agency} ↗`, c.agency));
    bar.appendChild(src);
    if (c.note) bar.appendChild(citeButton(c.note, c.note_html));
    root.appendChild(bar);
    return root;
  }

  function loading() {
    const root = el("div", "mv");
    root.setAttribute("role", "status");
    root.setAttribute("aria-label", "Checking");
    const sk = el("div", "mv-skel");
    sk.append(el("i"), el("i"), el("i"));
    const wait = el("div", "mv-wait");
    root.append(sk, wait);
    setTimeout(() => { if (root.isConnected) wait.textContent = "Searching published sources…"; }, 2500);
    return root;
  }

  function error(message, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    root.dataset.verdict = "unavailable";
    root.appendChild(verdictLine("unavailable", "Couldn't check"));
    root.appendChild(el("p", "mv-text", message || "Something went wrong."));
    if (typeof opts.onRetry === "function") {
      const bar = el("div", "mv-bar");
      bar.appendChild(el("div", "mv-src"));
      const b = el("button", "mv-btn", "Try again");
      b.type = "button";
      b.addEventListener("click", () => opts.onRetry());
      bar.appendChild(b);
      root.appendChild(bar);
    }
    return root;
  }

  function verdictKey(label) {
    const k = String(label || "").toLowerCase().replace(/\s+/g, "-");
    return k === "no-verdict" || k === "inconclusive" ? "no-verdict" : k;
  }

  // A claim states a value; a lookup only names a measure (and maybe a year).
  function intentOf(text) {
    const t = String(text || "").replace(/\b(19|20)\d{2}\b/g, " ").replace(/\bq[1-4]\b/gi, " ");
    return /\d/.test(t) ? "check" : "find";
  }

  // Real claims, each answered from an agency's own data (no AI search).
  const EXAMPLES = [
    "A dozen eggs cost $4.82 in January 2023",
    "Unemployment in Texas was 4.1% in 2023",
    "The government spent over $7 trillion in 2024",
    "Inflation hit 9.1% in June 2022",
    "The national debt is over $40 trillion",
  ];

  global.MiradorView = { css: CSS, render, renderLookup, loading, error, safeUrl, copyText,
    verdictKey, intentOf, examples: EXAMPLES };
})(typeof window !== "undefined" ? window : this);
