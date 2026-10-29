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
  const out = $("out");
  const recent = $("recent");
  let ticket = 0;

  const autosize = () => {
    q.style.height = "40px";
    q.style.height = `${Math.min(Math.max(q.scrollHeight + 2, 40), 120)}px`;
  };
  const text = () => q.value.replace(/\s+/g, " ").trim();

  const check = (claim, bypassCache = false) => {
    const mine = ++ticket;
    out.replaceChildren(View.loading());
    chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
      if (mine !== ticket) return;
      if (chrome.runtime.lastError || !resp) {
        out.replaceChildren(View.error("The extension didn't respond. Close this window and try again.",
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
      loadRecent();
    });
  };

  const find = async (query) => {
    const mine = ++ticket;
    out.replaceChildren(View.loading());
    try {
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query });
      if (mine !== ticket) return;
      if (!reply || !reply.ok) {
        out.replaceChildren(View.error((reply && reply.error) || "The lookup failed."));
        return;
      }
      out.replaceChildren(View.renderLookup(reply.result, { onSuggest: (s) => run(s, true) }));
    } catch (e) {
      if (mine === ticket) out.replaceChildren(View.error(e.message));
    }
  };

  const run = (t, setField = false) => {
    t = (t || "").trim();
    if (!t) return;
    if (setField) q.value = t;
    autosize();
    if (t.length > MAX_CLAIM_LEN) {
      out.replaceChildren(View.error(`That is ${t.length} characters. Use one sentence with one figure.`));
      return;
    }
    if (View.intentOf(t) === "check") check(t);
    else find(t);
  };

  const loadRecent = () => {
    chrome.storage.local.get(["stelthar_history"], (data) => {
      const history = (data.stelthar_history || []).slice(0, 5);
      recent.hidden = !history.length;
      $("items").replaceChildren(...history.map((h) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = h.claim;
        const v = document.createElement("span");
        v.className = "v";
        v.textContent = h.verdict === "No verdict" || h.verdict === "Inconclusive" ? "Can't verify" : (h.verdict || "");
        b.append(t, v);
        b.title = h.claim;
        b.addEventListener("click", () => run(h.claim, true));
        li.appendChild(b);
        return li;
      }));
    });
  };

  $("form").addEventListener("submit", (e) => { e.preventDefault(); run(text()); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(text()); }
  });
  q.addEventListener("input", autosize);

  // Text highlighted on the current page is what the person wants checked.
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

  loadRecent();
  q.focus();
  selectionOnPage().then((sel) => {
    if (!sel || text() || sel.length > MAX_CLAIM_LEN) return;
    q.value = sel;
    autosize();
    run(sel);
  });
});
