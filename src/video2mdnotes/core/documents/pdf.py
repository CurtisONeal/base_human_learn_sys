"""PDF → `SourceText`. The format that has pages but may have no structure.

PDF is the mirror image of EPUB. An EPUB always knows its chapters and never
knows its pages; a PDF always knows its pages and often has no idea what a
chapter is. So the two extractors optimise for opposite things, and the
provenance string says which situation produced a given file:

    pdf (bookmarks)   the author shipped an outline — structure is real
    pdf (headings)    inferred from font size — structure is a guess
    pdf (pages only)  no structure recovered; only page locators are trustworthy

That distinction is the reason `structure_is_inferred` exists. A citation of
"p. 214" is verifiable in any of the three cases. A citation of "ch. 7,
§Indexing" is only meaningful in the first, and a reader deserves to know
which they were handed.

**Text extraction is not free of judgement either.** PDF stores glyphs at
coordinates, not paragraphs, so hyphenated line breaks, two-column layouts and
code indentation all have to be reconstructed. Where reconstruction is
uncertain this module prefers keeping the text intact and slightly ugly over
making it pretty and wrong.

A scanned PDF with no text layer is detected and refused rather than returned
as a few dozen empty pages that summarize into confident nonsense. OCR of
scanned pages is explicitly out of scope (WP-02).
"""

import re
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from video2mdnotes.core.sources import Section, SourceText, build_full_text
from video2mdnotes.logger import logger

# A page with fewer than this many characters of text layer is treated as
# having no text. Scanned pages usually yield 0, but a stray header or page
# number can leak a handful of characters and look like success.
_MIN_CHARS_PER_PAGE = 40
_MIN_TEXT_PAGE_RATIO = 0.30     # below this, call it a scan and refuse


class ScannedPDFError(RuntimeError):
    """The PDF has no usable text layer. OCR is out of scope."""


class EncryptedPDFError(RuntimeError):
    """The PDF is password-protected. We do not attempt to crack it."""


def _require_pymupdf():
    try:
        import pymupdf                       # noqa: F401
        return pymupdf
    except ImportError as exc:               # pragma: no cover - env dependent
        raise ImportError(
            "PDF support needs the optional 'documents' extra:\n"
            "    uv sync --extra ocr --extra dev --extra documents"
        ) from exc


def _clean_page(text: str) -> str:
    """Undo the damage PDF line-boxes do to prose.

    Joining hyphenated line breaks is the one transformation worth doing
    unconditionally: "retrieval-\\naugmented" is never two words, and leaving it
    split corrupts exactly the technical terms a search over this corpus needs
    to match.
    """
    text = text.replace("­", "")                  # soft hyphen
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)       # hyphenated line break
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _strip_running_heads(pages: List[str]) -> List[str]:
    """Drop the header/footer line that repeats on most pages.

    A running head ("Chapter 3 | Indexing", "www.example.com") appears on every
    page and would otherwise be counted once per page — inflating term
    frequency for whatever words happen to sit in the header and polluting
    every chunk with the same noise.
    """
    if len(pages) < 8:
        return pages

    def edge_lines(page: str, top: bool) -> List[str]:
        lines = [ln for ln in page.splitlines() if ln.strip()]
        return lines[:1] if top else lines[-1:]

    repeated: set = set()
    for top in (True, False):
        counts: Dict[str, int] = {}
        for page in pages:
            for line in edge_lines(page, top):
                # Ignore the page number itself varying inside the line.
                key = re.sub(r"\d+", "#", line.strip())
                if len(key) > 3:
                    counts[key] = counts.get(key, 0) + 1
        repeated |= {k for k, n in counts.items() if n >= len(pages) * 0.5}

    if not repeated:
        return pages
    out = []
    for page in pages:
        lines = page.splitlines()
        kept = [
            ln for i, ln in enumerate(lines)
            if not (
                (i < 2 or i >= len(lines) - 2)
                and re.sub(r"\d+", "#", ln.strip()) in repeated
            )
        ]
        out.append("\n".join(kept).strip())
    return out


def _infer_headings(doc) -> List[Tuple[int, str, int]]:
    """Guess (level, title, page) from font size when there are no bookmarks.

    Body text dominates any book, so the modal font size *is* the body size.
    Anything meaningfully larger, short, and not a full sentence is a heading.
    This is a heuristic and is labelled as one in provenance — it recovers
    chapter starts reliably and sub-sections only sometimes.
    """
    sizes: List[float] = []
    spans: List[Tuple[float, str, int, bool]] = []
    for pno, page in enumerate(doc):
        try:
            data = page.get_text("dict")
        except Exception:
            continue
        for block in data.get("blocks", []):
            for line in block.get("lines", []):
                text = "".join(s.get("text", "") for s in line.get("spans", []))
                if not text.strip():
                    continue
                span = line["spans"][0]
                size = round(span.get("size", 0), 1)
                bold = "bold" in (span.get("font", "") or "").lower()
                sizes.append(size)
                spans.append((size, text.strip(), pno + 1, bold))

    if not sizes:
        return []
    body = statistics.mode(sizes)

    out: List[Tuple[int, str, int]] = []
    for size, text, page, bold in spans:
        words = text.split()
        if not (2 <= len(words) <= 18) or text.endswith((".", ",", ";")):
            continue
        if size >= body * 1.45 or (size >= body * 1.15 and bold):
            level = 1 if size >= body * 1.6 else 2
            if not out or out[-1][1] != text:
                out.append((level, text, page))
    return out


def extract(path: Path) -> SourceText:
    """Read a PDF into a `SourceText` with page-accurate locators.

    Raises:
        EncryptedPDFError: password-protected.
        ScannedPDFError: no usable text layer.
        ImportError: the optional `documents` extra is missing.
    """
    pymupdf = _require_pymupdf()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise EncryptedPDFError(
                f"{path.name} is password-protected. Supply a copy you can open."
            )

        pages = [_clean_page(p.get_text("text") or "") for p in doc]
        if not pages:
            raise ValueError(f"{path.name}: no pages")

        with_text = sum(1 for p in pages if len(p) >= _MIN_CHARS_PER_PAGE)
        if with_text / len(pages) < _MIN_TEXT_PAGE_RATIO:
            raise ScannedPDFError(
                f"{path.name}: only {with_text}/{len(pages)} pages have a text "
                "layer — this looks scanned. OCR of scanned PDFs is out of scope."
            )

        pages = _strip_running_heads(pages)

        # Bookmarks are the author's own structure; prefer them absolutely.
        toc = []
        try:
            toc = doc.get_toc() or []
        except Exception as exc:
            logger.warning(f"{path.name}: bookmarks unreadable ({exc})")

        inferred = False
        if not toc:
            guessed = _infer_headings(doc)
            if guessed:
                toc = [[lvl, title, pg] for lvl, title, pg in guessed]
                inferred = True

        # Materialise into plain Python before the document closes. pymupdf's
        # get_toc() result does not survive close(): read afterwards it yields
        # stale memory, which produced a single section titled
        # "7x9_web_Rabbit.pdf > Blank Page" spanning an entire 284-page book
        # while still reporting provenance "pdf (bookmarks)". Silent, and
        # plausible enough to ship.
        entries: List[Tuple[int, str, int]] = [
            (int(lvl), str(t).strip(), int(pg))
            for lvl, t, pg in toc
            if t and 1 <= int(pg) <= len(pages)
        ]

        title = str((doc.metadata or {}).get("title") or path.stem)
        language = str((doc.metadata or {}).get("language") or "unknown")
    finally:
        doc.close()

    sections: List[Section] = []
    if entries:
        # Assign every page to exactly one bookmark: the last one that starts
        # at or before it.
        #
        # The obvious alternative — give each bookmark the span from its own
        # start page to the next bookmark's — double-counts, because bookmarks
        # routinely share a start page (92 of 264 in one measured book) and
        # nested outlines let a chapter's range cover its own subsections. That
        # produced 213,451 words from a PDF holding 90,778: the same prose two
        # and a half times over, which inflates cost, skews any term frequency
        # computed from it, and invites a summary to treat a repeated passage
        # as emphasis.
        trails: List[List[str]] = []
        trail: List[str] = []
        for level, name, _ in entries:
            trail = trail[: max(0, level - 1)] + [name]
            trails.append(list(trail))

        # Ownership follows PAGE order, not outline order. A PDF outline is not
        # required to be monotonic, and real ones are not: LLMOps ends with
        # three production artifacts — "7x9_web_Rabbit.pdf", "Blank Page",
        # "Blank Page" — all pointing at page 1. Walking the outline and
        # letting each entry claim "my page to the end" handed the whole
        # 284-page book to the last of those, yielding one section titled
        # "7x9_web_Rabbit.pdf > Blank Page" while still reporting provenance
        # "pdf (bookmarks)".
        by_page = sorted(range(len(entries)), key=lambda i: (entries[i][2], i))
        owner: List[Optional[int]] = [None] * len(pages)
        cursor, current = 0, None
        for pno in range(len(pages)):
            while cursor < len(by_page) and entries[by_page[cursor]][2] <= pno + 1:
                current = by_page[cursor]
                cursor += 1
            owner[pno] = current

        grouped: Dict[int, List[int]] = {}
        for pno, idx in enumerate(owner):
            if idx is not None:
                grouped.setdefault(idx, []).append(pno + 1)

        # Pages before the first bookmark still hold text worth keeping.
        lead = [p for p, o in enumerate(owner, 1) if o is None and pages[p - 1].strip()]
        if lead:
            body = "\n\n".join(pages[p - 1] for p in lead).strip()
            if body:
                sections.append(Section(
                    path=["Front matter"],
                    locator=f"pp. {lead[0]}–{lead[-1]}" if len(lead) > 1 else f"p. {lead[0]}",
                    text=body, word_count=len(body.split()), href=f"page={lead[0]}",
                ))

        for idx in sorted(grouped, key=lambda i: grouped[i][0]):
            owned = grouped[idx]
            body = "\n\n".join(pages[p - 1] for p in owned).strip()
            if not body:
                continue
            first, last = owned[0], owned[-1]
            # Page range PLUS the section heading. A page number alone is
            # checkable, but on a densely-bookmarked book several sections
            # share a page span, and the reader still has to hunt. The heading
            # costs nothing to carry — see epub._locator_for for the measured
            # case that motivated this.
            # NB: not named `pages` — that is the list of page TEXTS in this
            # scope, and shadowing it turns pages[p-1] into a string index.
            page_ref = f"p. {first}" if first == last else f"pp. {first}–{last}"
            leaf = (trails[idx][-1] if trails[idx] else "").strip()
            title = leaf[:44].rstrip() + ("…" if len(leaf) > 44 else "")
            sections.append(Section(
                path=trails[idx],
                locator=f"{page_ref} §{title}" if title else page_ref,
                text=body,
                word_count=len(body.split()),
                href=f"page={first}",
            ))
        provenance = "pdf (headings)" if inferred else "pdf (bookmarks)"
    else:
        # No structure at all. Pages are still real, so cite those and be
        # explicit that nothing above page level was recovered.
        for pno, body in enumerate(pages, 1):
            if body.strip():
                sections.append(Section(
                    path=[f"p. {pno}"], locator=f"p. {pno}", text=body,
                    word_count=len(body.split()), href=f"page={pno}",
                ))
        provenance, inferred = "pdf (pages only)", True

    if not sections:
        raise ValueError(f"{path.name}: no readable text found")

    return SourceText(
        source_id=path.stem,
        title=title,
        kind="document",
        origin=str(path.resolve()),
        language=language,
        full_text=build_full_text(sections),
        sections=sections,
        provenance=provenance,
        extractor="pdf.extract/1",
        structure_is_inferred=inferred,
    )
