"""How far a web page can be trusted to settle a factual claim.

  official   the publisher of the figure: a government agency, a national
             statistics office, an intergovernmental statistical body
  reliable   professional fact-checkers, research institutes, peer-reviewed
             journals, universities, wire services and newspapers of record
  other      everything else: apps, blogs, aggregators, forums, social media

Only an official or reliable page may back a verdict. Domains are matched as
the registrable domain, so "pewresearch.org" covers "www.pewresearch.org" but
not "pewresearch.org.example.com".
"""
import re
from urllib.parse import urlparse

OFFICIAL = "official"
RELIABLE = "reliable"
OTHER = "other"

# National statistics offices and intergovernmental bodies without a "gov" label.
_OFFICIAL_DOMAINS = frozenset("""
insee.fr destatis.de istat.it ine.es ine.pt cbs.nl statbel.fgov.be scb.se ssb.no
dst.dk stat.fi cso.ie statistik.at bfs.admin.ch nisra.gov.uk stats.govt.nz
statcan.gc.ca abs.gov.au inegi.org.mx ibge.gov.br indec.gob.ar stat.go.jp
kostat.go.kr mospi.gov.in stats.gov.cn europa.eu ecb.europa.eu oecd.org imf.org
worldbank.org un.org who.int ilo.org bis.org wto.org federalreserve.gov
ons.gov.uk fred.stlouisfed.org stlouisfed.org newyorkfed.org
""".split())

# Fact-checkers (IFCN signatories and newsroom fact-check desks), research
# institutes, journals and newspapers of record.
_RELIABLE_DOMAINS = frozenset("""
politifact.com factcheck.org snopes.com fullfact.org leadstories.com
apnews.com reuters.com afp.com usatoday.com washingtonpost.com
nytimes.com wsj.com bloomberg.com ft.com economist.com npr.org pbs.org
bbc.com bbc.co.uk theguardian.com latimes.com axios.com cbsnews.com
nbcnews.com abcnews.go.com cnn.com foxnews.com cnbc.com politico.com thehill.com
propublica.org theatlantic.com newyorker.com time.com
pewresearch.org kff.org brookings.edu urban.org rand.org taxpolicycenter.org
crfb.org bipartisanpolicy.org nber.org taxfoundation.org cfr.org csis.org
usafacts.org ourworldindata.org gallup.com
nature.com science.org nejm.org thelancet.com jamanetwork.com bmj.com
cell.com pnas.org sciencedirect.com springer.com wiley.com
mayoclinic.org clevelandclinic.org hopkinsmedicine.org heart.org cancer.org
britannica.com ballotpedia.org
""".split())

# Kept out of search results entirely: they rarely publish a figure first-hand,
# and an answer resting on them cannot be checked.
BLOCKED_DOMAINS = (
    "wikipedia.org", "reddit.com", "quora.com", "facebook.com", "x.com",
    "twitter.com", "instagram.com", "tiktok.com", "youtube.com", "pinterest.com",
    "linkedin.com", "medium.com", "substack.com", "tumblr.com", "blogspot.com",
    "wordpress.com", "answers.com", "brainly.com", "chegg.com", "coursehero.com",
    "scribd.com", "fandom.com", "statista.com", "worldpopulationreview.com",
    "macrotrends.net", "nutritionix.com", "fatsecret.com", "myfitnesspal.com",
    "eatthismuch.com", "nutritionvalue.org", "fitbit.com",
)


def _host(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "//" in s:
        s = urlparse(s).netloc
    return s.split("@")[-1].split(":")[0].rstrip(".")


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_official(url_or_host: str) -> bool:
    host = _host(url_or_host)
    if not host:
        return False
    if host.endswith((".gov", ".mil", ".fed.us")) or _in(host, _OFFICIAL_DOMAINS):
        return True
    # National government domains such as gov.uk, gouv.fr, gc.ca, go.jp: the
    # label right before a two-letter country code ("abcnews.go.com" is not one).
    labels = host.split(".")
    return (len(labels) >= 2 and len(labels[-1]) == 2
            and labels[-2] in ("gov", "gouv", "gob", "govt", "gc", "go", "admin", "gv"))


def tier(url_or_host: str, fact_check_pages=(), claim: str = "") -> str:
    """official | reliable | other.

    Also reliable: a page in `fact_check_pages` (published fact-checks from
    Google's Fact Check Tools index, which lists only recognised fact-checking
    publishers), and a company's own site when the claim names that company
    ("A can of Coca-Cola has 140 calories" on coca-cola.com): the maker
    publishes its product's figures first-hand."""
    host = _host(url_or_host)
    if is_official(host):
        return OFFICIAL
    if _in(host, _RELIABLE_DOMAINS) or host.endswith(".edu") or _in(host, ("ac.uk",)):
        return RELIABLE
    if any(same_page(url_or_host, p) for p in fact_check_pages):
        return RELIABLE
    if claim and own_site(host, claim):
        return RELIABLE
    return OTHER


def _alnum(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def own_site(url_or_host: str, claim: str) -> bool:
    """True when the site is the claim's own subject: coca-cola.com for
    "Coca-Cola", mcdonalds.com for "McDonald's", toureiffel.paris for "the
    Eiffel Tower". A site does not qualify by naming itself as the source; the
    claim has to name it."""
    host = _host(url_or_host)
    labels = [l for l in host.split(".") if l]
    if len(labels) < 2:
        return False
    # The registrable name: "coca-cola" in www.coca-cola.com, "mcdonalds" in order.mcdonalds.co.uk.
    name = labels[-3] if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com") else labels[-2]
    name = _alnum(name)
    if len(name) < 4:
        return False
    # The claim's own words, joined one, two or three at a time, so "Coca-Cola"
    # and "McDonald's" match. The match must be capitalised (a name, not "an
    # apple"), and a brand that is an everyday word counts only mid-sentence.
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\u2019&-]*", claim or "")
    for i in range(len(words)):
        for n in (1, 2, 3):
            chunk = words[i:i + n]
            if len(chunk) < n or _alnum("".join(chunk)).removesuffix("s") not in (name, name.removesuffix("s")):
                continue
            if not chunk[0][:1].isupper():
                continue
            if name in _EVERYDAY and i == 0:
                continue
            return True
    # The subject's own site, its name in any order with at most one short
    # extra word: toureiffel.paris for "The Eiffel Tower". Not parisfoodblog.com.
    rest, used = name, 0
    for w in sorted({_alnum(w) for w in words if w[:1].isupper() and len(_alnum(w)) >= 4},
                    key=len, reverse=True):
        if w in rest and w not in _EVERYDAY:
            rest = rest.replace(w, "", 1)
            used += len(w)
    return used >= 5 and len(rest) <= 4


# Brands that are also ordinary words: "Apple" opening a sentence is a fruit
# as often as a company.
_EVERYDAY = frozenset("""
apple target subway shell gap delta visa dove tide amazon oracle jaguar puma
mars orange blackberry corona monster tesla mercury
""".split())


def same_page(a: str, b: str) -> bool:
    """Two URLs for one page: scheme, "www.", a trailing slash, a fragment and
    tracking parameters aside."""
    def norm(u: str) -> str:
        p = urlparse((u or "").strip())
        host = _host(p.netloc or u)
        host = host[4:] if host.startswith("www.") else host
        return f"{host}{p.path.rstrip('/')}".lower()
    return bool(a and b) and norm(a) == norm(b)
