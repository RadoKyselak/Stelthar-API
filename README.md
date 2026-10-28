# Stelthar API

<div align="center">

**Real-time, government-backed data fact-checking for a better, more transparent internet.**

[Install Extension](https://chromewebstore.google.com/detail/anehbncclhojcaihgeoebennaccpeegn?utm_source=item-share-cb) • [Report Bug](https://github.com/RadoKyselak/Project-Mirador/issues) • [Request Feature](https://github.com/RadoKyselak/Project-Mirador/issues)

</div>

---


## 📢 Updates

**November 19, 2025** - Project Mirador Chrome extension is now live on the Chrome Web Store!

**Install:** Search for **"Project Mirador"** on the Chrome Web Store or use the link above.

---

## 📑 Table of Contents

- [About](#about)
- [Key Features](#key-features)
- [How It Works](#how-it-works)
- [Getting Started](#getting-started)
  - [Chrome Extension](#chrome-extension)
  - [API Usage](#api-usage)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [API Documentation](#api-documentation)
- [Contributing](#contributing)
- [Disclaimer](#disclaimer)
- [License](#license)
- [Contact](#contact)

---

## 🎯 About

This repository contains the codebase for **Project Mirador** and the **Stelthar API**.

Stelthar API provides real-time, government-backed data fact-checking for a better, more transparent version of the internet.

### The Problem

Disinformation erodes public trust, and traditional fact-checking is slow, unreliable, or both. 

### The Solution

Stelthar's fact-checking API verifies claims by comparing them against official government data sources. Highlight any text, and our browser extension provides an evidence-based verdict, a confidence score, and links to the primary sources. 

**This reduces verification time from hours to seconds**, empowering users to challenge misinformation with auditable proof.

---

## ✨ Key Features

-  **Real-time claim verification** against official government data sources
-  **Evidence-based verdicts**: Supported, Contradicted, or Inconclusive
-  **Confidence scoring** that reflects data coverage and match quality
-  **Direct links** to the primary sources used for verification
-  **Browser extension integration** for on-page highlight-and-check functionality
-  **API-first design** for easy integration into apps, dashboards, and extensions

---

## 🔍 How It Works

1. **Highlight**: Select any text claim on a webpage
2. **Verify**: Click the Project Mirador extension icon
3. **Review**: Receive an instant verdict with:
   - Supported/Contradicted/Inconclusive status
   - Confidence score (0-100%)
   - Links to official government sources
   - Relevant data points used for verification

**Under the Hood:**
- Your claim is sent to the Stelthar API
- The API queries multiple government databases (Data.gov, BLS, BEA, etc.)
- AI-powered analysis compares your claim against official data
- Results are returned with full source attribution

---

## 🚀 Getting Started

### Chrome Extension

1. Visit the [Chrome Web Store](https://chromewebstore.google.com/detail/anehbncclhojcaihgeoebennaccpeegn?utm_source=item-share-cb)
2. Click **"Add to Chrome"**
3. Pin the extension to your toolbar for easy access
4. Navigate to any webpage and highlight text to fact-check
5. Click the Project Mirador icon to verify the claim

### API Usage

The Stelthar API powers Project Mirador and is available for integration into your own applications.

**Quick Example:**

```javascript
// Example API request (pseudocode)
fetch('https://api.stelthar.com/verify', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    claim: "The unemployment rate in 2024 was 3.7%"
  })
})
.then(response => response.json())
.then(data => console.log(data));
```

See the [API Documentation](#api-documentation) section below for detailed endpoint information.

---

## 🛠 Tech Stack

### Backend
- **Language**: Python
- **Hosting**: Vercel (API hosting)

### Frontend
- **Languages**: JavaScript, HTML, CSS
- **Platform**: Chrome Extension

### Data Sources

**Structured (return actual numbers — always preferred):**
- **USAspending.gov** — federal agency budgets & obligations by fiscal year *(no API key)*
- **Treasury Fiscal Data** — national debt / public debt outstanding *(no API key)*
- **Bureau of Economic Analysis (BEA)** — GDP, personal income, government receipts &
  expenditures by function, trade (12 NIPA tables)
- **Bureau of Labor Statistics (BLS)** — unemployment, CPI, core CPI, PPI, labor force
  participation, employment level, average hourly earnings
- **U.S. Census Bureau (ACS)** — population, income, poverty, age, home value, education,
  insurance coverage, by state or nationally

**Keyword search (return dataset descriptions, not values — supplement only):**
- Data.gov catalog
- Congress.gov (bills & legislation)

### AI & Intelligence
- **Gemini API** (restrained for fact-checking purposes)

### Development Tools
- **API Testing**: Postman
- **Version Control**: Git & GitHub

---

## 🏗 Architecture

Stelthar runs a **tiered evidence engine**. Cheap, exact sources run first; a
model is only called when no official series covers the claim.

| Tier | What runs | Cost | Typical latency |
|---|---|---|---|
| 1 — structured | Deterministic claim parser → BLS, BEA, Census ACS, Treasury | 0 model calls | 0.3–1s |
| 2 — grounded | One Gemini call with Google Search grounding | 1 model call | 3–8s |

- **Parse:** metric, year (calendar vs fiscal), month, geography, comparator
  ("more than", "under") and the precision the claim was written to.
- **Fetch:** the exact series for that metric, with the period and geography
  read from the agency's response — never copied from the request.
- **Assess:** compares within the claim's written precision (and the survey's
  margin of error for Census), and fails closed on period or geography mismatch.
- **Verdicts:** `Supported`, `Mostly accurate`, `Contradicted`, `Misleading`,
  or `No verdict` with a machine-readable `reason` saying what would have to
  change. `Mostly accurate` means the claim is off by one step in its last
  written digit and within 5% ("Texas unemployment was 4.1%" when BLS reports
  4.0%); the headline states the gap.

**Model resilience.** `GEMINI_MODEL` is a preference, not a single point of
failure: if Google retires it, the API falls through `GEMINI_FALLBACK_MODELS`
(default `gemini-3.8-flash`, then the `gemini-flash-latest` alias) and keeps
serving. `GET /health` shows the model in use and which source keys are set.

---

## 📖 API Documentation

### Base URL
```
https://stelthar-api.vercel.app
```

### Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /verify` | Verify a claim. v2 engine, v1-compatible response (what the extension calls). |
| `POST /v2/verify` | Same engine, v2-only response fields. |
| `POST /v2/lookup` | "What is the official figure?" — value plus ready-to-paste citations. |
| `POST /v1/verify` | Legacy multi-pass planner, kept for comparison. |
| `GET /health` | Model in use, configured sources. |

#### `POST /verify`

```json
{ "claim": "Inflation hit 9% in June 2022" }
```

Response (abridged — `sources`, `evidence`, `citations` and `parsed` are also returned):

```json
{
    "verdict": "Supported",
    "verdict_detail": "Supported",
    "confidence": 0.9,
    "confidence_tier": "High",
    "summary": "Claim says 9%. BLS reports 9.06% for June 2022 (M06 2022 vs M06 2021, not seasonally adjusted).",
    "evidence_links": [
        {
            "finding": "CPI-U inflation rate (year over year): 9.06% (June 2022 (M06 2022 vs M06 2021, not seasonally adjusted)) \u2014 BLS",
            "source_url": "https://data.bls.gov/timeseries/CUUR0000SA0"
        }
    ],
    "headline": "Claim says 9%. BLS reports 9.06% for June 2022 (M06 2022 vs M06 2021, not seasonally adjusted).",
    "reason": null,
    "claim_value": "9%",
    "official_value": "9.06% (June 2022 (M06 2022 vs M06 2021, not seasonally adjusted))",
    "caveats": [],
    "tier": "structured",
    "model_calls": 0,
    "elapsed_seconds": 0.0,
    "degraded": false
}
```

`search_suggestions_html` is set when the grounded tier answered. Any UI that
shows grounded citations **must** render it (Google Search grounding terms).

**Coverage (tier 1, exact figures, no AI):**

| Area | Claims it answers | Source |
|---|---|---|
| Jobs | Unemployment rate — national, **by state**, and for Black, White, Hispanic, Asian, women, men, teens (16–19), youth (16–24), veterans; labor-force participation; payrolls; hourly earnings | BLS (CPS, LAUS, CES) |
| Prices | Inflation, core inflation, PPI; **average prices of gas, eggs, milk, ground beef, coffee, electricity** (latest month for "is…" claims) | BLS (CPI, Average Price Data) |
| Federal budget | **Deficit, revenue, spending** by fiscal year; national debt | Treasury (Monthly Treasury Statement, Debt to the Penny) |
| Economy | GDP, real GDP growth (annual and **quarterly, annual rate**), personal income | BEA (NIPA) |
| People | Population, median household income, poverty, uninsured, home value, bachelor's degree — national or by state | Census ACS 1-year |

Also checked exactly: **two-point claims** ("unemployment fell from 8.1% in 2020 to 3.6% in 2023" — each value against its own year). Everything else goes to tier 2 (AI search with citations, behind spend limits); an AI verdict with no source behind it is never shown.

---

## 🤝 Contributing

Any contributions you make are **greatly appreciated**.

### Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/RadoKyselak/Project-Mirador.git
   cd Project-Mirador
   ```

2. **Install dependencies**
   ```bash
   # Backend dependencies
   pip install -r backend/requirements.txt -r backend/requirements-dev.txt
   
   # Frontend - load the extension in Chrome
   # Navigate to chrome://extensions/
   # Enable "Developer mode"
   # Click "Load unpacked" and select the extension directory
   ```

3. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Add your API keys for government data sources and Gemini
   ```

4. **Run tests**
   ```bash
   make test
   ```

### Contribution Guidelines

- Fork the repository
- Create a feature branch (`git checkout -b feature/AmazingFeature`)
- Commit your changes (`git commit -m 'Add some AmazingFeature'`)
- Push to the branch (`git push origin feature/AmazingFeature`)
- Open a Pull Request

---

## ⚠️ Disclaimer

Stelthar API uses publicly available U.S. Government open data sources but is **not affiliated with or endorsed by any governmental agency**.

Verdicts and confidence scores are algorithmic estimates — they **assist, not replace, human judgment**. Always verify critical information through multiple sources.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 📧 Contact

**Rado Kyselak** - Developer

- GitHub: [@RadoKyselak](https://github.com/RadoKyselak)

---

<div align="center">

**Open Truth. Verified Data.**

*Project Mirador - Powered by Stelthar API*
</div>
