const BACKEND_URL = "https://stelthar-api.vercel.app/verify";

let lastStoredClaim = "";

document.addEventListener("DOMContentLoaded", () => {
    const claimTextEl = document.getElementById("claim-text");
    const manualClaimInputEl = document.getElementById("manual-claim-input");
    const verifyBtn = document.getElementById("verifyBtn");
    const loadingIndicator = document.getElementById("loading-indicator");
    const resultsArea = document.getElementById("results-area");
    const verdictTextEl = document.getElementById("verdict-text");
    const confidenceTextEl = document.getElementById("confidence-text");
    const summaryTextEl = document.getElementById("summary-text");
    const evidenceLinksListEl = document.getElementById("evidence-links-list");
    const sourcesDropdown = document.getElementById("sources-dropdown");
    const sourcesListEl = document.getElementById("sources-list");
    const errorMessageEl = document.getElementById("error-message");

    const clearResults = () => {
        resultsArea.style.display = "none";
        sourcesDropdown.style.display = "none";
        sourcesDropdown.open = false;
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

    const setVerifyButtonState = () => {
        verifyBtn.disabled = !getClaimToVerify();
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

        setVerifyButtonState();
    });

    manualClaimInputEl.addEventListener("input", () => {
        setVerifyButtonState();
    });

    verifyBtn.onclick = async () => {
        clearResults();
        verifyBtn.disabled = true;
        loadingIndicator.style.display = "flex";

        const claim = getClaimToVerify();

        if (!claim) {
            errorMessageEl.textContent = "No claim found. Highlight text or paste one into the box.";
            errorMessageEl.style.display = "block";
            loadingIndicator.style.display = "none";
            setVerifyButtonState();
            return;
        }

        chrome.storage.local.set({ stelthar_last_claim: claim });
        lastStoredClaim = claim;

        try {
            const resp = await fetch(BACKEND_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ claim }),
            });

            if (!resp.ok) {
                let errorDetail = resp.statusText;
                try {
                    const errorJson = await resp.json();
                    errorDetail = errorJson.detail || errorDetail;
                } catch (e) {
                    // no-op: keep fallback detail
                }
                throw new Error(`Backend error: ${resp.status} ${errorDetail}`);
            }

            const jsonResp = await resp.json();
            loadingIndicator.style.display = "none";
            resultsArea.style.display = "block";
            setVerifyButtonState();

            const verdict = (jsonResp.verdict || "Inconclusive").toUpperCase();
            verdictTextEl.textContent = `VERDICT: ${verdict}`;
            verdictTextEl.className = (jsonResp.verdict || "inconclusive").toLowerCase();
            confidenceTextEl.textContent = `CONFIDENCE: ${formatConfidence(jsonResp.confidence)}`;
            summaryTextEl.textContent = jsonResp.summary || "No summary provided.";

            evidenceLinksListEl.innerHTML = "";
            if (jsonResp.evidence_links?.length) {
                jsonResp.evidence_links.forEach((link) => {
                    const li = document.createElement("li");
                    const a = document.createElement("a");
                    a.href = link.source_url;
                    a.textContent = link.finding;
                    a.target = "_blank";
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
                    li.appendChild(a);

                    sourcesListEl.appendChild(li);
                });
            }
        } catch (error) {
            loadingIndicator.style.display = "none";
            errorMessageEl.textContent = `Error: ${error.message}`;
            errorMessageEl.style.display = "block";
            setVerifyButtonState();
        }
    };
});
