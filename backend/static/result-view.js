/*
 * Project Mirador: the result card.
 *
 * One renderer for both surfaces, the in-page card and the toolbar popup. It
 * draws the API's `display` block (a claim check) or a lookup result (one
 * official figure).
 *
 * Visual language: plain and modern. System type, one accent color, soft
 * borders, a small tinted status badge, no gradients, glass or animation.
 * Nothing suggests an official government product (no banner, seal or
 * flag): this is an independent tool that cites government data.
 *
 * Safety: results contain text from claims and web pages, so every string
 * goes in through textContent and only http(s) links are clickable.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --ink:#111827; --ink2:#374151; --muted:#6b7280; --line:#e5e7eb; --subtle:#f9fafb; --bg:#ffffff;
  --accent:#2563eb; --accent-hover:#1d4ed8; --on-accent:#ffffff; --focus:#2563eb;
  --tone:#4b5563; --tint:#f3f4f6;
  font: 14px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--ink); text-align: left; -webkit-font-smoothing: antialiased; }
.mv *, .mv *::before, .mv *::after { box-sizing: border-box; }
.mv [data-verdict="supported"] { --tone:#15803d; --tint:#ecfdf3; }
.mv [data-verdict="mostly-accurate"] { --tone:#a16207; --tint:#fef9e7; }
.mv [data-verdict="misleading"] { --tone:#c2410c; --tint:#fff4ed; }
.mv [data-verdict="contradicted"] { --tone:#b91c1c; --tint:#fef2f2; }
.mv [data-verdict="unavailable"] { --tone:#0369a1; --tint:#f0f9ff; }
.mv a, .mv button { font-family: inherit; }
.mv a:focus-visible, .mv button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }

.mv-alert { }
.mv-alert-h { display: inline-flex; align-items: center; gap: 7px; margin: 0; padding: 3px 10px 3px 8px;
  border-radius: 6px; background: var(--tint); color: var(--tone); font-size: 13px; font-weight: 600; line-height: 1.4; }
.mv-alert-h::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
.mv-alert-b { margin: 8px 0 0; font-size: 14px; color: var(--ink2); }

.mv-sec { margin-top: 14px; }
.mv-label { font-size: 12px; font-weight: 500; color: var(--muted); }
.mv-fig { margin: 1px 0 0; font-size: 30px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.15;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.mv-fig.mv-long { font-size: 20px; letter-spacing: -0.01em; }
.mv-what { margin: 3px 0 0; font-size: 13px; color: var(--muted); }
.mv-note { margin: 10px 0 0; font-size: 12.5px; color: var(--muted); }

.mv-table { width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }
.mv-table th { text-align: left; font-size: 12px; font-weight: 500; color: var(--muted); padding: 0 8px 4px 0; }
.mv-table td { text-align: left; padding: 6px 8px 6px 0; border-top: 1px solid var(--line); }
.mv-table td:nth-child(2) { font-weight: 650; }

.mv-foot { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.mv-src { margin: 0 0 10px; font-size: 12.5px; color: var(--muted); overflow-wrap: anywhere; }
.mv a { color: var(--accent); text-decoration: none; }
.mv a:hover { text-decoration: underline; }
.mv-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.mv-btn { appearance: none; border: 0; border-radius: 8px; cursor: pointer; padding: 7px 12px;
  font-size: 13px; font-weight: 600; line-height: 1.2; background: var(--accent); color: var(--on-accent); }
.mv-btn:hover { background: var(--accent-hover); }
.mv-btn-outline { background: transparent; color: var(--ink); box-shadow: inset 0 0 0 1px var(--line); }
.mv-btn-outline:hover { background: var(--subtle); }
.mv-linkbtn { appearance: none; border: 0; background: none; padding: 0; cursor: pointer; font-size: 13px;
  font-weight: 500; color: var(--muted); }
.mv-linkbtn:hover { color: var(--ink); }
.mv-status { font-size: 12.5px; color: var(--muted); }
.mv-how { margin: 8px 0 0; font-size: 12px; color: var(--muted); }
.mv-loading { margin: 0; font-size: 13px; color: var(--muted); }
.mv-fc { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.mv-fc-list { list-style: none; margin: 6px 0 0; padding: 0; }
.mv-fc-list li { margin-top: 8px; font-size: 13.5px; line-height: 1.4; overflow-wrap: anywhere; }
.mv-fc-meta { display: block; margin-top: 2px; font-size: 12.5px; color: var(--muted); }

.mv { --mark:#fef3c7; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.mv-ex { margin: 12px 0 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.mv-ex-h { margin: 0; padding: 6px 10px; font-size: 11.5px; color: var(--muted); background: var(--subtle);
  border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.mv-ex-t { width: 100%; border-collapse: collapse; font: 12px/1.4 var(--mono); font-variant-numeric: tabular-nums; }
.mv-ex-t td { padding: 5px 10px; border-top: 1px solid var(--line); color: var(--ink2); vertical-align: top; }
.mv-ex-t tr:first-child td { border-top: 0; }
.mv-ex-t td:last-child { text-align: right; white-space: nowrap; }
.mv-ex-t tr.mv-cited td { background: var(--mark); color: var(--ink); font-weight: 700; }
.mv-ex-n { margin: 0; padding: 6px 10px; font-size: 11.5px; color: var(--muted); border-top: 1px solid var(--line); }
.mv-quote { margin: 12px 0 0; padding: 2px 0 2px 10px; border-left: 3px solid var(--line); font-size: 13px; color: var(--ink2); }
.mv-quote-by { display: block; margin-top: 3px; font-size: 11.5px; color: var(--muted); }

/* How the claim was read: the measure, place and period the answer is for. */
.mv-read { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; padding: 0; list-style: none; }
.mv-read li { padding: 2px 8px; border: 1px solid var(--line); border-radius: 999px; font-size: 12px; color: var(--ink2);
  overflow-wrap: anywhere; }
.mv-read b { font-weight: 500; color: var(--muted); }
.mv-rev { margin: 10px 0 0; padding: 8px 10px; border-left: 3px solid var(--tone); background: var(--subtle);
  font-size: 13px; color: var(--ink2); }
.mv-meta { margin: 0 0 10px; font-size: 12.5px; color: var(--muted); }

/* A passage: how its claims fell, then each claim, opened on request. */
.mv-tally-h { margin: 0; font-size: 15px; font-weight: 600; color: var(--ink); }
.mv-page { margin: 4px 0 0; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.mv-tally-l { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 6px 0 0; padding: 0; list-style: none; }
.mv-tally-l li { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--tone); }
.mv-tally-l li::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.mv-claims { margin: 14px 0 0; padding: 0; list-style: none; border-top: 1px solid var(--line); }
.mv-claims > li { border-bottom: 1px solid var(--line); }
.mv-claims summary { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; padding: 10px 0; cursor: pointer;
  list-style: none; }
.mv-claims summary::-webkit-details-marker { display: none; }
.mv-claims summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }
.mv-claim-v { grid-row: 1 / span 2; align-self: start; display: inline-flex; align-items: center; gap: 6px; min-width: 104px;
  margin-top: 1px; line-height: 1.35; font-size: 12.5px; font-weight: 600; color: var(--tone); }
.mv-claim-v::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none;
  align-self: flex-start; margin-top: 5px; }
.mv-claim-t { font-size: 14px; color: var(--ink); overflow-wrap: anywhere; }
.mv-claim-s { grid-column: 2; font-size: 12.5px; color: var(--muted); overflow-wrap: anywhere; }
.mv-claim-b { padding: 4px 0 16px; }
.mv-claim-b > .mv-note:first-child { margin-top: 0; }
.mv-claims .mv-fig { font-size: 24px; }
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

  /** "Copy link": the claim's own page, with what the check found. */
  function shareButton(d) {
    const url = safeUrl(d.share_url);
    if (!url) return null;
    const b = el("button", "mv-linkbtn", "Copy link");
    b.type = "button";
    b.title = "A link to this check, to send to someone";
    b.addEventListener("click", async () => {
      const text = [d.share_text, url].filter(Boolean).join("\n");
      const ok = await copyText(text);
      b.textContent = ok ? "Link copied" : "Couldn't copy";
      setTimeout(() => { b.textContent = "Copy link"; }, 2500);
    });
    return b;
  }

  /** The measure, place and period the answer is for, as labelled chips. */
  function readingChips(d) {
    const chips = (d.reading || []).filter((c) => c && c.value);
    if (!chips.length) return null;
    const ul = el("ul", "mv-read");
    ul.setAttribute("aria-label", "How the claim was read");
    chips.forEach((c) => {
      const li = el("li");
      li.append(el("b", "", `${c.label} `), document.createTextNode(c.value));
      ul.appendChild(li);
    });
    return ul;
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
      case "first": return `The claim says ${c.claim}, the figure the agency first published.`;
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
  /** The published rows a figure was read from, the cited one marked. */
  function excerptBox(ex) {
    if (!ex || !Array.isArray(ex.rows) || !ex.rows.length) return null;
    const box = el("figure", "mv-ex");
    const head = [ex.heading, ex.units].filter(Boolean).join(" · ");
    if (head) box.appendChild(el("figcaption", "mv-ex-h", head));
    const table = el("table", "mv-ex-t");
    ex.rows.forEach((r) => {
      const tr = el("tr", r.cited ? "mv-cited" : "");
      if (r.cited) tr.setAttribute("aria-label", `Cited: ${r.label}, ${r.value}`);
      tr.append(el("td", "", r.label), el("td", "", r.value));
      table.appendChild(tr);
    });
    box.appendChild(table);
    if (ex.note) box.appendChild(el("p", "mv-ex-n", ex.note));
    return box;
  }

  /** For a web-search answer: the sentence on the official page it rests on. */
  function quoteBox(d) {
    const s = (d.sources || []).find((x) => x && x.kind === "web" && x.quote && safeUrl(x.url)
      && x.official) || (d.sources || []).find((x) => x && x.kind === "web" && x.quote && safeUrl(x.url));
    if (!s) return null;
    const q = el("blockquote", "mv-quote", `“${s.quote}”`);
    const by = el("span", "mv-quote-by");
    by.appendChild(link(s.url, s.domain || s.title));
    q.appendChild(by);
    return q;
  }

  const FC_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
    return m && +m[2] >= 1 && +m[2] <= 12 ? `${FC_MON[+m[2] - 1]} ${+m[3]}, ${m[1]}` : "";
  }

  /** Other outlets' published fact-checks of similar claims: shown, never used. */
  function factChecks(d) {
    const list = (d.fact_checks || [])
      .filter((f) => f && f.publisher && f.rating && safeUrl(f.url)).slice(0, 3);
    if (!list.length) return null;
    const sec = el("div", "mv-fc");
    sec.setAttribute("aria-label", "Other fact-checkers");
    sec.appendChild(el("div", "mv-label", "Other fact-checkers on similar claims"));
    const ul = el("ul", "mv-fc-list");
    list.forEach((f) => {
      const li = el("li");
      li.appendChild(link(f.url, f.title || f.claim || f.publisher));
      li.appendChild(el("span", "mv-fc-meta",
        [`${f.publisher}: \u201c${f.rating}\u201d`, shortDate(f.date)].filter(Boolean).join(" \u00b7 ")));
      ul.appendChild(li);
    });
    sec.append(ul, el("p", "mv-how",
      "Their ratings, found with Google's fact-check search. Not part of this check."));
    return sec;
  }

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
    if (d.revision && d.revision.text) {
      const rev = el("p", "mv-rev", d.revision.text);
      rev.dataset.verdict = key;
      root.appendChild(rev);
    }

    const chips = readingChips(d);
    if (comps.length === 1) {
      const c = comps[0];
      const sec = el("div", "mv-sec");
      sec.append(el("div", "mv-label", d.revision ? "Official figure today" : "Official figure"),
        el("p", c.official.length > 16 ? "mv-fig mv-long" : "mv-fig", c.official),
        chips || el("p", "mv-what", describe(c)));
      const ex = excerptBox(c.excerpt);
      if (ex) sec.appendChild(ex);
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
      sec.append(table, chips || el("p", "mv-what", [first.measure, first.place, first.basis].filter(Boolean).join(", ")));
      comps.slice(0, 2).forEach((c) => { const ex = excerptBox(c.excerpt); if (ex) sec.appendChild(ex); });
      root.appendChild(sec);
    } else if (d.reported_value) {
      const sec = el("div", "mv-sec");
      // The model's own description of the publisher is not shown: the pages
      // themselves are linked under Sources.
      sec.append(el("div", "mv-label", "Figure found by web search"),
        el("p", d.reported_value.length > 16 ? "mv-fig mv-long" : "mv-fig", d.reported_value));
      root.appendChild(sec);
    }
    if (!comps.length) {
      const q = quoteBox(d);
      if (q) root.appendChild(q);
    }

    (d.notes || []).slice(0, 2).forEach((n) => root.appendChild(el("p", "mv-note", n)));

    const src = sourceLine(d);
    const share = shareButton(d);
    // How sure the answer is, and when it was checked: official figures get revised.
    const meta = [d.confidence && d.confidence.tier ? `${d.confidence.tier} confidence. ${d.confidence.text || ""}`.trim() : "",
      shortDate(d.checked_on) ? `Checked ${shortDate(d.checked_on)}.` : ""].filter(Boolean).join(" ");
    if (src || d.citation || opts.onRecheck || share || (meta && key !== "no-verdict" && key !== "unavailable")) {
      const foot = el("div", "mv-foot");
      if (src) foot.appendChild(src);
      if (meta && key !== "no-verdict" && key !== "unavailable") foot.appendChild(el("p", "mv-meta", meta));
      if (d.method && d.method.kind === "dataset") {
        foot.appendChild(el("p", "mv-how", "Matched to the agency's series; the figure comes straight from its data."));
      }
      const actions = d.citation ? citeButton(d.citation, d.citation_html) : el("div", "mv-actions");
      if (share) actions.appendChild(share);
      if (typeof opts.onEdit === "function" && chips) {
        const edit = el("button", "mv-linkbtn", "Not what you meant?");
        edit.type = "button";
        edit.title = "Edit the claim: name the measure, place or period you mean.";
        edit.addEventListener("click", () => opts.onEdit());
        actions.appendChild(edit);
      }
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
    const fc = factChecks(d);
    if (fc) root.appendChild(fc);
    return root;
  }

  const TALLY_WORDS = { "supported": "supported", "mostly-accurate": "mostly accurate",
    "contradicted": "contradicted", "misleading": "misleading", "no-verdict": "can't verify",
    "unavailable": "couldn't check" };
  const TALLY_ORDER = ["contradicted", "misleading", "mostly-accurate", "supported", "no-verdict", "unavailable"];

  /** What a claim's answer rests on, in one line. */
  function oneLine(d) {
    if (d.revision && d.revision.text) return d.revision.text;
    // With no verdict, the reason is the answer; a figure beside it would read as one.
    const key = (d.verdict && d.verdict.key) || "no-verdict";
    if ((key === "no-verdict" || key === "unavailable") && d.reason && d.reason.why) return d.reason.why;
    const comps = (d.comparisons || []).filter((c) => c.official);
    if (comps.length === 1) {
      const c = comps[0];
      return [`${c.source || "Official figure"}: ${c.official}`, c.context].filter(Boolean).join(" \u00b7 ");
    }
    if (d.reason && d.reason.why) return d.reason.why;
    const first = String(d.summary || "").split(/(?<=[.!?])\s/)[0];
    return first.length > 140 ? `${first.slice(0, 137)}\u2026` : first;
  }

  /**
   * A passage checked claim by claim (POST /v2/check).
   * opts: { onDeeper(claim): Promise<result> } runs the full check for one
   * claim the free data could not settle; every option of render() applies
   * to the claim cards too.
   */
  function renderPassage(check, opts) {
    opts = opts || {};
    const entries = ((check && check.claims) || []).map((e) => Object.assign({}, e));
    // One claim someone typed is shown as its own card; a page keeps its list.
    if (entries.length === 1 && !(check && check.page)) return render(entries[0].result, opts);
    const root = el("div", "mv");
    root.setAttribute("role", "region");
    const open = new Set();

    const draw = () => {
      const counts = {};
      entries.forEach((e) => {
        const k = displayOf(e.result).verdict.key || "no-verdict";
        counts[k] = (counts[k] || 0) + 1;
      });
      const head = el("div", "mv-tally");
      const n = entries.length;
      const page = check && check.page;
      head.appendChild(el("p", "mv-tally-h", n ? `${n} claim${n === 1 ? "" : "s"} checked` : "Nothing to check here"));
      if (page) {
        // A page checked by its address: say which page, and link it.
        const from = el("p", "mv-page");
        from.append(document.createTextNode("On "), link(page.url, page.title || page.host),
          document.createTextNode(page.title ? ` (${page.host})` : ""));
        head.appendChild(from);
      }
      const unsettled = (check && check.unsettled) || 0;
      if (!n) head.appendChild(el("p", "mv-alert-b", page
        ? (unsettled ? "Nothing on that page could be settled from official data."
          : "No sentence on that page states a figure that official data covers.")
        : "No sentence here states a figure that official data covers."));
      const ul = el("ul", "mv-tally-l");
      ul.setAttribute("aria-label", "How the claims fell");
      TALLY_ORDER.filter((k) => counts[k]).forEach((k) => {
        const li = el("li", "", `${counts[k]} ${TALLY_WORDS[k]}`);
        li.dataset.verdict = k;
        ul.appendChild(li);
      });
      if (ul.childNodes.length) head.appendChild(ul);
      root.setAttribute("aria-label", n ? `${n} claims checked` : "Nothing to check");

      const list = el("ol", "mv-claims");
      entries.forEach((e, i) => {
        const d = displayOf(e.result);
        const key = d.verdict.key || "no-verdict";
        const li = el("li");
        const det = el("details");
        det.open = open.has(i);
        det.addEventListener("toggle", () => { if (det.open) open.add(i); else open.delete(i); });
        const sum = el("summary");
        const v = el("span", "mv-claim-v", d.verdict.label);
        v.dataset.verdict = key;
        sum.append(v, el("span", "mv-claim-t", `\u201c${e.span || e.claim}\u201d`),
          el("span", "mv-claim-s", oneLine(d)));
        const body = el("div", "mv-claim-b");
        if (e.claim && e.span && e.claim !== e.span) {
          body.appendChild(el("p", "mv-note", `Checked as: \u201c${e.claim}\u201d`));
        }
        body.appendChild(render(e.result, opts));
        if (e.deeper && typeof opts.onDeeper === "function") {
          const more = el("button", "mv-btn mv-btn-outline", "Search further");
          more.type = "button";
          more.title = "Search the government datasets, then published sources, for this one claim.";
          more.addEventListener("click", async () => {
            more.disabled = true;
            more.textContent = "Searching\u2026";
            try {
              const result = await opts.onDeeper(e.claim);
              if (result) { e.result = result; e.deeper = false; }
            } catch (err) {
              e.result = { display: { verdict: { key: "unavailable", label: "Couldn't check" },
                summary: (err && err.message) || "Something went wrong.", comparisons: [], notes: [], sources: [] } };
            }
            open.add(i);
            draw();
          });
          const wrap = el("div", "mv-actions");
          wrap.appendChild(more);
          body.appendChild(wrap);
        }
        det.append(sum, body);
        li.appendChild(det);
        list.appendChild(li);
      });

      const parts = [head];
      if (n) parts.push(list);
      if (unsettled) parts.push(el("p", "mv-note",
        `${unsettled} more sentence${unsettled === 1 ? "" : "s"} with figures could not be settled from official data.`
        + " Highlight one and check it on its own to search further."));
      if (check && check.truncated) parts.push(el("p", "mv-note", page
        ? `The page holds more. Only the first ${n} claims were checked.`
        : `Only the first ${n} claims were checked. Highlight less text to check the rest.`));
      root.replaceChildren(...parts);
    };
    draw();
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
      if (r && r.deeper) box.appendChild(el("p", "mv-alert-b",
        "Press Enter to search all the government datasets for it."));
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
    if (r.note) root.appendChild(el("p", "mv-note", r.note));
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
  // A figure other than a year, a yes/no question or a sentence is a claim to
  // check; a short measure ("unemployment texas 2023") or a what/how question
  // is a figure to find.
  const VERB = /\b(is|isn['\u2019]t|was|wasn['\u2019]t|are|aren['\u2019]t|were|weren['\u2019]t|has|hasn['\u2019]t|have|had|will|won['\u2019]t|can|can['\u2019]t|cause[sd]?|won|lost|rose|fell|grew|recalled|banned|approved|signed|passed)\b/i;
  function intentOf(text) {
    const s = String(text || "").replace(/\s+/g, " ").trim();
    const t = s.replace(/\b(19|20)\d{2}\b/g, " ").replace(/\bq[1-4]\b/gi, " ");
    if (/\d/.test(t)) return "check";
    if (/^(what|how|which|when|where|who|whats|what['\u2019]s)\b/i.test(s)) return "find";
    if (/\?$/.test(s) || VERB.test(s)) return "check";
    return s.split(" ").length >= 8 ? "check" : "find";
  }

  // Real claims, each answered from an agency's own data (no AI search).
  const EXAMPLES = [
    "A dozen eggs cost $4.82 in January 2023",
    "Unemployment in Texas was 4.1% in 2023",
    "The federal deficit was $1.8 trillion in 2024",
  ];

  // A lone web address: the page it names is fetched and checked.
  function isLink(text) {
    const s = String(text || "").trim();
    return /^https?:\/\/\S+$/i.test(s) && !!safeUrl(s);
  }

  global.MiradorView = { css: CSS, render, renderPassage, renderLookup, loading, error, safeUrl, copyText,
    verdictKey, intentOf, isLink, examples: EXAMPLES };
})(typeof window !== "undefined" ? window : this);
