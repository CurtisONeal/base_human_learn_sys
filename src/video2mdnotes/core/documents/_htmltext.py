"""Shared HTML → plain-text extraction, used by every source that reads markup.

EPUB (a zip of XHTML), a local `.html` file, and a fetched public article are
the same parsing problem wearing three different containers. This module holds
the one parser; `epub.py` and `html.py` each supply their own container logic
(zip navigation vs. a bare fetch) and both hand the resulting markup to the
same `TextExtractor`.

**Code detection is publisher/site-aware, not tag-aware.** The obvious
`<pre>`/`<code>` test misses real content: Springer marks listings with
`class="ProgramCode"` (measured: 0 hits with a bare tag test across three
books that visibly contain code — see epub.py's FIXED_BUGS entry), and blog
platforms commonly use `class="highlight"` or `class="hljs"`. Class-name
matching catches the tag AND the common site/publisher conventions.
"""

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

# Publisher class-name conventions: Springer "ProgramCode", O'Reilly
# "programlisting", Manning "listing". This is the set validated against the
# real 880-book corpus — it is the DEFAULT and should not be widened without
# measuring the whole corpus first.
CODE_CLASS = re.compile(r"program|listing|code|verbatim|sourcecode", re.I)

# Syntax-highlighter classes common on blogs and doc sites. NOT in the default,
# because adding them here silently cost 8.8% of a book: technical EPUBs use
# `class="highlight"` on broad wrappers (masteringgo: 11,595 occurrences), and
# a wrapper opening a code fence mis-tracks the fence nesting and drops text.
# Measured: 10 of 78 EPUBs changed, −49,375 words, when this was the default.
WEB_CODE_CLASS = re.compile(
    r"program|listing|code|verbatim|sourcecode|highlight|hljs|codehilite", re.I
)
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "td", "dd", "dt",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# Tags whose CONTENT is never body text, in any container.
SKIP_TAGS = {"script", "style", "head", "title"}

# Site chrome. Additive, and NOT part of the default: a fetched article page
# carries a site nav and footer that are real navigation furniture, but an
# EPUB uses the same tags for content — `<footer>` for footnotes, `<nav>` for
# in-chapter structure.
#
# Measured, after this was briefly made the shared default: 24 of 78 real
# EPUBs lost content, up to 9.6% of a book (masteringgo lost 17,918 words),
# 72,239 words across the sample. It passed review because it was verified
# against ONE book that happened to be unaffected.
WEB_CHROME_TAGS = {"nav", "footer"}


class TextExtractor(HTMLParser):
    """Markup → plain text with code blocks fenced and headings recorded.

    Fencing code matters more than it looks: a summarizer shown de-indented,
    line-joined source will happily describe code that does not exist. Keeping
    the fence tells it — and any later reader — where prose stops.

    `skip_tags` is a constructor argument rather than a module constant
    because the correct set differs by container and getting it wrong is
    silent. A web page's `<nav>`/`<footer>` are chrome; an EPUB's are content
    (footnotes, in-chapter structure). Callers pass what their format means.
    """

    def __init__(self, skip_tags=None, code_class=None) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_tags = set(skip_tags) if skip_tags is not None else set(SKIP_TAGS)
        self.code_class = code_class if code_class is not None else CODE_CLASS
        self.parts: List[str] = []
        # (level, text, position-in-parts-when-the-heading-OPENED). Position is
        # what lets a caller with no other structure to key off (a bare local
        # .html file, a fetched article) split content at heading boundaries —
        # EPUB does the equivalent by matching TOC entries to anchor ids, but a
        # random page has no TOC, so the headings themselves are the structure.
        # Index 1 stays the text so existing `headings[0][1]` callers (EPUB's
        # no-TOC fallback title) keep working unchanged.
        self.headings: List[Tuple[int, str, int]] = []
        # (index into self.parts, element id) for every anchor a caller might
        # want to split on (EPUB's TOC does; plain HTML usually does not).
        self.anchors: List[Tuple[int, str]] = []
        self.title_parts: List[str] = []
        self._skip_depth = 0
        self._code_depth = 0
        self._title_depth = 0
        self._heading: Optional[int] = None
        self._heading_start = 0
        self._heading_buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._title_depth += 1
        if tag in self.skip_tags:
            self._skip_depth += 1
            return
        attr = dict(attrs)
        if attr.get("id"):
            self.anchors.append((len(self.parts), attr["id"]))
        is_code = tag in ("pre", "code") or bool(
            self.code_class.search(attr.get("class") or "")
        )
        if is_code:
            if self._code_depth == 0:
                self.parts.append("\n```\n")
            self._code_depth += 1
            return
        if tag in HEADING_TAGS:
            self._heading = int(tag[1])
            self._heading_start = len(self.parts)
            self._heading_buf = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if tag in self.skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._code_depth and (tag in ("pre", "code") or self._code_depth):
            # Close on the matching tag; class-based blocks close on their own
            # element, which we cannot know here, so fall back to the tag that
            # opened a fence. Imperfect nesting yields one extra fence, never
            # lost text.
            if tag in ("pre", "code", "div", "p", "figure", "table"):
                self._code_depth -= 1
                if self._code_depth == 0:
                    self.parts.append("\n```\n")
                return
        if tag in HEADING_TAGS and self._heading is not None:
            text = " ".join("".join(self._heading_buf).split())
            if text:
                self.headings.append((self._heading, text, self._heading_start))
            self._heading = None
            self._heading_buf = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        # <title> lives inside the skipped <head>, so it needs its own path:
        # captured regardless of _skip_depth, and never appended to `parts`.
        if self._title_depth:
            self.title_parts.append(data)
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._heading is not None:
            self._heading_buf.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    @staticmethod
    def _clean(raw: str) -> str:
        raw = html.unescape(raw)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return "\n".join(line.strip() for line in raw.splitlines()).strip()

    def text(self) -> str:
        return self._clean("".join(self.parts))

    def text_between(self, start: int, end: Optional[int]) -> str:
        """Text for the slice of parts between two anchor positions."""
        return self._clean("".join(self.parts[start:end]))
