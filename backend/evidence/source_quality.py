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


def tier(url_or_host: str, fact_check_pages=()) -> str:
    """official | reliable | other. A page in `fact_check_pages` (published
    fact-checks from Google's Fact Check Tools index, which lists only
    recognised fact-checking publishers) counts as reliable."""
    host = _host(url_or_host)
    if is_official(host):
        return OFFICIAL
    if _in(host, _RELIABLE_DOMAINS) or host.endswith(".edu") or _in(host, ("ac.uk",)):
        return RELIABLE
    if any(same_page(url_or_host, p) for p in fact_check_pages):
        return RELIABLE
    return OTHER


def same_page(a: str, b: str) -> bool:
    """Two URLs for one page: scheme, "www.", a trailing slash, a fragment and
    tracking parameters aside."""
    def norm(u: str) -> str:
        p = urlparse((u or "").strip())
        host = _host(p.netloc or u)
        host = host[4:] if host.startswith("www.") else host
        return f"{host}{p.path.rstrip('/')}".lower()
    return bool(a and b) and norm(a) == norm(b)
