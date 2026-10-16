"""Turn a long media transcript into time-ranged sections: the structure ladder.

A 12-hour course cannot be summarised in one call, and cutting it every N
words would slice mid-explanation and make every timestamp citation a guess.
This module decides *where* the cuts go, and says honestly how it decided.

The ladder, best evidence first (the first rung that yields wins):

    platform     meta.chapters, chapter_source == "platform"     author-declared
    description  meta.chapters, chapter_source == "description"  author-declared
    outline      a model's outline of a compressed transcript    inferred
                 (talks / topic shifts with real titles; core/outline.py
                 makes the call, this module only validates and snaps it)
    cues         discourse cues ("Okay, so", "Let's take a look") inferred
                 combined with pauses, constrained to min..max length
    windows      fixed ~25-minute windows, word-bounded           inferred

`structure_is_inferred` is True for the last three. Outline titles are a
model's guess from a compressed transcript; cue/window titles are positional
("Section 3"). Neither is the author's, and a reader should discount them.

The outline rung is optional: it runs only when the caller passes `outline`
entries (no model is called here). They are validated deterministically —
monotonic, inside the media, each snapped to a caption-line start within
±OUTLINE_SNAP_S preferring talk-transition lines — and any failure falls back
to `cues`, with the reason in the provenance.

Three invariants, all tested:

* **Coverage.** Every segment lands in exactly one section, in order. Speech
  before the first chapter becomes a leading "Preamble" section — never dropped.
* **Never mid-line.** Cuts fall only between caption lines. A section's text is
  its segments' text, one caption line per text line.
* **Budget.** A chapter over `max_words` is split into parts ("part 2/3"), each
  with its own time range and the chapter's `group_id`, preferring a cue line or
  the largest pause near the target split point. (A single caption line longer
  than the budget cannot be split without cutting it, so it is left whole.)

No overlap is added between sections: overlap is a prompt concern (WP-3).
Nothing here calls a model, reads config or touches the network. An optional
`boundary_scorer` hook lets a caller add e.g. an embedding topic-shift score to
candidate boundaries; the deterministic path never needs it. (The local
embedder in core/corpus/embed.py loads a sentence-transformers model, which is
not cheap enough to make a default here.)
"""

import math
import re
from typing import Callable, List, Literal, NamedTuple, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from video2mdnotes.core.chunking import DEFAULT_MAX_WORDS
from video2mdnotes.core.metadata import SourceMeta, format_duration
from video2mdnotes.core.sources import Chapter, Section, Segment

Rung = Literal["platform", "description", "outline", "cues", "windows", "empty"]
Mode = Literal["auto", "outline", "cues", "windows"]
MODES = ("auto", "outline", "cues", "windows")

PREAMBLE_TITLE = "Preamble"

# Discourse cues that open a new topic in spoken teaching. Matched at the start
# of a caption line, after optional fillers ("um,", "uh,").
_CUE_RE = re.compile(
    r"^(?:(?:um|uh|so|and)[,.]?\s+)*"
    r"(?:all\s*right[.,!]?\s+so\b|alright[.,!]?\s+so\b|okay[.,]?\s+so\b|"
    r"let'?s\s+take\s+a\s+look|let'?s\s+go\s+(?:ahead\s+and\s+)?(?:take\s+a\s+look|look)|"
    r"next\b|in\s+this\s+(?:section|video|lesson|chapter)|moving\s+on|"
    r"now\s+let'?s|so\s+now\b|let'?s\s+(?:move|talk|get)\s+(?:on|about|into|started))",
    re.IGNORECASE,
)
# The same cue right after a sentence end inside a line ("...done. Okay, so").
_MIDLINE_CUE_RE = re.compile(
    r"[.!?]\s+(?:all\s*right[.,!]?\s+so\b|okay[.,]?\s+so\b|let'?s\s+take\s+a\s+look|"
    r"moving\s+on|in\s+this\s+(?:section|video|lesson))",
    re.IGNORECASE,
)

# Talk-transition lines for snapping outline boundaries. An OPENER starts the
# new segment (cut AT it: the host's introduction belongs to the talk); a
# CLOSER ends the previous one (cut AFTER it).
_OPENER_RE = re.compile(
    r"\b(?:my\s+name\s+is|next\s+up|up\s+next|please\s+welcome|"
    r"(?:let'?s\s+)?give\s+it\s+up\s+for|without\s+further\s+ado|"
    r"(?:our|the)\s+next\s+(?:speaker|talk|presenter|presentation)|"
    r"i'?m\s+(?:\w+\s+){1,3}(?:from|with|at)\b|i\s+work\s+(?:at|for)\b|"
    r"come\s+on\s+up|welcome\s+(?:to\s+the\s+stage|back)|on\s+(?:to\s+)?the\s+stage|"
    r"let\s+me\s+introduce|(?:intro|introduction)\s+(?:about|to|for)\b|"
    # Not a bare "any questions": hosts say "ask any questions after" mid-talk.
    r"(?:we\s+have\s+time\s+for|let'?s\s+(?:take|open\s+(?:it\s+)?up\s+(?:to|for)))"
    r"\s+(?:a\s+few\s+|some\s+)?questions)",
    re.IGNORECASE,
)
_CLOSER_RE = re.compile(
    r"(?:\bthank\s+you\b|\bthanks\s+(?:so\s+much|everyone|everybody)\b|"
    r"\bapplause\b|\[(?:applause|laughter|music)\]|\(applause\))",
    re.IGNORECASE,
)

OUTLINE_SNAP_S = 60.0             # an outline start must land within this of a line
# Seconds of distance that cost one point of transition score when snapping:
# an opener (2 points) wins up to ~30 s from the model's time, the line after
# a "thank you" (1.5) up to ~22 s. The model copies its times from the
# compressed transcript, so an exact line is evidence too.
OUTLINE_SNAP_COST_S = 15.0
OUTLINE_MIN_SECTIONS = 2
OUTLINE_MAX_SECTIONS = 15
OUTLINE_MIN_SECTION_S = 60.0      # anything shorter is a degenerate outline

# Speech-rate estimate used when a segment's `end` is not informative (caption
# ends often overlap the next line). ~150 wpm.
SECONDS_PER_WORD = 0.4

DEFAULT_PAUSE_S = 1.5             # a cue needs at least this pause to qualify
DEFAULT_MIN_SECTION_S = 8 * 60
DEFAULT_MAX_SECTION_S = 30 * 60
DEFAULT_WINDOW_S = 25 * 60
SPLIT_TOLERANCE = 0.15            # search ±15 % of a part's size around a target
MAX_FORCED_CUE_FRACTION = 0.5     # above this, cue rung is rejected for windows

BoundaryScorer = Callable[[Sequence[Segment], int], float]


class OutlineEntry(BaseModel):
    """One section start proposed by a model outline (see core/outline.py)."""

    start_s: float
    title: str
    speaker: Optional[str] = None


class StructureResult(BaseModel):
    """Sections plus the honest account of how they were found."""

    sections: List[Section] = Field(default_factory=list)
    rung: Rung = "empty"
    structure_is_inferred: bool = False
    provenance: str = ""
    # Half-open segment index range [i, j) for each section, parallel to
    # `sections`, so a caller can re-render per-line timestamps.
    segment_spans: List[Tuple[int, int]] = Field(default_factory=list)
    # Why a higher rung was tried and not used (e.g. "outline rejected: ...").
    fallback_reason: str = ""

    def groups(self) -> List[List[Section]]:
        """Sections grouped by `group_id`, in order. Parts of one chapter are
        sequential within a group; groups are independent of each other."""
        out: List[List[Section]] = []
        for sec in self.sections:
            if out and out[-1][0].group_id == sec.group_id:
                out[-1].append(sec)
            else:
                out.append([sec])
        return out


# --- small helpers -----------------------------------------------------------


def fmt_hms(seconds: Optional[float]) -> str:
    return format_duration(max(0.0, seconds or 0.0))


def time_locator(start_s: float, end_s: float) -> str:
    return f"{fmt_hms(start_s)}–{fmt_hms(end_s)}"


def _words(seg: Segment) -> int:
    return len(seg.text.split())


def pause_before(segments: Sequence[Segment], i: int) -> float:
    """Silence before segment i, in seconds (0 for the first segment).

    A segment's `end` is trusted only when it ends before the speech-rate
    estimate would; caption `end`s routinely run into the next line, and
    parsed transcripts often have `end == next.start`.
    """
    if i <= 0:
        return 0.0
    prev = segments[i - 1]
    est_end = prev.start + _words(prev) * SECONDS_PER_WORD
    end = min(prev.end, est_end) if prev.end > prev.start else est_end
    return max(0.0, segments[i].start - end)


def cue_strength(text: str) -> float:
    """2 for a line opening with a discourse cue, 1 for one mid-line, else 0."""
    stripped = text.strip()
    if _CUE_RE.match(stripped):
        return 2.0
    if _MIDLINE_CUE_RE.search(stripped):
        return 1.0
    return 0.0


def _boundary_score(segments, i, scorer: Optional[BoundaryScorer]) -> float:
    score = cue_strength(segments[i].text) + min(pause_before(segments, i), 10.0) / 5.0
    if i > 0:
        prev = segments[i - 1].text
        # Prefer starting a part on a sentence start, not mid-sentence...
        if prev.rstrip().endswith((".", "!", "?")):
            score += 0.5
        # ...but never right after a topic opener, which belongs with its topic.
        if _CUE_RE.match(prev.strip()):
            score -= 1.5
    if scorer is not None:
        score += scorer(segments, i)
    return score


def _make_section(segments, i, j, path, start_s, end_s, group_id, part, parts) -> Section:
    text = "\n".join(s.text.strip() for s in segments[i:j])
    return Section(
        path=list(path),
        locator=time_locator(start_s, end_s),
        text=text,
        word_count=len(text.split()),
        start_s=start_s,
        end_s=end_s,
        group_id=group_id,
        part=part,
        parts=parts,
    )


# --- oversize splitting -------------------------------------------------------


def split_points(
    segments: Sequence[Segment],
    i: int,
    j: int,
    max_words: int,
    scorer: Optional[BoundaryScorer] = None,
) -> List[int]:
    """Segment indices (exclusive of i, j) at which to cut [i, j) into parts
    of at most `max_words`, each cut at a caption-line boundary.

    Parts are balanced: n = ceil(words / max_words), each cut aimed at an even
    share of what remains and moved to the best cue/pause line within
    ±SPLIT_TOLERANCE of that target, without letting any part exceed the budget.
    """
    cuts: List[int] = []
    start = i
    while True:
        words = [_words(s) for s in segments[start:j]]
        remaining = sum(words)
        n = math.ceil(remaining / max_words) if max_words > 0 else 1
        if n <= 1 or j - start <= 1:
            break
        target = remaining / n
        lo = max(target * (1 - SPLIT_TOLERANCE), remaining - (n - 1) * max_words)
        hi = min(target * (1 + SPLIT_TOLERANCE), max_words)
        best, best_key = None, None
        cum = 0
        for k in range(start, j):
            # cutting before k leaves `cum` words in this part
            if k > start and lo <= cum <= hi:
                distance = abs(cum - target) / max(target, 1)
                key = (_boundary_score(segments, k, scorer) - distance, -distance)
                if best_key is None or key > best_key:
                    best, best_key = k, key
            cum += words[k - start]
            if cum > hi:
                if best is None:
                    # No line ends inside the window (very long lines): cut at
                    # the last boundary that keeps the part within budget, or
                    # after one line if even that line alone is over budget.
                    best = k if k > start and cum - words[k - start] <= max_words else k + 1
                break
        if best is None or best >= j:
            break
        cuts.append(best)
        start = best
    return cuts


def _emit_group(
    out: List[Section],
    spans: List[Tuple[int, int]],
    segments: Sequence[Segment],
    i: int,
    j: int,
    title: str,
    start_s: float,
    end_s: float,
    group_id: str,
    max_words: int,
    scorer: Optional[BoundaryScorer],
) -> None:
    """Append one chapter as one section, or as `part k/n` sections."""
    cuts = split_points(segments, i, j, max_words, scorer) if j > i else []
    bounds = [i, *cuts, j]
    n = len(bounds) - 1
    for k in range(n):
        a, b = bounds[k], bounds[k + 1]
        part_start = start_s if k == 0 else segments[a].start
        part_end = end_s if k == n - 1 else segments[b].start
        path = [title] if n == 1 else [title, f"part {k + 1}/{n}"]
        out.append(_make_section(segments, a, b, path, part_start, part_end,
                                 group_id, k + 1, n))
        spans.append((a, b))


# --- rungs --------------------------------------------------------------------


def _media_end(segments: Sequence[Segment], meta: Optional[SourceMeta]) -> float:
    last = max((s.end for s in segments), default=0.0)
    duration = float(meta.duration_s) if meta is not None and meta.duration_s else 0.0
    return max(duration, last)


def _from_chapters(segments, chapters: List[Chapter], end_of_media, max_words, scorer):
    """Assign each segment to the chapter whose start it falls at/after."""
    chapters = sorted(chapters, key=lambda c: c.start_s)
    out: List[Section] = []
    spans: List[Tuple[int, int]] = []
    starts = [c.start_s for c in chapters]

    def first_at_or_after(t: float, frm: int) -> int:
        k = frm
        while k < len(segments) and segments[k].start < t:
            k += 1
        return k

    idx = first_at_or_after(starts[0], 0)
    if idx > 0:
        _emit_group(out, spans, segments, 0, idx, PREAMBLE_TITLE, 0.0, starts[0],
                    "ch00", max_words, scorer)
    for n, chapter in enumerate(chapters, 1):
        nxt = starts[n] if n < len(chapters) else None
        j = first_at_or_after(nxt, idx) if nxt is not None else len(segments)
        # Ranges are contiguous (next chapter's start), so the skeleton has no
        # holes; the last chapter runs to the end of the media.
        end_s = nxt if nxt is not None else end_of_media
        _emit_group(out, spans, segments, idx, j, chapter.title or f"Chapter {n}",
                    chapter.start_s, end_s, f"ch{n:02d}", max_words, scorer)
        idx = j
    return out, spans


def _cut_greedy(segments, max_words, min_s, max_s, pause_s, scorer, *, cues: bool):
    """Greedy boundaries for inferred structure.

    cues=True: in each [start+min_s, start+max_s] window pick the best line that
    has a cue AND a pause ≥ pause_s; if none qualifies, cut at the best-scoring
    line anyway and count it as forced. cues=False: cut at the best pause near
    start+max_s (fixed windows). Both stay within `max_words`.
    Returns (cuts, forced_count).
    """
    cuts: List[int] = []
    forced = 0
    start = 0
    total = len(segments)
    while start < total:
        t0 = segments[start].start
        if segments[-1].start - t0 < (min_s if cues else max_s) * (1.0 if cues else 0.5):
            break
        cand: List[Tuple[float, int, bool]] = []
        cum = 0
        lo_t = t0 + (min_s if cues else max_s - 120)
        hi_t = t0 + (max_s if cues else max_s + 120)
        for k in range(start, total):
            if k > start:
                t = segments[k].start
                if t > hi_t or cum > max_words:
                    break
                if t >= lo_t:
                    cue = cue_strength(segments[k].text)
                    pause = pause_before(segments, k)
                    qualifies = cue >= 1.0 and pause >= pause_s
                    score = _boundary_score(segments, k, scorer)
                    if not cues:
                        score = min(pause, 10.0) + (0.5 if cue else 0.0) - abs(t - t0 - max_s) / 600
                    cand.append((score, k, qualifies))
            cum += _words(segments[k])
        else:
            break  # reached the end of the media inside this window
        if cues:
            good = [c for c in cand if c[2]]
            pick = max(good) if good else (max(cand) if cand else None)
            if not good:
                forced += 1
        else:
            pick = max(cand) if cand else None
        if pick is None:
            # The word budget filled before min_s: cut where it filled.
            cum, k = 0, start
            while k < total and cum + _words(segments[k]) <= max_words:
                cum += _words(segments[k])
                k += 1
            k = max(k, start + 1)
            if k >= total:
                break
            cuts.append(k)
            forced += 1
            start = k
            continue
        cuts.append(pick[1])
        start = pick[1]
    return cuts, forced


def _from_cuts(segments, cuts, label, end_of_media, max_words, scorer, prefix):
    bounds = [0, *cuts, len(segments)]
    out: List[Section] = []
    spans: List[Tuple[int, int]] = []
    n = len(bounds) - 1
    for k in range(n):
        a, b = bounds[k], bounds[k + 1]
        start_s = 0.0 if k == 0 else segments[a].start
        end_s = end_of_media if k == n - 1 else segments[b].start
        _emit_group(out, spans, segments, a, b, f"{label} {k + 1}", start_s,
                    end_s, f"{prefix}{k + 1:02d}", max_words, scorer)
    return out, spans


# --- outline rung ----------------------------------------------------------------


def is_opener(text: str) -> bool:
    return bool(_OPENER_RE.search(text))


def is_closer(text: str) -> bool:
    return bool(_CLOSER_RE.search(text))


def transition_score(segments: Sequence[Segment], k: int) -> float:
    """How much line k looks like the first line of a new talk/segment.

    2 for an opener ("my name is", "please welcome"), 1.5 for the line right
    after a closer ("thank you", applause), plus up to 1 for the pause before.
    0 for an ordinary line with no pause.
    """
    score = 0.0
    if is_opener(segments[k].text):
        score += 2.0
    if k > 0 and is_closer(segments[k - 1].text) and not is_closer(segments[k].text):
        score += 1.5
    return score + min(pause_before(segments, k), 10.0) / 10.0


class SnappedOutline(NamedTuple):
    entries: List[OutlineEntry]     # after merging; parallel to `indices`
    indices: Optional[List[int]]    # None: outline rejected
    reason: str = ""
    on_transition: int = 0          # starts that landed on a transition line
    merged: List[str] = []          # titles folded into the previous section


def snap_outline(
    segments: Sequence[Segment],
    entries: Sequence[OutlineEntry],
    end_of_media: float,
    *,
    window_s: float = OUTLINE_SNAP_S,
) -> SnappedOutline:
    """Validate an outline and snap each start to a caption-line index.

    Rejected (indices None, `reason` says why): wrong section count, start
    times not strictly increasing, a start outside the media, an empty title,
    or no caption line within ±window_s of a start. Each start snaps to the
    best line in that window: transition score minus distance (one point per
    OUTLINE_SNAP_COST_S), ties to the nearest. A section that ends up shorter than
    OUTLINE_MIN_SECTION_S after snapping (a 30-second "announcement") is
    folded into the section before it — not worth a passage of its own, and
    no reason to throw the rest of the outline away.
    """
    entries = list(entries)
    n = len(entries)
    if not (OUTLINE_MIN_SECTIONS <= n <= OUTLINE_MAX_SECTIONS):
        return SnappedOutline(entries, None, f"{n} sections (need "
                              f"{OUTLINE_MIN_SECTIONS}–{OUTLINE_MAX_SECTIONS})")
    starts = [e.start_s for e in entries]
    for a, b in zip(starts, starts[1:]):
        if b <= a:
            return SnappedOutline(entries, None, f"start times not increasing "
                                  f"({fmt_hms(a)} then {fmt_hms(b)})")
    if starts[0] < 0 or starts[-1] >= end_of_media:
        return SnappedOutline(entries, None,
                              f"start time outside the media (0–{fmt_hms(end_of_media)})")
    for e in entries:
        if not e.title.strip():
            return SnappedOutline(entries, None, f"empty title at {fmt_hms(e.start_s)}")
    seg_starts = [s.start for s in segments]
    indices: List[int] = []
    for e in entries:
        if not indices and e.start_s <= seg_starts[0] + window_s:
            # An outline that opens at (or near) the top covers the start of
            # the media; no preamble is invented for a few seconds of chatter.
            indices.append(0)
            continue
        cand = [k for k, t in enumerate(seg_starts) if abs(t - e.start_s) <= window_s]
        if not cand:
            return SnappedOutline(entries, None, f"no caption line within "
                                  f"±{window_s:.0f} s of {fmt_hms(e.start_s)}")
        indices.append(max(cand, key=lambda k: (
            transition_score(segments, k) - abs(seg_starts[k] - e.start_s) / OUTLINE_SNAP_COST_S,
            -abs(seg_starts[k] - e.start_s))))

    def t_of(k: int) -> float:
        return seg_starts[k] if k else 0.0

    merged: List[str] = []
    keep_e, keep_i = [entries[0]], [indices[0]]
    for e, k in zip(entries[1:], indices[1:]):
        if t_of(k) - t_of(keep_i[-1]) < OUTLINE_MIN_SECTION_S:
            # The last kept section would be too short. Drop its start, so its
            # lines join the section before it, and this entry starts here.
            # If it is the first section there is nothing before it: drop this
            # entry instead (its lines join the first section).
            if len(keep_i) > 1:
                merged.append(keep_e[-1].title)
                keep_e[-1], keep_i[-1] = e, max(k, keep_i[-1])
            else:
                merged.append(e.title)
            continue
        keep_e.append(e)
        keep_i.append(k)
    if len(keep_i) > 1 and end_of_media - t_of(keep_i[-1]) < OUTLINE_MIN_SECTION_S:
        merged.append(keep_e.pop().title)
        keep_i.pop()
    if len(keep_e) < OUTLINE_MIN_SECTIONS:
        return SnappedOutline(entries, None, f"only {len(keep_e)} section(s) longer than "
                              f"{OUTLINE_MIN_SECTION_S:.0f} s")
    on_transition = sum(1 for k in keep_i if k and transition_score(segments, k) >= 1.0)
    return SnappedOutline(keep_e, keep_i, "", on_transition, merged)


def outline_title(entry: OutlineEntry) -> str:
    title = " ".join(entry.title.split())
    speaker = " ".join((entry.speaker or "").split())
    if speaker and speaker.lower() not in title.lower():
        return f"{title} ({speaker})"
    return title


def _from_outline(segments, entries, indices, end_of_media, max_words, scorer):
    out: List[Section] = []
    spans: List[Tuple[int, int]] = []
    if indices[0] > 0:
        _emit_group(out, spans, segments, 0, indices[0], PREAMBLE_TITLE, 0.0,
                    segments[indices[0]].start, "ch00", max_words, scorer)
    bounds = [*indices, len(segments)]
    for k, entry in enumerate(entries):
        a, b = bounds[k], bounds[k + 1]
        start_s = 0.0 if a == 0 else segments[a].start
        end_s = end_of_media if k == len(entries) - 1 else segments[b].start
        _emit_group(out, spans, segments, a, b, outline_title(entry), start_s,
                    end_s, f"o{k + 1:02d}", max_words, scorer)
    return out, spans


# --- entry point ----------------------------------------------------------------


def media_sections(
    segments: Sequence[Segment],
    meta: Optional[SourceMeta] = None,
    *,
    max_words: int = DEFAULT_MAX_WORDS,
    min_section_s: float = DEFAULT_MIN_SECTION_S,
    max_section_s: float = DEFAULT_MAX_SECTION_S,
    window_s: float = DEFAULT_WINDOW_S,
    pause_s: float = DEFAULT_PAUSE_S,
    boundary_scorer: Optional[BoundaryScorer] = None,
    mode: Mode = "auto",
    outline: Optional[Sequence[OutlineEntry]] = None,
    outline_source: str = "a model",
    outline_note: str = "",
) -> StructureResult:
    """Time-ranged sections for a media transcript, via the structure ladder.

    `mode` "auto" walks the whole ladder. "outline", "cues" and "windows"
    start at that rung (skipping declared chapters) and still fall down it.
    `outline` entries (from core/outline.py) are used only when given;
    `outline_source` names the model for the provenance, and `outline_note`
    is why the caller has no outline (recorded on the fallback).
    """
    if mode not in MODES:
        raise ValueError(f"structure mode must be one of {', '.join(MODES)}; got {mode!r}")
    segments = sorted(segments, key=lambda s: s.start)
    if not segments:
        return StructureResult(rung="empty", structure_is_inferred=False,
                               provenance="no segments")
    end_of_media = _media_end(segments, meta)

    if (mode == "auto" and meta is not None and meta.chapters
            and meta.chapter_source in ("platform", "description")):
        sections, spans = _from_chapters(segments, meta.chapters, end_of_media,
                                         max_words, boundary_scorer)
        named = len(meta.chapters)
        where = "platform chapter list" if meta.chapter_source == "platform" else "video description"
        return StructureResult(
            sections=sections, segment_spans=spans, rung=meta.chapter_source,
            structure_is_inferred=False,
            provenance=f"{named} chapters from the {where}",
        )

    fallback = ""
    if mode in ("auto", "outline") and outline is not None:
        snap = snap_outline(segments, outline, end_of_media)
        if snap.indices is not None:
            sections, spans = _from_outline(segments, snap.entries, snap.indices,
                                            end_of_media, max_words, boundary_scorer)
            merged = (f"; {len(snap.merged)} section(s) under {OUTLINE_MIN_SECTION_S:.0f} s "
                      f"merged into the previous: " + ", ".join(repr(t) for t in snap.merged)
                      if snap.merged else "")
            return StructureResult(
                sections=sections, segment_spans=spans, rung="outline",
                structure_is_inferred=True,
                provenance=(f"inferred: {len(snap.entries)} sections from an outline by "
                            f"{outline_source} of a compressed transcript (titles are "
                            f"the model's); {snap.on_transition} start(s) snapped to a "
                            f"talk-transition line{merged}"),
            )
        fallback = f"outline rejected: {snap.reason}"
    elif mode == "outline" or outline_note:
        fallback = f"outline unavailable: {outline_note or 'none given'}"

    def _note(text: str) -> str:
        return f"{text} ({fallback})" if fallback else text

    if mode != "windows":
        cuts, forced = _cut_greedy(segments, max_words, min_section_s, max_section_s,
                                   pause_s, boundary_scorer, cues=True)
    else:
        cuts, forced = [], 0
    if cuts and forced / len(cuts) <= MAX_FORCED_CUE_FRACTION:
        sections, spans = _from_cuts(segments, cuts, "Section", end_of_media,
                                     max_words, boundary_scorer, "s")
        return StructureResult(
            sections=sections, segment_spans=spans, rung="cues",
            structure_is_inferred=True, fallback_reason=fallback,
            provenance=_note(f"inferred: {len(cuts) - forced} cue+pause boundaries, "
                             f"{forced} forced, sections {fmt_hms(min_section_s)}–"
                             f"{fmt_hms(max_section_s)}"),
        )

    cuts, _ = _cut_greedy(segments, max_words, window_s, window_s, pause_s,
                          boundary_scorer, cues=False)
    sections, spans = _from_cuts(segments, cuts, "Window", end_of_media,
                                 max_words, boundary_scorer, "w")
    return StructureResult(
        sections=sections, segment_spans=spans, rung="windows",
        structure_is_inferred=True, fallback_reason=fallback,
        provenance=_note(f"inferred: fixed ~{fmt_hms(window_s)} windows at the nearest pause"),
    )


# --- course skeleton -------------------------------------------------------------


_RUNG_LABEL = {
    "platform": "chapters declared on the platform",
    "description": "chapters listed in the video description",
    "outline": "INFERRED by a model outline of a compressed transcript; titles are the model's, not the author's",
    "cues": "INFERRED from spoken cues and pauses; titles are positional, not the author's",
    "windows": "INFERRED fixed time windows; titles are positional, not the author's",
    "empty": "no transcript",
}


def course_skeleton(result: StructureResult, meta: Optional[SourceMeta] = None) -> str:
    """Compact table of contents every passage receives (one line per chapter)."""
    lines: List[str] = []
    if meta is not None:
        lines.append(f"Course: {meta.title or meta.source_id or 'untitled'}")
        published = meta.published_at.isoformat() if meta.published_at else "unknown"
        lines.append(f"Published: {published} by {meta.published_by or 'unknown'}"
                     f" · Duration: {format_duration(meta.duration_s)}")
    lines.append(f"Structure: {_RUNG_LABEL.get(result.rung, result.rung)}")
    lines.append("Chapters:")
    n = 0
    for group in result.groups():
        first, last = group[0], group[-1]
        title = first.path[0] if first.path else ""
        is_preamble = title == PREAMBLE_TITLE and first.group_id == "ch00"
        label = "-" if is_preamble else f"{(n := n + 1)}."
        line = f"{label} {title} [{time_locator(first.start_s or 0, last.end_s or 0)}]"
        if len(group) > 1:
            line += f" ({len(group)} parts)"
        lines.append(line)
    return "\n".join(lines)
