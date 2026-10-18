"""Learning packages: notes whose every citation is a working link (WP-L1).

Deterministic post-processing — no model calls. Design: design/LEARNING_PACKAGES.md.

    <vault>/<LEARNING_ROOT>/<subject>/<slug>/
        notes.md            the run's notes, citations rewritten into links
        text/NN_<slug>.md   the source's sections as markdown, headings kept
        images/…            figures extracted from the source
        transcript.md       local recordings only: raw transcript + block ids
        source_meta.json    provenance + the hash of the notes.md we wrote
        package_report.json counts, every unresolved citation
    <vault>/__sources/<subject>/<slug>/<slug>.pdf|.epub   the binary

**Anchor form.** Obsidian resolves a markdown link to a heading as
``file.md#Heading%20Text``: the heading text, case kept, spaces as ``%20`` —
NOT GitHub's kebab ``#heading-text`` (which Obsidian does not follow). Obsidian
compares headings after stripping the characters it reserves in links
(``# | ^ : [ ] \\ %``); a raw ``:`` in a markdown-link fragment has been reported
to break the link. So the anchor is the heading with those characters turned
into spaces, whitespace collapsed, then percent-encoded (``(``/``)`` too, so a
parenthesis can never end the markdown link early). `obsidian_anchor` builds
it; `anchor_matches` is the resolver the tests use.

**Relative paths.** Every link is relative to the file that holds it, computed
with `os.path.relpath` from the real directories, so a non-default
LEARNING_ROOT still resolves. With the default layout the PDF link from
notes.md is ``../../../../__sources/<subject>/<slug>/<slug>.pdf#page=N``.

**Nothing is dropped.** A citation that cannot be resolved stays as plain text
and is listed in package_report.json with its line.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as _html
import json
import os
import posixpath
import re
import shutil
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

PACKAGER = "video2mdnotes.package/1"
SOURCES_DIRNAME = "__sources"
META_FILENAME = "source_meta.json"
REPORT_FILENAME = "package_report.json"
NOTES_FILENAME = "notes.md"
TRANSCRIPT_FILENAME = "transcript.md"

# Figures smaller than this are icons, bullets, logos — not diagrams.
MIN_IMAGE_PX = 120
MIN_IMAGE_BYTES = 4096
# The same image on this many pages is page furniture (a header logo).
DECORATIVE_PAGES = 3
# Above this many figures in one notes section the figure callout starts folded.
FOLD_FIGURES_ABOVE = 4

_FORBIDDEN_PREFIXES = (
    "/Volumes/",
    str(Path("~/Movies/CLE-AI-26_09_safety_copies").expanduser()) + "/",
)


class PackageError(RuntimeError):
    """The input cannot be packaged (missing notes, unsupported source...)."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _refuse_forbidden(path: Path) -> None:
    raw = str(path)
    resolved = str(Path(path).expanduser().resolve())
    for prefix in _FORBIDDEN_PREFIXES:
        if raw.startswith(prefix) or resolved.startswith(prefix):
            raise PackageError(f"{path}: refusing to read from {prefix} (protected location)")


def slugify(text: str, max_len: int = 64) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut[: cut.rfind("_")] if "_" in cut else cut
    return slug or "source"


_ANCHOR_RESERVED = re.compile(r"[#|^:\[\]\\%]")


def _strip_heading(heading: str) -> str:
    return " ".join(_ANCHOR_RESERVED.sub(" ", heading or "").split())


def obsidian_anchor(heading: str) -> str:
    """Fragment for a markdown link to `heading` in Obsidian (see module doc)."""
    return quote(_strip_heading(heading), safe="!$&'*+,;=@-._~")


def anchor_matches(anchor: str, heading: str) -> bool:
    """Would Obsidian resolve `#anchor` to this heading? (decode, strip, compare)."""
    return _strip_heading(unquote(anchor)) == _strip_heading(heading)


def link_path(rel: str) -> str:
    """Percent-encode a relative path for a markdown link destination."""
    return quote(rel.replace(os.sep, "/"), safe="/-._~!$&'*+,;=@")


def _rel(target: Path, from_dir: Path) -> str:
    return os.path.relpath(target, from_dir).replace(os.sep, "/")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _norm_title(text: str) -> str:
    text = (text or "").replace("…", " ").replace("...", " ")
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[‘’“”'\"`]", "", text)
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _escape_md_line(line: str) -> str:
    """Keep extracted prose from turning into headings, rules or block ids."""
    stripped = line.lstrip()
    if stripped.startswith("#") or re.fullmatch(r"[-=*_]{2,}\s*", stripped or "x"):
        return "\\" + stripped
    return line


_BULLET_GLYPH = "-\u2022\u25a0\u25aa\u25cf\u25e6"
_LONE_BULLETS = re.compile(
    rf"(?m)^[{_BULLET_GLYPH}][\u200b \t]*(?:\n[{_BULLET_GLYPH}][\u200b \t]*)*\n(?=\S)")


def join_pdf_bullets(text: str) -> str:
    """PDF bullets often arrive as a lone glyph on its own line ("-" plus a
    zero-width space; Exam Ref books print two "\u25a0" lines per bullet).
    Rejoin them with the item text as a markdown list item."""
    return _LONE_BULLETS.sub("- ", text)


_FENCE = re.compile(r"^\s*(```|~~~)")


def _escape_text(text: str) -> str:
    out, in_fence = [], False
    for line in (text or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        out.append(line if in_fence else _escape_md_line(line))
    if in_fence:
        out.append("```")
    return "\n".join(out).strip()


_CHAPTER_NO = re.compile(r"^\s*(?:chapter|ch\.?)\s*(\d{1,3})\b|^\s*(\d{1,2})\s*[.:—-]", re.I)


def _chapter_no(crumb: str) -> Optional[int]:
    m = _CHAPTER_NO.match(crumb or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


# --------------------------------------------------------------------------- #
# Document model: headings (one per TOC entry) grouped into text files
# --------------------------------------------------------------------------- #

@dataclass
class Heading:
    idx: int
    title: str                       # source heading text (verbatim)
    trail: List[str]                 # full TOC path
    depth: int                       # md heading level inside its file (1..6)
    page: Optional[int] = None       # PDF: page the heading starts on
    file_no: int = 0
    text_heading: str = ""           # final heading in the file (deduped)
    chunks: List[str] = field(default_factory=list)
    pages: List[int] = field(default_factory=list)   # PDF pages with text here
    images: List["Figure"] = field(default_factory=list)

    @property
    def chapter(self) -> Optional[int]:
        for crumb in self.trail:
            n = _chapter_no(crumb)
            if n is not None:
                return n
        return None


@dataclass
class TextFile:
    no: int
    title: str
    name: str                        # "03_task_statements.md"
    headings: List[int] = field(default_factory=list)


@dataclass
class Figure:
    name: str                        # file name under images/
    data: bytes
    caption: str
    heading_idx: int
    page: Optional[int] = None


@dataclass
class DocModel:
    kind: str                        # "pdf" | "epub"
    title: str
    headings: List[Heading]
    files: List[TextFile]
    page_count: int = 0
    page_heading: Dict[int, int] = field(default_factory=dict)   # page -> heading active at its end
    figures: List[Figure] = field(default_factory=list)
    skipped_images: Counter = field(default_factory=Counter)
    provenance: str = ""


def _split_depth(trails: List[List[str]]) -> int:
    """Depth whose crumbs become text files.

    The shallowest depth at which the trails actually differ (a PDF whose whole
    outline sits under one root bookmark splits one level down), moved deeper
    when numbered chapters live deeper (Part I > Chapter 3)."""
    depth = 0
    max_len = max((len(t) for t in trails), default=0)
    while depth < max_len - 1:
        distinct = {tuple(t[: depth + 1]) for t in trails if len(t) > depth}
        if len(distinct) > 1:
            break
        depth += 1
    chapter_depths = Counter()
    for t in trails:
        for i, crumb in enumerate(t):
            if _chapter_no(crumb) is not None:
                chapter_depths[i] += 1
                break
    if chapter_depths:
        common, count = chapter_depths.most_common(1)[0]
        if common > depth and count >= 2:
            depth = common
    return depth


def _assign_files(headings: List[Heading], depth: int) -> List[TextFile]:
    files: List[TextFile] = []
    current_key = None
    for h in headings:
        # Headings that share their first depth+1 crumbs share a file; one at
        # or above the split depth (a root bookmark, a Part) stands alone.
        key = tuple(h.trail[: depth + 1])
        if key != current_key:
            files.append(TextFile(no=len(files) + 1, title=h.title, name=""))
            current_key = key
        h.file_no = files[-1].no
        h.depth = max(1, min(6, len(h.trail) - depth)) if len(h.trail) > depth else 1
        files[-1].headings.append(h.idx)
    for f in files:
        f.name = f"{f.no:02d}_{slugify(f.title, 40)}.md"
    # Headings must be unique inside a file, or Obsidian lands on the first.
    for f in files:
        seen: Counter = Counter()
        for i in f.headings:
            h = headings[i]
            base = _strip_heading(h.title) or "Section"
            seen[base] += 1
            h.text_heading = h.title if seen[base] == 1 else f"{h.title} ({seen[base]})"
    return files


def _build_pdf(path: Path, text_dir: Path, pdf_target: Path) -> DocModel:
    from video2mdnotes.core.documents import pdf as pdfx
    pymupdf = pdfx._require_pymupdf()

    doc = pymupdf.open(path)
    try:
        if doc.needs_pass:
            raise PackageError(f"{path.name} is password-protected")
        pages = pdfx._strip_running_heads(
            [pdfx._clean_page(p.get_text("text") or "") for p in doc])
        toc = []
        try:
            toc = doc.get_toc() or []
        except Exception:                     # noqa: BLE001
            toc = []
        provenance = "pdf (bookmarks)"
        if not toc:
            toc = [[lvl, t, pg] for lvl, t, pg in pdfx._infer_headings(doc)]
            provenance = "pdf (headings)" if toc else "pdf (pages only)"
        entries = [(int(lvl), str(t).strip(), int(pg)) for lvl, t, pg in toc
                   if str(t).strip() and 1 <= int(pg) <= len(pages)]
        title = str((doc.metadata or {}).get("title") or path.stem).strip() or path.stem
        if not entries:
            entries = [(1, f"p. {n}", n) for n in range(1, len(pages) + 1)]

        headings: List[Heading] = []
        trail: List[str] = []
        for i, (level, name, page) in enumerate(entries):
            trail = trail[: max(0, level - 1)] + [name]
            headings.append(Heading(idx=i, title=name, trail=list(trail), depth=1, page=page))

        # Page text is split at the line that carries each bookmark's title,
        # so a page holding three headings gives each its own text.
        order = sorted(range(len(headings)), key=lambda i: (headings[i].page, i))
        starts: Dict[int, List[int]] = {}
        for i in order:
            starts.setdefault(headings[i].page, []).append(i)

        front = Heading(idx=-1, title="Front matter", trail=["Front matter"], depth=1)
        current: Optional[Heading] = None
        page_heading: Dict[int, int] = {}

        def page_marker(pno: int) -> str:
            href = f"{link_path(_rel(pdf_target, text_dir))}#page={pno}"
            return f"*[p. {pno}]({href})*"

        def add_text(h: Heading, pno: int, lines: List[str]) -> None:
            joined = join_pdf_bullets("\n".join(lines))
            body = _escape_text(joined)
            if not body:
                return
            if pno not in h.pages:
                h.chunks.append(page_marker(pno))
                h.pages.append(pno)
            h.chunks.append(body)

        for pno in range(1, len(pages) + 1):
            lines = pages[pno - 1].splitlines()
            original = list(lines)
            cuts: List[Tuple[int, Heading]] = []
            cursor = 0
            for i in starts.get(pno, []):
                h = headings[i]
                want = _norm_title(h.title)
                found = None
                for li in range(cursor, len(lines)):
                    got = _norm_title(lines[li])
                    if got and (got == want or (len(got) >= 8 and want.startswith(got))
                                or (len(want) >= 8 and got.startswith(want))):
                        found = li
                        break
                at = found if found is not None else cursor
                cuts.append((at, h))
                cursor = at + (1 if found is not None else 0)
                if found is not None:
                    lines[found] = "\x00"     # the heading line itself is the heading
                    # A long title wraps onto following lines: consume those too.
                    combined = _norm_title(original[found])
                    nxt = found + 1
                    while nxt < len(lines) and combined != want:
                        joined = _norm_title(" ".join(original[found:nxt + 1]))
                        if not joined or not want.startswith(joined):
                            break
                        lines[nxt] = "\x00"
                        combined = joined
                        nxt += 1
                    cursor = nxt
            prev_end = 0
            holder = current or front
            for at, h in cuts:
                add_text(holder, pno, [ln for ln in lines[prev_end:at] if ln != "\x00"])
                holder = h
                prev_end = at
            add_text(holder, pno, [ln for ln in lines[prev_end:] if ln != "\x00"])
            current = holder
            if holder.idx >= 0:
                page_heading[pno] = holder.idx

        if front.chunks:
            front.idx = 0
            for h in headings:
                h.idx += 1
            headings.insert(0, front)
            page_heading = {p: i + 1 for p, i in page_heading.items()}
            for p in range(1, len(pages) + 1):
                page_heading.setdefault(p, 0)

        files = _assign_files(headings, _split_depth([h.trail for h in headings]))
        model = DocModel(kind="pdf", title=title, headings=headings, files=files,
                         page_count=len(pages), page_heading=page_heading,
                         provenance=provenance)
        _pdf_figures(doc, model)
    finally:
        doc.close()
    return model


_CAPTION_RE = re.compile(r"^\s*(figure|fig\.|exhibit|diagram|illustration)\s*[\dA-Z]", re.I)


def _clean_caption(text: str, fallback: str) -> str:
    text = " ".join((text or "").split())
    text = text.replace("[", "(").replace("]", ")").replace("|", "/")
    if len(text) > 140:
        text = text[:139].rstrip() + "…"
    return text or fallback


def _pdf_figures(doc, model: DocModel) -> None:
    import pymupdf

    pages_of: Dict[str, set] = {}
    found: List[Tuple] = []      # (page, n, digest, data, ext, xref)
    per_xref: Dict[int, Tuple[str, bytes, str]] = {}
    for pno, page in enumerate(doc, 1):
        n = 0
        for img in page.get_images(full=True):
            xref = img[0]
            if xref not in per_xref:
                try:
                    info = doc.extract_image(xref)
                except Exception:              # noqa: BLE001
                    model.skipped_images["unreadable"] += 1
                    continue
                data, ext = info.get("image") or b"", (info.get("ext") or "").lower()
                width, height = int(info.get("width") or 0), int(info.get("height") or 0)
                if width < MIN_IMAGE_PX or height < MIN_IMAGE_PX or len(data) < MIN_IMAGE_BYTES:
                    per_xref[xref] = ("", b"", "small")
                    continue
                if ext not in ("png", "jpeg", "jpg") or int(info.get("colorspace") or 3) >= 4:
                    try:
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.n - pix.alpha >= 4:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        data, ext = pix.tobytes("png"), "png"
                    except Exception:          # noqa: BLE001
                        per_xref[xref] = ("", b"", "unconvertible")
                        continue
                per_xref[xref] = (_sha256_bytes(data), data, "jpg" if ext == "jpeg" else ext)
            digest, data, ext = per_xref[xref]
            if not digest:
                model.skipped_images[ext] += 1
                continue
            pages_of.setdefault(digest, set()).add(pno)
            n += 1
            found.append((pno, n, digest, data, ext, xref))

    seen: set = set()
    for pno, n, digest, data, ext, xref in found:
        if len(pages_of[digest]) >= DECORATIVE_PAGES:
            model.skipped_images["decorative"] += 1
            continue
        if digest in seen:
            model.skipped_images["duplicate"] += 1
            continue
        seen.add(digest)
        page = doc[pno - 1]
        caption = ""
        try:
            rects = page.get_image_rects(xref)
            if rects:
                r = rects[0]
                best = None
                for b in page.get_text("blocks"):
                    text = (b[4] or "").strip()
                    if not _CAPTION_RE.match(text):
                        continue
                    gap = b[1] - r.y1 if b[1] >= r.y1 - 2 else r.y0 - b[3]
                    if -2 <= gap <= 120 and (best is None or abs(gap) < best[0]):
                        best = (abs(gap), text.splitlines()[0] if len(text) > 160 else text)
                caption = best[1] if best else ""
        except Exception:                      # noqa: BLE001
            caption = ""
        model.figures.append(Figure(
            name=f"p{pno:03d}_{n}.{ext}", data=data,
            caption=_clean_caption(caption, f"Figure p.{pno}"),
            heading_idx=model.page_heading.get(pno, 0), page=pno))


_IMG_RE = re.compile(
    r"<(?:img|image)\b[^>]*?(?:src|xlink:href|href)\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I | re.S)
_ID_RE = re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']", re.I)
_ALT_RE = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.I)

def _image_dims(data: bytes) -> Optional[Tuple[int, int]]:
    try:
        from io import BytesIO
        from PIL import Image                 # optional; installed with the extras
        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:                          # noqa: BLE001
        pass
    try:
        import pymupdf
        pix = pymupdf.Pixmap(data)
        return pix.width, pix.height
    except Exception:                          # noqa: BLE001
        return None


def _build_epub(path: Path) -> DocModel:
    from video2mdnotes.core.documents import epub as epubx

    source = epubx.extract(path)
    headings: List[Heading] = []
    open_trail: List[str] = []
    section_heading: Dict[int, int] = {}
    for si, sec in enumerate(source.sections):
        trail = list(sec.path) or [f"Section {si + 1}"]
        # Ancestors the extractor merged away still need headings, or a
        # chapter-level citation has nothing to land on.
        common = 0
        while (common < len(open_trail) and common < len(trail) - 1
               and open_trail[common] == trail[common]):
            common += 1
        for k in range(common, len(trail) - 1):
            headings.append(Heading(idx=len(headings), title=trail[k], trail=trail[: k + 1], depth=1))
        h = Heading(idx=len(headings), title=trail[-1], trail=trail, depth=1)
        body = sec.text or ""
        lines = body.splitlines()
        if lines and _norm_title(lines[0]) == _norm_title(trail[-1]):
            body = "\n".join(lines[1:])
        text = _escape_text(body)
        if text:
            h.chunks.append(text)
        headings.append(h)
        section_heading[si] = h.idx
        open_trail = trail

    files = _assign_files(headings, _split_depth([h.trail for h in headings]))
    model = DocModel(kind="epub", title=source.title, headings=headings, files=files,
                     provenance=source.provenance)

    # Figures: <img> in spine documents, attached to the last section anchor
    # that precedes them in the same document.
    href_sections: Dict[str, List[Tuple[str, int]]] = {}
    for si, sec in enumerate(source.sections):
        raw, _, frag = (sec.href or "").partition("#")
        href_sections.setdefault(raw, []).append((frag, section_heading[si]))
    seen: set = set()
    counter: Counter = Counter()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for doc_href, secs in href_sections.items():
            if doc_href not in names:
                continue
            html = zf.read(doc_href).decode("utf-8", errors="replace")
            id_pos = {m.group(1): m.start() for m in _ID_RE.finditer(html)}
            marks = sorted(((id_pos.get(frag, 0) if frag else 0), hidx) for frag, hidx in secs)
            for m in _IMG_RE.finditer(html):
                src = m.group(1).split("#")[0]
                if src.startswith(("data:", "http:", "https:")):
                    model.skipped_images["external"] += 1
                    continue
                member = posixpath.normpath(posixpath.join(posixpath.dirname(doc_href), unquote(src)))
                if member not in names:
                    model.skipped_images["missing"] += 1
                    continue
                data = zf.read(member)
                ext = posixpath.splitext(member)[1].lower().lstrip(".") or "img"
                if len(data) < MIN_IMAGE_BYTES:
                    model.skipped_images["small"] += 1
                    continue
                if ext != "svg":
                    dims = _image_dims(data)
                    if dims and (dims[0] < MIN_IMAGE_PX or dims[1] < MIN_IMAGE_PX):
                        model.skipped_images["small"] += 1
                        continue
                digest = _sha256_bytes(data)
                if digest in seen:
                    model.skipped_images["duplicate"] += 1
                    continue
                seen.add(digest)
                hidx = marks[0][1]
                for pos, idx in marks:
                    if pos <= m.start():
                        hidx = idx
                caption = ""
                alt = _ALT_RE.search(m.group(0))
                after = html[m.end(): m.end() + 800]
                cap = re.search(r"<figcaption\b[^>]*>(.*?)</figcaption>", after, re.I | re.S) or \
                    re.search(r"<(?:p|div|span)\b[^>]*class=[\"'][^\"']*(?:caption|figure)[^\"']*[\"'][^>]*>(.*?)</(?:p|div|span)>",
                              after, re.I | re.S)
                if cap:
                    caption = re.sub(r"<[^>]+>", " ", cap.group(1))
                elif alt and alt.group(1).strip():
                    caption = alt.group(1)
                file_no = headings[hidx].file_no
                counter[file_no] += 1
                base = slugify(posixpath.splitext(posixpath.basename(member))[0], 40)
                model.figures.append(Figure(
                    name=f"ch{file_no:02d}_{counter[file_no]:02d}_{base}.{ext}", data=data,
                    caption=_clean_caption(_html.unescape(caption),
                                           f"Figure {headings[hidx].title[:60]}"),
                    heading_idx=hidx))
    return model


def _render_text_file(model: DocModel, f: TextFile, source_label: str) -> str:
    out = [f"<!-- extracted from {source_label} by {PACKAGER}; regenerated on every package run -->", ""]
    for i in f.headings:
        h = model.headings[i]
        out.append("#" * h.depth + " " + h.text_heading)
        out.append("")
        for chunk in h.chunks:
            out.append(chunk)
            out.append("")
        for fig in h.images:
            out.append(f"![{fig.caption}](../images/{link_path(fig.name)})")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Citation rewriting
# --------------------------------------------------------------------------- #

_PAGE_PART = re.compile(
    r"^(?P<pp>pp?)\.\s*(?P<a>\d+)(?:\s*[–—-]\s*(?P<b>\d+))?(?:\s*,?\s*§\s*(?P<sec>.*))?$", re.I)
_CH_PART = re.compile(r"^ch(?:apter|\.)?\s*(?P<n>\d+)(?:\s*,?\s*§\s*(?P<sec>.*))?$", re.I)
_SEC_PART = re.compile(r"^§\s*(?P<sec>.+)$")
# A bracket that is not already a link, an embed target or a wiki link.
_BRACKET = re.compile(r"(?<![!\]\[])\[(?P<body>[^\[\]\n]{1,300})\](?![(\[:])")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_TS = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
_TIME_CITE = re.compile(rf"^(?P<a>{_TS})(?:\s*(?:-|–|—|to)\s*(?P<b>{_TS}))?$")


def _parse_ts(text: str) -> float:
    parts = [float(p) for p in text.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def _protected_spans(line: str) -> List[Tuple[int, int]]:
    spans = [m.span() for m in _INLINE_CODE.finditer(line)]
    spans += [m.span() for m in _MD_LINK.finditer(line)]
    return spans


def _iter_body_lines(text: str):
    """(index, line, rewritable) — frontmatter and fenced code are left alone."""
    lines = text.split("\n")
    in_front = bool(lines) and lines[0].strip() == "---"
    in_fence = False
    for i, line in enumerate(lines):
        if in_front:
            yield i, line, False
            if i > 0 and line.strip() == "---":
                in_front = False
            continue
        if _FENCE.match(line):
            in_fence = not in_fence
            yield i, line, False
            continue
        yield i, line, not in_fence


@dataclass
class Resolution:
    links: List[str]                 # rendered replacement for each part
    linked: int = 0
    unresolved: List[str] = field(default_factory=list)
    headings: List[int] = field(default_factory=list)
    pages: List[Tuple[int, int]] = field(default_factory=list)
    any_link: bool = False


class DocResolver:
    def __init__(self, model: DocModel, notes_dir: Path, text_dir: Path,
                 binary: Optional[Path]):
        self.model = model
        self.notes_dir = notes_dir
        self.text_dir = text_dir
        self.binary = binary
        self._norm = [(_norm_title(h.title), h) for h in model.headings]

    def _heading_link(self, h: Heading) -> str:
        f = self.model.files[h.file_no - 1]
        rel = _rel(self.text_dir / f.name, self.notes_dir)
        return f"{link_path(rel)}#{obsidian_anchor(h.text_heading)}"

    def _page_link(self, page: int) -> str:
        rel = _rel(self.binary, self.notes_dir)
        return f"{link_path(rel)}#page={page}"

    def find_section(self, sec: str, chapter: Optional[int] = None,
                     pages: Optional[Tuple[int, int]] = None) -> Optional[Heading]:
        raw = (sec or "").strip()
        truncated = raw.endswith("…") or raw.endswith("...")
        want = _norm_title(raw)
        if not want:
            return None
        cands = []
        for norm, h in self._norm:
            if norm == want or (truncated and norm.startswith(want)):
                cands.append(h)
        if not cands:
            # The notes shortened the heading to its label ("Skill 1.1" for
            # "Skill 1.1: Describe security …"): a whole-word prefix, narrowed
            # below by chapter / page like any other match.
            for norm, h in self._norm:
                if norm.startswith(want + " ") and (len(want) >= 6 or chapter or pages):
                    cands.append(h)
        if chapter is not None:
            in_ch = [h for h in cands if h.chapter == chapter]
            cands = in_ch or []
        if not cands:
            return None
        if pages and self.model.kind == "pdf":
            lo, hi = pages
            inside = [h for h in cands if h.page is not None and lo <= h.page <= hi]
            if inside:
                return inside[0]
            return min(cands, key=lambda h: abs((h.page or 0) - lo))
        return cands[0]

    def chapter_heading(self, n: int) -> Optional[Heading]:
        for h in self.model.headings:
            if _chapter_no(h.title) == n:
                return h
        for h in self.model.headings:
            if h.chapter == n:
                return h
        return None

    def resolve(self, body: str) -> Optional[Resolution]:
        parts = [p.strip() for p in body.split(";")]
        parsed = []
        for p in parts:
            m = _PAGE_PART.match(p) or _CH_PART.match(p) or _SEC_PART.match(p)
            if not m:
                return None                    # not a citation: leave untouched
            parsed.append((p, m))
        res = Resolution(links=[])
        for p, m in parsed:
            gd = m.groupdict()
            rendered: List[str] = []
            ok = True
            page_range = None
            if gd.get("a"):
                a = int(gd["a"])
                b = int(gd["b"]) if gd.get("b") else a
                page_range = (a, b)
                label = p.split("§")[0].strip().rstrip(",").strip()
                if self.model.kind == "pdf" and self.binary and 1 <= a <= self.model.page_count:
                    rendered.append(f"[{label}]({self._page_link(a)})")
                    res.pages.append(page_range)
                    for pg in range(a, min(b, self.model.page_count) + 1):
                        if pg in self.model.page_heading:
                            res.headings.append(self.model.page_heading[pg])
                else:
                    rendered.append(label)
                    ok = False
            chapter = int(gd["n"]) if gd.get("n") else None
            sec = gd.get("sec")
            if sec is not None and _norm_title(sec):
                h = self.find_section(sec, chapter=chapter, pages=page_range)
                label = "§" + sec.strip()
                if chapter is not None:
                    label = f"ch. {chapter}, {label}" if "," in p else f"ch. {chapter} {label}"
                if h is not None:
                    rendered.append(f"[{label}]({self._heading_link(h)})")
                    res.headings.append(h.idx)
                else:
                    rendered.append(label)
                    ok = False
            elif chapter is not None:
                h = self.chapter_heading(chapter)
                if h is not None:
                    rendered.append(f"[ch. {chapter}]({self._heading_link(h)})")
                    res.headings.append(h.idx)
                else:
                    rendered.append(p)
                    ok = False
            elif sec is not None and not page_range:
                rendered.append(p)
                ok = False
            res.links.append(" ".join(rendered) if rendered else p)
            res.any_link = res.any_link or any("](" in r for r in rendered)
            if ok:
                res.linked += 1
            else:
                res.unresolved.append(p)
        return res


class MediaResolver:
    def __init__(self, youtube_id: Optional[str], duration_s: Optional[float],
                 transcript_ids: Optional[List[Tuple[float, str]]], notes_dir: Path,
                 transcript_path: Optional[Path]):
        self.youtube_id = youtube_id
        self.duration_s = duration_s
        self.ids = transcript_ids or []
        self.starts = [s for s, _ in self.ids]
        self.notes_dir = notes_dir
        self.transcript_path = transcript_path

    def block_for(self, t: float) -> Optional[str]:
        if not self.ids:
            return None
        import bisect
        i = bisect.bisect_right(self.starts, int(t) + 0.999) - 1
        return self.ids[max(0, i)][1]

    def resolve(self, body: str) -> Optional[Resolution]:
        m = _TIME_CITE.match(body.strip())
        if not m:
            return None
        start = _parse_ts(m.group("a"))
        res = Resolution(links=[])
        if self.duration_s and start > self.duration_s + 1:
            res.links.append(body)
            res.unresolved.append(body + " (past the end of the recording)")
            return res
        if self.youtube_id:
            res.links.append(f"[{body}](https://youtu.be/{self.youtube_id}?t={int(start)})")
            res.linked, res.any_link = 1, True
            return res
        block = self.block_for(start)
        if block and self.transcript_path is not None:
            rel = link_path(_rel(self.transcript_path, self.notes_dir))
            res.links.append(f"[{body}]({rel}#^{block})")
            res.linked, res.any_link = 1, True
            return res
        res.links.append(body)
        res.unresolved.append(body)
        return res


def rewrite_citations(notes: str, resolver) -> Tuple[str, Dict]:
    """Replace each bracketed citation with links. Returns (text, stats)."""
    lines = notes.split("\n")
    stats = {"citations": 0, "linked": 0, "unresolved": [], "by_line": {}}
    for i, line, rewritable in list(_iter_body_lines(notes)):
        if not rewritable or "[" not in line:
            continue
        protected = _protected_spans(line)
        out, last = [], 0
        for m in _BRACKET.finditer(line):
            if any(a <= m.start() < b for a, b in protected):
                continue
            res = resolver.resolve(m.group("body"))
            if res is None:
                continue
            stats["citations"] += len(res.links)
            stats["linked"] += res.linked
            for u in res.unresolved:
                stats["unresolved"].append({"line": i + 1, "citation": u,
                                            "context": line.strip()[:160]})
            if res.headings or res.pages:
                stats["by_line"].setdefault(i, {"headings": set(), "pages": []})
                stats["by_line"][i]["headings"].update(res.headings)
                stats["by_line"][i]["pages"].extend(res.pages)
            out.append(line[last:m.start()])
            if not res.any_link:
                out.append(m.group(0))         # nothing resolved: keep it verbatim
            else:
                out.append("; ".join(res.links))
            last = m.end()
        if out:
            out.append(line[last:])
            lines[i] = "".join(out)
    return "\n".join(lines), stats


# --------------------------------------------------------------------------- #
# Figures in notes.md
# --------------------------------------------------------------------------- #

def _notes_sections(lines: List[str]) -> List[Tuple[int, int]]:
    """(start, end) line ranges of the H2 sections (end exclusive)."""
    starts = [i for i, _, ok in _iter_body_lines("\n".join(lines))
              if ok and lines[i].startswith("## ")]
    if not starts:
        return [(0, len(lines))]
    bounds = starts + [len(lines)]
    return [(bounds[k], bounds[k + 1]) for k in range(len(starts))]


def embed_figures(notes: str, model: DocModel, by_line: Dict[int, Dict]) -> Tuple[str, Dict]:
    lines = notes.split("\n")
    sections = _notes_sections(lines)
    sec_heads: List[set] = []
    sec_pages: List[List[Tuple[int, int]]] = []
    sec_files: List[set] = []
    for a, b in sections:
        hs, ps = set(), []
        for i in range(a, b):
            if i in by_line:
                hs |= by_line[i]["headings"]
                ps += by_line[i]["pages"]
        sec_heads.append(hs)
        sec_pages.append(ps)
        sec_files.append({model.headings[h].file_no for h in hs if 0 <= h < len(model.headings)})

    placed: Dict[int, List[Figure]] = {}
    appendix: List[Figure] = []
    for fig in model.figures:
        target = None
        if fig.page is not None:
            target = next((k for k, ps in enumerate(sec_pages)
                           if any(lo <= fig.page <= hi for lo, hi in ps)), None)
        if target is None:
            target = next((k for k, hs in enumerate(sec_heads) if fig.heading_idx in hs), None)
        if target is None:
            fno = model.headings[fig.heading_idx].file_no if fig.heading_idx < len(model.headings) else None
            target = next((k for k, fs in enumerate(sec_files) if fno in fs), None)
        if target is None:
            appendix.append(fig)
        else:
            placed.setdefault(target, []).append(fig)

    def block(figs: List[Figure], title: str) -> List[str]:
        fold = "-" if len(figs) > FOLD_FIGURES_ABOVE else "+"
        out = [f"> [!figure]{fold} {title} ({len(figs)})"]
        for n, fig in enumerate(figs):
            if n:
                out.append(">")
            out.append(f"> ![{fig.caption}](images/{link_path(fig.name)})")
        return out

    # Insert from the bottom so earlier line numbers stay valid.
    for k in sorted(placed, reverse=True):
        a, b = sections[k]
        end = b
        while end > a + 1 and (not lines[end - 1].strip() or lines[end - 1].strip() == "---"):
            end -= 1
        tail = [""] if end < len(lines) and lines[end].strip() else []
        lines[end:end] = [""] + block(placed[k], "Figures from the source") + tail
    if appendix:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", "## Figures from the source", "",
                  "_Figures the notes do not cite a section or page for._", ""]
        lines += block(appendix, "Figures") + [""]
    stats = {"embedded": sum(len(v) for v in placed.values()) + len(appendix),
             "in_sections": sum(len(v) for v in placed.values()),
             "in_appendix": len(appendix)}
    return "\n".join(lines).rstrip() + "\n", stats


# --------------------------------------------------------------------------- #
# Transcripts (local recordings)
# --------------------------------------------------------------------------- #

_TLINE = re.compile(r"^(\s*[-*]\s*\[)(?P<ts>\d+:\d{2}:\d{2}(?:\.\d+)?)(\].*?)\s*(\^t[\w-]+)?\s*$")


def block_id(seconds: float) -> str:
    s = int(seconds)
    return f"t{s // 3600}-{(s % 3600) // 60:02d}-{s % 60:02d}"


def transcript_with_block_ids(text: str) -> Tuple[str, List[Tuple[float, str]]]:
    """Append ``^tH-MM-SS`` to the first transcript line of each second."""
    out, ids, seen = [], [], set()
    for line in text.split("\n"):
        m = _TLINE.match(line)
        if not m:
            out.append(line)
            continue
        start = _parse_ts(m.group("ts"))
        bid = block_id(start)
        base = (m.group(1) + m.group("ts") + m.group(3)).rstrip()
        if bid in seen:
            out.append(base)
            continue
        seen.add(bid)
        ids.append((float(int(start)), bid))
        out.append(f"{base} ^{bid}")
    return "\n".join(out), ids


_YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{11})")


def youtube_id(url: Optional[str]) -> Optional[str]:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

@dataclass
class PackageResult:
    package_dir: Path
    notes_path: Path
    report: Dict
    written: List[str]
    notes_written: bool
    dry_run: bool = False

    def summary(self) -> str:
        r = self.report
        c = r["citations"]
        img = r.get("images", {})
        bits = [f"{c['linked']}/{c['total']} citations linked",
                f"{c['unresolved_count']} unresolved"]
        if img:
            bits.append(f"{img.get('extracted', 0)} images extracted, {img.get('embedded', 0)} embedded")
        if not self.notes_written:
            bits.append(f"notes.md NOT written ({r.get('notes_status')})")
        return f"{self.package_dir}: " + " · ".join(bits)


_LINK_RE = re.compile(r"(?<!\\)!?\[[^\]\n]*\]\(([^)\s]+)\)")


def check_links(pkg_dir: Path) -> List[Dict]:
    """Every relative link in notes.md and text/*.md must land somewhere real:
    the file exists; ``#page=N`` is within the PDF; ``#Heading`` matches a heading
    in the target (Obsidian's rules); ``#^block`` is a block id in the target."""
    problems: List[Dict] = []
    pdf_pages: Dict[Path, int] = {}
    cache: Dict[Path, Tuple[List[str], set]] = {}

    def headings_of(path: Path):
        if path not in cache:
            text = path.read_text(encoding="utf-8")
            blocks = set(re.findall(r"\s\^([A-Za-z0-9-]+)\s*$", text, re.M))
            hs = []
            for _, line, ok in _iter_body_lines(text):
                m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
                if ok and m:
                    hs.append(m.group(2))
            cache[path] = (hs, blocks)
        return cache[path]

    files = [pkg_dir / NOTES_FILENAME] + sorted((pkg_dir / "text").glob("*.md"))
    for src in files:
        if not src.exists():
            continue
        for lineno, line in enumerate(src.read_text(encoding="utf-8").split("\n"), 1):
            for m in _LINK_RE.finditer(line):
                target = m.group(1)
                if re.match(r"^[a-z]+:", target):
                    continue                    # https:, obsidian: ...
                path_part, _, frag = target.partition("#")
                dest = (src.parent / unquote(path_part)).resolve() if path_part else src
                where = {"file": src.name, "line": lineno, "link": target}
                if not dest.exists():
                    problems.append({**where, "problem": "missing target"})
                    continue
                if not frag:
                    continue
                if frag.startswith("page="):
                    if dest not in pdf_pages:
                        try:
                            import pymupdf
                            with pymupdf.open(dest) as d:
                                pdf_pages[dest] = d.page_count
                        except Exception:       # noqa: BLE001
                            pdf_pages[dest] = 0
                    n = int(frag[5:] or 0)
                    if not 1 <= n <= pdf_pages[dest]:
                        problems.append({**where, "problem": f"page {n} outside 1..{pdf_pages[dest]}"})
                elif frag.startswith("^"):
                    if frag[1:] not in headings_of(dest)[1]:
                        problems.append({**where, "problem": "block id not found"})
                elif not any(anchor_matches(frag, h) for h in headings_of(dest)[0]):
                    problems.append({**where, "problem": "heading not found"})
    return problems


def _notes_title(text: str) -> Optional[str]:
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', text.split("\n---", 1)[0], re.M) \
        if text.startswith("---") else None
    if m and m.group(1).strip():
        return m.group(1).strip()
    for line in text.splitlines():
        if line.startswith("# "):
            return re.sub(r":\s*(?:study\s+)?notes\s*$", "", line[2:].strip(), flags=re.I)
    return None


def _find_notes(path: Path) -> Tuple[Path, Optional[Path]]:
    """(notes file, run dir or None) for a run dir or a notes file."""
    path = Path(path).expanduser()
    if path.is_file():
        run_dir = None
        if path.parent.name == "summaries" and (path.parent.parent / "source_meta.json").exists():
            run_dir = path.parent.parent
        elif (path.parent / "source_meta.json").exists():
            run_dir = path.parent
        return path, run_dir
    if path.is_dir():
        cands = sorted((path / "summaries").glob("*.summary.md")) if (path / "summaries").is_dir() else []
        cands = [c for c in cands if not c.name.startswith("no_summary_")] or cands
        if not cands:
            cands = sorted(path.glob("*.summary.md"))
        if len(cands) != 1:
            raise PackageError(f"{path}: expected one *.summary.md, found {len(cands)} — "
                               "pass the notes file itself")
        return cands[0], path
    raise PackageError(f"{path}: no such notes file or run dir")


def _insert_banner(notes: str, banner: str) -> str:
    lines = notes.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines[i + 1:i + 1] = ["", banner]
            return "\n".join(lines)
    return banner + "\n\n" + notes


def package(
    notes_or_run: Path,
    subject: str,
    source: Optional[Path] = None,
    vault: Optional[Path] = None,
    learning_root: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    slug: Optional[str] = None,
) -> PackageResult:
    """Build (or refresh) one learning package. See the module docstring."""
    from video2mdnotes.config import settings

    subject_slug = slugify(subject)
    if not subject or subject_slug != subject.strip().lower():
        raise PackageError(f"--subject must be a simple name like 'azure_identity' (got {subject!r})")
    vault = Path(vault or settings.obsidian_vault).expanduser()
    learning_root = learning_root or settings.learning_root

    notes_path, run_dir = _find_notes(Path(notes_or_run))
    _refuse_forbidden(notes_path)
    notes_raw = notes_path.read_text(encoding="utf-8")
    run_meta: Dict = {}
    if run_dir and (run_dir / "source_meta.json").exists():
        run_meta = json.loads((run_dir / "source_meta.json").read_text(encoding="utf-8"))

    kind = None
    if source is not None:
        source = Path(source).expanduser()
        _refuse_forbidden(source)
        if not source.is_file():
            raise PackageError(f"{source}: source file not found")
        ext = source.suffix.lower()
        if ext not in (".pdf", ".epub"):
            raise PackageError(f"{source}: only .pdf and .epub sources are packaged with --source")
        kind = ext.lstrip(".")
    elif run_meta:
        kind = "youtube" if youtube_id(run_meta.get("url")) else "local_media"
    else:
        raise PackageError(f"{notes_path}: no --source given and no source_meta.json next to the "
                           "run — cannot tell what the citations point at")

    title = (_notes_title(notes_raw) or run_meta.get("title")
             or (source.stem if source else notes_path.stem))
    if kind in ("youtube", "local_media") and run_meta.get("title"):
        title = run_meta["title"]
    slug = slugify(slug) if slug else slugify(title)

    pkg_dir = vault / learning_root / subject_slug / slug
    src_dir = vault / SOURCES_DIRNAME / subject_slug / slug
    text_dir = pkg_dir / "text"
    files: Dict[str, bytes] = {}           # path relative to pkg_dir -> content
    report: Dict = {"packager": PACKAGER, "kind": kind, "subject": subject_slug, "slug": slug,
                    "notes_source": str(notes_path.resolve())}
    binary_target: Optional[Path] = None
    meta: Dict = {"packager": PACKAGER, "subject": subject_slug, "slug": slug, "kind": kind,
                  "title": title, "notes_source": str(notes_path.resolve()),
                  "run_dir": str(run_dir.resolve()) if run_dir else None}

    if kind in ("pdf", "epub"):
        binary_target = src_dir / f"{slug}.{kind}"
        model = (_build_pdf(source, text_dir, binary_target) if kind == "pdf"
                 else _build_epub(source))
        resolver = DocResolver(model, pkg_dir, text_dir, binary_target)
        notes, stats = rewrite_citations(notes_raw, resolver)
        for fig in model.figures:
            if 0 <= fig.heading_idx < len(model.headings):
                model.headings[fig.heading_idx].images.append(fig)
        notes, fig_stats = embed_figures(notes, model, stats["by_line"])
        for f in model.files:
            files[f"text/{f.name}"] = _render_text_file(model, f, source.name).encode("utf-8")
        for fig in model.figures:
            files[f"images/{fig.name}"] = fig.data
        sources_rel = link_path(_rel(binary_target, pkg_dir))
        banner = (f"> **Learning package** · citations link to the extracted text in `text/` "
                  f"and to the [source {kind.upper()}]({sources_rel}) · generated by {PACKAGER}")
        report["images"] = {"extracted": len(model.figures), **fig_stats,
                            "skipped": dict(model.skipped_images)}
        report["text_files"] = len(model.files)
        report["headings"] = len(model.headings)
        report["provenance"] = model.provenance
        from video2mdnotes.core.corpus.store import fingerprint
        meta["source"] = {
            "original_path": str(source.resolve()),
            "filename": source.name,
            "fingerprint": fingerprint(source),
            "sha256": _sha256_file(source),
            "bytes": source.stat().st_size,
            "vault_copy": _rel(binary_target, vault),
            "provenance": model.provenance,
        }
    else:
        transcript_target = None
        ids = None
        if kind == "local_media":
            transcripts = sorted(p for p in (run_dir / "transcripts").glob("*.md")
                                 if not p.name.endswith(".corrected.md")) if run_dir else []
            if transcripts:
                text, ids = transcript_with_block_ids(transcripts[0].read_text(encoding="utf-8"))
                files[TRANSCRIPT_FILENAME] = text.encode("utf-8")
                transcript_target = pkg_dir / TRANSCRIPT_FILENAME
                meta["transcript_source"] = str(transcripts[0].resolve())
            else:
                report["warning"] = "no raw transcript in the run dir: time citations stay text"
        yid = youtube_id(run_meta.get("url")) if kind == "youtube" else None
        resolver = MediaResolver(yid, run_meta.get("duration_s") or run_meta.get("duration"),
                                 ids, pkg_dir, transcript_target)
        notes, stats = rewrite_citations(notes_raw, resolver)
        target = (f"YouTube (`youtu.be/{yid}?t=…`)" if yid
                  else f"the raw transcript [`{TRANSCRIPT_FILENAME}`]({TRANSCRIPT_FILENAME}) (block ids `^tH-MM-SS`)")
        banner = f"> **Learning package** · time citations link to {target} · generated by {PACKAGER}"
        meta["source"] = {"url": run_meta.get("url"), "youtube_id": yid,
                          "published_at": run_meta.get("published_at"),
                          "duration_s": run_meta.get("duration_s")}

    notes = _insert_banner(notes, banner)
    notes_bytes = notes.encode("utf-8")
    report["citations"] = {"total": stats["citations"], "linked": stats["linked"],
                           "unresolved_count": len(stats["unresolved"]),
                           "unresolved": stats["unresolved"]}

    # ---- user-edit protection ------------------------------------------------
    notes_dest = pkg_dir / NOTES_FILENAME
    meta_path = pkg_dir / META_FILENAME
    old_meta: Dict = {}
    if meta_path.exists():
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old_meta = {}
    notes_status = "written"
    write_notes = True
    if notes_dest.exists():
        current = _sha256_file(notes_dest)
        if current == _sha256_bytes(notes_bytes):
            notes_status, write_notes = "unchanged", False
        elif current != old_meta.get("notes_sha256") and not force:
            notes_status, write_notes = "user-edited (kept; use --force to overwrite)", False
    report["notes_status"] = notes_status
    meta["notes_sha256"] = (_sha256_bytes(notes_bytes) if write_notes or notes_status == "unchanged"
                            else old_meta.get("notes_sha256"))
    meta["packaged_at"] = old_meta.get("packaged_at")

    written: List[str] = []
    if dry_run:
        return PackageResult(pkg_dir, notes_dest, report, sorted(files), write_notes, dry_run=True)

    pkg_dir.mkdir(parents=True, exist_ok=True)
    changed = False

    def put(rel: str, data: bytes) -> None:
        nonlocal changed
        dest = pkg_dir / rel
        if dest.exists() and dest.read_bytes() == data:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        written.append(rel)
        changed = True

    for rel, data in sorted(files.items()):
        put(rel, data)
    # Generated folders hold only what this run produced.
    for sub in ("text", "images"):
        d = pkg_dir / sub
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and f"{sub}/{p.name}" not in files:
                    p.unlink()
                    written.append(f"-{sub}/{p.name}")
                    changed = True
    if write_notes:
        put(NOTES_FILENAME, notes_bytes)
    if binary_target is not None:
        if not binary_target.exists() or _sha256_file(binary_target) != meta["source"]["sha256"]:
            binary_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, binary_target)
            written.append(_rel(binary_target, vault))
            changed = True
    if changed or not meta["packaged_at"]:
        meta["packaged_at"] = dt.datetime.now().isoformat(timespec="seconds")
    report["broken_links"] = check_links(pkg_dir)
    put(META_FILENAME, (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    # The stored report describes the package, not this run: "written" vs
    # "unchanged" would make every rerun rewrite it.
    stored = dict(report, notes_status=("generated" if notes_status in ("written", "unchanged")
                                        else notes_status))
    put(REPORT_FILENAME, (json.dumps(stored, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    return PackageResult(pkg_dir, notes_dest, report, written, write_notes)
