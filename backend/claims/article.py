"""The readable text of a web page: its title and its paragraphs.

A page is mostly not the article: menus, share buttons, related links, cookie
notices, comment forms. Checking those for claims wastes the few checks a
request gets and shows the reader sentences they never saw as the story.

So only running text is kept: paragraphs, list items, headings and block
quotes, outside anything that is navigation, a form or page furniture. When
the page marks its story with <article> or <main> and most of the text is in
there, only that part is used. A page that builds itself with scripts and has
no text in its HTML often still carries the story in its structured data
(`articleBody`), which is the fallback.

The standard library's parser only: one page is a few hundred kilobytes, and a
tolerant tokenizer is all that picking out paragraphs needs.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import List, Optional

# Never read, whatever is inside.
_SKIP = frozenset("script style noscript template svg iframe nav header footer aside form button "
                  "select option figure figcaption menu dialog".split())
# One block of running text each.
# (Table cells are left out: a table is data, and its cells are not sentences.)
_BLOCKS = frozenset("p li h1 h2 h3 h4 blockquote pre".split())
_STORY = frozenset(("article", "main"))
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_SPACE = re.compile(r"\s+")
# Footnote marks an encyclopedia leaves in its text: [12], [a], [citation needed].
_FOOTNOTE = re.compile(r"\[(?:\d{1,3}|[a-z]|citation needed|note \d+)\]", re.I)
# A line that points at another story.
_TEASER = re.compile(r"(?:READ MORE|READ|WATCH LIVE|WATCH MORE|WATCH|RELATED|MORE|SEE ALSO|ALSO READ|LISTEN|"
                     r"Read more|Related|Watch|See also|Listen)\s*:")
# Class or id names that mark page furniture inside the story.
_FURNITURE_WORDS = (r"nav|menu|share|social|related|promo|advert|ad|ads|sidebar|comment|comments|"
                    r"footer|newsletter|subscribe|cookie|breadcrumb|byline|caption|tags|toolbar")
_FURNITURE = re.compile(rf"(?:^|[-_])(?:{_FURNITURE_WORDS})(?:$|[-_])", re.I)
# A name that says the element *has* furniture is the element around it, not
# the furniture: "page__body--with-sidebar" holds the whole story.
_HOLDS_FURNITURE = re.compile(rf"(?:^|[-_])(?:with|has|have|having|no|non|without|show|shows|hide|hides|is|not)[-_]+"
                              rf"(?:the[-_]+)?(?:{_FURNITURE_WORDS})(?:$|[-_])", re.I)


def _is_furniture(marks: str) -> bool:
    """Whether an element's class or id names it as page furniture."""
    return any(_FURNITURE.search(name) and not _HOLDS_FURNITURE.search(name) for name in marks.split())


# Where a page says when it was published, and when it was last changed.
_PUBLISHED_META = frozenset("""
article:published_time og:article:published_time datepublished date pubdate publishdate publish-date
publish_date dc.date dc.date.created dc.date.issued dcterms.date dcterms.created dcterms.issued
parsely-pub-date sailthru.date article.published
""".split())
_MODIFIED_META = frozenset("""
article:modified_time og:updated_time datemodified last-modified dc.date.modified dcterms.modified
""".split())
_MONTH_NUMBER = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
_MONTH_NUMBER.update({m[:3]: n for m, n in list(_MONTH_NUMBER.items())}, sept=9)
_ISO_DATE = re.compile(r"\b((?:19|20)\d\d)-(\d{2})-(\d{2})(?!\d)")
_MONTH_WORD = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
_US_DATE = re.compile(rf"\b({_MONTH_WORD})\.?\s+(\d{{1,2}}),?\s+((?:19|20)\d\d)\b", re.I)
_DAY_FIRST_DATE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_WORD})\.?,?\s+((?:19|20)\d\d)\b", re.I)
# The line a press release opens with: "For release 8:30 a.m. (ET) Friday, November 6, 2026".
_RELEASE_LINE = re.compile(r"\b(?:embargoed until|for (?:immediate )?release|release date|date of release)\b", re.I)
# A page changed this long after it came out is kept current, not a dated story.
_STILL_THE_SAME_STORY = timedelta(days=60)


def read_date(text: str) -> Optional[date]:
    """The calendar date written in `text` ("2026-11-06T08:30", "November 6,
    2026", "6 Nov 2026"), or None. A date that cannot be (month 13, next
    year) is not a date."""
    text = text or ""
    found = None
    m = _ISO_DATE.search(text)
    if m:
        found = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = _US_DATE.search(text)
        if m:
            found = (int(m.group(3)), _MONTH_NUMBER[m.group(1).lower()], int(m.group(2)))
        else:
            m = _DAY_FIRST_DATE.search(text)
            if m:
                found = (int(m.group(3)), _MONTH_NUMBER[m.group(2).lower()], int(m.group(1)))
    if not found:
        return None
    try:
        when = date(*found)
    except ValueError:
        return None
    if when.year < 1990 or when > date.today() + timedelta(days=2):
        return None
    return when


@dataclass
class Article:
    title: str = ""
    blocks: List[str] = field(default_factory=list)
    # The day the page says it was published, when it says so and has not been
    # reworked since. A story that says "in October" means the October before
    # this date; a page without one cannot be read that way.
    published: Optional[date] = None

    @property
    def text(self) -> str:
        """One block per line: the splitter reads a line break as a sentence end."""
        return "\n".join(self.blocks)


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[tuple] = []        # (tag, skipped, in_story)
        self.skip = 0
        self.story = 0
        self.block: Optional[List[str]] = None
        self.block_depth = 0
        self.all: List[tuple] = []          # (text, in_story)
        self.title_parts: List[str] = []
        self.in_title = False
        self.og_title = ""
        self.ld: List[str] = []
        self.in_ld = False
        self.published: List[str] = []      # what the page's own markup gives as its date
        self.modified: List[str] = []
        self.pub_time: Optional[List[str]] = None   # text of <time pubdate>, while inside one

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("property") == "og:title" and a.get("content"):
            self.og_title = a["content"].strip()
        if tag == "meta" and a.get("content"):
            names = {(a.get(k) or "").strip().lower() for k in ("name", "property", "itemprop")}
            if names & _PUBLISHED_META:
                self.published.append(a["content"])
            elif names & _MODIFIED_META:
                self.modified.append(a["content"])
        if tag == "time" and ("pubdate" in a or (a.get("itemprop") or "").lower() == "datepublished"):
            if a.get("datetime"):
                self.published.append(a["datetime"])
            else:
                self.pub_time = []
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self.in_ld = True
            self.ld.append("")
        if tag == "title":
            self.in_title = True
        if tag in _VOID:
            if tag == "br" and self.block is not None:
                self.block.append(" ")
            return
        marks = f"{a.get('class') or ''} {a.get('id') or ''}"
        skipped = tag in _SKIP or a.get("aria-hidden") == "true" or "hidden" in a or (
            tag not in _STORY and tag not in ("body", "html") and _is_furniture(marks))
        story = tag in _STORY
        self.stack.append((tag, skipped, story))
        self.skip += skipped
        self.story += story
        if tag in _BLOCKS and not self.skip and self.block is None:
            self.block, self.block_depth = [], len(self.stack)

    def handle_endtag(self, tag):
        if tag == "time" and self.pub_time is not None:
            self.published.append(" ".join(self.pub_time))
            self.pub_time = None
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self.in_ld = False
        if tag in _VOID:
            return
        # Close back to the nearest matching open tag; ignore a stray close.
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                break
        else:
            return
        while len(self.stack) > i:
            if self.block is not None and len(self.stack) == self.block_depth:
                raw = _FOOTNOTE.sub("", "".join(self.block))
                # Preformatted text (agency news releases) keeps its own
                # paragraphs: a blank line ends one.
                parts = re.split(r"\n\s*\n", raw) if self.stack[-1][0] == "pre" else [raw]
                for part in parts:
                    text = _SPACE.sub(" ", part).strip()
                    if text:
                        self.all.append((text, self.story > 0))
                self.block = None
            _, skipped, story = self.stack.pop()
            self.skip -= skipped
            self.story -= story

    def handle_data(self, data):
        if self.pub_time is not None:
            self.pub_time.append(data)
        if self.in_ld and self.ld:
            self.ld[-1] += data
        elif self.in_title:
            self.title_parts.append(data)
        elif self.block is not None and not self.skip:
            self.block.append(data)


def _article_body(ld_blocks: List[str]) -> str:
    """`articleBody` from the page's structured data, if any."""
    def walk(node):
        if isinstance(node, dict):
            body = node.get("articleBody")
            if isinstance(body, str) and len(body) > 200:
                return body
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        return ""
    for raw in ld_blocks:
        try:
            found = walk(json.loads(raw))
        except (ValueError, RecursionError):
            continue
        if found:
            return found
    return ""


def _ld_dates(ld_blocks: List[str]):
    """(datePublished, dateModified) from the page's structured data."""
    found = {"datePublished": "", "dateModified": ""}

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            for key in found:
                if not found[key] and isinstance(node.get(key), str):
                    found[key] = node[key]
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:20]:
                walk(item, depth + 1)
    for raw in ld_blocks:
        try:
            walk(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    return found["datePublished"], found["dateModified"]


def _published(reader: "_Reader") -> Optional[date]:
    """The day the page was published, or None when it does not say, or says
    it has been changed long since (an encyclopedia entry, a page kept
    current): what such a page says "in October" cannot be dated by it."""
    ld_published, ld_modified = _ld_dates(reader.ld)
    published = next((d for d in map(read_date, reader.published + [ld_published]) if d), None)
    if published is None:
        # A press release dates itself in its opening lines.
        for text, _ in reader.all[:3]:
            if _RELEASE_LINE.search(text):
                published = read_date(text)
                break
    if published is None:
        return None
    modified = next((d for d in map(read_date, reader.modified + [ld_modified]) if d), None)
    if modified and modified - published > _STILL_THE_SAME_STORY:
        return None
    return published


def extract(html: str, max_chars: int = 40000) -> Article:
    """The article in a page of HTML. Never raises; an unreadable page is an
    Article with no blocks."""
    reader = _Reader()
    try:
        reader.feed(html or "")
        reader.close()
    except Exception:  # noqa: BLE001 — a broken page is an empty one
        pass
    title = reader.og_title or _SPACE.sub(" ", "".join(reader.title_parts)).strip()

    blocks = [t for t, _ in reader.all]
    story = [t for t, inside in reader.all if inside]
    # The page marks its story, and most of the text is in it: use that part.
    if story and sum(map(len, story)) >= 0.4 * max(sum(map(len, blocks)), 1):
        blocks = story
    # A line of running text, not a label or a link list, and not a pointer
    # to another story ("READ MORE: Inflation worsened last month to 3.7%"),
    # whose "last month" is that story's and not this page's.
    blocks = [b for b in blocks if len(b) >= 40 and len(b.split()) >= 7 and not _TEASER.match(b)]
    if sum(map(len, blocks)) < 400:
        body = _article_body(reader.ld)
        if body:
            blocks = [p.strip() for p in re.split(r"\n+", unescape(body)) if len(p.strip()) >= 40]

    # Where a wire story was filed ("WASHINGTON (AP) — ") is not part of what it says.
    from claims.geography import DATELINE_RE
    blocks = [DATELINE_RE.sub("", b, count=1) for b in blocks]

    out, total, seen = [], 0, set()
    for b in blocks:
        if b in seen:
            continue
        seen.add(b)
        if total + len(b) > max_chars:
            break
        out.append(b)
        total += len(b)
    return Article(title=title[:300], blocks=out, published=_published(reader))
