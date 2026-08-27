"""Group a document's sections into passages a model can actually read.

A 136,000-word book does not fit in one request, and cutting it every N words
would slice mid-argument and produce summaries that cite the wrong chapter.
Two rules follow from that:

**Never split across a top-level boundary.** A chunk may hold several sections
of one chapter; it may not straddle two chapters. That is what keeps every
claim attributable — a chunk has exactly one chapter locator, so a citation
derived from it cannot be wrong about which chapter it came from.

**Only split a section internally as a last resort**, when one section alone
exceeds the budget. Then split on paragraph boundaries and say so, because the
second half of a split section is missing the context of the first and a reader
should know that before trusting a claim drawn from it.

Word count, not tokens: the count is a *budgeting* device, and a tokenizer
would add a dependency and a model-specific assumption to a number that only
needs to be conservative. English prose runs ~1.3 tokens/word, code more, so
the default leaves generous headroom.
"""

import re
from typing import Iterable, List, Optional

from pydantic import BaseModel, Field

from video2mdnotes.core.sources import Section

# Conservative: ~1.3 tokens/word for prose, higher for code, and the summary
# prompt plus the model's own output have to fit alongside this.
DEFAULT_MAX_WORDS = 12_000
MIN_TAIL_WORDS = 400        # below this, a trailing chunk is merged backwards


class Chunk(BaseModel):
    """A contiguous run of sections small enough to summarize in one call."""

    index: int = 0
    total: int = 0
    sections: List[Section] = Field(default_factory=list)
    # Set when a single oversized section had to be cut internally.
    is_partial: bool = False
    part: Optional[str] = None            # "2 of 3"

    @property
    def word_count(self) -> int:
        return sum(s.word_count for s in self.sections)

    @property
    def locator(self) -> str:
        """The one locator every claim in this chunk can honestly carry.

        Section locators are now "ch. 7 §Indexing" / "pp. 61–62 §Indexing", so
        naive joining produced "ch. 1–ch. 1 §References" — which names one
        chapter twice and one arbitrary section. A chunk spans several
        sections, so the honest answer is the *span*: the chapter (or page
        range) it sits in, and the first and last section inside it.
        """
        seen = [s.locator for s in self.sections if s.locator]
        if not seen:
            return "unplaced"
        if len(seen) == 1:
            return seen[0]

        def split(loc):
            head, _, title = loc.partition(" §")
            return head.strip(), title.strip()

        first_head, first_title = split(seen[0])
        last_head, last_title = split(seen[-1])

        # Page spans collapse to their numeric endpoints.
        page = re.compile(r"^pp?\.\s*(\d+)(?:\s*[–-]\s*(\d+))?$")
        start, end = page.match(first_head), page.match(last_head)
        if start and end:
            low = start.group(1)
            high = end.group(2) or end.group(1)
            head = f"p. {low}" if low == high else f"pp. {low}–{high}"
        elif first_head == last_head:
            head = first_head
        else:
            head = f"{first_head}–{last_head}"

        # Only name sections when BOTH ends are known. Showing one end alone
        # ("ch. 1 §References") reads as though the chunk IS that section when
        # it actually spans the whole chapter — a locator that points somewhere
        # real but wrong, which is worse than a coarser honest one.
        if first_title and last_title:
            if first_title == last_title:
                return f"{head} §{first_title}"
            return f"{head} §{first_title} – §{last_title}"
        return head

    @property
    def heading_path(self) -> List[str]:
        """Longest heading prefix shared by every section in the chunk."""
        if not self.sections:
            return []
        shared = list(self.sections[0].path)
        for sec in self.sections[1:]:
            keep = 0
            for a, b in zip(shared, sec.path):
                if a != b:
                    break
                keep += 1
            shared = shared[:keep]
        return shared

    def to_prompt_text(self) -> str:
        """Render for the model, with a locator on EVERY section.

        The chunk's own locator is deliberately not the only one shown. A chunk
        can hold 19 sections; printing just `[ch. 2]` at the top meant the model
        had exactly one locator available and cited it for everything — which is
        what it did, faithfully, producing 239 bare `[ch. N]` citations from a
        book with 249 distinct section locators.

        Improving `Section.locator` did nothing on its own because the improved
        value never reached the model. Each section now carries its own locator
        inline, so a claim can be cited where it actually came from.
        """
        lines: List[str] = []
        if self.heading_path:
            lines.append(f"Passage span: [{self.locator}]  "
                         f"({' > '.join(self.heading_path)})")
        if self.is_partial:
            lines.append(
                f"(Continued passage, part {self.part} — earlier text of this "
                "section is not shown.)"
            )
        lines.append(
            "Each section below is preceded by ITS OWN locator in brackets. "
            "Cite the locator of the section a claim came from."
        )
        last: List[str] = []
        for sec in self.sections:
            for depth, crumb in enumerate(sec.path):
                if depth >= len(last) or last[depth] != crumb:
                    lines.append(f"\n{'#' * (depth + 1)} {crumb}")
            last = sec.path
            lines.append(f"[{sec.locator}]")
            lines.append(sec.text)
        return "\n".join(lines).strip()


# Front matter that costs a model call and returns nothing a reader wants.
# Preface, Introduction and Foreword are deliberately absent — they routinely
# state the book's thesis and audience, which is exactly what a summary needs.
_FRONT_MATTER = re.compile(
    r"^\s*(copyright|dedication|praise\b|title page|cover|colophon|"
    r"about the (author|technical reviewer|editor)|acknowledg|"
    r"table of contents|contents|index|document \d+|halftitle|frontmatter)",
    re.I,
)


def is_front_matter(section: Section) -> bool:
    """Whether a section is publisher boilerplate rather than the book."""
    if not section.path:
        return False
    return bool(_FRONT_MATTER.match(section.path[-1]) or
                _FRONT_MATTER.match(section.path[0]))


def drop_front_matter(sections: List[Section]) -> List[Section]:
    """Remove boilerplate that appears *before* the body begins.

    Bounded to the run before the first real chapter on purpose: an "Index" at
    the back is boilerplate, but a mid-book section whose title happens to
    start with "Contents" may not be, and dropping body text to save a few
    cents is the wrong trade.
    """
    first_body = next(
        (i for i, s in enumerate(sections) if not is_front_matter(s)), len(sections)
    )
    return sections[first_body:]


def _split_oversized(section: Section, max_words: int) -> List[Section]:
    """Cut one too-large section on paragraph boundaries."""
    paragraphs = re.split(r"\n\s*\n", section.text)
    out: List[Section] = []
    buf: List[str] = []
    count = 0
    for para in paragraphs:
        words = len(para.split())
        if buf and count + words > max_words:
            text = "\n\n".join(buf)
            out.append(section.model_copy(update={
                "text": text, "word_count": len(text.split())}))
            buf, count = [], 0
        buf.append(para)
        count += words
    if buf:
        text = "\n\n".join(buf)
        out.append(section.model_copy(update={
            "text": text, "word_count": len(text.split())}))
    return out or [section]


def chunk_sections(
    sections: Iterable[Section],
    max_words: int = DEFAULT_MAX_WORDS,
    skip_front_matter: bool = True,
) -> List[Chunk]:
    """Group sections into chunks that never straddle a top-level boundary."""
    sections = [s for s in sections if s.text.strip()]
    if skip_front_matter:
        sections = drop_front_matter(sections)
    if not sections:
        return []

    chunks: List[Chunk] = []
    current: List[Section] = []
    current_top: Optional[str] = None
    count = 0

    def flush() -> None:
        nonlocal current, count
        if current:
            chunks.append(Chunk(sections=current))
            current, count = [], 0

    for sec in sections:
        top = sec.path[0] if sec.path else None

        # A new chapter always starts a new chunk, even if the last one had room.
        if current and top != current_top:
            flush()
        current_top = top

        if sec.word_count > max_words:
            flush()
            pieces = _split_oversized(sec, max_words)
            for n, piece in enumerate(pieces, 1):
                chunks.append(Chunk(
                    sections=[piece],
                    is_partial=len(pieces) > 1,
                    part=f"{n} of {len(pieces)}" if len(pieces) > 1 else None,
                ))
            continue

        if count + sec.word_count > max_words:
            flush()
        current.append(sec)
        count += sec.word_count

    flush()

    # A tiny trailing chunk costs a whole model call to summarize a fragment.
    # Merge it backwards when that does not cross a chapter boundary and does
    # not blow the budget.
    if len(chunks) >= 2:
        tail, prev = chunks[-1], chunks[-2]
        same_chapter = (
            tail.sections and prev.sections
            and tail.sections[0].path[:1] == prev.sections[0].path[:1]
        )
        if (tail.word_count < MIN_TAIL_WORDS and not tail.is_partial
                and not prev.is_partial and same_chapter
                and prev.word_count + tail.word_count <= max_words):
            prev.sections.extend(tail.sections)
            chunks.pop()

    for i, chunk in enumerate(chunks):
        chunk.index, chunk.total = i + 1, len(chunks)
    return chunks
