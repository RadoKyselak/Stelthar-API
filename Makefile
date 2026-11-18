.PHONY: eval eval-live eval-news test

# Runs the eval harness against the live pipeline. Requires real API keys
# (GEMINI_API_KEY, BEA_API_KEY, CENSUS_API_KEY, BLS_API_KEY, CONGRESS_API_KEY)
# exported in the environment — with fake/missing keys the report will say so
# rather than producing a number that looks like real accuracy.
eval:
	cd backend && python -m eval.runner

# The eval set against the deployed service, on free data only: no keys are
# needed here and no model call is made there. The full results, misses
# included, go to backend/eval/results/latest.json.
eval-live:
	cd backend && python -m eval.remote --out eval/results/latest.json

# Real sentences from agency releases, wire stories and encyclopedia articles,
# against the deployed service. Free, like eval-live.
eval-news:
	cd backend && python -m eval.remote --set news --out eval/results/latest-news.json

test:
	cd backend && python -m pytest tests/ -q
