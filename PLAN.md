# Stelthar: Plan to become the best government-data research tool

> Status: strategic plan, written 2026-07-20, after an engineering pass on the
> verification pipeline. The engineering is now sound. The product thesis is not.
> This document is about the thesis.
>
> **2026-07-20 pivot:** narrowed scope from "fact-check every claim across every
> federal agency" to **a research tool** — the fastest way to find the right
> official number or report, not an arbiter of political truth. Concretely:
> dropped the FBI/crime-data coverage race (nobody is actually fact-checking
> crime stats with this tool); replaced the plan to hand-build one adapter per
> agency with a **search.gov-backed research harness** that reaches any federal
> site and fetches real report/page content, not just a snippet. This is
> implemented — see §3, Phase 2.

---

## 1. Honest diagnosis: why it doesn't currently solve a problem

The pipeline works. That is not the same as the product working. Four things are
wrong at the product level, and no amount of backend quality fixes them.

### 1.1 The claims it can verify are not the claims people encounter

The system is architected around clean quantitative propositions:

> "Federal spending on defense exceeded education in 2023."

Single metric, single year, single authoritative source, unambiguous relation.
This is a *demo claim*. Real claims in circulation look like:

> "Crime is out of control in our cities."
> "Inflation is the worst it's been in 40 years."
> "We're sending billions overseas while Americans go hungry."
> "They let in 10 million illegals."

These are vague (no year, no metric, no geography), often **numerically true but
framed to mislead**, and frequently about domains with no wired-up data source.
The planner is asked to turn these into a BEA table lookup. It cannot.

### 1.2 The verdict taxonomy has no cell for the most common failure mode

`Supported / Contradicted / Inconclusive` cannot express:

- true number, cherry-picked start date
- true number, nominal instead of inflation-adjusted
- true number, absolute instead of per-capita
- true number, correct value but false causal attribution
- true of one subgroup, presented as true of all

**This is what most real misinformation actually is.** Not fabricated numbers —
real numbers, dishonestly framed. A tool that can only say "Supported" is
actively harmful here: it stamps a misleading claim as true.

And because the honest answer for a vague claim is "Inconclusive," that becomes
the modal output. A fact-checker whose most common answer is "I don't know,"
delivered after a 10-40 second wait, does not solve a problem.

### 1.3 Coverage was a narrow slice of the argument space — now addressed structurally

Structured, exact-number sources: BEA, BLS, Census, Treasury, USAspending. That
covers macroeconomics, demographics, and the federal budget — narrow if the plan
were to keep hand-building one bespoke adapter per agency (CDC WONDER, EIA,
NCES, FEC, CMS, IRS SOI, …). That per-agency race is no longer the plan (see the
pivot note above): instead, the **search.gov research harness** now reaches any
federal domain and fetches real page/report content for qualitative and
long-tail questions the 5 structured sources can't answer. Breadth is now a
function of what's published on a `.gov` site, not of engineering time spent on
one-off adapters.

What's still genuinely missing is an honest **"I don't have a good answer for
this"** signal for questions the harness also can't resolve well (ambiguous,
non-empirical, or opinion claims) — see Phase 1.

### 1.4 Trust is asserted, never earned — and nobody has measured accuracy

The output is an LLM verdict plus a confidence number derived from a weighted
formula (`0.30·R + 0.45·S + 0.25·E`, with caps). That number has **never been
calibrated against ground truth.** There is no test set, no accuracy figure, no
calibration curve. We genuinely do not know if this tool is right more often
than a coin flip on real claims.

For a fact-checking product, being *believed* is the whole game. An unknown tool
printing "VERDICT: CONTRADICTED — 82% confidence" on a politically charged claim
will be dismissed by precisely the audience that most needs it. Asserted
authority does not work when the subject is contested.

---

## 2. The strategic reframe

> **Stop being a verdict oracle. Become a research tool** — the fastest,
> most complete, most auditable way to find the official U.S. government
> number or report behind a claim — **and layer honest framing analysis on
> top.**

This is also why the tool doesn't need to win a coverage race against every
federal agency one at a time. A research assistant's job is to *find the right
document*, not to have pre-built a bespoke adapter for every possible domain —
which is exactly what the search.gov harness (§3, Phase 2) does.

This is the single most important decision in this document. The rationale:

**Verdicts require winning a trust war you cannot win.** Adjudicating "is this
claim true" on contested political topics puts you against PolitiFact and Snopes
on their turf, as an unknown LLM, where any single wrong verdict is
catastrophic and where half the audience pre-rejects your conclusion.

**Evidence retrieval requires no trust at all.** "Here is the BLS number, here is
the number in the claim, here is the gap, here is the direct link to the series"
is *mechanically checkable*. The user verifies you in one click. Trust is earned
structurally rather than asserted. You are not telling anyone what to think.

What this changes concretely:

| Today | Reframed |
|---|---|
| `VERDICT: CONTRADICTED` | `Claim says 3.7%. BLS says 3.6% (2024 annual avg).` |
| Confidence: 82% | Source, series ID, publication date, direct link |
| Inconclusive (modal output) | `No official series covers this. Closest: …` |
| One opaque LLM judgment | Structured comparison the user completes |

The verdict does not vanish — it becomes a *secondary, clearly-labelled
interpretation* over primary evidence that stands on its own. When we are unsure,
we degrade to showing the data rather than to "I don't know."

**Why this can be best-in-class:** nobody currently offers a fast, unified way
to go from a claim or question straight to the exact BEA/BLS/Census/Treasury
number *or* the right GAO/CBO/agency report, with provenance, in one step. FRED
is closest but is economics-only, has no claim parsing, and doesn't search
reports/prose at all. Combining exact structured lookups with a real-content
research harness over any `.gov` site is the differentiated position — and it's
genuinely useful even when a claim turns out to be *true*, which is the
majority of the time.

---

## 3. Roadmap

### Phase 0 — Measurement (2 weeks) — **do this first, it gates everything**

We cannot build "the best" anything without a number that says how good we are.

**Status: harness built (`backend/eval/`), seeded with a 13-claim pilot set, not
yet run for real.** What exists:

- `eval/dataset.py` + `eval/claims.jsonl` — a labeled-claim schema and loader.
  All 13 seed claims are real, sourced PolitiFact rulings (never fabricated —
  see the module docstring for why that matters here specifically). Each entry
  records the ground truth verdict, whether the claim is a framing trap or a
  temporal trap, and whether this system currently has *any* data source that
  could answer it (`expected_coverage: yes/no/partial`).
- Ground truth uses three buckets, not PolitiFact's six, because that's what
  the system can express: `Supported` / `Contradicted` / `Mixed` (Half-True —
  a claim that's technically true but misleadingly framed). This directly
  operationalizes §1.2: on a `Mixed` claim, answering `Inconclusive` is scored
  as *correct*; confidently picking a side is scored as a distinct, worse
  failure mode (`overconfident_on_mixed`), not lumped in with a plain wrong answer.
- `eval/metrics.py` computes four things **separately**, because they diagnose
  different bugs:
  - **Retrieval recall** — did the source layer find a real datapoint at all,
    independent of what the LLM concluded from it (a wrong verdict with good
    retrieval is a synthesis bug; a wrong verdict with none is a coverage gap).
  - **Verdict accuracy** — with the Mixed-aware scoring above.
  - **Coverage honesty** — for the 7 seed claims marked `expected_coverage: no`
    (crime, immigration, state-level BLS, annual deficit — genuine gaps),
    did the system admit it has no source (Inconclusive / confidence ≤ 0.45)
    instead of guessing? This is the metric that catches unearned confidence
    (§1.4) before it ships.
  - **Calibration** — bucketed by confidence, with per-bucket N reported
    explicitly and a `reliable: false` flag below 10 samples, so a 1-sample
    bucket can never be mistaken for a real reliability curve.
- `eval/runner.py` / `make eval` — runs the dataset against the live
  `VerificationService`, refuses to pretend a fake-key run means anything (the
  report literally says "not a real accuracy measurement" if keys are
  missing), and writes a markdown report with every non-correct result listed
  individually for inspection, not just a rolled-up score.
- All harness math is covered by 16 unit tests against hand-checked synthetic
  inputs, plus a mocked dry-run proving the runner/report pipeline is
  mechanically correct — **not a claim about real system accuracy**, which
  requires the next step below.

**What's NOT done yet — this is a pilot, not the exit criterion:**
- No real run has happened. This sandbox has no live `GEMINI_API_KEY` /
  `BEA_API_KEY` / etc., so `python -m eval.runner` has only been proven against
  a fake service. Running it for real, with real keys, is the next concrete step.
- 13 claims, not 300–500. Scaling this up means more PolitiFact/Snopes
  sourcing (by hand or via a proper scraper — no bulk API exists for either
  site) and should happen once the 13-claim pilot has confirmed the harness
  surfaces something actionable.
- CI wiring — no CI config exists in this repo yet, so "every change reports a
  delta" isn't wired up. `make eval` exists and is CI-ready; hooking it to a
  pipeline is a small follow-up once one exists.

**Exit criterion (unchanged):** a dashboard with a current, honest, per-domain
score, run against the live pipeline. Expect it to be bad initially — that's
the point, and the harness is specifically built to show *where* it's bad
(retrieval vs. synthesis vs. coverage-honesty), not just a single number.

**Exit criterion:** a dashboard with a current, honest, per-domain score. Expect
it to be bad initially. That is the point.

### Phase 1 — Reframe the output (4–6 weeks)

- Rebuild the response around **evidence-first**: official value, claim value,
  delta, series ID, publication date, direct source link. Verdict demoted to a
  labelled interpretation.
- Add **explicit coverage honesty**: a fast, deterministic "no official source
  covers this" path that fires in <1s instead of degrading into Data.gov
  keyword mush. Route these to a "closest available data" suggestion.
- Rework the extension UI around this. The number and its provenance are the
  hero; the verdict is a subtitle.
- Kill `Inconclusive` as a user-facing string. Replace with the specific reason:
  *no source for this domain* / *claim too vague to test* / *data not yet
  published for that year* / *claim is not empirical*.

### Phase 2 — Coverage via a research harness — **implemented, needs tuning**

Instead of hand-building an adapter per agency, coverage now comes from a
**search.gov-backed research harness** ([search_gov.py](backend/api/search_gov.py),
[content_fetch.py](backend/utils/content_fetch.py)):

- search.gov is a free, public API purpose-built to search across federal
  government sites — no domain-whitelist logic needed, no per-call cost.
- Unlike the retired Data.gov catalog search, the harness **fetches and
  extracts real page/PDF content** for the top few results (HTML stripped,
  PDFs parsed via `pypdf`), not just a title and one-line snippet. That's what
  makes it usable for actual research questions ("what has GAO said about
  X") instead of returning dataset descriptions.
- Cost control: fetched content is cached for 24h (reports don't change
  hour to hour), fetch is capped to the top 3 results per query, downloads are
  capped at 8MB, and everything is truncated before it reaches an LLM prompt.
- Structured sources (BEA/BLS/Census/Treasury/USAspending) are still tried
  first for anything with a specific number — free and exact beats searched
  and extracted. The harness is the fallback/supplement tier, not a
  replacement for it.

**Status: built, not yet load-bearing.** It needs a `SEARCH_GOV_AFFILIATE` +
`SEARCH_GOV_API_KEY` (free sign-up at search.gov) before it does anything live —
until then `query_search_gov` returns a clean "not configured" error rather than
silently failing. The exact request/response shape was implemented against
search.gov's documented v2 API but **has not been smoke-tested against a live
key** — do that before relying on it in production; search APIs occasionally
rename fields between versions.

**Remaining work once the key is live:**
- Smoke-test the real request/response shape; adjust field names if needed.
- Tune result ranking (currently returns search.gov's default order) — may
  want to prefer `.gov` reports over agency press releases for research
  questions.
- Feed Phase 0's eval set through this path specifically to measure whether
  fetched excerpts actually improve synthesis accuracy vs. snippet-only.

### Phase 3 — Speed (3–4 weeks)

Current p95 is 10–40s (3 LLM passes + fan-out). That is fatal for a browsing
interaction.

- **Precompute the long tail**: the top ~2,000 statistics (unemployment by year
  and state, CPI, GDP, murder rate, border encounters…) are a bounded set.
  Snapshot them nightly into a local store. Most claims hit this — target
  **sub-second** for cache hits.
- Skip the LLM planner entirely for claims matching known patterns
  (`<metric> in <year> was <value>`) via a deterministic fast path.
- Reserve the full agentic loop for genuine long-tail misses.

### Phase 4 — The framing layer (6+ weeks) — **the real differentiator**

This is the intellectually valuable, genuinely unsolved part, and it is only
credible once Phases 0–3 make the underlying numbers trustworthy.

Detect and surface, mechanically, from the series itself:

- **Cherry-picked baselines** — claim's window vs. the full series; show both.
  ("True from 2021. From 2015, the trend reverses.")
- **Nominal vs. real** — flag un-adjusted dollar comparisons across years.
- **Absolute vs. per-capita** — flag raw counts across differently-sized
  populations or time spans.
- **Level vs. rate-of-change** conflation.
- **Subgroup generalisation** — true of one cohort, stated of all.

Output as a chart plus a plain sentence, not a verdict. This is where the tool
stops being a lookup and starts being genuinely educational — and it sidesteps
the trust problem entirely because the *data itself* makes the argument.

### Phase 5 — Distribution

- Public API with generous free tier — become the verification layer other
  tools build on.
- Embeddable widget for newsrooms and Substack.
- Social bot (reply with the official number + chart) — this is the viral loop
  the Chrome extension categorically lacks.

---

## 4. What to kill

- **Data.gov catalog keyword search as an evidence source. DONE.** It returned
  dataset descriptions, never values — the direct cause of the "surfaces old
  irrelevant data" complaint. Retired from the default query fan-out and
  replaced by the search.gov harness (Phase 2); `query_datagov` still exists as
  a module for a future *discovery* affordance ("here's a dataset you could
  explore"), clearly separated from evidence, but nothing calls it by default.
- **`Inconclusive` as a user-facing output.** See Phase 1.
- **The un-calibrated confidence percentage**, until Phase 0 gives it a
  reliability curve. Showing a precise-looking number with no empirical basis
  actively erodes trust. Ship a coarse High/Medium/Low, or nothing, until it is
  earned.
- **`backend/config.py`** — dead code, shadowed by the `config/` package.

---

## 5. Sequencing rationale

Phase 0 first because everything else is unfalsifiable without it. Phase 1
before Phase 2 because expanding coverage under a broken output format just
produces more bad verdicts. Phase 3 before Phase 4 because a slow tool never
gets used enough to matter. Phase 4 last because framing analysis is only
believable on top of numbers people already trust.

**The one-line version:** the moat is *coverage breadth × speed × auditability*,
not verdict cleverness. Win on being the place where any official U.S. number is
one second away with its provenance attached — then let the data do the arguing.
