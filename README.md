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
- Data.gov
- Bureau of Labor Statistics (BLS)
- Bureau of Economic Analysis (BEA)
- Congress.gov
- U.S. Census Bureau

### AI & Intelligence
- **Gemini API** (restrained for fact-checking purposes)

### Development Tools
- **API Testing**: Postman
- **Version Control**: Git & GitHub

---

## 🏗 Architecture

```
┌─────────────────┐
│  User Browser   │
│  (Chrome Ext)   │
└────────┬────────┘
         │
         │ Highlighted Claim
         ▼
┌─────────────────┐
│  Stelthar API   │
│   (Python)      │
└────────┬────────┘
         │
         ├──────────┐
         │          │
         ▼          ▼
┌──────────┐  ┌──────────┐
│ Gov Data │  │ Gemini   │
│ Sources  │  │ AI       │
└──────────┘  └──────────┘
         │          │
         └────┬─────┘
              │
              ▼
      ┌──────────────┐
      │   Verdict    │
      │ + Confidence │
      │ + Sources    │
      └──────────────┘
```

---

## 📖 API Documentation

### Base URL
```
https://api.stelthar.com
```

### Endpoints

#### `POST /verify`
Verify a claim against government data sources.

**Request Body:**
```json
{
    "claim": "The United States federal government spent more on defense than on education in 2023."
}
```

**Response:**
```json
{
    "claim_original": "The United States federal government spent more on defense than on education in 2023.",
    "claim_normalized": "US federal spending: defense > education (2023)",
    "claim_type": "quantitative_comparison",
    "verdict": "Supported",
    "confidence": 0.75,
    "confidence_tier": "Medium",
    "confidence_breakdown": {
        "source_reliability": 0.7,
        "evidence_density": 1.0,
        "semantic_alignment": 0.66
    },
    "summary": "Federal defense spending ($790.9B) exceeded education spending ($178.6B) in 2023 according to BEA NIPA data.",
    "evidence_links": [
        {
            "finding": "Defense: $790.9B",
            "source_url": "https://apps.bea.gov/NIPA/T31600"
        },
        {
            "finding": "Education: $178.6B",
            "source_url": "https://apps.bea.gov/NIPA/T31600"
        }
    ],
    "sources": [
        {
            "title": "BEA NIPA T31600 - National Defense",
            "url": "https://apps.bea.gov/NIPA/T31600",
            "snippet": "2023 defense spending: $790.2B",
            "data_value": 790197.0,
            "unit": "Millions USD"
        },
        {
            "title": "BEA NIPA T31600 - Education",
            "url": "https://apps.bea.gov/NIPA/T31600",
            "snippet": "2023 education spending: $178.6B",
            "data_value": 178621.0,
            "unit": "Millions USD"
        }
    ],
    "debug_plan": {
        "claim_type": "quantitative_comparison",
        "entities": ["defense", "education", "2023"],
        "api_plan": {
            "tier1": { "bea": { "table": "T31600", "year": "2023", "lines": ["2", "14"] } },
            "tier2_keywords": ["federal spending 2023"]
        }
    },
    "debug_log": []
}
```

**Rate Limits: 100 requests/day**

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
   pip install -r requirements.txt
   
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
   pytest tests/
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
