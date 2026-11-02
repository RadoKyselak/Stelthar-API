"""Nutrition claims checked against USDA FoodData Central.

  "A banana has 105 calories"
  "One large egg has 6 grams of protein"
  "There are 420 mg of potassium in a banana"
  "100 grams of almonds have 21 grams of protein"

The claim names a food, a portion (or none: one medium is used, and the
answer says so), a nutrient and an amount. USDA gives the nutrient per 100 g
and the weight of household portions; the portion's amount is computed and
the food USDA describes is shown, so a reader can see exactly what was
compared ("Bananas, raw", medium, 118 g).

Foods vary (size, variety, ripeness), so a claim within 10% is Supported and
within 25% Mostly accurate. Confidence is Medium unless the claim named its
portion. Recommendations ("adults need 4,700 mg a day") are not composition
and are left alone.
"""
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from claims.verdict import CONTRADICTED, HIGH, MEDIUM, MOSTLY_ACCURATE, SUPPORTED, Verdict
from config import logger
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, fooddata
from services.regulatory import fact, verdict

# nutrient: (FoodData Central nutrient ids, in order of preference)
_NUTRIENTS = [
    (r"saturated fat", "saturated fat", ("1258",)),
    (r"calories|kcals?|cals?\b", "calories", ("1008", "2047", "2048")),
    (r"protein", "protein", ("1003",)),
    (r"(?:total )?fat\b", "fat", ("1004",)),
    (r"carb(?:ohydrate)?s?\b", "carbohydrates", ("1005",)),
    (r"fib(?:er|re)", "fiber", ("1079",)),
    (r"sugars?\b", "sugar", ("2000", "1063")),
    (r"sodium", "sodium", ("1093",)),
    (r"potassium", "potassium", ("1092",)),
    (r"calcium", "calcium", ("1087",)),
    (r"iron\b", "iron", ("1089",)),
    (r"magnesium", "magnesium", ("1090",)),
    (r"zinc", "zinc", ("1095",)),
    (r"vitamin c\b", "vitamin C", ("1162",)),
    (r"vitamin d\b", "vitamin D", ("1114",)),
    (r"vitamin b-?12\b", "vitamin B12", ("1178",)),
    (r"cholesterol", "cholesterol", ("1253",)),
    (r"caffeine", "caffeine", ("1057",)),
]
_UNITS = {"g": 1.0, "gram": 1.0, "grams": 1.0, "mg": 1e-3, "milligram": 1e-3, "milligrams": 1e-3,
          "mcg": 1e-6, "µg": 1e-6, "ug": 1e-6, "microgram": 1e-6, "micrograms": 1e-6}
_AMOUNT = re.compile(
    r"(?P<cmp>more than|over|at least|nearly|almost|about|around|roughly|approximately|only|just|under|"
    r"less than|fewer than|~)?\s*(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<u>kcals?|calories|cals?|g|grams?|mg|milligrams?|mcg|µg|ug|micrograms?)\b"
    r"(?:\s+of)?(?:\s+(?P<nut>[a-z][a-z0-9 -]{1,30}?))?(?=[\s,.;!?]|$)", re.I)
_ADVICE = re.compile(r"\b(a day|per day|daily|each day|recommend\w*|you need|need(?:s)? about|should (?:eat|get|have)|"
                     r"limit|RDA|daily value|DV|intake|% of)\b|%", re.I)
_PORTION_UNITS = r"cups?|tablespoons?|tbsp|teaspoons?|tsp|ounces?|oz|slices?|pieces?|servings?|stalks?|spears?|cloves?|scoops?|sticks?|handfuls?|pats?"
_COUNT = {"a": 1, "an": 1, "one": 1, "1": 1, "each": 1, "per": 1, "every": 1, "two": 2, "2": 2, "three": 3, "3": 3,
          "half": 0.5, "half a": 0.5, "½": 0.5}
_SIZES = ("extra large", "extra small", "small", "medium", "large", "jumbo")
_FILLER = re.compile(r"\b(has|have|had|contains?|contained|packs?|provides?|gives?|delivers?|there (?:are|is)|"
                     r"in|with|of|about|around|roughly|approximately|only|just|nearly|almost|over|under|more than|"
                     r"less than|at least|typical|average|single|fresh|raw|plain|usda|says|according to|"
                     r"the|serving size)\b", re.I)
_COOKED_FIRST = ("rice", "pasta", "spaghetti", "chicken", "beef", "pork", "salmon", "fish", "tuna", "beans",
                 "lentils", "quinoa", "oatmeal", "turkey", "shrimp", "steak", "potato", "egg", "eggs", "bacon")
_BAD = ("dried", "dehydrated", "powder", "juice", "oil", "candies", "candy", "babyfood", "baby food",
        "snacks", "chips", "bread", "bagel", "cracker", "substitute", "flavored", "dessert", "cake",
        "pie", "muffin", "nectar", "imitation", "sauce", "soup", "salad", "sandwich", "smoothie", "with")
_COOKING = re.compile(r"\b(cooked|baked|roasted|boiled|broiled|grilled|fried|steamed|scrambled|poached|stewed|"
                      r"sauteed|microwaved|canned|raw|uncooked|dry)\b", re.I)


@dataclass
class Read:
    food: str
    nutrient: str           # label, e.g. "protein"
    ids: Tuple[str, ...]
    amount: float           # in the claim's unit
    unit: str               # "kcal" or a mass unit key
    comparator: str = "equals"
    approximate: bool = False
    count: float = 1.0
    size: str = ""
    portion_unit: str = ""
    grams: Optional[float] = None   # "100 grams of almonds"
    weight: str = ""                # as written: "3 oz"
    stated_portion: bool = False
    text: str = ""


def _singular(w: str) -> str:
    w = w.lower()
    for a, b in (("ies", "y"), ("oes", "o"), ("ches", "ch"), ("shes", "sh"), ("s", "")):
        if w.endswith(a) and len(w) > len(a) + 2 and not w.endswith("ss"):
            return w[: -len(a)] + b
    return w


def read(text: str) -> Optional[Read]:
    if _ADVICE.search(text):
        return None
    m = None
    for cand in _AMOUNT.finditer(text):
        after = re.sub(r"^\s+(?:of\s+)?", "", text[cand.end("u"):])
        nm = next((re.match(rx, after, re.I) for rx, _, _ in _NUTRIENTS if re.match(rx, after, re.I)), None)
        nut_text = nm.group(0) if nm else ""
        unit = cand.group("u").lower()
        if re.match(r"kcal|calor|cal", unit):
            m, label, ids, nut_span = cand, "calories", ("1008", "2047", "2048"), ""
            break
        hit = next(((lbl, ids) for rx, lbl, ids in _NUTRIENTS if re.match(rx, nut_text, re.I)), None)
        if hit:
            m, (label, ids), nut_span = cand, hit, nut_text
            break
    if m is None:
        return None
    unit = m.group("u").lower()
    unit = "kcal" if re.match(r"kcal|calor|cal", unit) else unit
    cmp_word = (m.group("cmp") or "").lower()
    comparator = ("greater than" if cmp_word in ("more than", "over", "at least") else
                  "less than" if cmp_word in ("under", "less than", "fewer than") else "equals")
    rest = (text[:m.start()] + " " + text[m.end("u"):]).strip()
    if label != "calories" and nut_span:
        rest = re.sub(r"\bof\s+" + re.escape(nut_span) + r"|" + re.escape(nut_span), " ", rest, count=1)
    rest = re.sub(r"\bcalories\b|\bkcals?\b", " ", rest, flags=re.I)

    grams, weight = None, ""
    g = re.search(r"\b(?:per\s+)?(\d+(?:\.\d+)?)\s*(g|grams?|ounces?|oz)\b(?:\s+of)?", rest, re.I)
    if g:
        oz = g.group(2).lower().startswith("o")
        grams = float(g.group(1)) * (28.3495 if oz else 1.0)
        weight = f"{float(g.group(1)):g} {'oz' if oz else 'g'}"
        rest = rest[:g.start()] + " " + rest[g.end():]
    count, size, punit, stated = 1.0, "", "", grams is not None
    c = re.search(r"\b(half an?|half|an?|one|1|each|per|every|two|2|three|3|½)\b\s*"
                  r"(?:(extra large|extra small|small|medium|large|jumbo)\s+)?"
                  r"(?:(" + _PORTION_UNITS + r")\s+(?:of\s+)?)?", rest, re.I)
    if c:
        count = _COUNT.get(c.group(1).lower(), 1)
        size = (c.group(2) or "").lower()
        punit = _singular(c.group(3) or "")
        stated = stated or bool(size or punit)
        rest = rest[:c.start()] + " " + rest[c.end():]
    else:
        s = re.search(r"\b(extra large|extra small|small|medium|large|jumbo)\b", rest, re.I)
        if s:
            size, stated = s.group(1).lower(), True
            rest = rest[:s.start()] + " " + rest[s.end():]
    food = _FILLER.sub(" ", rest)
    food = re.sub(r"[^A-Za-z' -]", " ", food)
    words = [w for w in food.split() if len(w) > 1 or w.isalpha()]
    keep_whole = re.search(r"\bwhole[- ](milk|wheat|grains?)\b", text, re.I)
    words = [w for w in words if w.lower() != "whole" or keep_whole]
    if not words or len(words) > 4:
        return None
    food = " ".join(words).strip(" -'").lower()
    if len(food) < 3:
        return None
    return Read(food=food, nutrient=label, ids=ids, amount=float(m.group("n")), unit=unit,
                comparator=comparator, approximate=cmp_word in ("about", "around", "roughly", "approximately",
                                                                  "nearly", "almost", "~"),
                count=count, size=size, portion_unit=punit, grams=grams, weight=weight,
                stated_portion=stated, text=text)


_CATEGORIES = {"fish", "nut", "cheese", "seed", "beverage", "cereal", "spice", "vegetable", "fruit",
               "beef", "pork", "chicken", "turkey", "lamb", "milk", "yogurt", "bread", "pasta"}


def _score(desc: str, food: str, text: str, dataset: str = "") -> float:
    d = desc.lower()
    segs = [s.strip() for s in d.split(",")]
    words = [_singular(w) for w in food.split()]
    head = " ".join(_singular(w) for w in segs[0].split())
    s = 0.0
    second = " ".join(_singular(w) for w in segs[1].split()) if len(segs) > 1 else ""
    if head == " ".join(words) or (head in _CATEGORIES and second == " ".join(words)):
        s += 6
    elif all(w in " ".join(_singular(x) for x in d.replace(",", " ").split()) for w in words):
        s += 2
    else:
        return -99
    if re.search(r"\bstyle\b|\brecipe\b|\bhomemade\b|\bwith\b", d) and not re.search(r"\bstyle\b|\bwith\b", text, re.I):
        s -= 2
    claim = text.lower()
    s -= sum(3 for b in _BAD if re.search(rf"\b{b}\b", d) and not re.search(rf"\b{b}\b", claim))
    cooked_first = any(w in _COOKED_FIRST for w in words)
    cook = _COOKING.findall(d)
    said = set(x.lower() for x in _COOKING.findall(claim))
    if said:
        s += 2 if said & set(x.lower() for x in cook) else -2
    elif cooked_first:
        s += 1.5 if any(x.lower() not in ("raw", "uncooked", "dry", "canned") for x in cook) else -1
    else:
        s += 1.5 if "raw" in [x.lower() for x in cook] or not cook else -1
    if re.search(r"all commercial varieties|all varieties|\bwhole\b|\bplain\b|no added fat|nfs", d):
        s += 1
    s -= 0.4 * (len(segs) - 1)
    if dataset == "SR Legacy" and "raw" in d:
        s += 1  # the reference entries, with small / medium / large
    return s


async def _pick(r: Read) -> Optional[Tuple[dict, Tuple[float, str, bool]]]:
    """The best USDA entry that has both the nutrient and the claim's portion."""
    foods = await fooddata.search(r.food)
    if not foods and r.food.endswith("s"):
        foods = await fooddata.search(_singular(r.food))
    scored = sorted(((_score(f["description"], r.food, r.text, f.get("dataType", "")), i, f)
                     for i, f in enumerate(foods)), key=lambda x: (-x[0], x[1]))
    for score, _, f in scored[:4]:
        if score < 4:
            break
        d = await fooddata.food(f["fdcId"])
        if not d or not any(i in d["nutrients"] for i in r.ids):
            logger.info("Nutrition: %s (%s) has no %s", f["description"], f["fdcId"], r.nutrient)
            continue
        portion = _portion(d, r)
        if portion is not None:
            return d, portion
    logger.info("Nutrition: no USDA food and portion for %r among %s", r.food,
                [(round(x[0], 1), x[2]["description"]) for x in scored[:4]])
    return None


def _portion(d: dict, r: Read) -> Optional[Tuple[float, str, bool]]:
    """(grams, how it reads, assumed)."""
    if r.grams is not None:
        shown = r.weight if r.weight.endswith(" g") else f"{r.weight} ({r.grams:.0f} g)"
        return r.grams, f"{shown} of {r.food}", False
    ps = d.get("portions") or []
    def find(pred):
        return next((p for p in ps if pred(p[0].lower())), None)
    p = None
    if r.portion_unit:
        p = find(lambda t: re.search(rf"\b{re.escape(r.portion_unit)}s?\b", t) and (not r.size or r.size in t))
        if p is None and r.portion_unit in ("ounce", "oz"):
            return 28.3495 * r.count, f"{r.count:g} oz ({28.3495 * r.count:.0f} g) of {r.food}", False
        if p is None:
            logger.info("Nutrition: no %r portion for %r in %s", r.portion_unit, r.food, [x[0] for x in ps][:8])
            return None
    elif r.size:
        p = find(lambda t: re.search(rf"(^|[^a-z]){r.size}\b", t) and not (r.size in ("small", "large") and f"extra {r.size}" in t))
    else:
        egg = "egg" in r.food
        item = _singular(r.food.split()[-1])
        serving = bool(re.search(r"\bserving\b", r.text, re.I))
        preds = [(lambda t: "large" in t and "extra" not in t) if egg else
                 (lambda t: "medium" in t and "slice" not in t and "cup" not in t),
                 # "1 avocado, NS as to Florida or California", "1 fruit", "1 whole"
                 lambda t: re.search(rf"^(?:1(?:\.0)?\s+)?(?:{re.escape(item)}s?\b|fruit\b|whole\b|item\b|each\b)", t)]
        if serving:
            preds.insert(0, lambda t: "nlea serving" in t or "serving" in t)
        for pred in preds:
            p = find(pred)
            if p:
                break
        if p is None:
            logger.info("Nutrition: no portion for %r in %s", r.food, [x[0] for x in ps][:8])
            return None
    if p is None:
        logger.info("Nutrition: no %r portion for %r in %s", r.size, r.food, [x[0] for x in ps][:8])
        return None
    text, grams, amount = p
    per_one = grams / (amount or 1)
    total = per_one * r.count
    return total, f"{_phrase(text, r)} ({total:.0f} g)", not r.stated_portion


_UNIT_LIKE = re.compile(r"^(cups?|tablespoons?|tbsps?|teaspoons?|tsps?|slices?|pieces?|oz|ounces?|servings?|"
                        r"nlea servings?|stalks?|spears?|sticks?|scoops?)\b", re.I)
_WORDS = {1: "one", 2: "two", 3: "three", 0.5: "half a", 4: "four"}


def _phrase(portion: str, r: Read) -> str:
    """"medium (7\" to 7-7/8\" long)" -> "one medium banana";
    "tablespoon" x2 -> "two tablespoons of peanut butter"."""
    shown = re.sub(r"\s*\([^)]*\)", "", re.sub(r"^1(?:\.0)?\s+", "", portion)).strip(" ,")
    shown = re.sub(r",?\s*NS as to .*$", "", shown, flags=re.I).strip(" ,")
    item = _singular(r.food.split()[-1])
    cw = _WORDS.get(r.count, f"{r.count:g}")
    if _UNIT_LIKE.match(shown):
        first, _, more = shown.split(",")[0].strip().partition(" ")
        if r.count > 1 and not first.endswith("s"):
            first += "s"
        return f"{cw} {first}{(' ' + more) if more else ''} of {r.food}"
    if item in shown.lower():
        return f"{cw} {shown}"
    return f"{cw} {shown} {item}"


def _fmt(v: float, unit: str) -> str:
    if unit == "kcal":
        return f"{v:.0f} calories"
    if v >= 10:
        return f"{v:.0f} {unit}"
    return f"{v:.1f} {unit}".replace(".0 ", " ")


async def check(text: str, parsed) -> Optional[Verdict]:
    r = read(text)
    if r is None:
        return None
    return await run(r)


async def run(r: Read) -> Optional[Verdict]:
    try:
        got = await _pick(r)
        if got is None:
            return None
        d, (grams, how, assumed) = got
        nid = next(i for i in r.ids if i in d["nutrients"])
        per100, unit, nname = d["nutrients"][nid]
        official = per100 * grams / 100.0
        if r.unit == "kcal":
            if unit != "kcal":
                return None
            claimed, shown_unit = r.amount, "kcal"
        else:
            to = {"g": 1.0, "mg": 1e-3, "µg": 1e-6, "ug": 1e-6}.get(unit)
            if to is None:
                return None
            claimed = r.amount * _UNITS[r.unit] / to
            shown_unit = {"µg": "mcg", "ug": "mcg"}.get(unit, unit)
    except SourceUnavailable as e:
        logger.warning("Nutrition check skipped for %r: %s", r.text[:80], e)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Nutrition check failed for %r", r.text[:120])
        return None
    rel = (claimed - official) / official if official else (0.0 if claimed == 0 else 1.0)
    if r.comparator == "greater than":
        outcome = SUPPORTED if official > claimed else (MOSTLY_ACCURATE if rel <= 0.1 else CONTRADICTED)
    elif r.comparator == "less than":
        outcome = SUPPORTED if official < claimed else (MOSTLY_ACCURATE if rel >= -0.1 else CONTRADICTED)
    else:
        from evidence.units import written_precision
        step = (written_precision(f"{r.amount:g}") or 0) * (claimed / r.amount if r.amount else 1)
        if abs(rel) <= 0.10 or abs(claimed - official) <= step / 2:
            outcome = SUPPORTED
        elif abs(rel) <= 0.25:
            outcome = MOSTLY_ACCURATE
        else:
            outcome = CONTRADICTED
    food_name = d["description"]
    official_s = _fmt(official, shown_unit)
    label = f"{r.nutrient.capitalize()} in {how}"
    dp = fact("USDA", label, official_s,
              f"https://fdc.nal.usda.gov/food-details/{d['fdcId']}/nutrients",
              magnitude=official, basis=f"FoodData Central ({d['dataType']}): {food_name}",
              series_id=str(d["fdcId"]),
              rows=[ex_row("Food", food_name), ex_row(f"{nname}, per 100 g", f"{per100:g} {shown_unit}"),
                    ex_row("Portion", how), ex_row(f"{r.nutrient.capitalize()} in that portion", official_s, cited=True)])
    caveats = []
    if assumed:
        caveats.append(f"The claim names no size; USDA's weight for {how} is used.")
    caveats.append("Amounts vary with size, variety and preparation, so a claim within 10% counts as a match.")
    claimed_s = _fmt(r.amount, "kcal" if r.unit == "kcal" else r.unit)
    what = official_s if r.nutrient == "calories" else f"{official_s} of {r.nutrient}"
    head = (f"USDA FoodData Central (\u201c{food_name}\u201d): {how} has {what}"
            + (f"; the claim says {claimed_s}." if outcome != SUPPORTED else "."))
    return verdict(outcome, head, [dp], caveats=caveats, claimed=claimed_s,
                   confidence=HIGH if r.stated_portion and not assumed else MEDIUM)
