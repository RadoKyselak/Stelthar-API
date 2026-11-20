const API = "https://stelthar-api.vercel.app";
// A passage, each claim in it answered on its own. One claim gets the full
// check; several are checked on free official data.
const CHECK_URL = `${API}/v2/check`;
// The full check for one claim: what "Search further" runs.
const VERIFY_URL = `${API}/v2/verify`;
// What every version before 2.2 called. Used only if /v2/check is not there
// (an older server, or the feature switched off by taking the route out).
const LEGACY_URL = `${API}/verify`;
// v3: answers are stored as a passage ({claims: [...]}), not one result.
const CACHE_KEY = "stelthar_cache_v3";
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

const verdictLabel = (result) =>
  (result && result.display && result.display.verdict && result.display.verdict.label) ||
  (result && (result.verdict_detail || result.verdict)) || "Can't verify";

// An outage is not a finding: saving it would keep showing the outage after
// the service recovers.
const isOutage = (result) =>
  !!(result && (result.degraded || (result.display && result.display.status === "unavailable")));

async function appendHistory(text, check) {
  const data = await storageGet([HISTORY_KEY]);
  const history = data[HISTORY_KEY] || [];
  const claims = check.claims || [];
  const entry = {
    claim: text,
    // One claim: its verdict. A passage: how many claims it held.
    verdict: claims.length === 1 ? verdictLabel(claims[0].result) : `${claims.length} claims`,
    ts: Date.now(),
  };
  const deduped = history.filter(
    (h) => normalizeClaim(h.claim) !== normalizeClaim(text)
  );
  deduped.unshift(entry);
  await storageSet({ [HISTORY_KEY]: deduped.slice(0, HISTORY_MAX_ENTRIES) });
}

async function postJSON(url, body, what) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("The check took too long. Try a shorter, more specific claim (a number, a year, and what it measures).");
    }
    throw new Error("Could not reach the checking service. Check your connection and try again.");
  } finally {
    clearTimeout(timer);
  }
  if (resp.status === 429) {
    throw new Error("Too many checks from this connection for now. Try again in a little while; saved results still open.");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const errJson = await resp.json();
      detail = typeof errJson.detail === "string" ? errJson.detail : detail;
    } catch (e) {
      // keep statusText
    }
    // A 400 carries a sentence written for the person (why a page could not be read).
    const err = new Error(resp.status === 400 && detail ? detail : `${what} failed (${resp.status}): ${detail}`);
    err.status = resp.status;
    throw err;
  }
  return resp.json();
}

// Every claim in `text`, checked. Answers with the passage shape the result
// card draws: { claims: [{ span, claim, deeper, result }], truncated }.
async function checkText(text, { bypassCache = false } = {}) {
  if (!bypassCache) {
    const cached = await getCachedResult(text);
    if (cached) return { ...cached, _cached: true };
  }
  // A lone web address is a page to fetch and check, not a claim.
  const link = /^https?:\/\/\S+$/i.test(text.trim());
  let check;
  try {
    // "Check again" (bypassCache) also asks the server for a fresh answer
    // instead of the one it stored, and that answer replaces it for everyone.
    const body = link ? { url: text.trim() } : { text };
    if (bypassCache) body.fresh = true;
    check = await postJSON(CHECK_URL, body, "The check");
  } catch (e) {
    if (e.status !== 404 && e.status !== 405) throw e;
    if (link) throw new Error("Checking a page by its link isn't available right now. Highlight the text on the page instead.");
    // No /v2/check on this server: one claim, the way versions before 2.2 asked.
    const result = await postJSON(LEGACY_URL, { claim: text.slice(0, 1000) }, "The check");
    check = { claims: [{ index: 0, span: text, claim: text, deeper: false, result }], count: 1, truncated: false };
  }
  if (!(check.claims || []).some((c) => isOutage(c.result))) {
    await setCachedResult(text, check);
    await appendHistory(text, check);
  }
  return check;
}

// The full check for one claim of a passage, asked for by the person reading.
const checkDeeper = (claim) => postJSON(VERIFY_URL, { claim }, "The search");

// Everything opens in Chrome's side panel. The extension never runs on the
// page itself: right-click passes the highlighted text, so no access to
// websites is needed.
const PENDING_KEY = "mirador_pending";

// The panel picks this up when it opens, or right away if it is already open.
// intent "check": right-click is "Fact-check with Mirador", so the
// highlighted text is checked as a claim even when it has no figure in it.
const sendToPanel = (text, intent) =>
  chrome.storage.session.set({ [PENDING_KEY]: { text, intent, at: Date.now() } });

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

chrome.runtime.onInstalled.addListener((details) => {
  // A first install opens the short walkthrough, once. Updates open nothing.
  if (details && details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") }).catch(() => {});
  }
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "mirador-check",
      title: "Fact-check with Mirador",
      contexts: ["selection"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "mirador-check" || !info.selectionText || !tab) return;
  // open() must run before anything async, or Chrome no longer counts it as
  // the user's click.
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
  sendToPanel(info.selectionText.replace(/\s+/g, " ").trim(), "check");
});

chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "open-panel" || !tab) return;
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "checkText") {
    checkText(message.text, { bypassCache: message.bypassCache })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true; // async sendResponse
  }
  if (message.type === "deeper") {
    checkDeeper(message.claim)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
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

const LOOKUP_URL = `${API}/v2/lookup`;

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
