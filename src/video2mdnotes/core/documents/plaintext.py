"""Plain text and Markdown → `SourceText`. No parsing to speak of, on purpose.

A `.txt` file has no structure to read — there is nothing to extract, only
text to keep. A `.md` file has exactly one piece of structure worth trusting:
ATX headings (`# `, `## `, ...), which the author wrote deliberately. Anything
fancier (setext headings, front matter, a full CommonMark parse) would be
guessing at structure the file does not actually assert, in the same spirit
that PDF's font-size heading guess is labelled `structure_is_inferred` rather
than trusted like a real bookmark.

**Markdown body text is kept verbatim, not re-rendered.** A fenced code block
in the source is already a fenced code block; running it through anything that
reformats markdown risks corrupting exactly the code samples the rest of this
pipeline works hard to preserve for EPUB and PDF. The only transformation here
is slicing the file at heading boundaries — the text inside each slice is
untouched.

**Locators have no chapter or page to point at.** A heading gives `§Title`,
matching the same convention EPUB uses for chapters with no number. A file
with no headings at all — most `.txt`, and a `.md` that is just prose — gets a
line-range locator (`L1-42`), because that is the one thing still true and
checkable about a flat file: a reader can open it and go to that line.
"""

import re
from pathlib import Path
from typing import List, Tuple

from video2mdnotes.core.sources import Section, SourceText, build_full_text

SUPPORTED = {".md", ".markdown", ".txt", ".text"}

_ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
# Some export pipelines double-hash a title ("# # Real Title") — found on a
# real vault note, likely an artifact of whatever produced it rather than
# something the author typed. A title starting with '#' is never intentional
# in real markdown, so it is stripped rather than kept "intact but ugly":
# unlike a fenced code block, a heading's OWN text is metadata, not content —
# nothing downstream needs to see the literal character.
_REDUNDANT_HASH_PREFIX = re.compile(r"^#+[ \t]*")
_LOCATOR_TITLE_CHARS = 44


def _locator_for(path: List[str]) -> str:
    if not path:
        return "unplaced"
    title = path[-1].strip()[:_LOCATOR_TITLE_CHARS].rstrip()
    if len(path[-1].strip()) > _LOCATOR_TITLE_CHARS:
        title += "…"
    return f"§{title}" if title else "unplaced"


def _split_markdown(text: str) -> List[Tuple[List[str], str, int, int]]:
    """(heading path, body, first line, last line) per ATX-delimited section.

    Line numbers are 1-indexed and inclusive, so they can be printed directly
    as a locator for a section that has no heading of its own (the leading
    text before the first heading, if any).
    """
    lines = text.splitlines()
    sections: List[Tuple[List[str], str, int, int]] = []
    trail: List[str] = []
    body_start = 0
    heading_start_line = 1

    def flush(end_line: int) -> None:
        body = "\n".join(lines[body_start:end_line]).strip("\n")
        if body.strip():
            sections.append((list(trail), body, heading_start_line, end_line))

    for i, line in enumerate(lines):
        match = _ATX_HEADING.match(line)
        if not match:
            continue
        flush(i)
        level = len(match.group(1))
        title = _REDUNDANT_HASH_PREFIX.sub("", match.group(2).strip()).strip()
        trail = trail[: level - 1] + [title]
        body_start = i + 1
        heading_start_line = i + 1

    flush(len(lines))
    return sections


def extract(path: Path) -> SourceText:
    """Read a `.md`/`.txt` file into a `SourceText`.

    Raises:
        UnicodeDecodeError: not text at all (caught by callers as a bad file,
            same as any other unreadable document — never silently coerced).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    raw = path.read_text(encoding="utf-8")
    is_markdown = path.suffix.lower() in (".md", ".markdown")

    sections: List[Section] = []
    if is_markdown:
        for heading_path, body, first_line, last_line in _split_markdown(raw):
            locator = (
                _locator_for(heading_path) if heading_path
                else f"L{first_line}-{last_line}"
            )
            sections.append(Section(
                path=heading_path or [path.stem],
                locator=locator,
                text=body,
                word_count=len(body.split()),
                href=f"line={first_line}",
            ))
    else:
        body = raw.strip()
        if body:
            total_lines = raw.count("\n") + (0 if raw.endswith("\n") else 1)
            sections.append(Section(
                path=[path.stem],
                locator=f"L1-{total_lines}",
                text=body,
                word_count=len(body.split()),
                href="line=1",
            ))

    if not sections:
        raise ValueError(f"{path.name}: empty file, nothing to summarize")

    # One shared check feeding both fields, after they diverged once already:
    # provenance said "markdown (headings)" while structure_is_inferred was
    # computed from `is_markdown` alone — true for every .md file regardless
    # of whether real headings were actually found, including one with none.
    has_real_headings = is_markdown and any(s.path != [path.stem] for s in sections)
    if has_real_headings:
        provenance = "markdown (headings)"
    elif is_markdown:
        provenance = "markdown (none)"
    else:
        provenance = "text (lines)"

    return SourceText(
        source_id=path.stem,
        title=path.stem,
        kind="document",
        origin=str(path.resolve()),
        language="unknown",
        full_text=build_full_text(sections),
        sections=sections,
        provenance=provenance,
        extractor="plaintext.extract/1",
        structure_is_inferred=not has_real_headings,
    )
