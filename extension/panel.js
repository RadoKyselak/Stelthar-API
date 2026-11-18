// Project Mirador side panel. One field: a sentence, a yes/no question or a
// whole passage is checked, each claim in it on its own; a short measure
// ("unemployment texas 2023") is looked up. Right-click "Check with Project
// Mirador" sends the highlighted text here; the address bar ("mir ...") opens
// this page in a tab with ?q=.
const MAX_TEXT_LEN = 8000;   // the server's cap for one passage
const MAX_FIND_LEN = 600;

// Text over the cap is cut at the end of its last whole sentence, so the
// part that is checked reads as it was written.
function fitPassage(t) {
  if (t.length <= MAX_TEXT_LEN) return { text: t, cut: false };
  const head = t.slice(0, MAX_TEXT_LEN);
  const end = Math.max(head.lastIndexOf(". "), head.lastIndexOf("? "), head.lastIndexOf("! "));
  return { text: end > MAX_TEXT_LEN / 2 ? head.slice(0, end + 1) : head, cut: true };
}

// "today", "yesterday" or "Nov 14": when a saved check was made.
function whenChecked(ts) {
  if (!ts) return "";
  const day = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(new Date()) - day(new Date(ts))) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const View = window.MiradorView;

document.addEventListener("DOMContentLoaded", () => {
  const style = document.createElement("style");
  style.textContent = View.css + ((window.MiradorSkin || {}).css || "");
  document.head.appendChild(style);

  const $ = (id) => document.getElementById(id);
  const q = $("q");
  const out = $("out");
  const recent = $("recent");
  const tryBox = $("try");
  const HISTORY_KEY = "stelthar_history";
  let ticket = 0;
  let hasHistory = false;
  let asked = ""; // what the current answer is for

  const autosize = () => {
    q.style.height = "40px";
    q.style.height = `${Math.min(Math.max(q.scrollHeight + 2, 40), 120)}px`;
  };
  const text = () => q.value.replace(/\s+/g, " ").trim();
  // Example claims only while there is nothing else to show.
  const showStart = () => { tryBox.hidden = hasHistory || out.childNodes.length > 0; };
  const show = (node) => { out.replaceChildren(node); showStart(); };

  const ask = (message) => new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        reject(new Error("The extension didn't respond. Close the panel and try again."));
      } else if (!resp.ok) reject(new Error(resp.error));
      else resolve(resp.result);
    });
  });

  const check = async (passage, bypassCache = false, cut = false) => {
    const mine = ++ticket;
    const retry = { onRetry: () => check(passage, true, cut) };
    show(View.loading());
    try {
      const result = await ask({ type: "checkText", text: passage, bypassCache });
      if (mine !== ticket) return;
      const card = View.renderPassage(result, {
        onRecheck: result._cached ? () => check(passage, true, cut) : null,
        // "Search further": the full check for one claim of a passage.
        onDeeper: (claim) => ask({ type: "deeper", claim }),
        onEdit: () => { q.focus(); q.select(); },
      });
      if (cut) {
        const note = document.createElement("p");
        note.className = "cut";
        note.textContent = "That was a lot of text: only the first part of it was checked.";
        const wrap = document.createElement("div");
        wrap.append(note, card);
        show(wrap);
      } else show(card);
      loadRecent();
    } catch (e) {
      if (mine === ticket) show(View.error(e.message, retry));
    }
  };

  const find = async (query) => {
    const mine = ++ticket;
    const retry = { onRetry: () => find(query) };
    show(View.loading());
    try {
      // An explicit search: a miss may go to Claude's dataset search.
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query, deep: true });
      if (mine !== ticket) return;
      if (!reply || !reply.ok) {
        show(View.error((reply && reply.error) || "The lookup didn't work. Try again in a moment.", retry));
        return;
      }
      show(View.renderLookup(reply.result, { onSuggest: (s) => run(s, true) }));
    } catch (e) {
      if (mine === ticket) show(View.error("The extension didn't respond. Close the panel and try again.", retry));
    }
  };

  const run = (t, setField = false, intent = null) => {
    t = (t || "").trim();
    if (!t) return;
    if (setField) q.value = t;
    asked = t.replace(/\s+/g, " ");
    document.body.classList.add("answered");
    document.body.classList.remove("editing");
    autosize(); // after the button hides, so the height fits the wider field
    window.scrollTo(0, 0); // the answer appears at the top, even if a recent item far down was clicked
    // A passage is always checked; only something short can be a lookup.
    const wants = t.length > MAX_FIND_LEN || View.isLink(t) ? "check" : (intent || View.intentOf(t));
    if (wants === "check") {
      const fit = fitPassage(t);
      check(fit.text, false, fit.cut);
    } else find(t);
  };

  const loadRecent = () => {
    chrome.storage.local.get([HISTORY_KEY], (data) => {
      const history = (data[HISTORY_KEY] || []).slice(0, 5);
      hasHistory = history.length > 0;
      recent.hidden = !hasHistory;
      showStart();
      $("items").replaceChildren(...history.map((h) => {
        const li = document.createElement("li");
        const b = document.createElement("button");
        b.type = "button";
        const t = document.createElement("span");
        t.className = "t";
        t.textContent = h.claim;
        const v = document.createElement("span");
        v.className = "v";
        v.dataset.verdict = View.verdictKey(h.verdict);
        v.textContent = v.dataset.verdict === "no-verdict" ? "Can't verify" : (h.verdict || "");
        const when = whenChecked(h.ts);
        if (when) v.title = `Checked ${when}`;
        b.append(t, v);
        if (when) {
          const d = document.createElement("span");
          d.className = "d";
          d.textContent = when;
          b.appendChild(d);
        }
        b.title = h.claim;
        b.addEventListener("click", () => run(h.claim, true));
        li.appendChild(b);
        return li;
      }));
    });
  };

  $("examples").replaceChildren(...View.examples.map((ex) => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = ex;
    b.addEventListener("click", () => run(ex, true));
    li.appendChild(b);
    return li;
  }));

  $("clear").addEventListener("click", () => {
    chrome.storage.local.remove(HISTORY_KEY, loadRecent);
    q.focus();
  });

  $("form").addEventListener("submit", (e) => { e.preventDefault(); run(text()); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(text()); }
    // Escape empties the field to start over.
    if (e.key === "Escape" && q.value) {
      e.preventDefault();
      q.value = "";
      q.dispatchEvent(new Event("input"));
    }
  });
  q.addEventListener("input", () => {
    // Editing the question brings the Check button back; an empty field the heading too.
    document.body.classList.toggle("editing", text() !== asked);
    if (!text()) document.body.classList.remove("answered");
    autosize();
  });

  // Text sent from a right-click, now or while the panel was closed.
  const PENDING_KEY = "mirador_pending";
  const takePending = (p) => {
    if (!p || !p.text || Date.now() - p.at > 60000) return;
    chrome.storage.session.remove(PENDING_KEY);
    run(p.text, true, p.intent);
  };
  chrome.storage.session.get(PENDING_KEY, (data) => takePending(data[PENDING_KEY]));
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && changes[PENDING_KEY]) takePending(changes[PENDING_KEY].newValue);
  });

  loadRecent();
  q.focus();
  const fromAddressBar = new URLSearchParams(location.search).get("q");
  if (fromAddressBar) {
    document.body.classList.add("tab");
    run(fromAddressBar.slice(0, MAX_TEXT_LEN), true);
  }
});
