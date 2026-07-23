const MAX_CLAIM_LEN = 600;

let lastStoredClaim = "";
let currentClaim = "";

document.addEventListener("DOMContentLoaded", () => {
    const claimTextEl = document.getElementById("claim-text");
    const manualClaimInputEl = document.getElementById("manual-claim-input");
    const charCountEl = document.getElementById("char-count");
    const verifyBtn = document.getElementById("verifyBtn");
    const loadingIndicator = document.getElementById("loading-indicator");
    const resultsArea = document.getElementById("results-area");
    const verdictTextEl = document.getElementById("verdict-text");
    const confidenceTextEl = document.getElementById("confidence-text");
    const cachedBadgeEl = document.getElementById("cached-badge");
    const recheckLink = document.getElementById("recheck-link");
    const breakdownEl = document.getElementById("breakdown");
    const summaryTextEl = document.getElementById("summary-text");
    const evidenceLinksListEl = document.getElementById("evidence-links-list");
    const sourcesDropdown = document.getElementById("sources-dropdown");
    const sourcesListEl = document.getElementById("sources-list");
    const errorMessageEl = document.getElementById("error-message");
    const historyDropdown = document.getElementById("history-dropdown");
    const historyListEl = document.getElementById("history-list");

    const clearResults = () => {
        resultsArea.style.display = "none";
        sourcesDropdown.style.display = "none";
        sourcesDropdown.open = false;
        cachedBadgeEl.style.display = "none";
        breakdownEl.style.display = "none";
        errorMessageEl.style.display = "none";
        errorMessageEl.textContent = "";
        verdictTextEl.textContent = "VERDICT: ...";
        verdictTextEl.className = "";
        confidenceTextEl.textContent = "CONFIDENCE: ...";
        summaryTextEl.textContent = "Loading summary...";
        evidenceLinksListEl.innerHTML = "";
        sourcesListEl.innerHTML = "";
    };

    const formatConfidence = (conf) => {
        if (conf === null || conf === undefined) return "N/A";

        if (typeof conf === "number") {
            const value = conf > 0 && conf <= 1 ? conf * 100 : conf;
            return `${Math.round(value)}%`;
        }

        if (typeof conf === "string") {
            const s = conf.trim();
            if (s.endsWith("%")) return s;

            const num = Number(s.replace(",", ""));
            if (Number.isNaN(num)) return s;

            const value = num > 0 && num <= 1 ? num * 100 : num;
            return `${Math.round(value)}%`;
        }

        return String(conf);
    };

    const getClaimToVerify = () => {
        const manualClaim = manualClaimInputEl.value.trim();
        return manualClaim || lastStoredClaim;
    };

    const updateCharCount = () => {
        const len = manualClaimInputEl.value.trim().length;
        if (!len) {
            charCountEl.textContent = "";
            charCountEl.className = "";
            return;
        }
        charCountEl.textContent = `${len} / ${MAX_CLAIM_LEN}`;
        charCountEl.className = len > MAX_CLAIM_LEN ? "over" : "";
    };

    const setVerifyButtonState = () => {
        const claim = getClaimToVerify();
        verifyBtn.disabled = !claim || claim.length > MAX_CLAIM_LEN;
    };

    const setBreakdownBar = (id, value) => {
        const fill = document.getElementById(id);
        const label = document.getElementById(`${id}-val`);
        const pct = Math.max(0, Math.min(100, Math.round((value ?? 0) * 100)));
        fill.style.width = `${pct}%`;
        label.textContent = `${pct}%`;
    };

    const renderResult = (jsonResp) => {
        loadingIndicator.style.display = "none";
        resultsArea.style.display = "block";
        setVerifyButtonState();

        const verdict = (jsonResp.verdict || "Inconclusive").toUpperCase();
        verdictTextEl.textContent = `VERDICT: ${verdict}`;
        verdictTextEl.className = (jsonResp.verdict || "inconclusive").toLowerCase();

        const tier = jsonResp.confidence_tier ? ` (${jsonResp.confidence_tier})` : "";
        confidenceTextEl.textContent = `CONFIDENCE: ${formatConfidence(jsonResp.confidence)}${tier}`;

        cachedBadgeEl.style.display = jsonResp._cached ? "block" : "none";

        const bd = jsonResp.confidence_breakdown;
        if (bd && typeof bd === "object") {
            breakdownEl.style.display = "block";
            setBreakdownBar("bd-reliability", bd.source_reliability);
            setBreakdownBar("bd-density", bd.evidence_density);
            setBreakdownBar("bd-semantic", bd.semantic_alignment);
        }

        summaryTextEl.textContent = jsonResp.summary || "No summary provided.";

        evidenceLinksListEl.innerHTML = "";
        if (jsonResp.evidence_links?.length) {
            jsonResp.evidence_links.forEach((link) => {
                const li = document.createElement("li");
                const a = document.createElement("a");
                a.href = link.source_url;
                a.textContent = link.finding;
                a.target = "_blank";
                a.rel = "noopener noreferrer";
                li.appendChild(a);
                evidenceLinksListEl.appendChild(li);
            });
        } else {
            evidenceLinksListEl.innerHTML = "<li>No evidence links provided.</li>";
        }

        sourcesListEl.innerHTML = "";
        if (jsonResp.sources?.length) {
            sourcesDropdown.style.display = "block";
            jsonResp.sources.forEach((source) => {
                const li = document.createElement("li");
                const titleSpan = document.createElement("span");
                titleSpan.className = "source-title";
                titleSpan.textContent = source.title;
                li.appendChild(titleSpan);

                if (source.snippet) {
                    const snippetSpan = document.createElement("span");
                    snippetSpan.className = "source-snippet";
                    snippetSpan.textContent = `"${source.snippet}"`;
                    li.appendChild(snippetSpan);
                }

                const a = document.createElement("a");
                a.href = source.url;
                a.textContent = source.url;
                a.target = "_blank";
                a.rel = "noopener noreferrer";
                li.appendChild(a);

                sourcesListEl.appendChild(li);
            });
        }
    };

    const runVerification = (claim, { bypassCache = false } = {}) => {
        clearResults();
        verifyBtn.disabled = true;
        loadingIndicator.style.display = "flex";
        currentClaim = claim;

        chrome.storage.local.set({ stelthar_last_claim: claim });
        lastStoredClaim = claim;

        chrome.runtime.sendMessage({ type: "verifyClaim", claim, bypassCache }, (resp) => {
            loadingIndicator.style.display = "none";
            setVerifyButtonState();

            if (chrome.runtime.lastError || !resp) {
                errorMessageEl.textContent = "Error: could not reach the extension background service. Try reloading the extension.";
                errorMessageEl.style.display = "block";
                return;
            }
            if (!resp.ok) {
                errorMessageEl.textContent = `Error: ${resp.error}`;
                errorMessageEl.style.display = "block";
                return;
            }

            renderResult(resp.result);
            loadHistory();
        });
    };

    const loadHistory = () => {
        chrome.storage.local.get(["stelthar_history"], (data) => {
            const history = data.stelthar_history || [];
            if (!history.length) {
                historyDropdown.style.display = "none";
                return;
            }
            historyDropdown.style.display = "block";
            historyListEl.innerHTML = "";
            history.forEach((entry) => {
                const li = document.createElement("li");
                const verdictSpan = document.createElement("span");
                const verdictRaw = (entry.verdict || "inconclusive").toLowerCase();
                verdictSpan.className = `hist-verdict ${verdictRaw}`;
                verdictSpan.textContent = `${verdictRaw.toUpperCase()} · ${formatConfidence(entry.confidence)}`;
                const claimSpan = document.createElement("span");
                claimSpan.className = "hist-claim";
                claimSpan.textContent = entry.claim;
                li.appendChild(verdictSpan);
                li.appendChild(claimSpan);
                li.title = "Click to re-run this check";
                li.onclick = () => {
                    manualClaimInputEl.value = entry.claim;
                    updateCharCount();
                    runVerification(entry.claim);
                };
                historyListEl.appendChild(li);
            });
        });
    };

    chrome.storage.local.get(["stelthar_last_claim"], (data) => {
        const storedClaim = (data.stelthar_last_claim || "").trim();
        lastStoredClaim = storedClaim;

        if (storedClaim) {
            claimTextEl.textContent = `Highlighted claim: "${storedClaim}"`;
            manualClaimInputEl.value = storedClaim;
        } else {
            claimTextEl.textContent = "No highlighted claim detected. Paste text below to verify directly.";
        }

        updateCharCount();
        setVerifyButtonState();
    });

    manualClaimInputEl.addEventListener("input", () => {
        updateCharCount();
        setVerifyButtonState();
    });

    recheckLink.addEventListener("click", () => {
        if (currentClaim) runVerification(currentClaim, { bypassCache: true });
    });

    verifyBtn.onclick = () => {
        const claim = getClaimToVerify();

        if (!claim) {
            errorMessageEl.textContent = "No claim found. Highlight text or paste one into the box.";
            errorMessageEl.style.display = "block";
            return;
        }
        if (claim.length > MAX_CLAIM_LEN) {
            errorMessageEl.textContent = `Claim is too long (${claim.length} chars). Keep it under ${MAX_CLAIM_LEN} characters for accurate verification.`;
            errorMessageEl.style.display = "block";
            return;
        }

        runVerification(claim);
    };

    loadHistory();
});
