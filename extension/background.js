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

async function openPopupWithClaim(claim) {
  await storageSet({ stelthar_last_claim: claim });
  try {
    await chrome.action.openPopup();
  } catch (e) {
    // openPopup requires a user gesture Chrome recognizes; the in-page
    // panel (content.js) is the fallback surface, so failing here is fine.
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "stelthar-verify",
    title: "Check with Project Mirador",
    contexts: ["selection"],
  });
});

// Nothing of Mirador runs on a page until it is asked to. The right-click
// item and the keyboard shortcut grant temporary access to that one tab
// (activeTab), and only then is the card injected. So the extension needs
// no access to any website, and installs with no permission warning.
async function showOnPage(tab, claim) {
  await storageSet({ stelthar_last_claim: claim });
  if (!tab?.id) return openPopupWithClaim(claim);
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["result-view.js", "content.js"],
    });
    await chrome.tabs.sendMessage(tab.id, { type: "showPanel", claim });
  } catch (e) {
    // Pages no extension may touch (chrome://, the Web Store, the PDF viewer).
    openPopupWithClaim(claim);
  }
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "stelthar-verify" || !info.selectionText) return;
  showOnPage(tab, info.selectionText.trim());
});

chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "verify-selection") return;
  if (!tab?.id) [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  try {
    const [{ result: selection }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => String(window.getSelection() || "").replace(/\s+/g, " ").trim(),
    });
    if (selection) await showOnPage(tab, selection);
  } catch (e) {
    // Restricted page (chrome://, the Web Store): nothing can be read there.
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "verify") {
    // Legacy path from the selection bubble: stash the claim and open the popup.
    openPopupWithClaim(message.claim);
    return false;
  }

  if (message.type === "verifyClaim") {
    verifyClaim(message.claim, { bypassCache: message.bypassCache })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true; // async sendResponse
  }

  return false;
});

// ===========================================================================
// Lookup — the retrieval path
//
// Verification needs the user to already suspect something, which is rare.
// Looking a figure up while writing is constant, so the fastest possible
// surface matters more than the prettiest one: the omnibox needs no click, no
// popup and no mouse. Type "mir", Tab, "unemployment 2023": the figure shows
// in the dropdown, and Enter opens it with its citation in a tab.
//
// Suggestions carry the actual number as the user types, because seeing
// "3.6% — BLS, 2023 annual average" in the dropdown is what tells them the
// query landed on the series they meant, before they commit to pasting it.
// ===========================================================================

const LOOKUP_URL = "https://stelthar-api.vercel.app/v2/lookup";

const lookupMemo = new Map(); // query -> result, best-effort across a keystroke burst

async function runLookup(query) {
  if (lookupMemo.has(query)) return lookupMemo.get(query);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const resp = await fetch(LOOKUP_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const result = await resp.json();
    lookupMemo.set(query, result);
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
            `<dim>${xmlEscape(c.attribution || "")}</dim> — press Enter for the source`,
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
        description: `<dim>Lookup unavailable (${xmlEscape(e.message)})</dim>`,
      });
    }
  }, 220);
});

// Enter opens the result in a tab of the extension's own page, where the card
// and its Copy citation button work like in the popup. (Copying straight from
// here would need clipboard, offscreen-document and notification permissions.)
chrome.omnibox.onInputEntered.addListener((input, disposition) => {
  const query = (input || "").trim();
  if (!query) return;
  const url = chrome.runtime.getURL(`popup.html?q=${encodeURIComponent(query.slice(0, 600))}`);
  if (disposition === "currentTab") chrome.tabs.update({ url });
  else chrome.tabs.create({ url, active: disposition !== "newBackgroundTab" });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "lookup") {
    runLookup(message.query)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  return false;
});
