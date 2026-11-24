"""Executive orders and federal rules checked against the Federal Register
(stubbed with the document shapes its API returns)."""
import pytest

from claims.verdict import CONTRADICTED, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import federalregister as fr
from services import regulatory, register


def _order(number, title, signed, president, name):
    return {"title": title, "executive_order_number": str(number) if number else None, "signing_date": signed,
            "publication_date": signed, "president": {"identifier": president, "name": name},
            "html_url": f"https://www.federalregister.gov/documents/x/{number}"}


WHO = _order(14155, "Withdrawing the United States From the World Health Organization", "2025-01-20",
             "donald-trump", "Donald Trump")
AI = _order(14110, "Safe, Secure, and Trustworthy Development and Use of Artificial Intelligence", "2023-10-30",
            "joe-biden", "Joseph R. Biden Jr.")
CRYPTO = _order(14067, "Ensuring Responsible Development of Digital Assets", "2022-03-09", "joe-biden",
                "Joseph R. Biden Jr.")
ORDERS = [WHO, AI, CRYPTO]

PFAS = {"title": "PFAS National Primary Drinking Water Regulation", "type": "Rule", "action": "Final rule.",
        "abstract": "EPA is finalizing limits for six PFAS in drinking water.", "publication_date": "2024-04-26",
        "effective_on": "2024-06-25", "html_url": "https://www.federalregister.gov/documents/2024/04/26/2024-07773"}
PFAS_FIX = dict(PFAS, title="PFAS National Primary Drinking Water Regulation; Correction",
                action="Final rule; correction.", publication_date="2024-06-11")
RED3 = {"title": "Color Additive Petition From Center for Science in the Public Interest, et al.; Request To Revoke "
                 "Color Additive Listing for Use of FD&C Red No. 3 in Food and Ingested Drugs",
        "type": "Rule", "action": "Final amendment; order.", "abstract": "FDA is revoking the authorized uses.",
        "publication_date": "2025-01-16", "effective_on": "2025-01-15", "html_url": "https://www.federalregister.gov/d/r3"}
HEAT = {"title": "Heat Injury and Illness Prevention in Outdoor and Indoor Work Settings", "type": "Proposed Rule",
        "action": "Notice of proposed rulemaking.", "abstract": "", "publication_date": "2024-08-30",
        "effective_on": None, "html_url": "https://www.federalregister.gov/d/heat"}


@pytest.fixture
def register_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    calls = []

    async def get(params):
        calls.append(params)
        p = {}
        for k, v in params:
            p.setdefault(k, v)
        if p.get("conditions[presidential_document_type][]") == "executive_order":
            pres = p.get("conditions[president][]", "")
            if p.get("per_page") == "1000":      # a count: documents, some repeating or unnumbered
                if pres == "barack-obama":
                    docs = [{"executive_order_number": str(13489 + i)} for i in range(276)]
                    docs += [{"executive_order_number": "13526"}, {"executive_order_number": None}]
                elif pres == "donald-trump" and p.get("conditions[signing_date][year]") == "2025":
                    docs = [{"executive_order_number": str(14147 + i)} for i in range(225)]
                elif pres == "joe-biden":
                    docs = [{"executive_order_number": str(13985 + i)} for i in range(162)]
                else:
                    docs = []
                return {"count": len(docs), "total_pages": 1, "results": docs}
            term = (p.get("conditions[term]") or "").lower()
            hits = [o for o in ORDERS if (not pres or o["president"]["identifier"] == pres)
                    and (term == o["executive_order_number"]
                         or any(w in o["title"].lower() for w in term.split() if len(w) > 3))]
            return {"count": len(hits), "results": hits}
        agency, kind = p.get("conditions[agencies][]"), p.get("conditions[type][]")
        rules = {("environmental-protection-agency", "RULE"): [PFAS_FIX, PFAS],
                 ("food-and-drug-administration", "RULE"): [RED3],
                 ("occupational-safety-and-health-administration", "PRORULE"): [HEAT]}.get((agency, kind), [])
        return {"count": len(rules), "results": rules}
    monkeypatch.setattr(fr, "_get", get)
    return calls


@pytest.mark.parametrize("claim,expected,says", [
    ("Executive Order 14067 was signed by Biden.", SUPPORTED, "signed by Joe Biden on March 9, 2022"),
    ("Executive Order 14067 was signed by Trump.", CONTRADICTED, "signed by Joe Biden"),
    ("Executive Order 14067 was signed in 2021.", CONTRADICTED, "in 2022, not 2021"),
    ("Trump signed an executive order withdrawing the United States from the World Health Organization.",
     SUPPORTED, "Executive Order 14155"),
    ("Biden signed an executive order withdrawing the United States from the World Health Organization.",
     CONTRADICTED, "no order with this subject signed by Joe Biden"),
    ("Biden signed an executive order on artificial intelligence in 2023.", SUPPORTED, "October 30, 2023"),
    ("Biden signed an executive order on artificial intelligence in 2021.", CONTRADICTED, "in 2023, not 2021"),
    ("Trump signed an executive order on his first day withdrawing the United States from the World Health "
     "Organization.", SUPPORTED, "January 20, 2025"),
])
async def test_executive_orders(register_api, claim, expected, says):
    v = await register.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline


@pytest.mark.parametrize("claim,expected,says", [
    ("Biden signed 162 executive orders.", SUPPORTED, "162 executive orders signed by Joe Biden"),
    # 278 documents, 276 orders: a correction and a reprint are not orders.
    ("Obama signed 276 executive orders.", SUPPORTED, "276 executive orders"),
    ("Obama signed 500 executive orders.", CONTRADICTED, "276 executive orders"),
    ("Trump signed more than 200 executive orders in 2025.", SUPPORTED, "225 executive orders"),
    ("Trump signed fewer than 100 executive orders in 2025.", CONTRADICTED, "225 executive orders"),
])
async def test_how_many_orders(register_api, claim, expected, says):
    v = await register.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline


@pytest.mark.parametrize("claim,expected,says", [
    ("The EPA finalized a rule limiting PFAS in drinking water in 2024.", SUPPORTED, "April 26, 2024"),
    ("The EPA finalized a rule limiting PFAS in drinking water in 2021.", CONTRADICTED, "in 2024, not 2021"),
    ("The FDA banned Red Dye No. 3 in food.", SUPPORTED, "FD&C Red No. 3"),
    ("OSHA proposed a rule on heat injury and illness prevention.", SUPPORTED, "a proposed rule"),
])
async def test_rules(register_api, claim, expected, says):
    v = await register.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline
    assert any("court or a later rule" in c for c in v.caveats)


async def test_a_correction_is_not_the_rule(register_api):
    v = await register.check("The EPA finalized a rule limiting PFAS in drinking water.")
    assert "Correction" not in v.headline


@pytest.mark.parametrize("claim", [
    "Trump signed an executive order renaming the Gulf of Mexico.",   # no order's title says so
    "The president signed an executive order.",                       # no subject
    "The EPA finalized a rule on unicorn habitat protection.",        # nothing matches
    "The EPA will finalize a rule limiting PFAS in drinking water.",  # a prediction
    "The EPA has not finalized a rule limiting PFAS in drinking water.",
    "The FDA approved Ozempic in 2017.",                              # an approval, not a rule
    "The EPA announced a plan to cut emissions.",
    "Executive Order 14067 is about digital assets.",                 # nothing on its record is claimed
])
async def test_what_is_not_settled_is_left_alone(register_api, claim):
    assert await register.check(claim) is None


def test_register_claims_are_a_topic_for_routing():
    assert regulatory.topic("Biden signed 162 executive orders.") == "register"
    assert regulatory.topic("The EPA finalized a rule limiting PFAS in drinking water in 2024.") == "register"
    assert "Federal Register" in regulatory.search_hint("Biden signed 162 executive orders.")
    assert regulatory.topic("The unemployment rate was 3.6% in 2023.") == ""
