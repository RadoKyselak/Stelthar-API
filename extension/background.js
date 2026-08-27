const BACKEND_URL = "https://stelthar-api.vercel.app/verify";
const CACHE_KEY = "stelthar_cache";
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
    verdict: result.verdict || "Inconclusive",
    confidence: result.confidence ?? null,
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
      throw new Error("Verification timed out. The claim may be too complex — try a shorter, more specific claim.");
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
  await setCachedResult(claim, result);
  await appendHistory(claim, result);
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
    title: "Verify with Project Mirador",
    contexts: ["selection"],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "stelthar-verify" || !info.selectionText) return;
  const claim = info.selectionText.trim();
  storageSet({ stelthar_last_claim: claim }).then(() => {
    if (tab?.id) {
      chrome.tabs.sendMessage(tab.id, { type: "showPanel", claim }, () => {
        // Content script may not be injected (chrome:// pages, PDFs) —
        // fall back to the popup.
        if (chrome.runtime.lastError) openPopupWithClaim(claim);
      });
    } else {
      openPopupWithClaim(claim);
    }
  });
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "verify-selection") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  try {
    const [{ result: selection }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection().toString().trim(),
    });
    if (selection) {
      await storageSet({ stelthar_last_claim: selection });
      chrome.tabs.sendMessage(tab.id, { type: "showPanel", claim: selection }, () => {
        if (chrome.runtime.lastError) openPopupWithClaim(selection);
      });
    }
  } catch (e) {
    // Restricted page (chrome://, web store) — nothing we can do.
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
// popup and no mouse. Type "mir", Tab, "unemployment 2023", Enter — the
// citation is on the clipboard.
//
// Suggestions carry the actual number as the user types, because seeing
// "3.6% — BLS, 2023 annual average" in the dropdown is what tells them the
// query landed on the series they meant, before they commit to pasting it.
// ===========================================================================

const LOOKUP_URL = "https://stelthar-api.vercel.app/v2/lookup";

// "unemployment 2023 /note" -> copy the full footnote instead of the inline form.
const FORMAT_SUFFIXES = {
  "/note": "note",
  "/md": "markdown",
  "/markdown": "markdown",
  "/value": "value",
  "/raw": "value",
  "/html": "html",
};

function splitFormat(input) {
  const trimmed = (input || "").trim();
  for (const [suffix, format] of Object.entries(FORMAT_SUFFIXES)) {
    if (trimmed.toLowerCase().endsWith(suffix)) {
      return { query: trimmed.slice(0, -suffix.length).trim(), format };
    }
  }
  return { query: trimmed, format: "inline" };
}

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
  const { query, format } = splitFormat(input);
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
            `<dim>${xmlEscape(c.attribution || "")}</dim> — press Enter to copy` +
            (format !== "inline" ? ` <dim>as ${xmlEscape(format)}</dim>` : ""),
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

async function ensureClipboardDocument() {
  const existing = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
  });
  if (existing && existing.length) return;
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["CLIPBOARD"],
    justification: "Copy a citation to the clipboard from the omnibox.",
  });
}

async function copyCitation(plain, html) {
  await ensureClipboardDocument();
  const reply = await chrome.runtime.sendMessage({
    target: "offscreen-clipboard",
    plain,
    html,
  });
  return Boolean(reply && reply.ok);
}

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icons/icon128.png",
    title,
    message,
  });
}

chrome.omnibox.onInputEntered.addListener(async (input) => {
  const { query, format } = splitFormat(input);
  if (!query) return;
  try {
    const r = await runLookup(query);
    if (!r.ok) {
      notify("Mirador — no figure copied", r.headline || "No official series matches that.");
      return;
    }
    const c = r.citations || {};
    const plain = c[format] || c.inline || c.value || "";
    // Only the inline form has a rich equivalent; a footnote pastes as text.
    const html = format === "inline" ? c.html : "";
    const copied = await copyCitation(plain, html);
    notify(
      copied ? "Copied to clipboard" : "Could not reach the clipboard",
      copied ? plain + (c.caveat ? `\n\nNote: ${c.caveat}` : "") : plain
    );
  } catch (e) {
    notify("Mirador — lookup failed", e.message);
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "lookup") {
    runLookup(message.query)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message.type === "copyCitation") {
    copyCitation(message.plain, message.html)
      .then((ok) => sendResponse({ ok }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  return false;
});
