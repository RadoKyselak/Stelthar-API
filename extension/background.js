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
