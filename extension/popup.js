// Project Mirador popup: check a claim, look up a figure, recent checks.
// Results are drawn by result-view.js, the same renderer the in-page panel
// and the web app use.
const MAX_CLAIM_LEN = 600;
const AI_CHAR_LIMIT = 300;
const View = window.MiradorView;

document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = View.css;
  document.head.appendChild(style);
  document.getElementById("brand").prepend(View.logo(28));

  setupTabs();
  setupCheck();
  setupLookup();
});

// ---------------------------------------------------------------- tabs
function setupTabs() {
  const tabs = Array.from(document.querySelectorAll(".tab"));
  const select = (tab) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    const focusEl = tab.id === "tab-lookup"
      ? document.getElementById("lookup-input")
      : document.getElementById("manual-claim-input");
    focusEl.focus();
    chrome.storage.local.set({ stelthar_popup_tab: tab.id });
  };
  tabs.forEach((t) => t.addEventListener("click", () => select(t)));
  chrome.storage.local.get(["stelthar_popup_tab"], (d) => {
    const saved = tabs.find((t) => t.id === d.stelthar_popup_tab);
    if (saved && saved.id !== "tab-check") select(saved);
  });
}

// ---------------------------------------------------------------- check
function setupCheck() {
  const input = document.getElementById("manual-claim-input");
  const count = document.getElementById("char-count");
  const btn = document.getElementById("verifyBtn");
  const hint = document.getElementById("claim-hint");
  const out = document.getElementById("result");
  const historyBox = document.getElementById("history-dropdown");
  const historyList = document.getElementById("history-list");

  const claimNow = () => input.value.trim();

  const refresh = () => {
    const n = claimNow().length;
    count.textContent = n ? `${n} / ${AI_CHAR_LIMIT}` : "";
    count.classList.toggle("over", n > AI_CHAR_LIMIT);
    btn.disabled = !n || n > MAX_CLAIM_LEN;
  };

  const run = (claim, { bypassCache = false } = {}) => {
    claim = (claim || "").trim();
    if (!claim) return;
    if (claim.length > MAX_CLAIM_LEN) {
      out.replaceChildren(View.error(
        `That is ${claim.length} characters. Highlight a single sentence with one figure (under ${MAX_CLAIM_LEN}).`));
      return;
    }
    btn.disabled = true;
    out.replaceChildren(View.loading());
    chrome.storage.local.set({ stelthar_last_claim: claim });

    chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
      refresh();
      if (chrome.runtime.lastError || !resp) {
        out.replaceChildren(View.error(
          "The extension's background service did not answer. Try reloading the extension.",
          { onRetry: () => run(claim, { bypassCache: true }) }));
        return;
      }
      if (!resp.ok) {
        out.replaceChildren(View.error(resp.error, { onRetry: () => run(claim, { bypassCache: true }) }));
        return;
      }
      out.replaceChildren(View.render(resp.result, {
        cached: !!resp.result._cached,
        onRecheck: () => run(claim, { bypassCache: true }),
      }));
      loadHistory();
    });
  };

  const loadHistory = () => {
    chrome.storage.local.get(["stelthar_history"], (data) => {
      const history = data.stelthar_history || [];
      historyBox.hidden = !history.length;
      historyList.replaceChildren(...history.map((entry) => {
        const label = entry.verdict === "Inconclusive" ? "No verdict" : (entry.verdict || "No verdict");
        const li = document.createElement("li");
        li.tabIndex = 0;
        li.title = "Show this check again";
        const dot = document.createElement("span");
        dot.className = `hdot ${View.verdictKey(label)}`;
        const text = document.createElement("div");
        text.className = "htext";
        const v = document.createElement("span");
        v.className = "hverdict";
        v.textContent = entry.confidence_tier && label !== "No verdict"
          ? `${label} · ${entry.confidence_tier}` : label;
        const c = document.createElement("span");
        c.className = "hclaim";
        c.textContent = entry.claim;
        text.append(v, c);
        li.append(dot, text);
        const open = () => { input.value = entry.claim; refresh(); run(entry.claim); };
        li.addEventListener("click", open);
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
        return li;
      }));
    });
  };

  btn.addEventListener("click", () => run(claimNow()));
  input.addEventListener("input", refresh);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(claimNow()); }
  });

  chrome.storage.local.get(["stelthar_last_claim"], (data) => {
    const stored = (data.stelthar_last_claim || "").trim();
    if (stored) {
      input.value = stored;
      hint.textContent = "Your last highlighted or checked claim. Edit it, or press Check claim.";
    } else {
      hint.textContent = "Tip: highlight a sentence on any page and click “Check this claim”.";
    }
    refresh();
  });
  loadHistory();
  input.focus();
}

// ---------------------------------------------------------------- lookup
// The popup is the discoverable surface; the omnibox is the fast one. Both
// hit the same endpoint. Copying happens here rather than via the offscreen
// document because a button click is a real user gesture, so the async
// Clipboard API works directly and can carry both flavours.
function setupLookup() {
  const input = document.getElementById("lookup-input");
  const resultEl = document.getElementById("lookup-result");
  const valueEl = document.getElementById("lookup-value");
  const attrEl = document.getElementById("lookup-attribution");
  const caveatEl = document.getElementById("lookup-caveat");
  const citeEl = document.getElementById("lookup-cite");
  const linkEl = document.getElementById("lookup-source-link");
  const missEl = document.getElementById("lookup-miss");
  const buttons = Array.from(document.querySelectorAll(".copy-btn"));

  let current = null;
  let debounce = null;
  let ticket = 0;

  const hideAll = () => {
    resultEl.style.display = "none";
    missEl.style.display = "none";
  };

  const showMiss = (result) => {
    current = null;
    resultEl.style.display = "none";
    missEl.style.display = "block";
    missEl.textContent = result.headline || "No match.";
    (result.suggestions || []).slice(0, 5).forEach((s) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "sugg";
      chip.textContent = s.example;
      chip.addEventListener("click", () => { input.value = s.example; run(s.example); });
      missEl.appendChild(chip);
    });
  };

  const showHit = (result) => {
    current = result;
    missEl.style.display = "none";
    resultEl.style.display = "block";
    const c = result.citations || {};
    valueEl.textContent = c.value || result.value || "";
    attrEl.textContent = [result.label, c.attribution].filter(Boolean).join(" · ");
    // A figure the agency did not publish, standing in for one it did, has to
    // say so where the writer will actually read it.
    caveatEl.textContent = c.caveat ? `Basis: ${c.caveat}` : "";
    citeEl.textContent = c.note || c.inline || "";
    linkEl.replaceChildren();
    const url = View.safeUrl(c.url);
    if (url) {
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = "Open the official data ↗";
      linkEl.appendChild(a);
    }
    buttons.forEach((b) => { b.classList.remove("copied"); b.textContent = b.dataset.label; });
  };

  async function run(query) {
    query = (query || "").trim();
    const mine = ++ticket;
    if (query.length < 2) { hideAll(); return; }
    try {
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query });
      if (mine !== ticket) return;
      if (!reply || !reply.ok) {
        showMiss({ headline: `Lookup failed: ${(reply && reply.error) || "unknown error"}` });
        return;
      }
      if (reply.result.ok) showHit(reply.result);
      else showMiss(reply.result);
    } catch (e) {
      if (mine === ticket) showMiss({ headline: `Lookup failed: ${e.message}` });
    }
  }

  input.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => run(input.value), 260);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { clearTimeout(debounce); run(input.value); }
  });

  async function copy(plain, html) {
    try {
      if (html && window.ClipboardItem) {
        await navigator.clipboard.write([
          new ClipboardItem({
            "text/plain": new Blob([plain], { type: "text/plain" }),
            "text/html": new Blob([html], { type: "text/html" }),
          }),
        ]);
        return true;
      }
      await navigator.clipboard.writeText(plain);
      return true;
    } catch (e) {
      return false;
    }
  }

  buttons.forEach((btn) => {
    btn.dataset.label = btn.textContent;
    btn.addEventListener("click", async () => {
      if (!current) return;
      const c = current.citations || {};
      const format = btn.dataset.format;
      const plain = c[format] || c.inline || "";
      // Only the inline form has a rich equivalent; a footnote pastes as text.
      const ok = await copy(plain, format === "inline" ? c.html : "");
      btn.classList.toggle("copied", ok);
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(() => {
        btn.classList.remove("copied");
        btn.textContent = btn.dataset.label;
      }, 1400);
    });
  });
}
