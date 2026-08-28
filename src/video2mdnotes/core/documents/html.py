"""HTML → `SourceText`. Same parser as EPUB, no publisher TOC to lean on.

A local `.html` file and a fetched public article are the same problem: no
manifest, no spine, no table of contents — the only structure available is
whatever headings the page itself contains, in document order. So this module
splits at heading POSITIONS in the parsed text, unlike EPUB which splits at
anchor positions matched against a separately-parsed TOC. Headings-as-structure
is a weaker signal than a real TOC (a badly-styled page can use headings for
visual weight rather than structure), but it is the only signal a bare page
offers, and it is what `structure_is_inferred` exists to be honest about.

**Fetching goes through `core/webfetch.fetch_text`, not a bespoke request.**
That function is already hardened (http/https only, size-capped) because
caption tracks and embed-scraped pages both need the same guarantees — a URL
from an article link is exactly as untrusted as one from video metadata, and a
second copy of that hardening is how a scheme check ends up on one path and
not the other.
"""

from pathlib import Path
from typing import List
from urllib.parse import urlparse

from video2mdnotes.core.documents._htmltext import TextExtractor
from video2mdnotes.core.sources import Section, SourceText, build_full_text
from video2mdnotes.core.webfetch import fetch_text

SUPPORTED = {".html", ".htm"}
_LOCATOR_TITLE_CHARS = 44


def _locator_for(title: str) -> str:
    title = title.strip()[:_LOCATOR_TITLE_CHARS].rstrip()
    return f"§{title}" if title else "unplaced"


def _sections_from_headings(parser: TextExtractor) -> List[Section]:
    """Split parsed text at heading positions, building a nested path.

    Mirrors EPUB's anchor-cut logic in shape, but the cut points here are the
    page's own h1-h6 tags rather than TOC entries, because a bare page has no
    TOC to match against.
    """
    sections: List[Section] = []
    trail: List[str] = []

    if not parser.headings:
        body = parser.text()
        if body.strip():
            sections.append(Section(
                path=["Document"], locator="unplaced", text=body,
                word_count=len(body.split()),
            ))
        return sections

    # Text before the first heading is still real content (an intro
    # paragraph before the page gets to its first `<h1>`).
    lead = parser.text_between(0, parser.headings[0][2])
    if lead.strip():
        sections.append(Section(
            path=["Document"], locator="unplaced", text=lead,
            word_count=len(lead.split()),
        ))

    for i, (level, title, start) in enumerate(parser.headings):
        end = parser.headings[i + 1][2] if i + 1 < len(parser.headings) else None
        body = parser.text_between(start, end)
        if not body.strip():
            continue
        trail = trail[: max(0, level - 1)] + [title]
        sections.append(Section(
            path=list(trail), locator=_locator_for(title), text=body,
            word_count=len(body.split()),
        ))
    return sections


def _build(html_text: str, title_fallback: str, origin: str, provenance: str) -> SourceText:
    parser = TextExtractor()
    parser.feed(html_text)
    sections = _sections_from_headings(parser)
    if not sections:
        raise ValueError(f"{origin}: no readable text found")

    title = parser.title or title_fallback
    return SourceText(
        source_id=Path(urlparse(origin).path or origin).stem or title_fallback,
        title=title,
        kind="document",
        origin=origin,
        language="unknown",
        full_text=build_full_text(sections),
        sections=sections,
        provenance=provenance,
        extractor="html.extract/1",
        structure_is_inferred=True,   # headings-as-structure is always a guess
    )


def extract(path: Path) -> SourceText:
    """Read a local `.html`/`.htm` file into a `SourceText`."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    return _build(raw, path.stem, str(path.resolve()), "html (headings)")


def extract_url(url: str) -> SourceText:
    """Fetch a public article URL and read it into a `SourceText`.

    This is the fallback below video: `probe_source` tries yt-dlp, then
    embed-scraping, and only reaches here when a URL is neither a video nor a
    page that embeds one — i.e. it is presumably an ordinary article.

    Raises:
        ValueError: scheme refused, body too large, or no readable text
            (`core/webfetch.fetch_text` raises the first two).
    """
    raw = fetch_text(url)
    return _build(raw, urlparse(url).path.rsplit("/", 1)[-1] or url, url, "html (fetched)")
