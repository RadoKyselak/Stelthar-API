# Release 2.2: passages, revisions, shareable checks

Branch `feat/number-one`, cut from `main` at `e80cf10`. What changed, how to turn each part off, and how
to take it back out.

## What changed

| # | Change | Where | Commit |
|---|---|---|---|
| 1 | "Inflation is 3%" is read against the newest month that has a 12-month rate, not last year's average | `evidence/sources/bls.py`, `cpi_local.py` | `6ef21c6` |
| 2 | `POST /v2/check`: a passage is cut into its claims and each is checked on its own | `claims/splitter.py`, `services/check_text.py`, `routes/check.py` | `d43a38c` |
| 3 | A claim that matches a figure since revised is labelled "Accurate when published"; a year's real GDP growth joins the first-release check | `services/derived.py`, `services/presentation.py` | `939875c` |
| 4 | The result card shows a passage claim by claim, how each claim was read, confidence, the date, and "Copy link"; `GET /c?claim=` is a check's own page | `extension/result-view.js`, `routes/share.py`, `static/app.js` | `686478e` |
| 5 | Extension 2.2.0 checks every claim in highlighted text; no new permission | `extension/` | `3392e08` |
| 6 | Home page sections; `/methodology` and `/developers`; privacy policy updated | `backend/static/`, `routes/pages.py` | `1a7529f` |
| 7 | `POST /mcp`: the checks as tools for assistants | `routes/mcp.py` | `f703451` |
| 8 | `make eval-live`: the eval set against the deployed service, free | `eval/remote.py` | `6229bf8` |

Nothing that existed was removed. `/verify`, `/v2/verify` and `/v2/lookup` answer as before, so every
installed extension keeps working.

## What it costs

Nothing new. A passage of two or more claims, a shared link and every `/mcp` tool run on free
government data only: no dataset picker, no interpreter, no web search. One claim on its own still gets
the full pipeline, behind the same daily caps as before (`utils/spend_guard.py`). The paid step for one
claim of a passage runs only when a person presses "Search further".

Change #3 removes a paid call: "real GDP grew 2.5% in 2023" used to go to web search and is now settled
from the Federal Reserve's archive.

Each claim of a passage is one request to an agency, so `/v2/check` has its own, smaller allowance per
visitor (6 a minute, 60 a day) and checks at most 12 claims per request.

## Switches (Vercel environment variables, no deploy needed beyond a restart)

| Variable | Default | Effect when changed |
|---|---|---|
| `MULTI_CLAIM` | `on` | `off`: `/v2/check` treats any text as one claim, exactly as `/v2/verify` does. The site and extension keep working. Use this first if the splitter misreads real text. |
| `MCP_ENABLED` | `on` | `off`: `/mcp` answers 404. |
| `CHECK_MAX_CLAIMS` | `12` | Claims checked per request (1–25). |
| `CHECK_MAX_CHARS` | `8000` | Longest passage (600–12,000). |
| `CHECK_PER_MINUTE`, `CHECK_PER_DAY` | `6`, `60` | Passage checks per visitor. |
| `CHECK_CONCURRENCY` | `4` | Claims of one passage checked at once (1–8). |
| `PUBLIC_BASE_URL` | the Vercel address | Where "Copy link" points. Set it when the site moves to its own domain. |

## Rolling back

From least to most drastic:

1. **One behaviour:** flip its switch above.
2. **One feature:** revert its commit. Each row of the first table is one commit that can be reverted on
   its own, newest first: `git revert <sha>`. Reverting #4 also needs #5 and #6 reverted (they use its
   card). Reverting #2 needs #4–#8 reverted.
3. **A route only:** delete its `include_router` line in `backend/main.py`. Each new route is its own
   module in `backend/routes/`.
4. **The whole release:** in Vercel, promote the previous production deployment (instant, no code
   change), or `git revert` the merge commit.

The extension is separate from the server:

- Extension 2.2 falls back to `/verify` when `/v2/check` is missing, so rolling the server back does not
  break it.
- To roll the extension back, upload the 2.1.3 package again with a higher version number (the Chrome Web
  Store does not accept a lower one). The 2.1.3 source is the `extension/` folder at `e80cf10`.

## Verified

- 1,271 tests pass (`make test`); 1,154 before this branch.
- By hand, against live agency data, on a local server without agency keys: the passage check, the
  revision label, the shared page and its link preview tags, the home page, both new pages, and a full
  `/mcp` exchange (initialize, tools/list, tools/call).
- The side panel's layout, in a browser harness that stands in for Chrome's extension APIs.

## Not verified, or not done

- **The extension in Chrome itself.** The panel was exercised in a harness, not loaded as an unpacked
  extension. Load `extension/` at `chrome://extensions` and check: right-click a paragraph, the passage
  list, "Search further", "Copy link", Recent.
- **`/mcp` with a real assistant.** It was driven with hand-written protocol messages only.
- **A published benchmark.** `make eval-live` needs this branch deployed (it calls `/v2/check`). Run it
  after deploy; it writes `backend/eval/results/latest.json` and refuses to print an accuracy figure if a
  source was down. No accuracy number is claimed anywhere on the site.
- **Census and BEA paths locally.** Those need keys that live only on the server. They are covered by
  the existing tests against captured agency payloads.
- **Source stance labels** (supports / contradicts) on web-search answers. That changes the prompt of the
  paid search step and needs a paid evaluation run to prove verdicts do not move.
- **In-page highlighting** ("scan this page"). It needs the `activeTab` and `scripting` permissions; the
  extension deliberately has neither (`tests/test_extension_permissions.py`). Highlighting an article and
  right-clicking gives the same answers with no new permission.
- **Spanish.** The engine reads English only.
- **Terms of use, a custom domain, one brand name.** Decisions for the owner.

## Things to know

- `/c?claim=` puts the claim in a web address. The page is marked `noindex`, escapes everything it
  shows, and takes nothing about the result from the address.
- The eval set's three other-country rows changed from "no verdict" to the World Bank's figures, because
  the engine has answered those since the World Bank adapter was added.
- `README.md` is also modified on the `docs/readme-promo` branch; expect to merge the two by hand.
