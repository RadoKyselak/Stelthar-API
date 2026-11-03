"""FDA claims checked against FDA's own records (openFDA), not web pages.

  approval     "The FDA approved Wegovy in 2021", "X is not FDA approved",
               "semaglutide was first approved in 2017"     Drugs@FDA, labels
  indication   "Ozempic is FDA-approved for weight loss"     the approved label
  boxed        "Zoloft has a black box warning"              the approved label
  recall       "Boar's Head was recalled for listeria"       enforcement reports
  shortage     "There is a shortage of Adderall"             FDA shortage list
  device       "The Dexcom G7 is FDA approved"               PMA vs clearance
  supplement   "This supplement is FDA-approved"             the law (DSHEA)
  novel count  "The FDA approved 50 novel drugs in 2024"     CDER's yearly list

Each check names the product, reads the record, and says what it shows. When
the record cannot settle the claim (no match for the name, a year the data
does not carry), it returns None and the claim moves on, never a guess.
"""
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from claims.verdict import CONTRADICTED, MEDIUM, MISLEADING, SUPPORTED, Verdict
from config import logger
from evidence.excerpt import row as ex_row
from evidence.sources import SourceUnavailable, openfda
from evidence.units import UNKNOWN
from services.regulatory import NEGATION, fact, figure_check, verdict

_GATE = re.compile(r"\bFDA\b|\bFood and Drug Administration\b|\bblack[- ]box\b|\bboxed warning\b|"
                   r"\bapproved (?:for|to treat)\b|"
                   r"\brecall(?:ed|s|ing)?\b|\bshortages?\b|\bshort supply\b", re.I)
_YEAR = re.compile(r"\b(19[3-9]\d|20[0-4]\d)\b")

# Words around a product name that are not the name.
_STOP = set("""the a an its their his her this that these those new popular common widely used
drug drugs medication medications medicine medicines pill pills shot shots injection injections
vaccine vaccines device devices treatment treatments therapy therapies product products brand
weight-loss weight loss diabetes blockbuster company company's maker fda fda's approved approval
generic version versions monitor system app feature""".split())


@dataclass
class Read:
    kind: str                 # approval | indication | boxed | recall | shortage | supplement | novel
    product: str = ""
    indication: str = ""
    year: Optional[int] = None
    negated: bool = False
    reason: str = ""
    figure: str = ""
    present: bool = True      # "is approved" (now) vs "was approved"
    first: bool = False       # "first approved"
    text: str = ""


def _fmt(d: str) -> str:
    """"20210604" or "06/04/2021" -> "June 4, 2021"."""
    try:
        dt = (datetime.strptime(d, "%Y%m%d") if re.fullmatch(r"\d{8}", d)
              else datetime.strptime(d, "%m/%d/%Y"))
        return f"{dt:%B} {dt.day}, {dt.year}"
    except (TypeError, ValueError):
        return d or ""


def _year_of(d: str) -> Optional[int]:
    m = re.search(r"(19|20)\d\d", d or "")
    return int(m.group(0)) if m else None


# ------------------------------------------------------------------ reading

_P = r"(?P<p>[A-Za-z0-9][\w'’&.\- ]{1,60}?)"
_END = r"(?=\s+(?:in|on|back in|for|to|as|last|this|after|before|during|since|by|over|due|because|from)\b|[,.;:!?)]|$)"
_SUPPLEMENT = re.compile(r"\b(dietary supplements?|supplements?|vitamins?|probiotics?|gumm(?:y|ies)|"
                         r"multivitamins?|herbal)\b", re.I)
_APPROVED_WORD = re.compile(r"\bFDA[- ](?:approved|certified|registered)\b|\bapproved by (?:the )?FDA\b|"
                            r"\bFDA approv(?:es|al)\b", re.I)
_NOVEL = re.compile(r"\b(?:approved\s+)?(?P<n>\d{1,3})\s+(?:new|novel)\s+(?:drugs|medicines|therapies|molecular entities)\b", re.I)
_BOXED = [
    re.compile(_P + r"\s+(?:has|have|had|carries|carry|carried|comes with|come with|got|received|now has|includes|bears)"
                    r"\s+(?:a |an |the )?(?:FDA(?:'s)? )?(?:black[- ]box|boxed)\s+warning"
                    r"(?:\s+(?:for|about|on|over|warning of|of)\s+(?P<t>[^.;]+))?", re.I),
    re.compile(_P + r"\s+(?:does not|doesn't|did not|didn't|has no|have no|carries no|lacks?)\s+(?:have\s+|carry\s+)?"
                    r"(?:a |an |any )?(?:black[- ]box|boxed)\s+warning", re.I),
    re.compile(r"\bFDA\s+(?:added|put|placed|issued|required)\s+(?:a |an )?(?:black[- ]box|boxed)\s+warning\s+"
               r"(?:on|to|for)\s+" + _P + _END, re.I),
]
_RECALL = [
    re.compile(r"\b(?:FDA|company|firm|maker)\s+(?:has\s+)?recalled\s+" + _P + _END, re.I),
    re.compile(_P + r"\s+(?:was|were|has been|have been|is being|are being|got|had been)\s+(?:voluntarily\s+)?recalled\b", re.I),
    re.compile(r"\brecalls?\s+of\s+" + _P + _END, re.I),
    re.compile(_P + r"\s+recalls?\b", re.I),
]
_REASON = re.compile(r"\b(?:over|for|due to|because of|after)\s+(?:a\s+|possible\s+|potential\s+|fears of\s+)*"
                     r"(?P<r>[^.,;]+)", re.I)
_SHORTAGE = [
    re.compile(r"\bshortages?\s+of\s+" + _P + _END, re.I),
    re.compile(_P + r"\s+(?:is|are|remains?|was|were)\s+(?:still\s+|now\s+|not\s+|no longer\s+)?(?:in short supply|in shortage|"
                    r"on (?:the )?(?:FDA'?s? )?(?:drug )?shortage list)", re.I),
    re.compile(_P + r"\s+shortages?\b", re.I),
]
_SHORTAGE_OVER = re.compile(r"\bshortage\s+(?:is|was|has)\s+(?:now\s+)?(?:over|ended|resolved|been resolved)|"
                            r"\bno longer\b|\bended\b|\bresolved\b|\bnot in short\b|\bnot on\b", re.I)
_INDICATION = [
    re.compile(_P + r"\s+(?:is|was|has been|isn't|is not|wasn't|was not|was never|is now|got|received|gained|has)\s+"
                    r"(?:also\s+|now\s+|officially\s+|fully\s+|never\s+)?(?:been\s+)?(?:FDA[- ])?approv(?:ed|al)\s+"
                    r"(?:by the FDA\s+)?(?:only\s+)?(?:for|to treat|to help(?: with)?|as a treatment for)\s+(?P<i>(?!use\b)[^.;,]+)", re.I),
    re.compile(r"\bFDA\s+(?:has\s+)?(?:approved|cleared)\s+" + _P + r"\s+(?:for|to treat|as a treatment for)\s+(?P<i>[^.;,]+)", re.I),
]
_APPROVAL = [
    re.compile(r"\bFDA\s+(?:has\s+)?(?:first\s+|officially\s+|finally\s+|just\s+|quietly\s+|never\s+)?"
               r"(?:approved|cleared|authorized|greenlit|green-lit|okayed|OK'?d)\s+" + _P + _END, re.I),
    re.compile(_P + r"\s+(?:was|is|has been|got|received|won|gained|became|wasn'?t|isn'?t|was never|is not|was not|"
                    r"has never been|has not been|hasn'?t been|never got|never received|never was|is still not|remains)\s+"
                    r"(?:first\s+|officially\s+|fully\s+|initially\s+|originally\s+|ever\s+)?"
                    r"(?:FDA[- ]approved|approved|approval|FDA approval|cleared|FDA[- ]cleared|authorized)\b", re.I),
    re.compile(r"\bFDA[- ](?:approved|cleared)\s+" + _P + _END, re.I),
]


def _clean(phrase: str) -> List[str]:
    """Candidate product names in a phrase, most specific first:
    "Novo Nordisk's weight-loss drug Wegovy" -> ["Wegovy", ...]."""
    words = [w.strip("'’\".,") for w in phrase.split()]
    words = [w for w in words if w]
    while words and words[0].lower() in _STOP:
        words.pop(0)
    if not words:
        return []
    out: List[str] = []
    caps: List[str] = []
    for w in reversed(words):
        if (w[:1].isupper() or w[:1].isdigit() or w.isupper()) and w.lower() not in _STOP:
            caps.insert(0, w)
        elif caps:
            break
    if caps and len(caps) < len(words):
        out.append(" ".join(caps[-3:]))
    tail = [w for w in words if w.lower() not in _STOP]
    if tail:
        out.append(" ".join(tail[-3:]))
        if len(tail) > 1:
            out.append(tail[-1])
    if len(words) <= 4:
        out.append(" ".join(words))
    seen, uniq = set(), []
    for c in out:
        c = re.sub(r"'s$|’s$", "", c).strip()
        if c and c.lower() not in seen and c.lower() not in _STOP and len(c) >= 3:
            seen.add(c.lower())
            uniq.append(c)
    return uniq[:3]


def read(text: str) -> Optional[Read]:
    """What an FDA claim asserts, or None if it is not one this reads."""
    r = _read(text)
    if r is not None:
        r.text = text
    return r


def _read(text: str) -> Optional[Read]:
    if not _GATE.search(text):
        return None
    years = [int(y) for y in _YEAR.findall(text)]
    year = years[0] if years else None
    neg = bool(NEGATION.search(text))
    present = not re.search(r"\b(was|were|got|received|won|gained|became|had|first)\b", text, re.I)

    if _SUPPLEMENT.search(text) and _APPROVED_WORD.search(text):
        return Read("supplement", negated=neg)
    m = _NOVEL.search(text)
    if m and re.search(r"\bFDA\b|\bCDER\b", text) and year:
        return Read("novel", year=year, figure=m.group("n"))
    for i, rx in enumerate(_BOXED):
        m = rx.search(text)
        if m:
            topic = (m.groupdict().get("t") or "").strip()
            return Read("boxed", product=m.group("p"), negated=(i == 1) or neg, indication=topic,
                        present=present)
    if re.search(r"\brecall", text, re.I):
        for rx in _RECALL:
            m = rx.search(text)
            if m:
                r = _REASON.search(text[m.end():]) or _REASON.search(text)
                return Read("recall", product=m.group("p"), year=year, negated=neg,
                            reason=(r.group("r").strip() if r else ""))
    if re.search(r"\bshortage|\bshort supply", text, re.I):
        for rx in _SHORTAGE:
            m = rx.search(text)
            if m:
                return Read("shortage", product=m.group("p"), year=year,
                            negated=bool(_SHORTAGE_OVER.search(text)), present=year is None)
    for rx in _INDICATION:
        m = rx.search(text)
        if m:
            return Read("indication", product=m.group("p"), indication=m.group("i").strip(),
                        year=year, negated=neg, present=present)
    for rx in _APPROVAL:
        m = rx.search(text)
        if m and re.search(r"\bFDA\b|Food and Drug", text):
            return Read("approval", product=m.group("p"), year=year, negated=neg, present=present,
                        first=bool(re.search(r"\bfirst\b", text, re.I)))
    return None


# ------------------------------------------------------------------ checks

async def check(text: str, parsed) -> Optional[Verdict]:
    r = read(text)
    if r is None:
        return None
    return await run(r, text, parsed)


async def run(r: Read, text: str, parsed) -> Optional[Verdict]:
    try:
        if r.kind == "supplement":
            return _supplement(r)
        if r.kind == "novel":
            return _novel(r, text, parsed)
        names = _clean(r.product) if r.product else []
        if r.kind == "recall":
            # "distributed by Coast Citrus Distributors": the firm is the most
            # exact name a recall claim gives, so it is tried first.
            products = _clean(_ORIGIN.sub("", r.product)) if r.product else []
            for firm in _firms(text):
                v = await _recall(r, firm, products=products)
                if v is not None:
                    return v
            names = products
        # "Mexico" in "Jalapeños from Sinaloa, Mexico were recalled" is where
        # the food came from; a place alone would match any firm named for it.
        names = [n for n in names if not _is_place(n)]
        for name in names:
            fn = {"approval": _approval, "indication": _indication, "boxed": _boxed,
                  "recall": _recall, "shortage": _shortage}[r.kind]
            v = await fn(r, name)
            if v is not None:
                return v
    except SourceUnavailable as e:
        logger.warning("FDA check skipped for %r: %s", text[:80], e)
    except Exception:  # noqa: BLE001 — never fail a request over this check
        logger.exception("FDA check failed for %r", text[:120])
    return None


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _ingredient_match(app: dict, name: str) -> bool:
    n = name.lower()
    for p in app.get("products") or []:
        for ai in p.get("active_ingredients") or []:
            a = (ai.get("name") or "").lower()
            if a == n or a.startswith(n + " "):
                return True
    return False


_STRENGTH = re.compile(r"^(?:[\d.,/ ]+(?:mg|mcg|ml|%)?|xr|er|sr|cr|xl|la|odt|pen|kit)(?:\b|$)", re.I)


def _brand_match(app: dict, name: str) -> bool:
    """"ADDERALL 5" and "ADDERALL XR 10" are Adderall."""
    n = " ".join(name.lower().split())
    for p in app.get("products") or []:
        b = " ".join((p.get("brand_name") or "").lower().split())
        if b == n or (b.startswith(n + " ") and _STRENGTH.match(b[len(n) + 1:])):
            return True
    return False


def _originals(apps: List[dict]) -> List[Tuple[str, dict]]:
    out = []
    for a in apps:
        for s in a.get("submissions") or []:
            if s.get("submission_type") == "ORIG" and s.get("submission_status") == "AP" and s.get("submission_status_date"):
                out.append((s["submission_status_date"], a))
    return sorted(out, key=lambda x: x[0])


def _efficacy_years(apps: List[dict]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for a in apps:
        for s in a.get("submissions") or []:
            if (s.get("submission_type") == "SUPPL" and s.get("submission_status") == "AP"
                    and (s.get("submission_class_code") or "").upper() == "EFFICACY"):
                y = _year_of(s.get("submission_status_date") or "")
                if y and y not in out:
                    out[y] = s["submission_status_date"]
    return out


def _drugsfda_url(app_no: str) -> str:
    digits = re.sub(r"\D", "", app_no)
    return f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={digits}"


def _label_url(lab: dict) -> str:
    sid = lab.get("set_id") or ""
    return f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={sid}" if sid else "https://open.fda.gov/apis/drug/label/"


async def _approval(r: Read, name: str) -> Optional[Verdict]:
    apps = await openfda.applications(name)
    ingr = [a for a in apps if _ingredient_match(a, name)
            and (a.get("application_number") or "").startswith(("NDA", "BLA"))]
    # A generic sold under its own name ("SEMAGLUTIDE") is the ingredient, not a brand.
    brand = [] if ingr else [a for a in apps if _brand_match(a, name)]
    relevant, as_brand = (brand, True) if brand else (ingr, False)
    if not relevant:
        return await _approval_other(r, name)
    origs = _originals(relevant)
    if not origs:
        # Approved before Drugs@FDA kept dates (Adderall's NDA dates to 1960).
        if r.year:
            return None
        a = relevant[0]
        app_no = a.get("application_number", "")
        dp = fact("FDA", f"{name}: FDA approval", f"Approved ({app_no})", _drugsfda_url(app_no),
                  basis="Drugs@FDA", series_id=app_no,
                  rows=[ex_row("Application", app_no, cited=True), ex_row("Sponsor", a.get("sponsor_name") or "")])
        return verdict(CONTRADICTED if r.negated else SUPPORTED,
                       f"{name} is FDA-approved ({app_no}); Drugs@FDA has no date for the original approval.", [dp])
    first_d, first_app = origs[0]
    app_no = first_app.get("application_number", "")
    # Applications from before 1985 often began under another product name
    # (Adderall's NDA is Obetrol's, from 1960): the date is the file's, not the brand's.
    old_file = first_d < "19850101"
    if old_file and r.year:
        return None
    shown_name = name if not name.islower() else name.lower()
    when = _fmt(first_d)
    fy = _year_of(first_d)
    marketed = any((p.get("marketing_status") or "").lower() != "discontinued"
                   for a in relevant for p in a.get("products") or [])
    rows = [ex_row("Application", app_no), ex_row("Sponsor", first_app.get("sponsor_name") or ""),
            ex_row("Original approval", when, cited=True)]
    caveats = []
    if as_brand and r.first is False:
        ing = sorted({(ai.get("name") or "").title() for a in brand for p in a.get("products") or []
                      for ai in p.get("active_ingredients") or [] if ai.get("name")})
        if len(ing) == 1:
            other = [a for a in await openfda.applications(ing[0]) if _ingredient_match(a, ing[0])
                     and (a.get("application_number") or "").startswith(("NDA", "BLA"))]
            o = _originals(other)
            if o and o[0][0] < first_d:
                caveats.append(f"Its active ingredient, {ing[0].lower()}, was first approved "
                               f"{_fmt(o[0][0])} under another name.")
    if not marketed:
        caveats.append("Drugs@FDA lists every product under this approval as discontinued.")
    dp = fact("FDA", f"{shown_name}: FDA approval", f"Approved {when}", _drugsfda_url(app_no),
              year=fy, record_date=first_d, basis="Drugs@FDA original approval", rows=rows,
              series_id=app_no)
    if old_file:
        caveats.append(f"Drugs@FDA dates {app_no} to {when}; applications that old often began under "
                       f"another product name.")
        head = f"{shown_name} is FDA-approved ({app_no}, on file since {when})."
        return verdict(CONTRADICTED if r.negated else SUPPORTED, head, [dp], caveats=caveats)
    if r.negated:
        return verdict(CONTRADICTED, f"FDA approved {shown_name} on {when} ({app_no}).", [dp], caveats=caveats)
    if r.year:
        if r.year == fy:
            return verdict(SUPPORTED, f"FDA approved {shown_name} on {when} ({app_no}).", [dp],
                           caveats=caveats, claimed=str(r.year))
        eff = _efficacy_years(relevant)
        if r.year in eff:
            note = (f"FDA approved a new use for {shown_name} on {_fmt(eff[r.year])}; the drug itself was "
                    f"first approved {when}.")
            outcome = SUPPORTED if re.search(r"\bfor\b|\bto (?:treat|reduce|lower|prevent)\b", r.text, re.I) else CONTRADICTED
            return verdict(outcome, note, [dp], caveats=caveats, claimed=str(r.year), confidence=MEDIUM)
        return verdict(CONTRADICTED, f"FDA first approved {shown_name} on {when} ({app_no}), not in {r.year}.",
                       [dp], caveats=caveats, claimed=str(r.year))
    if not marketed and r.present:
        return verdict(MISLEADING, f"FDA approved {shown_name} on {when}, but no product under that "
                                   f"approval is marketed now.", [dp], caveats=caveats)
    return verdict(SUPPORTED, f"FDA approved {shown_name} on {when} ({app_no}).", [dp], caveats=caveats)


async def _approval_other(r: Read, name: str) -> Optional[Verdict]:
    """Biologics outside Drugs@FDA (the label carries the license), then devices."""
    labs = [l for l in await openfda.labels(name)
            if any(_same(b, name) for b in (l.get("openfda") or {}).get("brand_name") or [])
            or any(_same(g, name) for g in (l.get("openfda") or {}).get("generic_name") or [])]
    licensed = sorted({n for l in labs for n in (l.get("openfda") or {}).get("application_number") or []
                       if n.startswith(("BLA", "NDA"))})
    if licensed:
        if r.year:
            return None  # the label does not carry the approval date
        dp = fact("FDA", f"{name}: FDA license", f"Approved ({licensed[0]})", _label_url(labs[0]),
                  basis="FDA-approved prescribing label", series_id=licensed[0],
                  rows=[ex_row("Application on the label", ", ".join(licensed[:3]), cited=True)])
        if r.negated:
            return verdict(CONTRADICTED, f"{name} is FDA-approved: its label carries {licensed[0]}.", [dp])
        return verdict(SUPPORTED, f"{name} is FDA-approved: its label carries {licensed[0]}.", [dp])
    return await _device(r, name)


async def _device(r: Read, name: str) -> Optional[Verdict]:
    pmas = [p for p in await openfda.pma(name) if name.lower() in (p.get("trade_name") or "").lower()]
    if pmas:
        p = sorted(pmas, key=lambda x: x.get("decision_date") or "")[0]
        when = p.get("decision_date") or ""
        dp = fact("FDA", f"{name}: premarket approval", f"Approved {_fmt(when.replace('-', ''))}",
                  f"https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpma/pma.cfm?id={p.get('pma_number')}",
                  year=_year_of(when), record_date=when, basis="FDA premarket approval (PMA)",
                  series_id=p.get("pma_number") or "",
                  rows=[ex_row("PMA", p.get("pma_number") or ""), ex_row("Trade name", p.get("trade_name") or ""),
                        ex_row("Decision", _fmt(when.replace("-", "")), cited=True)])
        if r.negated:
            return verdict(CONTRADICTED, f"FDA approved {name} (PMA {p.get('pma_number')}).", [dp])
        if r.year and r.year != _year_of(when):
            return verdict(CONTRADICTED, f"FDA first approved {name} in {_year_of(when)}, not {r.year}.", [dp],
                           claimed=str(r.year))
        return verdict(SUPPORTED, f"FDA approved {name} (PMA {p.get('pma_number')}).", [dp])
    lists = [d for d in await openfda.device_listings(name)
             if any(name.lower() in (n or "").lower() for n in d.get("proprietary_name") or [])]
    classes = {(p.get("openfda") or {}).get("device_class") for d in lists for p in d.get("products") or []}
    classes.discard(None)
    if not lists or not classes or not classes <= {"1", "2"}:
        return None
    cls = "II" if "2" in classes else "I"
    dp = fact("FDA", f"{name}: device class", f"Class {cls} device (cleared, not approved)",
              "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfRL/rl.cfm",
              basis="FDA device registration and listing",
              rows=[ex_row("Listed name", (lists[0].get("proprietary_name") or [name])[0]),
                    ex_row("Device class", cls, cited=True)])
    said_cleared = re.search(r"\bcleared\b|\bauthori[sz]ed\b|\bclearance\b|\bDe Novo\b", r.text, re.I)
    if r.negated or said_cleared:
        return None
    return verdict(MISLEADING,
                   f"{name} is a class {cls} device. FDA clears such devices (510(k)) or grants them "
                   f"(De Novo); it does not approve them.", [dp],
                   caveats=["FDA's rule (21 CFR 807.97) says calling a 510(k) clearance an approval "
                            "is misleading."])


_INDICATIONS = {
    r"weight|obes|overweight|fat loss|slim": ["weight management", "weight reduction", "obesity",
                                             "excess body weight", "reduce body weight", "body weight"],
    r"type 2 diabetes|diabetes|blood sugar|glycemic": ["diabetes", "glycemic control"],
    r"alzheimer": ["alzheimer"],
    r"sleep apnea": ["sleep apnea"],
    r"heart attack|stroke|cardiovascular|heart disease": ["cardiovascular", "myocardial infarction", "stroke"],
    r"depress": ["depressive", "depression"],
    r"adhd|attention deficit": ["attention deficit", "adhd"],
    r"smok|nicotine": ["smoking cessation", "nicotine"],
    r"covid|coronavirus": ["covid", "sars-cov-2"],
    r"hair loss|baldness|alopecia": ["alopecia", "hair loss"],
    r"kidney": ["kidney"],
    r"anxiety": ["anxiety"],
    r"insomnia|sleep\b": ["insomnia", "sleep"],
    r"migraine": ["migraine"],
    r"opioid (?:use|addiction)|opioid overdose": ["opioid use disorder", "opioid overdose", "opioid"],
    r"alcohol": ["alcohol"],
    r"cancer|tumou?r": ["cancer", "carcinoma", "lymphoma", "leukemia", "tumor", "melanoma", "myeloma", "neoplasm"],
}


def _indication_text(labs: List[dict]) -> str:
    return " ".join(" ".join(l.get("indications_and_usage") or []) for l in labs)


def _short_indications(text: str) -> str:
    t = re.sub(r"^\s*\d?\s*INDICATIONS (?:AND|&) USAGE\s*", "", " ".join(text.split()), flags=re.I)
    return (t[:220].rsplit(" ", 1)[0] + "…") if len(t) > 220 else t


async def _labels_for(name: str) -> List[dict]:
    """Labels for exactly this name; a label for this product alone first."""
    labs = [l for l in await openfda.labels(name)
            if any(_same(b, name) for b in (l.get("openfda") or {}).get("brand_name") or [])
            or any(_same(g, name) for g in (l.get("openfda") or {}).get("generic_name") or [])]
    def alone(l):
        names = (l.get("openfda") or {}).get("brand_name") or []
        return 0 if names and all(_same(b, name) for b in names) else 1
    return sorted(labs, key=alone)


async def _indication(r: Read, name: str) -> Optional[Verdict]:
    labs = await _labels_for(name)
    if not labs:
        return None
    body = _indication_text(labs).lower()
    if not body:
        return None
    want = r.indication.lower()
    terms = next((t for k, t in _INDICATIONS.items() if re.search(k, want)), None)
    confidence = "High"
    if terms:
        found = any(t in body for t in terms)
    else:
        words = [w for w in re.findall(r"[a-z]{4,}", want) if w not in ("with", "adults", "patients", "people", "treat", "treatment")]
        if not words or not all(w in body for w in words):
            return None
        found, confidence = True, MEDIUM
    gen = sorted({g for l in labs for g in (l.get("openfda") or {}).get("generic_name") or []}, key=len)
    url = _label_url(labs[0])
    shown = _short_indications(_indication_text(labs[:1]))
    if found and terms:
        flat = re.sub(r"^\s*\d?\s*INDICATIONS (?:AND|&) USAGE\s*", "", " ".join(_indication_text(labs).split()), flags=re.I)
        at = next(i for i in (flat.lower().find(t) for t in terms) if i >= 0)
        start = max(0, flat.rfind("•", 0, at), flat.rfind(". ", 0, at) + 1)
        shown = flat[start:at + 160].lstrip("• .").rsplit(" ", 1)[0] + "…"
    dp = fact("FDA", f"{name}: approved uses (label)", shown, url,
              basis="FDA-approved prescribing label, Indications and Usage",
              rows=[ex_row("Indications and usage", shown, cited=True)])
    caveats = []
    if not found and terms and gen:
        extra = "(" + " OR ".join(f"indications_and_usage:{openfda._quote(t)}" for t in terms[:4]) + ")"
        alt = await openfda.labels(gen[0].split(",")[0], extra=extra)
        others = sorted({b.title() for l in alt for b in (l.get("openfda") or {}).get("brand_name") or []
                         if not _same(b, name) and any(t in _indication_text([l]).lower() for t in terms)})
        if others:
            caveats.append(f"The same active ingredient is approved for that as {', '.join(others[:2])}.")
    says_yes = not r.negated
    if found == says_yes:
        head = (f"{name}'s FDA-approved label lists {r.indication}." if found
                else f"{name}'s FDA-approved label does not list {r.indication}.")
        return verdict(SUPPORTED, head, [dp], caveats=caveats, confidence=confidence)
    head = (f"{name}'s FDA-approved label does not list {r.indication}; it is approved for: "
            f"{_short_indications(_indication_text(labs[:1]))}" if not found
            else f"{name}'s FDA-approved label does list {r.indication}.")
    return verdict(CONTRADICTED, head, [dp], caveats=caveats, confidence=confidence)


async def _boxed(r: Read, name: str) -> Optional[Verdict]:
    labs = await _labels_for(name)
    if not labs:
        return None
    warn = next((" ".join(l.get("boxed_warning") or []) for l in labs if l.get("boxed_warning")), "")
    heading = ""
    if warn:
        w = re.sub(r"^\s*(?:BOXED\s+)?WARNINGS?:?\s*", "", " ".join(warn.split()), flags=re.I)
        m = re.match(r"([A-Z0-9][A-Z0-9 ,;:'()/&-]{5,}?)\s+(?=[A-Z][a-z])", w)
        heading = (m.group(1).strip(" ,;:").capitalize() if m else w.split(". ")[0])[:140]
    url = _label_url(labs[0])
    shown = f"Boxed warning: {heading}" if warn else "No boxed warning"
    dp = fact("FDA", f"{name}: boxed warning (label)", shown, url, basis="FDA-approved prescribing label",
              rows=[ex_row("Boxed warning", heading or "none on the current label", cited=True)])
    if not warn and not r.present:
        return None  # a warning can be removed; the current label cannot say it never had one
    if r.negated:
        return verdict(CONTRADICTED if warn else SUPPORTED,
                       f"{name}'s current label {'has a boxed warning: ' + heading if warn else 'has no boxed warning'}.", [dp])
    if not warn:
        return verdict(CONTRADICTED, f"{name}'s current FDA-approved label has no boxed warning.", [dp])
    topic = [w for w in re.findall(r"[a-z]{5,}", (r.indication or "").lower())
             if w not in ("about", "risks", "warning", "serious", "increased")]
    if topic and not any(t[:6] in warn.lower() for t in topic):
        return verdict(CONTRADICTED, f"{name}'s boxed warning is about {heading.lower()}, not {r.indication}.", [dp],
                       confidence=MEDIUM)
    return verdict(SUPPORTED, f"{name}'s FDA-approved label has a boxed warning: {heading}.", [dp])


_REASONS = {
    r"listeria": "listeria", r"salmonella": "salmonella", r"e\.? ?coli": "coli", r"\blead\b": "lead",
    r"allerg|undeclared": "undeclared", r"metal": "metal", r"benzene": "benzene", r"mold": "mold",
    r"glass": "glass", r"plastic": "plastic", r"botul": "botul", r"cronobacter": "cronobacter",
    r"hepatitis": "hepatitis", r"nitrosamine|ndma": "nitros", r"steril": "steril",
}


def _recall_row(x: dict) -> str:
    return " · ".join(s for s in (_fmt(x.get("recall_initiation_date") or ""), x.get("classification") or "",
                                  (x.get("reason_for_recall") or "").strip()[:90].rstrip(".")) if s)


# "Fresh Jalapeños from Sinaloa, Mexico": where a food came from is not its name.
_ORIGIN = re.compile(r"\s+(?:imported from|grown in|made in|produced in|from)\s+.*$", re.I)
_FIRM = re.compile(r"\b(?:distributed|recalled|sold|packed|packaged|manufactured|made|produced|imported|"
                   r"supplied|issued)\s+by\s+(?P<f>(?:[A-Z][\w&'’.-]*,?\s?){1,5})")


def _firms(text: str) -> List[str]:
    out = []
    for m in _FIRM.finditer(text or ""):
        words = [w.strip(",") for w in m.group("f").split()]
        while words and words[-1].lower() in _STOP | {"inc", "llc", "co"}:
            words.pop()
        name = " ".join(words)
        if len(name) >= 4 and name.lower() not in ("fda", "the fda"):
            out.append(name)
    return out[:2]


def _is_place(name: str) -> bool:
    from claims import geography
    n = _fold(name).strip(" .")
    return (n in geography._NON_US or n in geography._STATE_FIPS
            or n in ("united states", "america", "usa", "u.s", "us"))


def _fold(s: str) -> str:
    """Lower case, accents off: "Jalapeños" -> "jalapenos", as FDA spells it."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch)).lower()


def _tokens(s: str) -> List[str]:
    """Words folded and made singular, so "Jalapeños" finds "Jalapeno Pepper"."""
    return [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")) else w
            for w in re.findall(r"[a-z0-9]+", _fold(s))]


def _names(text: str, name: str) -> bool:
    want, have = _tokens(name), _tokens(text)
    if not want:
        return False
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


# Meat, poultry and egg products are recalled by USDA's FSIS, not FDA.
_USDA = re.compile(r"\b(meats?|deli|ham|bacon|sausages?|hot dogs?|beef|pork|chicken|turkey|poultry|"
                   r"jerky|salami|pepperoni|bologna|liverwurst|egg products?)\b", re.I)


async def _recall(r: Read, name: str, products: List[str] = ()) -> Optional[Verdict]:
    """Only what FDA's records show. A recall missing from them, in a year or
    for a reason, may be USDA's (meat, poultry) or older than 2012, so a miss
    is never read as "no recall"."""
    if _USDA.search(r.text) and not r.negated:
        return None
    def mine(rows):
        rows = [x for x in rows if _names(x.get("product_description") or "", name)
                or _names(x.get("recalling_firm") or "", name)]
        if products:  # a firm named with its product: the record must be that product's
            rows = [x for x in rows if any(_names(x.get("product_description") or "", p) for p in products)]
        return rows
    # FDA writes "Jalapeno", a claim "Jalapeños": the singular is searched
    # only when the name as written finds nothing.
    hits = []
    for q in dict.fromkeys((_fold(name), " ".join(_tokens(name)))):
        hits = mine(await openfda.recalls(q, r.year))
        if hits:
            break
    want = next((v for k, v in _REASONS.items() if re.search(k, r.reason, re.I)), None)
    if want and not r.negated:
        hits = [x for x in hits if want in (x.get("reason_for_recall") or "").lower()]
    if not hits:
        return None
    rows_src = sorted(hits, key=lambda x: x.get("recall_initiation_date") or "", reverse=True)
    latest = rows_src[0]
    dp = fact("FDA", f"{name}: FDA recall record", _recall_row(latest),
              "https://www.accessdata.fda.gov/scripts/ires/index.cfm",
              year=_year_of(latest.get("recall_initiation_date") or ""),
              record_date=latest.get("recall_initiation_date") or "", basis="FDA enforcement report",
              series_id=str(latest.get("recall_number") or latest.get("event_id") or ""),
              rows=[ex_row(x.get("recalling_firm") or name, _recall_row(x), cited=(i == 0))
                    for i, x in enumerate(rows_src[:3])],
              note="FDA enforcement reports, 2012 on.")
    if r.negated:
        return verdict(CONTRADICTED, f"FDA's recall records show {name} was recalled: {_recall_row(latest)}.", [dp])
    return verdict(SUPPORTED, f"FDA's recall records show it: {_recall_row(latest)}.", [dp])


async def _shortage(r: Read, name: str) -> Optional[Verdict]:
    if not r.present:
        return None  # the list shows today's status, not a past year's
    rows = await openfda.shortages(name)
    generic = name
    if not rows:
        labs = await _labels_for(name)
        gens = [g for l in labs for g in (l.get("openfda") or {}).get("generic_name") or []]
        if not gens:
            return None
        generic = re.split(r",| and ", gens[0])[0].strip().lower()
        rows = await openfda.shortages(generic)
    current = [x for x in rows if (x.get("status") or "").lower() == "current"]
    url = "https://dps.fda.gov/drugshortages"
    if rows:
        x = (current or rows)[0]
        status = "Current shortage" if current else (x.get("status") or "Not current")
        dp = fact("FDA", f"{name}: FDA drug shortage list", status, url,
                  basis="FDA drug shortage list", record_date=x.get("update_date") or "",
                  rows=[ex_row(x.get("generic_name") or generic, status, cited=True),
                        ex_row("First posted", x.get("initial_posting_date") or ""),
                        ex_row("Updated", x.get("update_date") or "")])
    else:
        dp = fact("FDA", f"{name}: FDA drug shortage list", "Not on the shortage list", url,
                  basis="FDA drug shortage list", rows=[ex_row(generic, "not listed", cited=True)])
    short = bool(current)
    if short != r.negated:
        head = (f"{name} is on FDA's drug shortage list as a current shortage." if short
                else f"FDA's drug shortage list shows no current shortage of {name}.")
        return verdict(SUPPORTED, head, [dp], confidence="High" if rows else MEDIUM)
    head = (f"{name} is on FDA's drug shortage list as a current shortage." if short
            else f"FDA's drug shortage list shows no current shortage of {name}.")
    return verdict(CONTRADICTED, head, [dp], confidence="High" if rows else MEDIUM)


_DSHEA_URL = ("https://www.fda.gov/food/information-consumers-using-dietary-supplements/"
              "questions-and-answers-dietary-supplements")


def _supplement(r: Read) -> Verdict:
    dp = fact("FDA", "FDA and dietary supplements", "FDA does not approve dietary supplements", _DSHEA_URL,
              basis="Dietary Supplement Health and Education Act of 1994 (DSHEA)",
              rows=[ex_row("FDA approval before sale", "none: FDA has no authority to approve supplements", cited=True)])
    if r.negated:
        return verdict(SUPPORTED, "Correct: by law, FDA does not approve dietary supplements or their labels.", [dp])
    return verdict(CONTRADICTED, "FDA does not approve dietary supplements. By law (DSHEA) they are sold "
                                 "without FDA approval; a supplement called \"FDA-approved\" is not.", [dp])


# CDER novel drug approvals, from FDA's "Novel Drug Approvals for <year>" pages.
_NOVEL_COUNTS = {2021: 50, 2022: 37, 2023: 55, 2024: 50, 2025: 46}


def _novel(r: Read, text: str, parsed) -> Optional[Verdict]:
    n = _NOVEL_COUNTS.get(r.year or 0)
    if n is None:
        return None
    url = f"https://www.fda.gov/drugs/novel-drug-approvals-fda/novel-drug-approvals-{r.year}"
    dp = fact("FDA", f"Novel drugs approved by CDER, {r.year}", str(n), url, year=r.year,
              magnitude=n,
              rows=[ex_row(f"Novel drug approvals, {r.year}", str(n), cited=True)])
    return figure_check(parsed, r.figure, dp, text, UNKNOWN)
