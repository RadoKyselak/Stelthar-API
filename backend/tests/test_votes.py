"""Vote claims checked against the chambers' roll calls (the Senate's and the
House Clerk's files, stubbed in the shapes the live files have)."""
from datetime import date

import pytest

from claims.verdict import CONTRADICTED, MOSTLY_ACCURATE, SUPPORTED
from evidence import cache as cache_mod
from evidence.sources import congress, rollcall
from services import bills, regulatory, votes


def _senator(last, first, party, state, cast):
    return (f"<member><member_full>{last} ({party}-{state})</member_full><last_name>{last}</last_name>"
            f"<first_name>{first}</first_name><party>{party}</party><state>{state}</state>"
            f"<vote_cast>{cast}</vote_cast></member>")


def _senate_vote(number, day, question, result, yeas, nays, members, tie=""):
    tb = f"<tie_breaker><by_whom>Vice President</by_whom><tie_breaker_vote>{tie}</tie_breaker_vote></tie_breaker>" \
        if tie else "<tie_breaker><by_whom/><tie_breaker_vote/></tie_breaker>"
    return (f"<?xml version='1.0' encoding='UTF-8'?><roll_call_vote><congress>117</congress><session>2</session>"
            f"<vote_number>{number}</vote_number><vote_date>{day},  03:04 PM</vote_date>"
            f"<vote_question_text>{question}</vote_question_text><vote_result_text>{result}</vote_result_text>"
            f"<vote_title>t</vote_title><count><yeas>{yeas}</yeas><nays>{nays}</nays></count>{tb}"
            f"<members>{''.join(members)}</members></roll_call_vote>")


def _rep(name, party, state, cast):
    return (f"<recorded-vote><legislator name-id='X' unaccented-name='{name}' party='{party}' state='{state}' "
            f"role='legislator'>{name}</legislator><vote>{cast}</vote></recorded-vote>")


def _house_vote(number, day, result, yeas, nays, members):
    return (f"<?xml version='1.0'?><rollcall-vote><vote-metadata><rollcall-num>{number}</rollcall-num>"
            f"<legis-num>H R 5376</legis-num><vote-question>On Motion to Concur</vote-question>"
            f"<vote-result>{result}</vote-result><action-date>{day}</action-date><vote-desc>d</vote-desc>"
            f"<vote-totals><totals-by-vote><total-stub>Totals</total-stub><yea-total>{yeas}</yea-total>"
            f"<nay-total>{nays}</nay-total></totals-by-vote></vote-totals></vote-metadata>"
            f"<vote-data>{''.join(members)}</vote-data></rollcall-vote>")


SENATE_325 = "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1172/vote_117_2_00325.xml"
HOUSE_420 = "https://clerk.house.gov/evs/2022/roll420.xml"
SENATE_134 = "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1172/vote_117_2_00134.xml"
SENATE_15 = "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00015.xml"

FILES = {
    SENATE_325: _senate_vote(325, "August 7, 2022", "On Passage of the Bill H.R. 5376",
                             "Bill Passed (50-50, Vice President of the United States, voted Yea)", 50, 50, [
                                 _senator("Manchin", "Joseph", "D", "WV", "Yea"),
                                 _senator("Sinema", "Kyrsten", "D", "AZ", "Yea"),
                                 _senator("Sanders", "Bernard", "I", "VT", "Yea"),
                                 _senator("Collins", "Susan", "R", "ME", "Nay"),
                                 _senator("Romney", "Mitt", "R", "UT", "Nay"),
                                 _senator("Scott", "Tim", "R", "SC", "Nay"),
                                 _senator("Scott", "Rick", "R", "FL", "Nay"),
                                 _senator("Cornyn", "John", "R", "TX", "Not Voting")], tie="Yea"),
    HOUSE_420: _house_vote(420, "12-Aug-2022", "Passed", 220, 207, [
        _rep("Ocasio-Cortez", "D", "NY", "Yea"), _rep("Pelosi", "D", "CA", "Yea"),
        _rep("Massie", "R", "KY", "Nay"), _rep("Cheney", "R", "WY", "Nay"),
        _rep("Smith (NJ)", "R", "NJ", "Nay"), _rep("Smith (WA)", "D", "WA", "Yea")]),
    SENATE_134: _senate_vote(134, "April 7, 2022", "On the Nomination PN1783", "Nomination Confirmed (53-47)", 53, 47, [
        _senator("Collins", "Susan", "R", "ME", "Yea"), _senator("Romney", "Mitt", "R", "UT", "Yea"),
        _senator("Cruz", "Ted", "R", "TX", "Nay")]),
    SENATE_15: _senate_vote(15, "January 24, 2025", "On the Nomination PN11-8",
                            "Nomination Confirmed (50-50, Vice President of the United States, voted Yea)", 50, 50, [
                                _senator("McConnell", "Mitch", "R", "KY", "Nay"),
                                _senator("Thune", "John", "R", "SD", "Yea")], tie="Yea"),
}


def _menu(congress_n, session, year, votes_):
    rows = "".join(
        f"<vote><vote_number>{n:05d}</vote_number><vote_date>{d}</vote_date><issue>{issue}</issue>"
        f"<question>{q}</question><result>{res}</result><vote_tally><yeas>{y}</yeas><nays>{nn}</nays></vote_tally>"
        f"<title>{title}</title></vote>" for n, d, issue, q, res, y, nn, title in votes_)
    return (f"<?xml version='1.0'?><vote_summary><congress>{congress_n}</congress><session>{session}</session>"
            f"<congress_year>{year}</congress_year><votes>{rows}</votes></vote_summary>")


MENUS = {
    (117, 2): _menu(117, 2, 2022, [
        (134, "07-Apr", "PN1783", "On the Nomination", "Confirmed", 53, 47,
         "Confirmation: Ketanji Brown Jackson, of the District of Columbia, to be an Associate Justice of the "
         "Supreme Court of the United States"),
        (133, "07-Apr", "PN1783", "On the Cloture Motion", "Agreed to", 53, 47,
         "Motion to Invoke Cloture: Ketanji Brown Jackson to be an Associate Justice of the Supreme Court")]),
    (119, 1): _menu(119, 1, 2025, [
        (15, "24-Jan", "PN11-8", "On the Nomination", "Confirmed", 50, 50,
         "Confirmation: Peter Hegseth, of Tennessee, to be Secretary of Defense")]),
}

_IRA_ACTIONS = [
    {"actionDate": "2022-08-12", "actionCode": "H41610", "type": "ResolvingDifferences",
     "text": "On motion that the House agree to the Senate amendment Agreed to by the Yeas and Nays: 220 - 207 "
             "(Roll no. 420).", "recordedVotes": [{"chamber": "House", "url": HOUSE_420}]},
    {"actionDate": "2022-08-07", "actionCode": "17000", "type": "Floor",
     "text": "Passed/agreed to in Senate: Passed Senate with an amendment by Yea-Nay Vote. 51 - 50. "
             "Record Vote Number: 325.", "recordedVotes": [{"chamber": "Senate", "url": SENATE_325}]},
    {"actionDate": "2022-08-07", "actionCode": "", "type": "Floor",
     "text": "Motion by Senator Cruz to commit to Senate Committee on the Judiciary with instructions rejected",
     "recordedVotes": [{"chamber": "Senate", "url": SENATE_325.replace("00325", "00316")}]},
    {"actionDate": "2021-11-19", "actionCode": "8000", "type": "Floor",
     "text": "Passed/agreed to in House: On passage Passed by the Yeas and Nays: 220 - 213 (Roll no. 385).",
     "recordedVotes": [{"chamber": "House", "url": "https://clerk.house.gov/evs/2021/roll385.xml"}]},
]


@pytest.fixture
def sources(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(bills, "_today", lambda: date(2026, 11, 23))
    fetched = []

    async def fetch(url, source):
        fetched.append(url)
        if "vote_menu_" in url:
            c, s = url.rsplit("vote_menu_", 1)[1].removesuffix(".xml").split("_")
            return MENUS.get((int(c), int(s)), _menu(int(c), int(s), 2000, []))
        return FILES.get(url)

    async def get(path, **params):
        if path.startswith("law/118"):
            return {"pagination": {"count": 274}}
        if not path.startswith("bill/117/hr/5376"):
            return None
        tail = path.split("/")[4] if len(path.split("/")) > 4 else ""
        if tail == "actions":
            return {"actions": _IRA_ACTIONS}
        if tail == "titles":
            return {"titles": [{"title": "Inflation Reduction Act of 2022", "titleType": "Popular Titles"}]}
        return {"bill": {"number": "5376", "type": "HR", "congress": 117, "title": "Inflation Reduction Act of 2022",
                         "laws": [{"number": "117-169", "type": "Public Law"}], "sponsors": [],
                         "latestAction": {"actionDate": "2022-08-16", "text": "Became Public Law No: 117-169."}}}
    monkeypatch.setattr(rollcall, "_fetch", fetch)
    monkeypatch.setattr(congress, "_get", get)
    return fetched


@pytest.mark.parametrize("claim,expected,says", [
    ("Senator Joe Manchin voted for the Inflation Reduction Act.", SUPPORTED, "Manchin (D-WV) voted Yea"),
    ("Senator Susan Collins voted for the Inflation Reduction Act.", CONTRADICTED, "voted Nay"),
    ("Mitt Romney voted against the Inflation Reduction Act.", SUPPORTED, "Romney (R-UT) voted Nay"),
    ("Senator Susan Collins did not vote for the Inflation Reduction Act.", SUPPORTED, "voted Nay"),
    ("Senator John Cornyn voted against the Inflation Reduction Act.", CONTRADICTED, "did not vote"),
    ("Rep. Thomas Massie voted against the Inflation Reduction Act.", SUPPORTED, "House roll call of August 12, 2022"),
    # A senator who was a House member at the time is found in the House's vote.
    ("Senator Liz Cheney voted against the Inflation Reduction Act.", SUPPORTED, "Rep. Liz Cheney (R-WY)"),
    ("Every Republican senator voted against the Inflation Reduction Act.", SUPPORTED, "0 Republicans voted for"),
    ("No Senate Democrats voted against the Inflation Reduction Act.", SUPPORTED, "2 Democrats voted for and 0"),
    ("Three Senate Republicans voted for the Inflation Reduction Act.", CONTRADICTED, "0 Republicans voted for"),
    ("Every House Democrat voted for the Inflation Reduction Act.", SUPPORTED, "3 Democrats voted for and 0"),
    ("All 50 Senate Democrats voted for the Inflation Reduction Act.", MOSTLY_ACCURATE, "2 Democrats voted for"),
])
async def test_votes_on_a_bill(sources, claim, expected, says):
    v = await votes.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline


async def test_the_last_passage_vote_is_read_not_a_motion(sources):
    await votes.check("Senator Joe Manchin voted for the Inflation Reduction Act.")
    assert SENATE_325 in sources and HOUSE_420 in sources
    assert not any("00316" in u or "roll385" in u for u in sources)


@pytest.mark.parametrize("claim", [
    "Smith voted for the Inflation Reduction Act.",              # two Smiths in the House
    "Senator Scott voted against the Inflation Reduction Act.",  # two Scotts in the Senate
    "Senator Jane Nobody voted for the Inflation Reduction Act.",
    "Republicans voted against the Inflation Reduction Act.",    # no quantity claimed
    "The Senate passed the Inflation Reduction Act 51-50.",      # the bill check's claim, not this one's
    "Senators voted for hours on the Inflation Reduction Act.",
])
async def test_what_is_not_settled_is_left_alone(sources, claim):
    assert await votes.check(claim) is None


@pytest.mark.parametrize("claim,expected,says", [
    ("The Senate confirmed Ketanji Brown Jackson 53-47.", SUPPORTED, "April 7, 2022"),
    ("The Senate confirmed Ketanji Brown Jackson 60-40.", CONTRADICTED, "53–47, not 60–40"),
    ("Ketanji Brown Jackson was confirmed in 2022.", SUPPORTED, "Associate Justice"),
    ("The Senate rejected Ketanji Brown Jackson.", CONTRADICTED, "The Senate confirmed"),
    # 50-50 with the Vice President's vote is reported as 51-50; a nickname is the same person.
    ("The Senate confirmed Pete Hegseth as Secretary of Defense 51-50.", SUPPORTED, "Vice President broke the tie"),
    ("Senator Mitch McConnell voted against confirming Pete Hegseth.", SUPPORTED, "McConnell (R-KY) voted Nay"),
    ("Senator Susan Collins voted to confirm Ketanji Brown Jackson.", SUPPORTED, "Collins (R-ME) voted Yea"),
    ("Senator Ted Cruz voted to confirm Ketanji Brown Jackson.", CONTRADICTED, "voted Nay"),
])
async def test_confirmations(sources, claim, expected, says):
    v = await votes.check(claim)
    assert v is not None, claim
    assert v.verdict == expected, v.headline
    assert says in v.headline, v.headline


async def test_a_nominee_with_no_roll_call_is_not_judged(sources):
    assert await votes.check("The Senate confirmed Jane Q. Nobody in 2022.") is None


async def test_laws_a_congress_enacted(sources):
    v = await votes.check("The 118th Congress passed fewer than 300 laws.")
    assert v.verdict == SUPPORTED and "274 public laws" in v.headline
    v = await votes.check("The 118th Congress enacted 500 laws.")
    assert v.verdict == CONTRADICTED


async def test_only_the_chambers_own_files_are_fetched(sources):
    assert await rollcall.vote("https://example.com/evs/2022/roll420.xml") is None
    assert await rollcall.vote("https://www.senate.gov.evil.example/legislative/LIS/roll_call_votes/"
                               "vote1172/vote_117_2_00325.xml") is None
    assert sources == []


def test_parsers_read_the_live_shapes():
    s = rollcall.parse_senate_vote(FILES[SENATE_325], SENATE_325)
    assert (s["yea"], s["nay"], s["date"], s["tie_breaker"]) == (50, 50, "2022-08-07", "Yea")
    assert s["url"].endswith("vote_117_2_00325.htm")
    h = rollcall.parse_house_vote(FILES[HOUSE_420], HOUSE_420)
    assert (h["yea"], h["nay"], h["date"]) == (220, 207, "2022-08-12")
    assert h["url"] == "https://clerk.house.gov/Votes/2022420"
    assert [m["last"] for m in h["members"] if m["state"] == "NJ"] == ["Smith"]
    assert rollcall.parse_senate_vote("<html>blocked</html>", SENATE_325) is None
    assert rollcall.parse_senate_menu("not xml") == []


def test_vote_claims_are_a_topic_for_routing(sources):
    assert regulatory.topic("Senator Joe Manchin voted for the Inflation Reduction Act.") == "vote"
    assert regulatory.topic("The Senate confirmed Ketanji Brown Jackson 53-47.") == "vote"
    assert regulatory.topic("The Senate passed the Inflation Reduction Act 51-50.") == "bill"
    assert "roll call" in regulatory.search_hint("Senator Joe Manchin voted for the Inflation Reduction Act.")


@pytest.mark.parametrize("claim,topic", [
    ("Senator Joe Manchin voted against the border bill.", "vote"),      # no bill this can name: search
    ("Every House Republican voted against the amendment.", "vote"),
    ("Americans voted for Trump by 49.8% in 2024.", ""),                 # an election
    ("Shareholders voted against the merger.", ""),
])
def test_only_roll_calls_are_routed_as_votes(claim, topic):
    assert regulatory.topic(claim) == topic
