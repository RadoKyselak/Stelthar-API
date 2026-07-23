.PHONY: eval test

# Runs the eval harness against the live pipeline. Requires real API keys
# (GEMINI_API_KEY, BEA_API_KEY, CENSUS_API_KEY, BLS_API_KEY, CONGRESS_API_KEY)
# exported in the environment — with fake/missing keys the report will say so
# rather than producing a number that looks like real accuracy.
eval:
	cd backend && python -m eval.runner

test:
	cd backend && python -m pytest tests/ -q
