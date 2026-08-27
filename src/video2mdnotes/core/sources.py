"""The canonical input to summarization, whatever the material was.

Until now the pipeline had exactly one way in: audio → Whisper (or a caption
track) → `TranscriptResult` → summary. That worked because every source was a
video. A book is not a video, and forcing one through a type named "transcript"
would mean lying about provenance in the one place a reader most needs the
truth.

`SourceText` is the shared shape both families produce. The split that matters
is not media-vs-document but **how you point at a claim**:

    media     segments[]   "at 04:12"        time is the locator
    document  sections[]   "ch. 3, §Indexing"  structure is the locator

Both lists are optional and a source fills in whichever it actually has. That
is deliberate: a document has no timeline, and the visual-extraction plugin
keys off `segments` being empty, so a book skips the frame path by having
nothing to key on rather than by a special case somewhere in main().

Nothing here reaches the network, reads a config value, or calls a model. It is
a description of text that was obtained somehow, plus honest metadata about
how — which is what lets a summary built on it be audited later.
"""

import datetime as dt
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Section(BaseModel):
    """One structural unit of a document: a chapter, or a section within one.

    `locator` is what a citation prints. It is a *string* rather than a page
    number because the honest answer differs by format — an EPUB genuinely has
    no pages, and inventing one would fabricate precision the file cannot
    support. A PDF says "p. 214", an EPUB says "ch. 7", and a reader can tell
    which they were given.
    """

    path: List[str] = Field(default_factory=list)   # ["Part I", "3. Indexing"]
    locator: str = ""                                # "ch. 3" | "p. 214"
    text: str = ""
    word_count: int = 0
    # Where in the container this came from, for re-extraction and debugging.
    href: Optional[str] = None

    @property
    def title(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def depth(self) -> int:
        return len(self.path)


class Segment(BaseModel):
    """One timed unit of a media source. Mirrors transcriber.Segment."""

    start: float
    end: float
    text: str


class SourceText(BaseModel):
    """Text plus the truth about where it came from.

    `provenance` and `extractor` are separate on purpose. Provenance answers
    "what kind of evidence is this" — a human-authored caption track and
    Whisper output are not interchangeable, and neither are a publisher's XHTML
    and a heuristic guess at a PDF's chapter breaks. `extractor` answers "which
    code produced it", which is what you need when a regression appears and you
    have to know whether the file changed or the parser did.
    """

    source_id: str
    title: str
    kind: Literal["media", "document"]
    origin: str                       # URL or absolute file path
    language: str = "unknown"
    full_text: str = ""

    segments: List[Segment] = Field(default_factory=list)
    sections: List[Section] = Field(default_factory=list)

    # "whisper" | "captions (manual, en)" | "epub (toc.ncx)" | "pdf (heuristic)"
    provenance: str = "unknown"
    extractor: str = ""
    captured_at: dt.datetime = Field(default_factory=dt.datetime.now)

    # Set when a structural guess was made rather than read from the file, so a
    # downstream reader can discount the locators accordingly.
    structure_is_inferred: bool = False

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())

    @property
    def has_timeline(self) -> bool:
        """Whether time-based features (frames, visuals, dwell) can apply."""
        return bool(self.segments)

    @classmethod
    def from_transcript(cls, transcript) -> "SourceText":
        """Adapt an existing `TranscriptResult` without disturbing it.

        The media path is the one part of this system that demonstrably works
        end to end, and an overnight refactor of it would be trading a working
        feature for tidiness. Adapting at the boundary gets the seam in place
        while leaving `transcriber.py` untouched; collapsing the two types can
        happen later, under a test suite that already covers both.
        """
        return cls(
            source_id=Path(transcript.source_file).stem,
            title=getattr(transcript, "title", "") or Path(transcript.source_file).stem,
            kind="media",
            origin=str(transcript.source_file),
            language=transcript.language,
            full_text=transcript.full_text,
            segments=[
                Segment(start=s.start, end=s.end, text=s.text)
                for s in transcript.segments
            ],
            provenance=getattr(transcript, "transcript_source", "whisper"),
            extractor=transcript.model_name,
            captured_at=transcript.generated_at,
        )


def build_full_text(sections: List[Section]) -> str:
    """Flatten a section tree into readable prose with headings preserved.

    Headings stay in the text rather than being stripped, because they are the
    cheapest signal a summarizer gets about what a passage is *for*, and they
    are what makes a locator verifiable — a reader can find "§Indexing" in the
    output and match it to the book.
    """
    parts: List[str] = []
    last_path: List[str] = []
    for sec in sections:
        # Only print the part of the heading path that changed, so a book with
        # 200 sections does not repeat "Part I > Chapter 3" 200 times.
        for depth, crumb in enumerate(sec.path):
            if depth >= len(last_path) or last_path[depth] != crumb:
                parts.append(f"{'#' * (depth + 1)} {crumb}")
        last_path = sec.path
        if sec.text.strip():
            parts.append(sec.text.strip())
    return "\n\n".join(parts)
