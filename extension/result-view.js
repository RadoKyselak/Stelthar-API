/*
 * Project Mirador: the result card.
 *
 * One renderer for both surfaces, the in-page card and the toolbar popup. It
 * draws the API's `display` block (a claim check) or a lookup result (one
 * official figure).
 *
 * Visual language follows the U.S. Web Design System's plain conventions:
 * system sans-serif type, high contrast, square corners, underlined links,
 * alert boxes with a colored left border for status. It deliberately uses
 * nothing that would suggest an official government product (no banner,
 * seal or flag): this is an independent tool that cites government data.
 *
 * Safety: results contain text from claims and web pages, so every string
 * goes in through textContent and only http(s) links are clickable.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --ink:#1b1b1b; --muted:#565c65; --line:#dfe1e2; --bg:#ffffff; --link:#005ea2; --link-hover:#1a4480;
  --btn:#005ea2; --btn-hover:#1a4480; --btn-ink:#ffffff; --focus:#2491ff;
  --stripe:#71767a; --tint:#f0f0f0;
  font: 15px/1.5 "Public Sans", "Source Sans Pro", "Helvetica Neue", Helvetica, Roboto, Arial, sans-serif;
  color: var(--ink); text-align: left; }
.mv *, .mv *::before, .mv *::after { box-sizing: border-box; }
.mv [data-verdict="supported"] { --stripe:#00a91c; --tint:#ecf3ec; }
.mv [data-verdict="mostly-accurate"] { --stripe:#ffbe2e; --tint:#faf3d1; }
.mv [data-verdict="misleading"] { --stripe:#e66f0e; --tint:#fbe9dc; }
.mv [data-verdict="contradicted"] { --stripe:#d54309; --tint:#f4e3db; }
.mv [data-verdict="unavailable"] { --stripe:#00bde3; --tint:#e7f6f8; }
@media (prefers-color-scheme: dark) {
  .mv { --ink:#f0f0f0; --muted:#a9aeb1; --line:#565c65; --bg:#1b1b1b; --link:#73b3e7; --link-hover:#a8d3f5;
    --btn:#73b3e7; --btn-hover:#a8d3f5; --btn-ink:#1b1b1b; --stripe:#a9aeb1; --tint:#2d2e2f; }
  .mv [data-verdict="supported"] { --stripe:#00a91c; --tint:#19311e; }
  .mv [data-verdict="mostly-accurate"] { --stripe:#ffbe2e; --tint:#3b2f0b; }
  .mv [data-verdict="misleading"] { --stripe:#e66f0e; --tint:#3d220e; }
  .mv [data-verdict="contradicted"] { --stripe:#d54309; --tint:#3d1a0e; }
  .mv [data-verdict="unavailable"] { --stripe:#00bde3; --tint:#0d3640; }
}
.mv a, .mv button { font-family: inherit; }
.mv a:focus, .mv button:focus { outline: 4px solid var(--focus); outline-offset: 0; }
.mv a:focus:not(:focus-visible), .mv button:focus:not(:focus-visible) { outline: none; }

.mv-alert { border-left: 8px solid var(--stripe); background: var(--tint); padding: 10px 12px; }
.mv-alert-h { margin: 0; font-size: 17px; font-weight: 700; line-height: 1.3; }
.mv-alert-b { margin: 4px 0 0; font-size: 15px; }

.mv-sec { margin-top: 14px; }
.mv-label { font-size: 14px; color: var(--muted); }
.mv-fig { margin: 0; font-size: 28px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.mv-fig.mv-long { font-size: 20px; }
.mv-what { margin: 2px 0 0; font-size: 15px; }
.mv-note { margin: 8px 0 0; font-size: 14px; color: var(--muted); }

.mv-table { width: 100%; border-collapse: collapse; font-size: 15px; font-variant-numeric: tabular-nums; }
.mv-table th, .mv-table td { text-align: left; padding: 6px 8px 6px 0; border-bottom: 1px solid var(--line); }
.mv-table th { font-weight: 700; font-size: 14px; }

.mv-foot { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.mv-src { margin: 0 0 10px; font-size: 15px; overflow-wrap: anywhere; }
.mv a { color: var(--link); text-decoration: underline; }
.mv a:hover { color: var(--link-hover); }
.mv-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.mv-btn { appearance: none; border: 0; border-radius: 4px; cursor: pointer; padding: 8px 16px;
  font-size: 15px; font-weight: 700; line-height: 1; background: var(--btn); color: var(--btn-ink); }
.mv-btn:hover { background: var(--btn-hover); }
.mv-btn-outline { background: transparent; color: var(--link); box-shadow: inset 0 0 0 2px var(--link); }
.mv-btn-outline:hover { background: transparent; color: var(--link-hover); box-shadow: inset 0 0 0 2px var(--link-hover); }
.mv-linkbtn { appearance: none; border: 0; background: none; padding: 0; cursor: pointer; font-size: 15px;
  color: var(--link); text-decoration: underline; }
.mv-status { font-size: 14px; color: var(--muted); }
.mv-loading { font-size: 15px; color: var(--muted); }
`;

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
    const wrap = el("div", "mv-actions");
    const b = el("button", "mv-btn", "Copy citation");
    b.type = "button";
    const status = el("span", "mv-status");
    status.setAttribute("role", "status");
    b.addEventListener("click", async () => {
      const ok = await copyText(plain, html);
      status.textContent = ok ? "Citation copied." : "Couldn't copy. Select the text instead.";
      setTimeout(() => { status.textContent = ""; }, 2500);
    });
    wrap.append(b, status);
    return wrap;
  }

  function alertBox(key, heading, body) {
    const a = el("div", "mv-alert");
    a.dataset.verdict = key;
    a.appendChild(el("p", "mv-alert-h", heading));
    (Array.isArray(body) ? body : [body]).filter(Boolean)
      .forEach((t) => a.appendChild(el("p", "mv-alert-b", t)));
    return a;
  }

  // One plain-language sentence relating the claim to the official figure.
  function relationText(c) {
    if (!c.claim) return "";
    switch (c.relation) {
      case "exact": return "The claim matches the official figure.";
      case "within": return `The claim says ${c.claim}, within the margin of the official figure.`;
      case "off": return `The claim says ${c.claim}, a difference of ${c.gap || c.gap_short}.`;
      default: return `The claim says ${c.claim}.`;
    }
  }

  function describe(c) {
    const when = [c.period, c.basis].filter(Boolean).join(", ");
    return [c.measure, c.place, when].filter(Boolean).join(", ");
  }

  function sourceLine(d) {
    const p = el("p", "mv-src");
    const sources = (d.sources || []).filter((s) => s && safeUrl(s.url));
    const data = sources.filter((s) => s.kind === "data");
    const web = sources.filter((s) => s.kind !== "data");
    if (data.length) {
      p.appendChild(document.createTextNode("Source: "));
      const seen = new Set();
      data.forEach((s) => {
        if (seen.has(s.url)) return;
        if (seen.size) p.appendChild(document.createTextNode("; "));
        seen.add(s.url);
        const detail = [s.dataset, s.identifier].filter(Boolean).join(", ");
        p.appendChild(link(s.url, s.agency, detail));
        if (detail) p.appendChild(document.createTextNode(`, ${detail}`));
      });
    } else if (web.length) {
      p.appendChild(document.createTextNode("Sources found by web search: "));
      const byDomain = new Map();
      web.forEach((s) => { if (!byDomain.has(s.domain)) byDomain.set(s.domain, s); });
      Array.from(byDomain.values()).slice(0, 3).forEach((s, i) => {
        if (i) p.appendChild(document.createTextNode(", "));
        p.appendChild(link(s.url, s.domain, s.title));
      });
    }
    return p.childNodes.length ? p : null;
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
   * opts: { onRecheck(): void } adds "Check again" (for saved results).
   */
  function render(result, opts) {
    opts = opts || {};
    const d = displayOf(result);
    const key = d.verdict.key || "no-verdict";
    const root = el("div", "mv");
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", d.verdict.label);

    const comps = (d.comparisons || []).filter((c) => c.official);
    const alertBody = [];
    if (comps.length === 1) alertBody.push(relationText(comps[0]));
    else if (comps.length > 1) {
      comps.filter((c) => c.relation === "off" || c.relation === "within")
        .forEach((c) => alertBody.push(`${c.label}: ${relationText(c)}`));
    } else alertBody.push(d.summary);
    if (d.reason && d.reason.next) alertBody.push(d.reason.next);
    root.appendChild(alertBox(key, d.verdict.label, alertBody));

    if (comps.length === 1) {
      const c = comps[0];
      const sec = el("div", "mv-sec");
      sec.append(el("div", "mv-label", "Official figure"),
        el("p", c.official.length > 16 ? "mv-fig mv-long" : "mv-fig", c.official),
        el("p", "mv-what", describe(c)));
      root.appendChild(sec);
    } else if (comps.length > 1) {
      const sec = el("div", "mv-sec");
      const table = el("table", "mv-table");
      const head = el("tr");
      ["", "Official", "Claim"].forEach((h) => head.appendChild(el("th", "", h)));
      table.appendChild(head);
      comps.forEach((c) => {
        const tr = el("tr");
        tr.append(el("td", "", c.label || c.period), el("td", "", c.official), el("td", "", c.claim));
        table.appendChild(tr);
      });
      const first = comps[0];
      sec.append(table, el("p", "mv-what", [first.measure, first.place, first.basis].filter(Boolean).join(", ")));
      root.appendChild(sec);
    } else if (d.reported_value) {
      const sec = el("div", "mv-sec");
      // The model's own description of the publisher is not shown: the pages
      // themselves are linked under Sources.
      sec.append(el("div", "mv-label", "Figure found by web search"),
        el("p", d.reported_value.length > 16 ? "mv-fig mv-long" : "mv-fig", d.reported_value));
      root.appendChild(sec);
    }

    (d.notes || []).slice(0, 2).forEach((n) => root.appendChild(el("p", "mv-note", n)));

    const src = sourceLine(d);
    if (src || d.citation || opts.onRecheck) {
      const foot = el("div", "mv-foot");
      if (src) foot.appendChild(src);
      const actions = d.citation ? citeButton(d.citation, d.citation_html) : el("div", "mv-actions");
      if (typeof opts.onRecheck === "function") {
        const again = el("button", "mv-linkbtn", "Check again");
        again.type = "button";
        again.title = "This result was saved earlier today.";
        again.addEventListener("click", () => opts.onRecheck());
        actions.appendChild(again);
      }
      if (actions.childNodes.length) foot.appendChild(actions);
      root.appendChild(foot);
    }
    return root;
  }

  /** One official figure (a lookup). opts: { onSuggest(query) } */
  function renderLookup(r, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    if (!r || !r.ok) {
      const sugg = ((r && r.suggestions) || []).slice(0, 3);
      const box = alertBox("no-verdict", "No official figure found",
        (r && r.headline) || "No official series matches that search.");
      if (sugg.length && typeof opts.onSuggest === "function") {
        const p = el("p", "mv-alert-b", "Try: ");
        sugg.forEach((s, i) => {
          const b = el("button", "mv-linkbtn", s.example);
          b.type = "button";
          b.addEventListener("click", () => opts.onSuggest(s.example));
          p.appendChild(b);
          if (i < sugg.length - 1) p.appendChild(document.createTextNode(", "));
        });
        box.appendChild(p);
      }
      root.appendChild(box);
      return root;
    }
    const c = r.citations || {};
    // attribution reads "BLS, Texas, 2023 annual average": agency, then context.
    const attr = c.attribution || "";
    const cut = attr.indexOf(", ");
    const context = cut > 0 ? attr.slice(cut + 2) : "";
    const sec = el("div");
    const value = c.value || r.value || "";
    sec.append(el("div", "mv-label", r.label || "Official figure"),
      el("p", value.length > 16 ? "mv-fig mv-long" : "mv-fig", value));
    if (context) sec.appendChild(el("p", "mv-what", context));
    root.appendChild(sec);
    if (c.caveat) root.appendChild(el("p", "mv-note", c.caveat));
    const foot = el("div", "mv-foot");
    if (safeUrl(c.url)) {
      const p = el("p", "mv-src", "Source: ");
      p.appendChild(link(c.url, c.agency || "official data"));
      foot.appendChild(p);
    }
    if (c.note) foot.appendChild(citeButton(c.note, c.note_html));
    root.appendChild(foot);
    return root;
  }

  function loading() {
    const root = el("div", "mv");
    root.setAttribute("role", "status");
    const t = el("p", "mv-loading", "Checking official data…");
    root.appendChild(t);
    setTimeout(() => {
      if (root.isConnected) t.textContent = "Searching published sources. This can take up to 15 seconds.";
    }, 2500);
    return root;
  }

  function error(message, opts) {
    opts = opts || {};
    const root = el("div", "mv");
    root.appendChild(alertBox("unavailable", "Couldn't check", message || "Something went wrong."));
    if (typeof opts.onRetry === "function") {
      const foot = el("div", "mv-foot");
      const b = el("button", "mv-btn mv-btn-outline", "Try again");
      b.type = "button";
      b.addEventListener("click", () => opts.onRetry());
      foot.appendChild(b);
      root.appendChild(foot);
    }
    return root;
  }

  function verdictKey(label) {
    const k = String(label || "").toLowerCase().replace(/\s+/g, "-");
    return k === "no-verdict" || k === "inconclusive" || k === "can't-verify" ? "no-verdict" : k;
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
    "The federal deficit was $1.8 trillion in 2024",
  ];

  global.MiradorView = { css: CSS, render, renderLookup, loading, error, safeUrl, copyText,
    verdictKey, intentOf, examples: EXAMPLES };
})(typeof window !== "undefined" ? window : this);
