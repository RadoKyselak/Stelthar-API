// Project Mirador side panel. One field: a sentence with a figure in it is
// checked as a claim; a measure without one ("unemployment texas 2023") is
// looked up. Right-click "Check with Project Mirador" sends the highlighted
// text here; the address bar ("mir ...") opens this page in a tab with ?q=.
const MAX_CLAIM_LEN = 600;
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

  const check = (claim, bypassCache = false) => {
    const mine = ++ticket;
    show(View.loading());
    chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
      if (mine !== ticket) return;
      if (chrome.runtime.lastError || !resp) {
        show(View.error("The extension didn't respond. Close the panel and try again.",
          { onRetry: () => check(claim, true) }));
        return;
      }
      if (!resp.ok) {
        show(View.error(resp.error, { onRetry: () => check(claim, true) }));
        return;
      }
      show(View.render(resp.result, {
        onRecheck: resp.result._cached ? () => check(claim, true) : null,
      }));
      loadRecent();
    });
  };

  const find = async (query) => {
    const mine = ++ticket;
    const retry = { onRetry: () => find(query) };
    show(View.loading());
    try {
      const reply = await chrome.runtime.sendMessage({ type: "lookup", query });
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

  const run = (t, setField = false) => {
    t = (t || "").trim();
    if (!t) return;
    if (setField) q.value = t;
    asked = t.replace(/\s+/g, " ");
    document.body.classList.add("answered");
    document.body.classList.remove("editing");
    autosize(); // after the button hides, so the height fits the wider field
    window.scrollTo(0, 0); // the answer appears at the top, even if a recent item far down was clicked
    if (t.length > MAX_CLAIM_LEN) {
      show(View.error(`That's ${t.length} characters, too long to check. Highlight one sentence with one figure in it.`));
      return;
    }
    if (View.intentOf(t) === "check") check(t);
    else find(t);
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
        b.append(t, v);
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
    run(p.text, true);
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
    run(fromAddressBar.slice(0, MAX_CLAIM_LEN), true);
  }
});
