"""FTC claims checked against the FTC's own record.

  fraud data   "Americans lost $12.5 billion to fraud in 2024"
               "The FTC got 2.6 million fraud reports last year"
               Consumer Sentinel figures as the FTC first published them
  rules        "The FTC banned noncompete agreements"
               "The click-to-cancel rule is in effect"
               what the rule says and whether it is in force, from the
               Federal Register (the FTC's own notices of each rule)

Consumer Sentinel counts reports people chose to file with the FTC, not all
fraud; every answer says so. The FTC revises a year's totals as late reports
arrive; the figure here is the one it first published.
"""
import re
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from claims.verdict import CONTRADICTED, MISLEADING, SUPPORTED, Verdict
from evidence.excerpt import row as ex_row
from evidence.units import PERSONS, USD
from evidence.units import parse_quantity
from services.regulatory import fact, figure_check, verdict


def parsed_value(text: str) -> float:
    q = parse_quantity(text)
    return q.magnitude if q else float("inf")

_FTC = re.compile(r"\bFTC\b|\bFederal Trade Commission\b|\bConsumer Sentinel\b|\bSentinel\b", re.I)
_FRAUD = re.compile(r"\b(fraud|frauds|scams?|scammers?|scammed|imposters?|impostors?)\b", re.I)
_PEOPLE = re.compile(r"\b(Americans|consumers|people|U\.S\.|US residents|the public)\b", re.I)
# Figures the table does not hold: another publisher, a subgroup, a channel.
_OTHER = re.compile(r"\b(FBI|IC3|Internet Crime|global(?:ly)?|worldwide|world|AARP|older (?:adults|Americans)|"
                    r"seniors|elderly|over 60|60 and (?:over|older)|social media|text messages?|romance|"
                    r"crypto(?:currency)?|bitcoin|gift cards?|phone calls?|email|online shopping|"
                    r"job scams?|business imposters?|government imposters?|tech support|state|county|city)\b", re.I)

_RELEASES = {
    2021: "https://www.ftc.gov/news-events/news/press-releases/2022/02/new-data-shows-ftc-received-28-million-fraud-reports-consumers-2021-0",
    2022: "https://www.ftc.gov/news-events/news/press-releases/2023/02/new-ftc-data-show-consumers-reported-losing-nearly-88-billion-scams-2022",
    2023: "https://www.ftc.gov/news-events/news/press-releases/2024/02/nationwide-fraud-losses-top-10-billion-2023-ftc-steps-efforts-protect-public",
    2024: "https://www.ftc.gov/news-events/news/press-releases/2025/03/new-ftc-data-show-big-jump-reported-losses-fraud-125-billion-2024",
    2025: "https://www.ftc.gov/news-events/news/press-releases/2026/03/ftc-testifies-joint-economic-committee-agencys-efforts-combat-fraud",
}

# (value, as the FTC wrote it). From the FTC's release for each year.
_SENTINEL: Dict[int, Dict[str, Tuple[float, str]]] = {
    2021: {"fraud_losses": (5.8e9, "more than $5.8 billion"), "fraud_reports": (2.8e6, "more than 2.8 million"),
           "total_reports": (5.7e6, "more than 5.7 million"), "identity_theft": (1.4e6, "nearly 1.4 million"),
           "imposter_losses": (2.3e9, "more than $2.3 billion")},
    2022: {"fraud_losses": (8.8e9, "nearly $8.8 billion"), "fraud_reports": (2.4e6, "2.4 million"),
           "total_reports": (5.1e6, "more than 5.1 million"), "identity_theft": (1.1e6, "over 1.1 million"),
           "investment_losses": (3.8e9, "more than $3.8 billion"), "imposter_losses": (2.6e9, "$2.6 billion")},
    2023: {"fraud_losses": (10e9, "more than $10 billion"), "fraud_reports": (2.6e6, "2.6 million"),
           "total_reports": (5.4e6, "5.4 million"), "identity_theft": (1.0e6, "more than 1 million"),
           "investment_losses": (4.6e9, "more than $4.6 billion"), "imposter_losses": (2.7e9, "nearly $2.7 billion")},
    2024: {"fraud_losses": (12.5e9, "more than $12.5 billion"), "fraud_reports": (2.6e6, "2.6 million"),
           "total_reports": (6.5e6, "6.5 million"), "identity_theft": (1.1e6, "more than 1.1 million"),
           "investment_losses": (5.7e9, "$5.7 billion"), "imposter_losses": (2.95e9, "$2.95 billion")},
    2025: {"fraud_losses": (15.9e9, "$15.9 billion"), "fraud_reports": (3.0e6, "3 million"),
           "investment_losses": (7.9e9, "$7.9 billion"), "imposter_losses": (3.5e9, "more than $3.5 billion")},
}
_LABELS = {
    "fraud_losses": "Fraud losses reported to the FTC",
    "fraud_reports": "Fraud reports to the FTC",
    "total_reports": "All reports to Consumer Sentinel",
    "identity_theft": "Identity theft reports to the FTC",
    "investment_losses": "Investment scam losses reported to the FTC",
    "imposter_losses": "Imposter scam losses reported to the FTC",
}
_SENTINEL_NOTE = ("These are losses and reports people filed with the FTC's Consumer Sentinel; most "
                  "fraud is never reported, so the true totals are higher.")

_DOLLARS = re.compile(r"\$\s?\d[\d,.]*\s*(?:billion|million|trillion|bn|b|m)?\b", re.I)
_COUNT = re.compile(r"\b\d[\d,.]*\s*(?:million|thousand)?(?=\s+(?:fraud |scam |identity theft |consumer |total )?"
                    r"(?:reports|complaints|cases|people|consumers|Americans)\b)", re.I)
_YEAR = re.compile(r"\b(20[12]\d)\b")


def _measure(text: str) -> Optional[str]:
    money = bool(_DOLLARS.search(text))
    if re.search(r"\bidentity theft\b", text, re.I) and not money:
        return "identity_theft"
    if not money and re.search(r"\b(reports?|complaints?)\b", text, re.I):
        if re.search(r"\bSentinel\b", text, re.I) and not _FRAUD.search(text):
            return "total_reports"
        if _FRAUD.search(text):
            return "fraud_reports"
        return None
    if not money or not re.search(r"\b(lost|lose|losing|losses?|cost|stolen|stole|took|swindled|bilked|cheated|scammed out of)\b", text, re.I):
        return None
    if re.search(r"\binvestment (?:scams?|fraud)\b", text, re.I):
        return "investment_losses"
    if re.search(r"\bimpost[eo]rs?\b", text, re.I):
        return "imposter_losses"
    if _FRAUD.search(text):
        return "fraud_losses"
    return None


def _sentinel(text: str, parsed) -> Optional[Verdict]:
    if not _FRAUD.search(text) and not re.search(r"identity theft", text, re.I):
        return None
    if not (_FTC.search(text) or _PEOPLE.search(text)):
        return None
    scrub = re.sub(r"\b(investment|imposter|impostor)s?\b", "", text, flags=re.I)
    if _OTHER.search(scrub):
        return None
    measure = _measure(text)
    if measure is None:
        return None
    years = [int(y) for y in _YEAR.findall(text)]
    year = years[0] if years else (parsed.year or None)
    caveats = [_SENTINEL_NOTE]
    if year is None:
        year = max(y for y, row in _SENTINEL.items() if measure in row)
        caveats.append(f"The claim names no year; the FTC's latest annual figure ({year}) is used.")
    row = _SENTINEL.get(year, {}).get(measure)
    if row is None:
        return None
    value, as_written = row
    figure = (_DOLLARS if measure.endswith("losses") else _COUNT).search(text)
    if figure is None and not measure.endswith("losses"):
        figure = re.search(r"\b\d[\d,.]*\s+(?:million|thousand)\b", text, re.I)
    if figure is None:
        return None
    family = USD if measure.endswith("losses") else PERSONS
    dp = fact("FTC", _LABELS[measure], as_written,
              _RELEASES[year], year=year, magnitude=value, family=family,
              rows=[ex_row(f"{_LABELS[measure]}, {year}", as_written, cited=True)],
              note="As first published by the FTC; later Data Books can revise it.")
    v = figure_check(parsed, figure.group(0).strip(), dp, text, family)
    if v is None:
        return None
    if (v.verdict == CONTRADICTED and v.comparator == "greater than"
            and re.match(r"(more than|over)\b", as_written) and parsed_value(figure.group(0)) <= value):
        # The FTC itself wrote "more than 1 million": the claim says the same.
        v = replace(v, verdict=SUPPORTED, headline=f"FTC reports {as_written} for {year}.")
    return replace(v, caveats=list(v.caveats) + caveats)


# ------------------------------------------------------------------ rules

_FR = "https://www.federalregister.gov/documents/"
_CONFORM = _FR + ("2026/02/12/2026-02866/revision-of-the-negative-option-rule-withdrawal-of-the-cars-rule-"
                  "removal-of-the-non-compete-rule-to")


@dataclass(frozen=True)
class Rule:
    key: str
    pattern: str
    name: str
    in_force: bool
    status: str           # shown as the official answer
    story: str            # one or two sentences, every date from the Federal Register
    url: str
    scope: str = ""       # what the rule covers, when narrower than its nickname
    scope_pattern: str = ""


_RULES = (
    Rule("noncompete", r"\bnon-?compete", "Non-Compete Clause Rule", False,
         "Never took effect; removed from the CFR Feb 12, 2026",
         "The FTC published a rule banning most noncompete agreements on May 7, 2024, but a federal "
         "court set it aside before its September 4, 2024 effective date, and the FTC removed it from "
         "the Code of Federal Regulations on February 12, 2026.", _CONFORM),
    Rule("click_to_cancel", r"\bclick[- ]to[- ]cancel\b|\bnegative option rule\b", "Negative Option Rule (2024 \"click-to-cancel\" amendments)", False,
         "Vacated; FTC restored the earlier rule Feb 12, 2026",
         "The FTC published its click-to-cancel amendments on November 15, 2024; a federal appeals court "
         "vacated them in July 2025, and on February 12, 2026 the FTC put back the rule's earlier text.", _CONFORM),
    Rule("cars", r"\bCARS rule\b|\bcombating auto retail scams\b", "Combating Auto Retail Scams (CARS) Rule", False,
         "Withdrawn Feb 12, 2026",
         "The FTC published the CARS Rule on January 4, 2024; after a federal appeals court vacated it, "
         "the FTC withdrew it on February 12, 2026.", _CONFORM),
    Rule("fake_reviews", r"\bfake (?:online )?reviews?\b|\bconsumer reviews and testimonials\b", "Rule on the Use of Consumer Reviews and Testimonials", True,
         "In effect since Oct 21, 2024",
         "The FTC's rule against fake reviews and testimonials was published August 22, 2024 and took "
         "effect October 21, 2024.",
         _FR + "2024/08/22/2024-18519/trade-regulation-rule-on-the-use-of-consumer-reviews-and-testimonials"),
    Rule("junk_fees", r"\bjunk fees?\b|\bhidden fees?\b|\bdrip pricing\b|\bunfair or deceptive fees\b", "Rule on Unfair or Deceptive Fees", True,
         "In effect since May 12, 2025 (live-event tickets and short-term lodging)",
         "The FTC's fee rule was published January 10, 2025 and took effect May 12, 2025. It covers "
         "live-event tickets and short-term lodging only.",
         _FR + "2025/01/10/2024-30293/trade-regulation-rule-on-unfair-or-deceptive-fees",
         scope="live-event tickets and short-term lodging",
         scope_pattern=r"\btickets?\b|\bconcerts?\b|\bevents?\b|\bhotels?\b|\blodging\b|\bvacation rentals?\b|"
                       r"\bAirbnb\b|\bresort fees?\b|\bsports\b"),
    Rule("impersonation", r"\bimpersonation rule\b|\bimpersonat\w* (?:of )?(?:government|businesses)\b", "Rule on Impersonation of Government and Businesses", True,
         "In effect since Apr 1, 2024",
         "The FTC's impersonation rule was published March 1, 2024 and took effect April 1, 2024.",
         _FR + "2024/03/01/2024-04335/trade-regulation-rule-on-impersonation-of-government-and-businesses"),
)

_SAYS_DEAD = re.compile(r"\b(struck down|strikes down|blocked|vacated|overturned|set aside|thrown out|tossed|"
                        r"never took effect|not in effect|no longer in effect|killed|scrapped|repealed|removed|"
                        r"withdr[ae]w[n]?|rescinded|dropped|abandoned|halted|invalidated|gutted|dead|reversed)\b", re.I)
_SAYS_LIVE = re.compile(r"\b(banned|bans|ban on|banning|prohibit(?:s|ed)?|outlaw(?:ed|s)?|requires?|required|"
                        r"took effect|takes effect|in effect|in force|is now law|now illegal|made illegal|"
                        r"illegal|must|forbids?|forbade|cracked down|now has to)\b", re.I)
_STATES = re.compile(r"\b(California|Minnesota|Oklahoma|North Dakota|Montana|New York|Colorado|state law|states)\b")


def _rule(text: str) -> Optional[Verdict]:
    if not re.search(r"\bFTC\b|\bFederal Trade Commission\b|\bfederal\b|\bnationwide\b|\bnational\b", text, re.I):
        return None
    if _STATES.search(text) or re.search(r"\bwill\b|\bplans? to\b|\bwants? to\b|\bpropos", text, re.I):
        return None
    rule = next((r for r in _RULES if re.search(r.pattern, text, re.I)), None)
    if rule is None:
        return None
    dead, live = bool(_SAYS_DEAD.search(text)), bool(_SAYS_LIVE.search(text))
    if not dead and not live:
        return None
    says_in_force = live and not dead
    dp = fact("FTC", rule.name, rule.status, rule.url, basis="Federal Register",
              rows=[ex_row(rule.name, rule.status, cited=True)], note=rule.story)
    if rule.in_force and says_in_force and rule.scope and not re.search(rule.scope_pattern, text, re.I):
        return verdict(MISLEADING, f"{rule.story} It does not ban such fees everywhere.", [dp])
    outcome = SUPPORTED if says_in_force == rule.in_force else CONTRADICTED
    return verdict(outcome, rule.story, [dp])


async def check(text: str, parsed) -> Optional[Verdict]:
    return _rule(text) or _sentinel(text, parsed)
