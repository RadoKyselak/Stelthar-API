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
# Class or id names that mark page furniture inside the story.
_FURNITURE = re.compile(r"(?:^|[-_ ])(?:nav|menu|share|social|related|promo|advert|ad|ads|sidebar|comment|comments|"
                        r"footer|newsletter|subscribe|cookie|breadcrumb|byline|caption|tags|toolbar)(?:$|[-_ ])", re.I)


@dataclass
class Article:
    title: str = ""
    blocks: List[str] = field(default_factory=list)

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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("property") == "og:title" and a.get("content"):
            self.og_title = a["content"].strip()
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
            tag not in _STORY and tag not in ("body", "html") and bool(_FURNITURE.search(marks)))
        story = tag in _STORY
        self.stack.append((tag, skipped, story))
        self.skip += skipped
        self.story += story
        if tag in _BLOCKS and not self.skip and self.block is None:
            self.block, self.block_depth = [], len(self.stack)

    def handle_endtag(self, tag):
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
    # A line of running text, not a label or a link list.
    blocks = [b for b in blocks if len(b) >= 40 and len(b.split()) >= 7]
    if sum(map(len, blocks)) < 400:
        body = _article_body(reader.ld)
        if body:
            blocks = [p.strip() for p in re.split(r"\n+", unescape(body)) if len(p.strip()) >= 40]

    out, total, seen = [], 0, set()
    for b in blocks:
        if b in seen:
            continue
        seen.add(b)
        if total + len(b) > max_chars:
            break
        out.append(b)
        total += len(b)
    return Article(title=title[:300], blocks=out)
