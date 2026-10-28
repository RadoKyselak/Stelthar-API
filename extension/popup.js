// Project Mirador popup: check a claim or find a number. Results are drawn by
// result-view.js, the same renderer the in-page panel and the web app use.
const MAX_CLAIM_LEN = 600;
const AI_CHAR_LIMIT = 300;
const View = window.MiradorView;

document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = View.css;
  document.head.appendChild(style);

  const $ = (id) => document.getElementById(id);
  const form = $("form");
  const q = $("q");
  const go = $("go");
  const out = $("out");
  const count = $("count");
  const MODES = {
    check: { tab: $("mode-check"), label: "Claim to check", button: "Check",
      placeholder: "Paste or type a claim with a number" },
    find: { tab: $("mode-find"), label: "Number to find", button: "Find",
      placeholder: "unemployment rate texas 2023" },
  };
  let mode = "check";
  let ticket = 0;
  let debounce = null;
  let lastSurprise = "";
  const drafts = { check: "", find: "" };

  const autosize = () => {
    q.style.height = "38px";
    q.style.height = `${Math.min(q.scrollHeight, 120)}px`;
  };

  const sync = () => {
    const n = q.value.trim().length;
    go.disabled = n < 2 || (mode === "check" && n > MAX_CLAIM_LEN);
    count.textContent = mode === "check" && n > AI_CHAR_LIMIT * 0.8 ? `${n} / ${AI_CHAR_LIMIT}` : "";
    count.classList.toggle("over", n > AI_CHAR_LIMIT);
    autosize();
  };

  const setMode = (next) => {
    drafts[mode] = q.value;
    mode = next;
    Object.entries(MODES).forEach(([k, m]) => m.tab.setAttribute("aria-selected", String(k === next)));
    q.placeholder = MODES[next].placeholder;
    $("q-label").textContent = MODES[next].label;
    go.setAttribute("aria-label", MODES[next].button);
    q.value = drafts[next];
    out.replaceChildren();
    sync();
    q.focus();
    chrome.storage.local.set({ stelthar_popup_mode: next });
  };

  // ------------------------------------------------------------- check
  const check = (claim, { bypassCache = false } = {}) => {
    claim = (claim || "").trim();
    if (!claim) return;
    const mine = ++ticket;
    go.disabled = true;
    out.replaceChildren(View.loading());
    chrome.storage.local.set({ stelthar_last_claim: claim });
    chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
      if (mine !== ticket) return;
      sync();
      if (chrome.runtime.lastError || !resp) {
        out.replaceChildren(View.error("The extension did not answer. Try reloading it.",
          { onRetry: () => check(claim, { bypassCache: true }) }));
        return;
      }
      if (!resp.ok) {
        out.replaceChildren(View.error(resp.error, { onRetry: () => check(claim, { bypassCache: true }) }));
        return;
      }
      out.replaceChildren(View.render(resp.result, {
        cached: !!resp.result._cached,
        onRecheck: () => check(claim, { bypassCache: true }),
      }));
      loadHistory();
    });
  };

  // -------------------------------------------------------------- find
  const find = async (query) => {
    query = (query || "").trim();
    const mine = ++ticket;
    if (query.length < 2) { out.replaceChildren(); return; }
    try {
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query });
      if (mine !== ticket) return;
      if (!reply || !reply.ok) {
        out.replaceChildren(View.error((reply && reply.error) || "Lookup failed."));
        return;
      }
      out.replaceChildren(View.renderLookup(reply.result, {
        onSuggest: (s) => { q.value = s; sync(); find(s); },
      }));
    } catch (e) {
      if (mine === ticket) out.replaceChildren(View.error(e.message));
    }
  };

  const run = (text) => (mode === "check" ? check(text) : find(text));

  const surprise = () => {
    const text = View.pick(mode === "check" ? View.examples.claims : View.examples.lookups, lastSurprise);
    lastSurprise = text;
    q.value = text;
    sync();
    run(text);
  };

  // ------------------------------------------------------------ recent
  const loadHistory = () => {
    chrome.storage.local.get(["stelthar_history"], (data) => {
      const history = data.stelthar_history || [];
      $("recent").hidden = !history.length;
      $("history").replaceChildren(...history.map((entry) => {
        const label = entry.verdict === "Inconclusive" ? "No verdict" : (entry.verdict || "No verdict");
        const li = document.createElement("li");
        li.tabIndex = 0;
        li.title = `${label}: show this check again`;
        const dot = document.createElement("span");
        dot.className = `dot ${View.verdictKey(label)}`;
        const c = document.createElement("span");
        c.className = "hclaim";
        c.textContent = entry.claim;
        li.append(dot, c);
        const open = () => {
          if (mode !== "check") setMode("check");
          q.value = entry.claim;
          sync();
          check(entry.claim);
        };
        li.addEventListener("click", open);
        li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
        return li;
      }));
    });
  };

  // ------------------------------------------------------------ wiring
  form.addEventListener("submit", (e) => { e.preventDefault(); run(q.value); });
  q.addEventListener("input", () => {
    sync();
    if (mode === "find") {
      clearTimeout(debounce);
      debounce = setTimeout(() => find(q.value), 260);
    }
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(q.value); }
  });
  MODES.check.tab.addEventListener("click", () => mode !== "check" && setMode("check"));
  MODES.find.tab.addEventListener("click", () => mode !== "find" && setMode("find"));
  $("surprise").addEventListener("click", surprise);

  chrome.storage.local.get(["stelthar_last_claim", "stelthar_popup_mode"], (data) => {
    drafts.check = (data.stelthar_last_claim || "").trim();
    if (data.stelthar_popup_mode === "find") setMode("find");
    else { q.value = drafts.check; sync(); q.focus(); q.select(); }
  });
  loadHistory();
});
