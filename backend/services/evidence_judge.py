"""A verdict on any claim, from evidence: the instructions and the reading.

The rest of the engine answers a claim only when an official figure settles
it, and declines the rest. A fact-checker does not decline: a claim it takes
up gets True, False or something in between, read from whatever evidence
there is. This module is that step, written once, so the same instructions
serve a production model call and a test run in which something else reads
them (eval/quantemp_judge.py).

It never fetches anything. It is given the claim, the day it was said if
known, and evidence: search snippets, and the official figure when the engine
found one. It returns one of three verdicts, in fact-checkers' terms and
in this service's:

    True         Supported       the figure and what is made of it hold
    Conflicting  Misleading      partly right: a true figure stretched,
                                 cherry-picked or missing context, or a mix
    False        Contradicted    the figure is wrong, unsupported or invented

The instructions encode how fact-checkers rate claims: general knowledge of
the trade, and how their ratings fall by kind of claim in the training split
of the benchmark (QuanTemp's train file: a politician's quoted statement is
rated True 29%, Conflicting 36%, False 34%; a photo, video or viral post
8%, 17%, 76%). Nothing in them comes from the claims a run is scored on.

Measured on QuanTemp (macro-F1, 300 held-out test claims, the published
systems' evidence, the same frozen INSTRUCTIONS; 18 Nov 2026):

    Haiku-class judge     50.71
    Sonnet-class judge    61.32   (+10.6 on the same claims; 95% +3.4 to +17.6)
    best published        58.32   (FinQA-RoBERTa with ClaimDecomp, no leaked evidence)

The judges were agents reading the instructions, not the production API.
"""
import json
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

VERDICTS = ("True", "Conflicting", "False")
# This service's names for them.
AS_SERVICE = {"True": "Supported", "Conflicting": "Misleading", "False": "Contradicted"}

# Snippets that name a fact-checker may quote its ruling on this very claim:
# reading those would be copying the answer, not checking the claim.
_FACT_CHECKERS = re.compile(
    r"politi\s?fact|snopes|factcheck\.org|full\s?fact|lead\s?stories|check\s?your\s?fact|truth\s?or\s?fiction|"
    r"\bafp fact|reuters fact|fact[- ]check(?:ed|er|ers|ing)?\b|pinocchio", re.IGNORECASE)

INSTRUCTIONS = """You rate a claim the way a professional fact-checker would, from the evidence given. You never look anything up and never rely on what you happen to remember about how this claim was rated; the evidence is all you have, with general knowledge of how numbers, statistics and politics work.

Return exactly one verdict:

- "True": the claim's figure is right (or close, as stated: "about", "nearly", rounding) AND what the claim makes of it holds. Fact-checkers' "True" and "Mostly True".
- "Conflicting": partly right. A real figure stretched: missing context that changes its meaning, a cherry-picked period or comparison, credit or blame for something with many causes, an old figure presented as current, one part right and another wrong, a vote or record described accurately but spun. Fact-checkers' "Half True", PolitiFact's "Mostly False", "Mixture", "Partly false", "Miscaptioned".
- "False": the figure is wrong or invented, the evidence contradicts it, or the claim's central point does not survive. Fact-checkers' "False", "Pants on Fire", "Fake", "Unproven".

How to decide:
1. Find the claim's checkable core: the figure, what it is a figure of, the place and the period, and what the speaker says it shows. Note who is speaking and how: a politician or official on the record, or an anonymous post, photo, video or meme.
2. Look for that figure in the evidence. Snippets are lower-case fragments, often about something nearby; weigh only those about the same thing, place and time.
3. When the evidence addresses the figure: close and fairly used -> True; close but the framing overreaches -> Conflicting; off by more than rounding, or about something else -> False.
4. When the evidence says nothing about it, silence is NOT evidence that it is false. Decide from the kind of claim, as fact-checkers' ratings fall:
   - A politician's or official's statement on the record (a quote, "Says...", a figure about their own record, a budget, a bill, votes, jobs, taxes): ratings split about evenly, True 30%, Conflicting 36%, False 34%. Politicians usually cite a real number; the question is usually what they make of it. A specific, checkable figure about their own record or an official count -> lean True. A figure used to claim credit, assign blame, or compare selectively -> Conflicting. A sweeping or extreme figure, or one about an opponent's character -> False.
   - An anonymous or viral post, a photo or video caption, a meme, a chain message, a quote attributed to a celebrity: about 3 in 4 are False, and fewer than 1 in 10 True. Lean False unless the evidence supports it.
   - A claim about another country made in that country's political debate: about 4 in 5 are False.
5. Never treat the speaker's own repetition of the claim as evidence for it.
6. A claim worded as a fact-check's headline ("No, X did not...", "No, this photo does not show...") names a claim that went round and was rated; rate that claim, the one being denied, not the headline. Such claims are False.

Reply with JSON only: {"verdict": "True" | "Conflicting" | "False", "why": "<one sentence citing the evidence, or the kind of claim when the evidence is silent>"}"""


@dataclass
class Evidence:
    text: str
    source: str = ""          # where it came from, when known
    official: bool = False    # an agency's own figure, from this service


def usable(snippets: Iterable[str], limit: int = 30, max_chars: int = 400) -> List[Evidence]:
    """The snippets a judge may read: no fact-checker's ruling, no repeats,
    in the order given (best first), at most `limit`."""
    out: List[Evidence] = []
    seen = set()
    for s in snippets:
        text = " ".join(str(s or "").split())[:max_chars]
        key = text.lower()[:120]
        if len(text) < 30 or key in seen or _FACT_CHECKERS.search(text):
            continue
        seen.add(key)
        out.append(Evidence(text))
        if len(out) >= limit:
            break
    return out


def render(claim: str, evidence: List[Evidence], said_on: str = "") -> str:
    """What the judge reads after the instructions."""
    lines = [f"Claim: {' '.join(claim.split())}"]
    if said_on:
        lines.append(f"Said on: {said_on}")
    official = [e for e in evidence if e.official]
    if official:
        lines.append("Official figures found by this service (exact, from the agency):")
        lines += [f"  * {e.text}" + (f" [{e.source}]" if e.source else "") for e in official]
    rest = [e for e in evidence if not e.official]
    lines.append(f"Search snippets ({len(rest)}):" if rest else "Search snippets: none")
    lines += [f"  {i}. {e.text}" for i, e in enumerate(rest, 1)]
    return "\n".join(lines)


def read(reply: str) -> Optional[dict]:
    """The verdict in a judge's reply, or None if there is none."""
    m = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    verdict = str(got.get("verdict") or "").strip().title()
    if verdict not in VERDICTS:
        return None
    return {"verdict": verdict, "service_verdict": AS_SERVICE[verdict], "why": str(got.get("why") or "")[:400]}

# ------------------------------------------------------------------ decomposed

# The published systems' largest gain on QuanTemp came from splitting a
# claim into the questions that check it (ClaimDecomp: +8.8% macro-F1). A
# half-true claim is one whose figure survives and whose framing does not;
# asked as one question, a judge tends to call it False. Asked as several,
# with the verdict worked out here from the answers, it should be Conflicting.
#
# Measured on QuanTemp's 150 development claims (18 Nov 2026), it did not
# pay: 46.74 macro-F1, against 58.68 for the single verdict on the same
# claims (the single verdict's instructions were tuned on them; its held-out
# score is 50.71). Half-true claims were caught more often (Conflicting F1
# 45.65 against 43.48), but True collapsed: the snippets rarely confirm a
# figure outright, so most "core" answers were "unclear" (79 of 150) and the
# verdict fell to the lean. Kept, with its tests, so a stronger judge or
# better evidence can be measured against it; INSTRUCTIONS is what to use.

DECOMPOSE_INSTRUCTIONS = """You check a claim the way a professional fact-checker would: by splitting it into the questions that decide it, and answering each from the evidence given. You never look anything up and never rely on what you happen to remember about how this claim was rated.

Write 2 to 4 yes/no questions whose answers decide the claim:
- "core": is the claim's figure itself right? One or two questions: the number, for the thing, place and period named ("Did 15 million jobs get created between 2010 and 2016?", "Is about 22,000 the number of deaths a year?").
- "framing": does what the claim makes of the figure hold? One or two questions: the comparison, the cause or credit, the period chosen, what is left out ("Is the count taken from the job market's low point rather than the start of the term?", "Does the evidence attribute those deaths to lack of insurance?").
Write each so that "yes" is good for the claim.

Answer each "yes", "no" or "unclear" from the evidence. Snippets are lower-case fragments, often about something nearby; weigh only those about the same thing, place and time. Silence in the evidence is "unclear", never "no".

Then give "lean": your overall verdict if the answers alone cannot decide it. Use how fact-checkers rate claims of this kind:
- A politician's or official's statement on the record: True 30%, Conflicting 36%, False 34%. Politicians usually cite a real number; the question is usually what they make of it.
- An anonymous or viral post, a photo or video caption, a meme, a quote attributed to a celebrity: about 3 in 4 False.
- A claim about another country made in its political debate: about 4 in 5 False.
- A claim worded as a fact-check's headline ("No, X did not...") names the claim being denied: lean False.

Reply with JSON only:
{"questions": [{"q": "...", "kind": "core" | "framing", "answer": "yes" | "no" | "unclear", "why": "<short, citing the evidence>"}], "lean": "True" | "Conflicting" | "False"}"""


def decide(questions: List[dict], lean: str) -> str:
    """The verdict from a claim's checked questions. Fact-checkers' scale,
    worked out the way they work it:

      the figure is wrong                          -> False
      the figure is right, the framing is not      -> Conflicting
      the figure is right, the framing holds       -> True
      the figure cannot be checked from the evidence -> the lean, which
                                                      rests on the kind of claim
    """
    core = [q["answer"] for q in questions if q.get("kind") == "core"]
    framing = [q["answer"] for q in questions if q.get("kind") == "framing"]
    lean = lean if lean in VERDICTS else "False"
    if "no" in core:
        # A wrong figure whose framing is otherwise fair is still a wrong figure.
        return "False"
    if core and all(a == "yes" for a in core):
        if "no" in framing:
            return "Conflicting"
        if all(a == "yes" for a in framing):
            return "True"
        # The figure holds and the framing cannot be checked: never worse
        # than Conflicting, and True only if the kind of claim leans that way.
        return "True" if lean == "True" else "Conflicting"
    # The figure itself is unclear.
    if "no" in framing and lean == "True":
        return "Conflicting"
    return lean


def read_decomposed(reply: str) -> Optional[dict]:
    """The questions and the verdict worked out from them, or None."""
    m = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    qs = []
    for q in got.get("questions") or []:
        if not isinstance(q, dict):
            continue
        kind = str(q.get("kind") or "").lower()
        answer = str(q.get("answer") or "").lower()
        if kind in ("core", "framing") and answer in ("yes", "no", "unclear"):
            qs.append({"q": str(q.get("q") or "")[:240], "kind": kind, "answer": answer,
                       "why": str(q.get("why") or "")[:240]})
    lean = str(got.get("lean") or "").strip().title()
    if not qs and lean not in VERDICTS:
        return None
    verdict = decide(qs, lean)
    return {"verdict": verdict, "service_verdict": AS_SERVICE[verdict], "lean": lean, "questions": qs}
