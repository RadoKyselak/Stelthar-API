"""FDA and FTC claims checked against the agencies' own records (openFDA
stubbed with real record shapes; FTC figures from its releases)."""
import pytest

from claims.verdict import CONTRADICTED, MISLEADING, NO_VERDICT, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import openfda
from services import fda
from services.verify_v2 import verify


def _app(no, brand, ingredient, orig, status="Prescription", suppl=()):
    subs = [{"submission_type": "ORIG", "submission_status": "AP", "submission_status_date": orig,
             "submission_class_code": "TYPE 1"}]
    subs += [{"submission_type": "SUPPL", "submission_status": "AP", "submission_status_date": d,
              "submission_class_code": "EFFICACY"} for d in suppl]
    return {"application_number": no, "sponsor_name": "NOVO",
            "products": [{"brand_name": brand, "marketing_status": status,
                          "active_ingredients": [{"name": ingredient}]}],
            "submissions": subs}


APPS = {
    "WEGOVY": [_app("NDA215256", "WEGOVY", "SEMAGLUTIDE", "20210604", suppl=["20240308"])],
    "SEMAGLUTIDE": [_app("NDA209637", "OZEMPIC", "SEMAGLUTIDE", "20171205"),
                    _app("NDA215256", "WEGOVY", "SEMAGLUTIDE", "20210604")],
    "ADDERALL": [_app("NDA011522", "ADDERALL 10", "AMPHETAMINE ASPARTATE", "19600119")],
    "BELVIQ": [_app("NDA022529", "BELVIQ", "LORCASERIN HYDROCHLORIDE", "20120627", status="Discontinued")],
}
LABELS = {
    "OZEMPIC": [{"set_id": "s1", "openfda": {"brand_name": ["OZEMPIC"], "generic_name": ["SEMAGLUTIDE"],
                 "application_number": ["NDA209637"]},
                 "indications_and_usage": ["1 INDICATIONS AND USAGE OZEMPIC is indicated as an adjunct to diet "
                                           "and exercise to improve glycemic control in adults with type 2 "
                                           "diabetes mellitus."]}],
    "WEGOVY": [{"set_id": "s2", "openfda": {"brand_name": ["WEGOVY"], "generic_name": ["SEMAGLUTIDE"]},
                "indications_and_usage": ["WEGOVY is indicated to reduce excess body weight and maintain "
                                          "weight reduction long term in adults with obesity."]}],
    "ZOLOFT": [{"set_id": "s3", "openfda": {"brand_name": ["ZOLOFT"], "generic_name": ["SERTRALINE"]},
                "boxed_warning": ["WARNING: SUICIDAL THOUGHTS AND BEHAVIORS Antidepressants increased the risk."],
                "indications_and_usage": ["ZOLOFT is indicated for major depressive disorder."]}],
    "LIPITOR": [{"set_id": "s4", "openfda": {"brand_name": ["LIPITOR"], "generic_name": ["ATORVASTATIN"]},
                 "indications_and_usage": ["LIPITOR is indicated to reduce the risk of stroke."]}],
    "LEQEMBI": [{"set_id": "s5", "openfda": {"brand_name": ["LEQEMBI"], "generic_name": ["LECANEMAB-IRMB"],
                 "application_number": ["BLA761269"]},
                 "indications_and_usage": ["LEQEMBI is indicated for the treatment of Alzheimer's disease."]}],
}
RECALLS = [{"product_description": "Similac Special Care 24 infant formula", "recalling_firm": "Abbott",
            "classification": "Class II", "recall_initiation_date": "20231031",
            "reason_for_recall": "Marketed without approval", "recall_number": "F-1"}]
SHORTAGES = {"ADDERALL": [{"generic_name": "Amphetamine Aspartate Tablet", "status": "Current",
                           "initial_posting_date": "10/12/2022", "update_date": "10/15/2026"}]}
LISTINGS = {"DEXCOM G7": [{"proprietary_name": ["Dexcom G7 Continuous Glucose Monitoring System"],
                           "products": [{"openfda": {"device_class": "2"}}]}]}


def _name(search):
    return search.split('"')[1].upper() if '"' in search else ""


@pytest.fixture
def fda_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    calls = []

    async def get(endpoint, search, limit=25):
        calls.append((endpoint, search))
        n = _name(search)
        rows = {"drug/drugsfda": APPS.get(n, []), "drug/label": LABELS.get(n, []),
                "drug/shortages": SHORTAGES.get(n, []), "device/registrationlisting": LISTINGS.get(n, []),
                "food/enforcement": RECALLS if n in ("SIMILAC", "SIMILAC BABY FORMULA") else []}.get(endpoint, [])
        if "indications_and_usage" in search and endpoint == "drug/label" and n == "SEMAGLUTIDE":
            rows = LABELS["WEGOVY"]
        return {"results": rows, "total": len(rows)}
    monkeypatch.setattr(openfda, "_get", get)
    return calls


@pytest.mark.parametrize("claim,expected", [
    ("The FDA approved Wegovy in 2021.", SUPPORTED),
    ("The FDA approved Wegovy in 2019.", CONTRADICTED),
    ("Semaglutide was first approved by the FDA in 2017.", SUPPORTED),
    ("Adderall is not FDA approved.", CONTRADICTED),
    ("Belviq is FDA approved.", MISLEADING),            # every product discontinued
    ("Leqembi is FDA approved.", SUPPORTED),            # a biologic: the label carries the license
    ("Ozempic is FDA-approved for weight loss.", CONTRADICTED),
    ("Wegovy is approved for weight loss.", SUPPORTED),
    ("Zoloft has a black box warning.", SUPPORTED),
    ("Lipitor has a black box warning.", CONTRADICTED),
    ("The FDA recalled Similac baby formula.", SUPPORTED),
    ("There is a shortage of Adderall.", SUPPORTED),
    ("This supplement is FDA-approved.", CONTRADICTED),
    ("The FDA approved 50 novel drugs in 2024.", SUPPORTED),
    ("The FDA approved 60 novel drugs in 2023.", CONTRADICTED),  # 55: a count is exact
    ("The Dexcom G7 is FDA approved.", MISLEADING),     # class II: cleared, not approved
])
async def test_fda_claims(fda_api, claim, expected):
    r = await verify(claim, allow_grounding=False)
    assert r.verdict == expected, r.headline
    assert r.model_calls == 0 and r.evidence and r.evidence[0]["source"] == "FDA"


async def test_wrong_brand_points_to_the_right_one(fda_api):
    r = await verify("Ozempic is FDA-approved for weight loss.", allow_grounding=False)
    assert any("Wegovy" in c for c in r.caveats), r.caveats


async def test_a_new_use_is_not_the_first_approval(fda_api):
    r = await verify("The FDA approved Wegovy in 2024 to reduce heart attack risk.", allow_grounding=False)
    assert r.verdict == SUPPORTED and r.confidence == "Medium"
    assert "first approved June 4, 2021" in r.headline


async def test_old_files_are_not_year_checked(fda_api):
    r = await verify("Adderall was approved by the FDA in 1996.", allow_grounding=False)
    assert r.verdict == NO_VERDICT  # its 1960 file is Obetrol's, not Adderall's


async def test_meat_recalls_are_usdas(fda_api):
    r = await verify("Boar's Head deli meat was recalled for listeria.", allow_grounding=False)
    assert r.verdict == NO_VERDICT and not any(e == "food/enforcement" for e, _ in fda_api)


async def test_unknown_product_is_never_a_verdict(fda_api):
    r = await verify("The FDA approved Zorblax in 2020.", allow_grounding=False)
    assert r.verdict == NO_VERDICT


def test_reading():
    r = fda.read("Novo Nordisk's weight-loss drug Wegovy was approved by the FDA in 2021.")
    assert r.kind == "approval" and r.year == 2021 and fda._clean(r.product)[0] == "Wegovy"
    assert fda.read("The FDA said the economy grew.") is None
    assert fda.read("Unemployment was 4.1% in June.") is None


@pytest.mark.parametrize("claim,expected", [
    ("Americans lost $12.5 billion to fraud in 2024, according to the FTC.", SUPPORTED),
    ("Consumers reported losing $16 billion to scams in 2025.", SUPPORTED),
    ("Consumers reported losing $20 billion to scams in 2025.", CONTRADICTED),
    ("The FTC received 2.6 million fraud reports in 2024.", SUPPORTED),
    ("People lost $5.7 billion to investment scams in 2024.", SUPPORTED),
    ("The FTC says identity theft reports topped 1 million in 2023.", SUPPORTED),
    ("The FTC banned noncompete agreements.", CONTRADICTED),
    ("A federal court struck down the FTC's noncompete ban.", SUPPORTED),
    ("The FTC's click-to-cancel rule is in effect.", CONTRADICTED),
    ("The FTC banned junk fees.", MISLEADING),
    ("The FTC banned hidden fees on concert tickets.", SUPPORTED),
    ("The FTC banned fake reviews.", SUPPORTED),
])
async def test_ftc_claims(claim, expected):
    r = await verify(claim, allow_grounding=False)
    assert r.verdict == expected, r.headline
    assert r.model_calls == 0 and r.evidence and r.evidence[0]["source"] == "FTC"


@pytest.mark.parametrize("claim", [
    "California banned noncompetes.",                      # a state, not the FTC
    "Americans lost $3 billion to romance scams in 2024.",   # a category the table does not hold
    "The FBI says Americans lost $16.6 billion to internet crime in 2024.",
])
async def test_ftc_leaves_other_claims_alone(claim):
    from services import ftc
    assert await ftc.check(claim, None) is None


async def test_interpreted_fda_query(fda_api, monkeypatch):
    from services import interpret
    q = interpret.validate({"measure": "fda", "operation": "regulatory", "fda_check": "approval",
                            "product": "Wegovy", "place": "", "figure": "", "comparator": "equals",
                            "year": 2021}, "Wegovy got the green light from regulators in 2021.")
    assert q is not None and q.fields["product"] == "Wegovy"
    from claims.parser import parse_claim
    text = "Wegovy got the green light from regulators in 2021."
    v = await interpret.run(q, text, parse_claim(text))
    assert v is not None and v.verdict == SUPPORTED
    # A product the claim does not name is never used.
    assert interpret.validate({"measure": "fda", "operation": "regulatory", "fda_check": "approval",
                               "product": "Ozempic", "place": "", "figure": "", "comparator": "equals"},
                              text) is None


# ------------------------------------------------------------------ misses go to Claude

@pytest.mark.parametrize("claim,where", [
    ("Zorblax is FDA-approved for migraines.", "Drugs@FDA"),         # a product openFDA lacks
    ("Boar's Head deli meat was recalled for listeria.", "fsis.usda.gov"),  # USDA's recall
    ("The FTC sued Amazon over Prime cancellations.", "Federal Register"),
])
async def test_a_miss_is_searched_with_the_agency_named(fda_api, claim, where):
    from unittest.mock import AsyncMock, patch
    from evidence.grounded import GroundedAnswer
    search = AsyncMock(return_value=GroundedAnswer(ok=False, error="x", model_calls=0, skipped=True))
    found = AsyncMock(return_value=None)
    with patch("services.verify_v2.ai_search.search", search), \
            patch("services.dataset_tier.verify_found", found):
        await verify(claim, client_ip="1.2.3.4")
    search.assert_awaited_once()
    args, kwargs = search.call_args
    assert where in args[1] and kwargs["names_a_metric"] is True  # no number needed
    found.assert_not_awaited()  # the dataset tools hold no FDA/FTC records


async def test_a_settled_claim_costs_nothing(fda_api):
    from unittest.mock import AsyncMock, patch
    search = AsyncMock()
    with patch("services.verify_v2.ai_search.search", search):
        r = await verify("The FDA approved Wegovy in 2021.", client_ip="1.2.3.4")
    assert r.verdict == SUPPORTED and r.model_calls == 0
    search.assert_not_awaited()


# A recall named by its firm and product, with a place in between: the firm
# and the product settle it; the place ("Mexico") never does.
_JALAPENO = {"product_description": "Jalapeno Pepper packaged in bulk generic Coast Tropical cardboard packaging.",
             "recalling_firm": "Coast Citrus Distributors", "classification": "Class I",
             "recall_initiation_date": "20260722", "recall_number": "H-1242-2026",
             "reason_for_recall": "Potential Salmonella Javiana contamination"}
_GUMMY = {"product_description": "Jovy Gummy Worms Neon; packaged in 1lb plastic units.",
          "recalling_firm": "El Ruisenor De Mexico, Inc.", "classification": "Class II",
          "recall_initiation_date": "20260824", "recall_number": "H-0130-2027",
          "reason_for_recall": "undeclared allergen ingredient (Red#40)"}
_OTHER = {"product_description": "Cucumbers grown in Mexico", "recalling_firm": "Some Farm",
          "classification": "Class I", "recall_initiation_date": "20260123", "recall_number": "H-0500-2026",
          "reason_for_recall": "Potential contamination with Salmonella."}


@pytest.fixture
def recall_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    asked = []

    async def get(endpoint, search, limit=25):
        n = _name(search)
        asked.append(n)
        rows = {"COAST CITRUS DISTRIBUTORS": [_JALAPENO], "JALAPENO": [_JALAPENO],
                "MEXICO": [_GUMMY, _OTHER]}.get(n, []) if endpoint == "food/enforcement" else []
        return {"results": rows, "total": len(rows)}
    monkeypatch.setattr(openfda, "_get", get)
    return asked


async def test_recall_by_firm_and_product(recall_api):
    t = ("2026 Recalls of Food Products Associated with Fresh Jalapeños from Sinaloa, Mexico and "
         "distributed by Coast Citrus Distributors due to the Potential Risk of Salmonella")
    v = await fda.check(t, None)
    assert v.verdict == SUPPORTED and v.evidence[0].series_id == "H-1242-2026"
    assert "July 22, 2026" in v.headline and ".." not in v.headline


async def test_recall_spelled_with_accents_and_plurals(recall_api):
    v = await fda.check("Jalapeños were recalled for salmonella in 2026", None)
    assert v.verdict == SUPPORTED and v.evidence[0].series_id == "H-1242-2026"


async def test_a_place_is_not_a_product(recall_api):
    assert await fda.check("Fresh Jalapeños from Sinaloa, Mexico were recalled in 2026", None) is None
    assert "MEXICO" not in recall_api


async def test_cited_record_is_the_one_that_matched_the_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)

    async def get(endpoint, search, limit=25):
        rows = [_GUMMY, _OTHER] if endpoint == "food/enforcement" and _name(search) == "SOME FARM" else []
        return {"results": [{**r, "recalling_firm": "Some Farm"} for r in rows], "total": len(rows)}
    monkeypatch.setattr(openfda, "_get", get)
    v = await fda.check("Some Farm was recalled for salmonella in 2026", None)
    assert v.verdict == SUPPORTED and v.evidence[0].series_id == "H-0500-2026"
