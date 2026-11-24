"""Bill claims checked against Congress.gov and, for states, LegiScan (both
stubbed with the record shapes the live APIs return)."""
from datetime import date

import pytest

import config
from claims.verdict import CONTRADICTED, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import congress, legiscan
from services import bills, regulatory
from services.verify_v2 import verify


def _a(day, text, code="", typ="Floor"):
    return {"actionDate": day, "text": text, "actionCode": code, "type": typ}


# H.R. 1, 119th Congress, as Congress.gov records it (actions trimmed).
_HR1 = {
    "bill": {"number": "1", "type": "HR", "congress": 119, "introducedDate": "2025-05-20",
             "title": "An act to provide for reconciliation pursuant to title II of H. Con. Res. 14.",
             "latestAction": {"actionDate": "2025-07-04", "text": "Became Public Law No: 119-21."},
             "laws": [{"number": "119-21", "type": "Public Law"}],
             "sponsors": [{"fullName": "Rep. Arrington, Jodey C. [R-TX-19]", "lastName": "Arrington"}]},
    "actions": [
        _a("2025-07-04", "Became Public Law No: 119-21.", "36000", "BecameLaw"),
        _a("2025-07-04", "Signed by President.", "E30000", "President"),
        _a("2025-07-03", "On motion that the House agree to the Senate amendment Agreed to by recorded vote: "
                         "218 - 214 (Roll no. 190). (text: CR H3059-3143)", "H41610", "ResolvingDifferences"),
        _a("2025-07-01", "Passed/agreed to in Senate: Passed Senate with an amendment by Yea-Nay Vote. 51 - 50. "
                         "Record Vote Number: 372.", "17000"),
        _a("2025-05-22", "Passed/agreed to in House: On passage Passed by the Yeas and Nays: 215 - 214 "
                         "(Roll no. 145).", "8000"),
        _a("2025-05-20", "Introduced in House", "Intro-H", "IntroReferral"),
    ],
    "titles": ["One Big Beautiful Bill Act", "An act to provide for reconciliation pursuant to title II of "
                                             "H. Con. Res. 14."],
}
_IRA = {
    "bill": {"number": "5376", "type": "HR", "congress": 117, "introducedDate": "2021-09-27",
             "title": "Inflation Reduction Act of 2022",
             "latestAction": {"actionDate": "2022-08-16", "text": "Became Public Law No: 117-169."},
             "laws": [{"number": "117-169", "type": "Public Law"}],
             "sponsors": [{"fullName": "Rep. Yarmuth, John A. [D-KY-3]", "lastName": "Yarmuth"}]},
    "actions": [
        _a("2022-08-16", "Became Public Law No: 117-169.", "36000", "BecameLaw"),
        _a("2022-08-16", "Signed by President.", "E30000", "President"),
        _a("2022-08-12", "On motion that the House agree to the Senate amendment Agreed to by recorded vote: "
                         "220 - 207 (Roll no. 420).", "H41610", "ResolvingDifferences"),
        _a("2022-08-07", "Passed/agreed to in Senate: Passed Senate with an amendment by Yea-Nay Vote. 51 - 50. "
                         "Record Vote Number: 325.", "17000"),
        _a("2021-11-19", "Passed/agreed to in House: On passage Passed by the Yeas and Nays: 220 - 213 "
                         "(Roll no. 385).", "8000"),
    ],
    "titles": ["Inflation Reduction Act of 2022", "Build Back Better Act"],
}
_SAVE = {
    "bill": {"number": "22", "type": "HR", "congress": 119, "introducedDate": "2025-01-03",
             "title": "SAVE Act", "laws": [],
             "latestAction": {"actionDate": "2025-04-28", "text": "Received in the Senate."},
             "sponsors": [{"fullName": "Rep. Roy, Chip [R-TX-21]", "lastName": "Roy"}]},
    "actions": [
        _a("2025-04-28", "Received in the Senate."),
        _a("2025-04-10", "Passed/agreed to in House: On passage Passed by the Yeas and Nays: 220 - 208 "
                         "(Roll no. 102).", "8000"),
    ],
    "titles": ["Safeguard American Voter Eligibility Act", "SAVE Act"],
}
FEDERAL = {"119/hr/1": _HR1, "117/hr/5376": _IRA, "119/hr/22": _SAVE}


@pytest.fixture
def congress_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(bills, "_today", lambda: date(2026, 11, 11))
    calls = []

    async def get(path, **params):
        calls.append(path)
        parts = path.split("/")
        rec = FEDERAL.get("/".join(parts[1:4]))
        if rec is None:
            return None
        tail = parts[4] if len(parts) > 4 else ""
        if tail == "actions":
            return {"actions": rec["actions"]}
        if tail == "titles":
            return {"titles": [{"title": t, "titleType": "Popular Titles" if i == 0 else "Display Title"}
                               for i, t in enumerate(rec["titles"])]}
        if tail == "cosponsors":
            return {"cosponsors": []}
        return {"bill": rec["bill"]}
    monkeypatch.setattr(congress, "_get", get)
    return calls


@pytest.mark.parametrize("claim,expected,says", [
    ("The Senate passed H.R. 1 51-50 in 2025.", SUPPORTED, "51–50"),
    ("The House passed H.R. 1 by 220-210 in 2025.", CONTRADICTED, "215–214"),
    ("Biden signed the Inflation Reduction Act in 2022.", SUPPORTED, "August 16, 2022"),
    ("Trump signed the Inflation Reduction Act.", CONTRADICTED, "Joe Biden"),
    ("The Inflation Reduction Act was signed into law in 2023.", CONTRADICTED, "not in 2023"),
    ("The SAVE Act became law.", CONTRADICTED, "has not become law"),
    ("The SAVE Act has not become law.", SUPPORTED, "Received in the Senate"),
    ("The House passed the SAVE Act 220-208.", SUPPORTED, "220–208"),
    ("The SAVE Act passed the Senate.", CONTRADICTED, "has not passed the Senate"),
    ("Congress passed the One Big Beautiful Bill Act.", SUPPORTED, "Senate"),
    ("Rep. Jodey Arrington introduced H.R. 1 in 2025.", SUPPORTED, "Arrington"),
    ("Rep. Nancy Pelosi sponsored H.R. 1 in 2025.", CONTRADICTED, "Arrington"),
    # H.R. 1 passed the House twice (May 22, 215-214; July 3, 218-214): the month picks the vote.
    ("The House passed H.R. 1 215-214 in July 2025.", CONTRADICTED, "not July 2025"),
    ("The House passed H.R. 1 218-214 in July 2025.", SUPPORTED, "July 3, 2025"),
    ("The House passed H.R. 1 in July 2025.", SUPPORTED, "July 3, 2025"),
    ("The House passed H.R. 1 in May 2025.", SUPPORTED, "May 22, 2025"),
    ("The Senate passed H.R. 1 in June 2025.", CONTRADICTED, "not June 2025"),
])
async def test_federal_bill_claims(congress_api, claim, expected, says):
    v = await bills.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline
    assert v.evidence[0].source == "CONGRESS"
    assert v.evidence[0].source_url.startswith("https://www.congress.gov/bill/")
    assert bills.ONLY_RECORD in v.caveats


async def test_bill_link_is_the_congress_gov_page(congress_api):
    v = await bills.check("Biden signed the Inflation Reduction Act.")
    assert v.evidence[0].source_url == "https://www.congress.gov/bill/117th-congress/house-bill/5376"


async def test_bill_is_shown_by_its_popular_title(congress_api):
    v = await bills.check("H.R. 1 was signed into law in 2025.")
    assert v.evidence[0].label == "H.R. 1 (119th Congress): One Big Beautiful Bill Act"


async def test_number_without_a_year_reads_the_current_congress(congress_api):
    v = await bills.check("H.R. 1 was signed into law.")
    assert v.verdict == SUPPORTED
    assert any("119th Congress" in c for c in v.caveats)
    assert "119/hr/1" in congress_api[0]


@pytest.mark.parametrize("claim", [
    "H.R. 1 cut Medicaid by $1 trillion.",        # what the bill does, not its record
    "The bill passed the Senate.",                 # no bill named
    "The U.S. 2025 budget passed.",                # "S. 2025" after "U.S." is not a bill
    "Newsom vetoed AB 1955.",                      # a state bill with no state named
])
async def test_not_read_as_bill_claims(congress_api, claim):
    assert bills.read(claim) is None
    assert await bills.check(claim) is None


async def test_an_unknown_bill_is_not_a_verdict(congress_api):
    assert await bills.check("H.R. 9999 was signed into law in 2025.") is None


async def test_a_name_that_does_not_match_the_titles_is_dropped(congress_api, monkeypatch):
    monkeypatch.setitem(FEDERAL, "117/hr/5376", {**_IRA, "titles": ["Some Other Act"],
                                                  "bill": {**_IRA["bill"], "title": "Some Other Act"}})
    assert await bills.check("Biden signed the Inflation Reduction Act.") is None


async def test_verify_answers_bills_without_a_model(congress_api):
    r = await verify("Biden signed the Inflation Reduction Act in 2022.", allow_grounding=False)
    assert r.verdict == SUPPORTED and r.model_calls == 0, r.headline
    assert r.evidence and r.evidence[0].get("source") == "CONGRESS"


def test_bill_claims_route_to_the_bill_hint():
    assert regulatory.topic("The House passed H.R. 1 in 2025.") == "bill"
    assert "Congress.gov" in regulatory.search_hint("The House passed H.R. 1 in 2025.")


# ------------------------------------------------------------------ states

_SB17 = {
    "bill_id": 1700001, "state": "TX", "bill_number": "SB17", "body": "S", "status": 4,
    "status_date": "2023-06-17", "title": "Relating to diversity, equity, and inclusion initiatives at public "
                                          "institutions of higher education.",
    "description": "", "state_link": "https://capitol.texas.gov/BillLookup/History.aspx?LegSess=88R&Bill=SB17",
    "url": "https://legiscan.com/TX/bill/SB17/2023",
    "session": {"session_id": 1950, "year_start": 2023, "year_end": 2023, "sine_die": 1,
                "session_title": "2023 Regular Session"},
    "progress": [{"date": "2023-03-10", "event": 1}, {"date": "2023-04-19", "event": 2},
                 {"date": "2023-05-28", "event": 3}, {"date": "2023-06-17", "event": 4}],
    "history": [{"date": "2023-03-10", "action": "Filed"},
                {"date": "2023-06-17", "action": "Signed by the Governor"},
                {"date": "2023-06-17", "action": "Effective on 1/1/24"}],
    "sponsors": [{"name": "Brandon Creighton", "last_name": "Creighton", "sponsor_type_id": 1}],
    "votes": [{"date": "2023-04-19", "desc": "Read 3rd time", "yea": 19, "nay": 12, "chamber": "S"},
              {"date": "2023-05-21", "desc": "Passed as amended", "yea": 83, "nay": 62, "chamber": "H"}],
}


@pytest.fixture
def legiscan_api(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "LEGISCAN_API_KEY", "test-key")
    monkeypatch.setattr(bills, "_today", lambda: date(2026, 11, 11))

    async def get(op, **params):
        if op == "getSessionList":
            return {"status": "OK", "sessions": [
                {"session_id": 1950, "year_start": 2023, "year_end": 2023, "special": 0,
                 "session_title": "2023 Regular Session"},
                {"session_id": 1960, "year_start": 2023, "year_end": 2023, "special": 1,
                 "session_title": "2023 1st Special Session"}]}
        if op == "getMasterList" and params.get("id") == 1950:
            return {"status": "OK", "masterlist": {"session": {}, "0": {"bill_id": 1700001, "number": "SB17"},
                                                   "1": {"bill_id": 1700002, "number": "SB18"}}}
        if op == "getMasterList":
            return {"status": "OK", "masterlist": {"session": {}, "0": {"bill_id": 99, "number": "SB17"}}}
        if op == "getBill" and params.get("id") == 1700001:
            return {"status": "OK", "bill": _SB17}
        return None
    monkeypatch.setattr(legiscan, "_get", get)


@pytest.mark.parametrize("claim,expected,says", [
    ("Texas passed SB 17 in 2023.", SUPPORTED, "Texas SB17"),
    ("Texas Senate Bill 17 was signed by the governor in 2023.", SUPPORTED, "June 17, 2023"),
    ("Texas SB 17 was vetoed in 2023.", CONTRADICTED, "no veto"),
    ("The Texas House passed SB 17 83-62 in 2023.", SUPPORTED, "83–62"),
    ("Texas SB 17, sponsored by Sen. Brandon Creighton, passed in 2023.", SUPPORTED, "Creighton"),
])
async def test_state_bill_claims(legiscan_api, claim, expected, says):
    v = await bills.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline
    assert v.evidence[0].source == "Texas Legislature"
    assert v.evidence[0].source_url.startswith("https://capitol.texas.gov/")
    assert legiscan.ATTRIBUTION in v.caveats


async def test_state_bills_need_a_legiscan_key(monkeypatch):
    monkeypatch.setattr(config, "LEGISCAN_API_KEY", None)
    assert await bills.check("Texas passed SB 17 in 2023.") is None


@pytest.mark.parametrize("claim,bill", [
    ("Obama signed Dodd-Frank in 2010.", (111, "hr", 4173)),
    ("Bush signed the Patriot Act in 2001.", (107, "hr", 3162)),
    ("Clinton signed NAFTA into law.", (103, "hr", 3450)),
    ("The Senate passed the Americans with Disabilities Act.", (101, "s", 933)),
    ("The For the People Act became law.", (117, "hr", 1)),
    ("Congress passed the 1994 crime bill.", (103, "hr", 3355)),
])
def test_well_known_acts_are_read_by_name(claim, bill):
    r = bills.read(claim)
    assert r is not None, claim
    assert (r.ref.congress, r.ref.kind, r.ref.number) == bill


def test_every_named_act_is_one_bill():
    import re
    seen = {}
    for pattern, flags, c, bill, canonical in bills._ACTS:
        re.compile(pattern, flags)
        kind, number = bill.split()
        assert kind in bills._FED_SHOW and number.isdigit() and 93 <= c <= 130
        assert seen.setdefault(canonical, (c, bill)) == (c, bill)
