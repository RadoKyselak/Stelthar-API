"""Nutrition claims against USDA FoodData Central (stubbed with real record
shapes; the banana is SR Legacy 173944 as the API returns it)."""
import pytest

from claims.verdict import CONTRADICTED, MOSTLY_ACCURATE, NO_VERDICT, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import fooddata
from services import nutrition
from services.verify_v2 import verify


def _n(i, name, unit, amount):
    return {"nutrient": {"id": i, "name": name, "unitName": unit}, "amount": amount}


def _p(mod, grams, desc=None, amount=1.0):
    return {"amount": amount, "modifier": mod, "portionDescription": desc, "gramWeight": grams,
            "measureUnit": {"name": "undetermined"}}


FOODS = {
    173944: {"fdcId": 173944, "description": "Bananas, raw", "dataType": "SR Legacy",
             "foodNutrients": [_n(1008, "Energy", "kcal", 89.0), _n(1003, "Protein", "g", 1.09),
                               _n(1004, "Total lipid (fat)", "g", 0.33), _n(1092, "Potassium, K", "mg", 358.0),
                               _n(2000, "Sugars, total including NLEA", "g", 12.23)],
             "foodPortions": [_p("NLEA serving", 126.0), _p('large (8" to 8-7/8" long)', 136.0),
                              _p("cup, sliced", 150.0), _p('medium (7" to 7-7/8" long)', 118.0),
                              _p('extra large (9" or longer)', 152.0)]},
    173945: {"fdcId": 173945, "description": "Bananas, dehydrated, or banana powder", "dataType": "SR Legacy",
             "foodNutrients": [_n(1008, "Energy", "kcal", 346.0)], "foodPortions": []},
    171287: {"fdcId": 171287, "description": "Egg, whole, raw, fresh", "dataType": "SR Legacy",
             "foodNutrients": [_n(1008, "Energy", "kcal", 143.0), _n(1003, "Protein", "g", 12.56)],
             "foodPortions": [_p("large", 50.0), _p("medium", 44.0), _p("extra large", 56.0)]},
    170567: {"fdcId": 170567, "description": "Nuts, almonds", "dataType": "SR Legacy",
             "foodNutrients": [_n(1003, "Protein", "g", 21.15)], "foodPortions": [_p("cup, whole", 143.0)]},
}
FOODS[171705] = {"fdcId": 171705, "description": "Avocados, raw, all commercial varieties", "dataType": "SR Legacy",
                 "foodNutrients": [_n(1004, "Total lipid (fat)", "g", 14.66)],
                 "foodPortions": [_p("NLEA serving", 50.0), _p("cup, cubes", 150.0),
                                  _p("avocado, NS as to Florida or California", 201.0)]}
# As-eaten (FNDDS) entries listed first by USDA's search, as live: no sizes.
FOODS[2707205] = {"fdcId": 2707205, "description": "Egg, whole, raw", "dataType": "Survey (FNDDS)",
                  "foodNutrients": [_n(1003, "Protein", "g", 12.4)],
                  "foodPortions": [_p(None, 50.0, "1 egg"), _p(None, 243.0, "1 cup")]}
FOODS[2709224] = {"fdcId": 2709224, "description": "Banana, raw", "dataType": "Survey (FNDDS)",
                  "foodNutrients": [_n(1008, "Energy", "kcal", 97.0)],
                  "foodPortions": [_p(None, 126.0, "1 banana")]}
FOODS[2706000] = {"fdcId": 2706000, "description": "Stewed salmon, Puerto Rican style", "dataType": "Survey (FNDDS)",
                  "foodNutrients": [_n(1003, "Protein", "g", 11.8)], "foodPortions": []}
FOODS[175168] = {"fdcId": 175168, "description": "Fish, salmon, Atlantic, farmed, cooked, dry heat",
                 "dataType": "SR Legacy", "foodNutrients": [_n(1003, "Protein", "g", 22.1)], "foodPortions": []}
FOODS[169967] = {"fdcId": 169967, "description": "Broccoli, raw", "dataType": "SR Legacy",
                 "foodNutrients": [_n(1162, "Vitamin C, total ascorbic acid", "mg", 89.2)],
                 "foodPortions": [_p("cup chopped", 91.0)]}
SEARCH = {
    "banana": [2709224, 173945, 173944], "bananas": [2709224, 173945, 173944],
    "egg": [2707205, 171287], "salmon": [2706000, 175168], "broccoli": [169967], "almonds": [170567], "avocado": [171705],
}


@pytest.fixture
def fdc(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)

    async def get(path, params):
        if path == "/foods/search":
            ids = SEARCH.get(params["query"].lower(), [])
            return {"foods": [{"fdcId": i, "description": FOODS[i]["description"],
                               "dataType": FOODS[i]["dataType"]} for i in ids]}
        return FOODS.get(int(path.rsplit("/", 1)[1]))
    monkeypatch.setattr(fooddata, "_get", get)


@pytest.mark.parametrize("claim,expected", [
    ("A banana has 105 calories.", SUPPORTED),                     # medium, 118 g: 105
    ("A banana has 200 calories.", CONTRADICTED),
    ("A banana has 125 calories.", MOSTLY_ACCURATE),               # 19% over
    ("There are 420 mg of potassium in a banana.", SUPPORTED),     # 422
    ("A large banana has 121 calories.", SUPPORTED),               # 136 g: 121
    ("One large egg has 6 grams of protein.", SUPPORTED),          # 50 g: 6.3
    ("An egg has 70 calories.", SUPPORTED),                        # large by default: 72
    ("100 grams of almonds have 21 grams of protein.", SUPPORTED),
    ("Almonds have more than 30 grams of protein per 100 grams.", CONTRADICTED),
    ("An avocado has 30 grams of fat.", SUPPORTED),                # the whole fruit, 201 g: 29.5
    ("A serving of avocado has 7 grams of fat.", SUPPORTED),       # USDA's serving, 50 g: 7.3
    ("3 ounces of salmon has 22 grams of protein.", MOSTLY_ACCURATE),  # plain salmon, 85 g: 18.8
    ("A cup of broccoli has 80 mg of vitamin C.", SUPPORTED),      # 91 g: 81
])
async def test_nutrition_claims(fdc, claim, expected):
    r = await verify(claim, allow_grounding=False)
    assert r.verdict == expected, r.headline
    assert r.model_calls == 0 and r.evidence[0]["source"] == "USDA"


async def test_a_stated_size_skips_entries_without_sizes(fdc):
    r = await verify("One large egg has 6 grams of protein.", allow_grounding=False)
    assert "Egg, whole, raw, fresh" in r.headline and "(50 g)" in r.headline, r.headline


async def test_salmon_is_salmon(fdc):
    r = await verify("3 ounces of salmon has 22 grams of protein.", allow_grounding=False)
    assert "Puerto Rican" not in r.headline and "Fish, salmon" in r.headline, r.headline


async def test_picks_the_food_not_the_powder(fdc):
    r = await verify("A banana has 105 calories.", allow_grounding=False)
    assert "bananas, raw" in r.headline.lower()


def test_key_never_reaches_a_log_line():
    import logging
    import config
    rec = logging.LogRecord("httpx", logging.INFO, "", 0, 'HTTP Request: GET %s "%s"',
                            ("https://api.nal.usda.gov/fdc/v1/foods/search?query=egg&api_key=SECRET123", "200 OK"), None)
    config._RedactKeys().filter(rec)
    assert "SECRET123" not in rec.getMessage() and "REDACTED" in rec.getMessage()


async def test_says_which_portion_was_assumed(fdc):
    r = await verify("A banana has 105 calories.", allow_grounding=False)
    assert any("names no size" in c and "medium banana (118 g)" in c for c in r.caveats), r.caveats
    assert r.confidence == "Medium"


async def test_unknown_food_is_no_verdict(fdc):
    r = await verify("A zorblax fruit has 50 calories.", allow_grounding=False)
    assert r.verdict == NO_VERDICT


@pytest.mark.parametrize("text", [
    "Adults need 4,700 mg of potassium a day.",     # advice, not composition
    "Unemployment was 4.1% in June.",
    "The bill costs $5 billion.",
])
def test_not_nutrition(text):
    assert nutrition.read(text) is None


async def test_interpreted_nutrient_query(fdc):
    from services import interpret
    text = "Bananas pack roughly 420 milligrams of potassium apiece."
    q = interpret.validate({"measure": "usda_nutrient", "operation": "value", "food": "Bananas",
                            "nutrient": "potassium", "portion": "", "figure": "420 milligrams",
                            "place": "", "comparator": "equals"}, text)
    v = await interpret.run(q, text, None)
    assert v is not None and v.verdict == SUPPORTED


async def test_miss_goes_to_search_with_usda_named(fdc):
    from unittest.mock import AsyncMock, patch
    from evidence.grounded import GroundedAnswer
    search = AsyncMock(return_value=GroundedAnswer(ok=False, error="x", model_calls=0, skipped=True))
    with patch("services.verify_v2.ai_search.search", search):
        await verify("A zorblax fruit has 50 calories.", client_ip="1.2.3.4")
    assert "FoodData Central" in search.call_args.args[1]
    search.reset_mock()
    with patch("services.verify_v2.ai_search.search", search):
        await verify("The Thrifty Food Plan for a family of four costs $1,024 a month.", client_ip="1.2.3.4")
    assert "Cost of Food reports" in search.call_args.args[1]
