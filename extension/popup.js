// Project Mirador popup. One field: a sentence with a figure in it is checked
// as a claim; a measure without one ("unemployment texas 2023") is looked up.
const MAX_CLAIM_LEN = 600;
const View = window.MiradorView;

document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = View.css;
  document.head.appendChild(style);

  const $ = (id) => document.getElementById(id);
  const q = $("q");
  const hint = $("hint");
  const out = $("out");
  const list = $("list");
  let ticket = 0;
  let debounce = null;
  let shownFor = "";

  const autosize = () => {
    q.style.height = "20px";
    q.style.height = `${Math.min(q.scrollHeight, 110)}px`;
  };

  const text = () => q.value.trim();

  const refresh = () => {
    autosize();
    const t = text();
    if (!t) {
      hint.textContent = "";
      out.replaceChildren();
      shownFor = "";
      showList();
      return;
    }
    list.hidden = true;
    if (t.length > MAX_CLAIM_LEN) {
      hint.textContent = "Too long. Select one sentence with one figure.";
      return;
    }
    hint.textContent = t === shownFor ? ""
      : View.intentOf(t) === "check" ? "Return to check this claim" : "Return to find this number";
  };

  // ------------------------------------------------------------- actions
  const check = (claim, bypassCache = false) => {
    const mine = ++ticket;
    shownFor = claim;
    hint.textContent = "";
    out.replaceChildren(View.loading());
    chrome.storage.local.set({ stelthar_last_claim: claim });
    chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
      if (mine !== ticket) return;
      if (chrome.runtime.lastError || !resp) {
        out.replaceChildren(View.error("The extension didn't respond. Try reopening it.",
          { onRetry: () => check(claim, true) }));
        return;
      }
      if (!resp.ok) {
        out.replaceChildren(View.error(resp.error, { onRetry: () => check(claim, true) }));
        return;
      }
      out.replaceChildren(View.render(resp.result, {
        onRecheck: resp.result._cached ? () => check(claim, true) : null,
      }));
    });
  };

  const find = async (query, { quiet = false } = {}) => {
    const mine = ++ticket;
    if (!quiet) out.replaceChildren(View.loading());
    try {
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query });
      if (mine !== ticket) return;
      if (!reply || !reply.ok) {
        if (!quiet) out.replaceChildren(View.error((reply && reply.error) || "Lookup failed."));
        return;
      }
      // While typing, only show a result once there is one.
      if (quiet && !reply.result.ok) return;
      shownFor = query;
      hint.textContent = "";
      out.replaceChildren(View.renderLookup(reply.result, { onSuggest: (s) => run(s, true) }));
    } catch (e) {
      if (mine === ticket && !quiet) out.replaceChildren(View.error(e.message));
    }
  };

  const run = (t, setField = false) => {
    t = (t || "").trim();
    if (!t || t.length > MAX_CLAIM_LEN) return;
    if (setField) { q.value = t; refresh(); }
    list.hidden = true;
    clearTimeout(debounce);
    if (View.intentOf(t) === "check") check(t);
    else find(t);
  };

  // ------------------------------------------- recent checks / examples
  const showList = () => {
    chrome.storage.local.get(["stelthar_history"], (data) => {
      if (text()) return;
      const history = (data.stelthar_history || []).slice(0, 6);
      const rows = history.length
        ? history.map((h) => ({ text: h.claim, key: View.verdictKey(h.verdict), title: h.verdict }))
        : View.examples.map((e) => ({ text: e, key: "example", title: "Example" }));
      $("list-title").textContent = history.length ? "Recent" : "Try";
      $("items").replaceChildren(...rows.map((r) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        b.title = r.title || "";
        const dot = document.createElement("span");
        dot.className = `dot ${r.key}`;
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = r.text;
        b.append(dot, t);
        b.addEventListener("click", () => run(r.text, true));
        li.appendChild(b);
        return li;
      }));
      list.hidden = false;
    });
  };

  // ------------------------------------------------------------- wiring
  $("form").addEventListener("submit", (e) => { e.preventDefault(); run(text()); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(text()); }
  });
  q.addEventListener("input", () => {
    refresh();
    const t = text();
    clearTimeout(debounce);
    // Lookups answer as you type; claims wait for Return.
    if (t.length > 2 && View.intentOf(t) === "find" && t !== shownFor) {
      debounce = setTimeout(() => find(t, { quiet: true }), 350);
    }
  });

  // Highlighted text on the current page is what the person wants checked:
  // open straight onto its result. Otherwise start empty, with recent checks.
  const selectionOnPage = async () => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.id) return "";
      const [{ result }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => String(window.getSelection() || "").replace(/\s+/g, " ").trim(),
      });
      return result || "";
    } catch (e) {
      return ""; // chrome:// pages, the Web Store, PDFs
    }
  };

  refresh();
  q.focus();
  selectionOnPage().then((sel) => {
    if (!sel || text() || sel.length > MAX_CLAIM_LEN) return;
    q.value = sel;
    refresh();
    run(sel);
  });
});
