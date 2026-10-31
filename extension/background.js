const BACKEND_URL = "https://stelthar-api.vercel.app/verify";
// v2: results cached under the old key include a week of "Inconclusive"
// answers produced while the backend's Gemini model was retired. A new key
// drops them on update instead of serving them for another 12 hours.
const CACHE_KEY = "stelthar_cache_v2";
const HISTORY_KEY = "stelthar_history";
const CACHE_TTL_MS = 12 * 60 * 60 * 1000; // 12h — gov data doesn't change intra-day
const CACHE_MAX_ENTRIES = 50;
const HISTORY_MAX_ENTRIES = 10;
const FETCH_TIMEOUT_MS = 60000;

const normalizeClaim = (claim) => claim.trim().replace(/\s+/g, " ").toLowerCase();

const storageGet = (keys) =>
  new Promise((resolve) => chrome.storage.local.get(keys, resolve));
const storageSet = (obj) =>
  new Promise((resolve) => chrome.storage.local.set(obj, resolve));

async function getCachedResult(claim) {
  const data = await storageGet([CACHE_KEY]);
  const cache = data[CACHE_KEY] || {};
  const entry = cache[normalizeClaim(claim)];
  if (entry && Date.now() - entry.ts < CACHE_TTL_MS) return entry.result;
  return null;
}

async function setCachedResult(claim, result) {
  const data = await storageGet([CACHE_KEY]);
  const cache = data[CACHE_KEY] || {};
  cache[normalizeClaim(claim)] = { ts: Date.now(), result };

  // Evict oldest entries beyond the cap
  const keys = Object.keys(cache);
  if (keys.length > CACHE_MAX_ENTRIES) {
    keys
      .sort((a, b) => cache[a].ts - cache[b].ts)
      .slice(0, keys.length - CACHE_MAX_ENTRIES)
      .forEach((k) => delete cache[k]);
  }
  await storageSet({ [CACHE_KEY]: cache });
}

async function appendHistory(claim, result) {
  const data = await storageGet([HISTORY_KEY]);
  const history = data[HISTORY_KEY] || [];
  const entry = {
    claim,
    verdict: result.verdict_detail || result.verdict || "Inconclusive",
    confidence: result.confidence ?? null,
    confidence_tier: result.confidence_tier || null,
    ts: Date.now(),
  };
  const deduped = history.filter(
    (h) => normalizeClaim(h.claim) !== normalizeClaim(claim)
  );
  deduped.unshift(entry);
  await storageSet({ [HISTORY_KEY]: deduped.slice(0, HISTORY_MAX_ENTRIES) });
}

async function verifyClaim(claim, { bypassCache = false } = {}) {
  if (!bypassCache) {
    const cached = await getCachedResult(claim);
    if (cached) return { ...cached, _cached: true };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(BACKEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ claim }),
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("Verification timed out. Try a shorter, more specific claim (a number, a year, and what it measures).");
    }
    throw new Error("Could not reach the verification service. Check your connection and try again.");
  } finally {
    clearTimeout(timer);
  }

  if (resp.status === 429) {
    throw new Error("Daily rate limit reached (100 checks/day). Cached results are still available.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const errJson = await resp.json();
      detail = errJson.detail || detail;
    } catch (e) {
      // keep statusText
    }
    throw new Error(`Verification failed (${resp.status}): ${detail}`);
  }

  const result = await resp.json();
  // A degraded answer is an outage, not a finding; caching it would keep
  // showing the outage after the service recovers.
  if (!result.degraded) {
    await setCachedResult(claim, result);
    await appendHistory(claim, result);
  }
  return result;
}

// Everything opens in Chrome's side panel. The extension never runs on the
// page itself: right-click passes the highlighted text, so no access to
// websites is needed.
const PENDING_KEY = "mirador_pending";

// The panel picks this up when it opens, or right away if it is already open.
const sendToPanel = (text) =>
  chrome.storage.session.set({ [PENDING_KEY]: { text, at: Date.now() } });

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "mirador-check",
      title: "Check with Project Mirador",
      contexts: ["selection"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "mirador-check" || !info.selectionText || !tab) return;
  // open() must run before anything async, or Chrome no longer counts it as
  // the user's click.
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
  sendToPanel(info.selectionText.replace(/\s+/g, " ").trim());
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "open-panel" || !tab) return;
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "verifyClaim") {
    verifyClaim(message.claim, { bypassCache: message.bypassCache })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true; // async sendResponse
  }
  if (message.type === "lookup") {
    runLookup(message.query, !!message.deep)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  return false;
});

// ===========================================================================
// Lookup — the retrieval path
//
// Verification needs the user to already suspect something, which is rare.
// Looking a figure up while writing is constant, so the fastest possible
// surface matters more than the prettiest one: the omnibox needs no click, no
// panel and no mouse. Type "mir", Tab, "unemployment 2023": the figure shows
// as you type, and Enter opens it with its citation in the side panel.
//
// Suggestions carry the actual number as the user types, because seeing
// "3.6% — BLS, 2023 annual average" in the dropdown is what tells them the
// query landed on the series they meant.
// ===========================================================================

const LOOKUP_URL = "https://stelthar-api.vercel.app/v2/lookup";

const lookupMemo = new Map(); // query -> result, best-effort across a keystroke burst

// deep: an explicit search from the panel. Only then may the server have
// Claude search the datasets; the omnibox, which runs per keystroke, never does.
async function runLookup(query, deep = false) {
  const memoKey = (deep ? "deep:" : "") + query;
  if (lookupMemo.has(memoKey)) return lookupMemo.get(memoKey);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), deep ? 40000 : 15000);
  try {
    let resp;
    try {
      resp = await fetch(LOOKUP_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, deep }),
        signal: controller.signal,
      });
    } catch (e) {
      throw new Error(e.name === "AbortError"
        ? "The lookup took too long. Try again in a moment."
        : "Could not reach the lookup service. Check your connection and try again.");
    }
    if (resp.status === 429) throw new Error("Daily limit reached. Try again tomorrow.");
    if (!resp.ok) throw new Error(`The lookup service had a problem (${resp.status}). Try again in a moment.`);
    const result = await resp.json();
    lookupMemo.set(memoKey, result);
    if (lookupMemo.size > 40) lookupMemo.delete(lookupMemo.keys().next().value);
    return result;
  } finally {
    clearTimeout(timer);
  }
}

// Omnibox suggestion text is parsed as XML, so anything from the API has to be
// escaped or a stray ampersand in a series label silently drops the entry.
const xmlEscape = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&apos;");

let inputDebounce = null;

chrome.omnibox.setDefaultSuggestion({
  description: "Mirador — type a metric and a year, e.g. <match>unemployment 2023</match>",
});

chrome.omnibox.onInputChanged.addListener((input, suggest) => {
  const query = (input || "").trim();
  if (query.length < 2) return;

  clearTimeout(inputDebounce);
  inputDebounce = setTimeout(async () => {
    try {
      const r = await runLookup(query);
      if (r.ok) {
        const c = r.citations || {};
        chrome.omnibox.setDefaultSuggestion({
          description:
            `<match>${xmlEscape(c.value || r.value)}</match> ` +
            `<dim>${xmlEscape(c.attribution || "")}</dim> — press Enter to open`,
        });
        const rows = [];
        if (c.caveat) {
          rows.push({ content: input, description: `<dim>Note: ${xmlEscape(c.caveat)}</dim>` });
        }
        for (const alt of (r.also || []).slice(0, 3)) {
          rows.push({
            content: `${alt.metric} ${alt.period_year || ""}`.trim(),
            description:
              `<match>${xmlEscape(alt.value_display)}</match> ` +
              `<dim>${xmlEscape(alt.source)}, ${xmlEscape(alt.period)}</dim>`,
          });
        }
        suggest(rows);
        return;
      }

      chrome.omnibox.setDefaultSuggestion({
        description: `<dim>${xmlEscape(r.headline || "No match")}</dim>`,
      });
      suggest(
        (r.suggestions || []).slice(0, 5).map((s) => ({
          content: s.example,
          description: `<match>${xmlEscape(s.label)}</match> <dim>${xmlEscape(s.source)} — try “${xmlEscape(s.example)}”</dim>`,
        }))
      );
    } catch (e) {
      chrome.omnibox.setDefaultSuggestion({
        description: `<dim>${xmlEscape(e.message)}</dim>`,
      });
    }
  }, 220);
});

// Enter opens the figure, with its citation and Copy button, in the side
// panel. The panel needs a window id at the moment of the keypress, so the
// last focused window is tracked ahead of time (no permission needed).
let lastWindowId = null;
const rememberWindow = () =>
  chrome.windows.getLastFocused().then((w) => { lastWindowId = w.id; }).catch(() => {});
rememberWindow();
chrome.windows.onFocusChanged.addListener((id) => {
  if (id !== chrome.windows.WINDOW_ID_NONE) lastWindowId = id;
});
chrome.omnibox.onInputStarted.addListener(rememberWindow);

chrome.omnibox.onInputEntered.addListener((input) => {
  const query = (input || "").trim();
  if (!query) return;
  // Only if Chrome refuses the panel: show the same page in a tab instead.
  const inTab = () =>
    chrome.tabs.create({ url: chrome.runtime.getURL("panel.html") + "?q=" + encodeURIComponent(query) });
  if (lastWindowId == null) { inTab(); return; }
  chrome.sidePanel.open({ windowId: lastWindowId })
    .then(() => sendToPanel(query))
    .catch(inTab);
});
