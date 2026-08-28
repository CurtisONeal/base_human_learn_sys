"""EPUB → `SourceText`, preserving the structure the publisher already wrote.

An EPUB is a zip of XHTML with a manifest, a reading order (the *spine*) and a
table of contents. All three are worth reading rather than guessing at:

    container.xml  →  where the OPF lives
    OPF spine      →  reading order (the only reliable sequence)
    toc.ncx / nav  →  human chapter titles and hierarchy

Two decisions in here are load-bearing.

**Spine for order, TOC for names.** The TOC is allowed to skip content, list
things out of order, or point mid-file; the spine is the actual reading
sequence. Ordering by TOC would silently drop any chapter the publisher left
out of it. So the spine drives iteration and the TOC only supplies titles.

**Code detection is publisher-aware.** The obvious `<pre>`/`<code>` test finds
nothing in Springer/Apress books — measured on the real corpus: 0 hits across
three titles that visibly contain code, because Springer marks listings with
`class="ProgramCode"`. A generic extractor would have quietly turned every code
sample into run-together prose, and the summary would have read as if the book
had no code in it. Class-name matching catches the common publisher
conventions alongside the standard tags.

No DRM circumvention: an encrypted EPUB is detected and refused with a clear
error. Nothing here writes the extracted text anywhere — the caller decides,
and the text is copyrighted.
"""

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from video2mdnotes.core.documents._htmltext import TextExtractor
from video2mdnotes.core.sources import Section, SourceText, build_full_text
from video2mdnotes.logger import logger

_NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# "13. Agentic RAG", "Chapter 7", "Ch 2 —" all denote a numbered chapter.
_CHAPTER_NO = re.compile(r"^\s*(?:chapter|ch\.?)?\s*(\d{1,2})\s*[.:—-]", re.I)


class DRMError(RuntimeError):
    """The file is encrypted. We do not attempt to circumvent it."""


class ImageOnlyEPUBError(RuntimeError):
    """The EPUB carries pictures, not text.

    Comics, manga and graphic-novel EPUBs are a page-image per spine document
    with no prose to read. Measured: 9 of 396 files in the real corpus. They
    must fail by name rather than as a generic "no text" error, because the
    correct response is different — there is nothing wrong with the file, and
    the fix is OCR (out of scope), not a better parser.
    """


# How much of the section title to keep in a locator. Long enough to find the
# passage, short enough to sit in a citation.
_LOCATOR_TITLE_CHARS = 44


def _locator_for(path: List[str]) -> str:
    """Print the tightest *true* reference for a section.

    An EPUB has no pages, so a page number here would be fabricated. What it
    does have is a chapter number and the section heading the publisher wrote.

    **Chapter alone is not a citation.** Measured on LLMOps: "[ch. 1]" covers
    33 sections and 6,923 words, and the whole 259-section book collapses to 15
    distinct locators. Checking one claim meant reading a whole chapter — a
    human tester went looking for a claim about open-source trade-offs and
    landed in the vocabulary section, four subsections away.

    The section path was already extracted and then thrown away here. Keeping
    it turns a chapter pointer into a citation:

        before   [ch. 1]
        after    [ch. 1 §The Big Debate: Open Source Versus Proprietary LLMs]

    Section titles are quoted verbatim so a reader can search the book for the
    exact string.
    """
    if not path:
        return "unplaced"

    chapter = None
    for crumb in path:
        match = _CHAPTER_NO.match(crumb)
        if match:
            chapter = f"ch. {int(match.group(1))}"
            break

    # The most specific heading is the leaf, unless the leaf IS the chapter
    # heading (a chapter with no subsections), in which case there is nothing
    # narrower to add.
    leaf = path[-1].strip()
    if chapter and _CHAPTER_NO.match(leaf):
        return chapter

    title = leaf[:_LOCATOR_TITLE_CHARS].rstrip()
    if len(leaf) > _LOCATOR_TITLE_CHARS:
        title += "…"

    if chapter:
        return f"{chapter} §{title}" if title else chapter
    return title or "unplaced"


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    return zf.read(name)


def _opf_path(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(_read(zf, "META-INF/container.xml"))
    rootfile = root.find(".//container:rootfile", _NS)
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB container.xml declares no rootfile")
    return rootfile.get("full-path")


def _parse_opf(zf: zipfile.ZipFile, opf: str) -> Tuple[str, str, List[str], Optional[str]]:
    """Return (title, language, spine hrefs in order, toc href)."""
    base = posixpath.dirname(opf)
    root = ET.fromstring(_read(zf, opf))

    def _dc(tag: str, default: str) -> str:
        el = root.find(f".//dc:{tag}", _NS)
        return (el.text or default).strip() if el is not None and el.text else default

    manifest: Dict[str, Tuple[str, str]] = {}
    for item in root.findall(".//opf:manifest/opf:item", _NS):
        iid, href = item.get("id"), item.get("href")
        if iid and href:
            manifest[iid] = (posixpath.normpath(posixpath.join(base, href)),
                             item.get("properties") or "")

    spine_el = root.find(".//opf:spine", _NS)
    spine: List[str] = []
    for ref in (spine_el.findall("opf:itemref", _NS) if spine_el is not None else []):
        entry = manifest.get(ref.get("idref") or "")
        if entry:
            spine.append(entry[0])

    # EPUB3 nav first, then the EPUB2 ncx the spine points at.
    toc = next((h for h, props in manifest.values() if "nav" in props), None)
    if toc is None and spine_el is not None:
        entry = manifest.get(spine_el.get("toc") or "")
        toc = entry[0] if entry else None
    if toc is None:
        toc = next((h for h, _ in manifest.values() if h.endswith(".ncx")), None)

    return _dc("title", Path(opf).stem), _dc("language", "unknown"), spine, toc


# One TOC entry: which file, which anchor inside it, and the heading path.
# Keeping the fragment is the whole point — a book's real structure lives in
# the 1800 anchors pointing *into* 30 spine files, not in the 30 files.
TocEntry = Tuple[str, str, List[str]]      # (href, fragment, path)


def _parse_ncx(zf: zipfile.ZipFile, toc: str) -> List[TocEntry]:
    """Ordered TOC entries from an EPUB2 toc.ncx."""
    base = posixpath.dirname(toc)
    root = ET.fromstring(_read(zf, toc))
    out: List[TocEntry] = []

    def walk(node, trail: List[str]):
        for nav in node.findall("ncx:navPoint", _NS):
            label = nav.find(".//ncx:navLabel/ncx:text", _NS)
            content = nav.find("ncx:content", _NS)
            name = (label.text or "").strip() if label is not None and label.text else ""
            path = trail + ([name] if name else [])
            src = content.get("src") if content is not None else None
            if src:
                raw, _, frag = src.partition("#")
                out.append((posixpath.normpath(posixpath.join(base, raw)), frag, path))
            walk(nav, path)

    nav_map = root.find("ncx:navMap", _NS)
    if nav_map is not None:
        walk(nav_map, [])
    return out


def _parse_nav(zf: zipfile.ZipFile, toc: str) -> List[TocEntry]:
    """Ordered TOC entries from an EPUB3 nav.xhtml.

    A light regex walk over the nested `<ol>` rather than an XML parse: nav
    documents in real books are frequently not well-formed XML, and failing to
    read the TOC would cost the entire structure of the book.
    """
    base = posixpath.dirname(toc)
    text = _read(zf, toc).decode("utf-8", errors="replace")

    # A nav document holds several <nav> lists: the TOC, a page-list mapping
    # print page numbers to anchors, and landmarks. Only the first is
    # structure. Parsing all of them yielded 1833 "sections" for a 31-chapter
    # book, most of them page markers named things like "xxxix" — structure
    # that does not exist, attached to locators that mean nothing.
    navs = re.findall(r"<nav\b[^>]*>.*?</nav>", text, re.S | re.I)
    toc_navs = [n for n in navs if re.search(r'epub:type=["\'][^"\']*\btoc\b', n, re.I)]
    if toc_navs:
        text = toc_navs[0]
    elif navs:
        # No epub:type at all — take the first nav and hope, but drop any that
        # self-identify as something else.
        text = next(
            (n for n in navs
             if not re.search(r'epub:type=["\'][^"\']*(page-list|landmarks)', n, re.I)),
            navs[0],
        )

    out: List[TocEntry] = []
    depth = 0
    trail: List[str] = []
    for token in re.finditer(
        r"<ol\b|</ol>|<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", text, re.S | re.I
    ):
        chunk = token.group(0).lower()
        if chunk.startswith("<ol"):
            depth += 1
        elif chunk.startswith("</ol"):
            depth = max(0, depth - 1)
            trail = trail[:depth]
        else:
            name = " ".join(re.sub(r"<[^>]+>", "", token.group(2) or "").split())
            raw, _, frag = (token.group(1) or "").partition("#")
            trail = trail[: max(0, depth - 1)] + ([name] if name else [])
            if raw:
                out.append(
                    (posixpath.normpath(posixpath.join(base, raw)), frag, list(trail))
                )
    return out


def _merge_adjacent(sections: List[Section]) -> List[Section]:
    """Rejoin consecutive sections that share a heading path.

    Publishers put `id` attributes on figures, tables and individual paragraphs,
    not only on headings. Splitting at every anchor therefore cuts one real
    section into a dozen fragments that all carry the same title — 1063 of them
    in a 31-chapter book. Fragments that cite identically *are* the same
    section, so rejoining them costs no structure and gives chunking passages
    big enough to be worth summarizing.
    """
    merged: List[Section] = []
    for sec in sections:
        if merged and merged[-1].path == sec.path:
            prev = merged[-1]
            prev.text = f"{prev.text}\n\n{sec.text}".strip()
            prev.word_count = len(prev.text.split())
            continue
        merged.append(sec.model_copy(deep=True))
    return merged


def extract(path: Path) -> SourceText:
    """Read an EPUB into a `SourceText` with one `Section` per spine document.

    Raises:
        DRMError: the file is encrypted.
        ValueError: the file is not a readable EPUB.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "META-INF/encryption.xml" in names or "META-INF/rights.xml" in names:
            raise DRMError(
                f"{path.name} is DRM-encrypted. This tool does not remove DRM; "
                "use a copy you can read without it."
            )

        title, language, spine, toc = _parse_opf(zf, _opf_path(zf))

        toc_entries: List[TocEntry] = []
        toc_kind = "none"
        if toc and toc in names:
            try:
                if toc.endswith(".ncx"):
                    toc_entries, toc_kind = _parse_ncx(zf, toc), "toc.ncx"
                else:
                    toc_entries, toc_kind = _parse_nav(zf, toc), "nav.xhtml"
            except (ET.ParseError, KeyError) as exc:
                logger.warning(f"{path.name}: TOC unreadable ({exc}); using headings")

        by_href: Dict[str, List[TocEntry]] = {}
        for entry in toc_entries:
            by_href.setdefault(entry[0], []).append(entry)

        sections: List[Section] = []
        doc_no = 0
        for href in spine:
            if href not in names:
                continue
            parser = TextExtractor()
            try:
                parser.feed(zf.read(href).decode("utf-8", errors="replace"))
            except Exception as exc:               # malformed XHTML is common
                logger.warning(f"{path.name}: skipping {href} ({exc})")
                continue
            if not parser.text().strip():
                continue
            doc_no += 1

            # Anchor positions the TOC actually points at, in document order.
            anchor_at = {aid: idx for idx, aid in parser.anchors}
            cuts: List[Tuple[int, List[str]]] = []
            for _, frag, crumbs in by_href.get(href, []):
                if frag and frag in anchor_at:
                    cuts.append((anchor_at[frag], crumbs))
            cuts.sort(key=lambda c: c[0])

            if not cuts:
                # No usable anchors: the whole document is one section. Name it
                # from the TOC, else its own first heading, else positionally —
                # and a positional name is honest that it is one.
                entries = by_href.get(href, [])
                crumbs = (
                    entries[0][2] if entries
                    else [parser.headings[0][1]] if parser.headings
                    else [f"Document {doc_no}"]
                )
                body = parser.text()
                if body.strip():
                    sections.append(Section(
                        path=crumbs, locator=_locator_for(crumbs), text=body,
                        word_count=len(body.split()), href=href,
                    ))
                continue

            # Any prose before the first anchor still belongs to the book.
            if cuts[0][0] > 0:
                lead = parser.text_between(0, cuts[0][0])
                if lead.strip():
                    sections.append(Section(
                        path=cuts[0][1][:-1] or [f"Document {doc_no}"],
                        locator=_locator_for(cuts[0][1]), text=lead,
                        word_count=len(lead.split()), href=href,
                    ))

            for i, (start, crumbs) in enumerate(cuts):
                end = cuts[i + 1][0] if i + 1 < len(cuts) else None
                body = parser.text_between(start, end)
                if not body.strip():
                    continue
                sections.append(Section(
                    path=crumbs, locator=_locator_for(crumbs), text=body,
                    word_count=len(body.split()),
                    href=f"{href}#{[f for _, f, c in by_href[href] if c == crumbs][0]}"
                    if any(c == crumbs for _, f, c in by_href[href]) else href,
                ))

    sections = _merge_adjacent(sections)

    if not sections:
        # Distinguish "this is a picture book" from "this file is broken". An
        # image-heavy container with no extractable prose is the former.
        with zipfile.ZipFile(path) as probe:
            names = probe.namelist()
        images = sum(1 for n in names
                     if n.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")))
        if images >= 5:
            raise ImageOnlyEPUBError(
                f"{path.name}: {images} images and no readable text — this looks "
                "like a comic, manga or graphic novel. Reading pictures is out of "
                "scope (no OCR of book pages)."
            )
        raise ValueError(f"{path.name}: no readable text found")

    full = build_full_text(sections)
    return SourceText(
        source_id=path.stem,
        title=title,
        kind="document",
        origin=str(path.resolve()),
        language=language,
        full_text=full,
        sections=sections,
        provenance=f"epub ({toc_kind})",
        extractor="epub.extract/1",
        structure_is_inferred=(toc_kind == "none"),
    )
