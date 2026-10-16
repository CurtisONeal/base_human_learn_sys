"""Cap ASR segment length — one shared splitter for every backend.

Why. Measured on the E3 CLE-AI meetup (parakeet-mlx): one 229-word "sentence"
ran 1:53:41–1:55:44. The transcript markdown stores start times only, so the
evaluator saw a 122.9 s start-to-start gap (P3 FAIL), and any claim cited
inside that stretch could only point at a two-minute line.

What. `split_long_segments` cuts any segment longer than `max_s` seconds or
`max_words` words into consecutive parts:

* cut points prefer a sentence end (.?!), then a clause break (,;:), then
  any word boundary — never inside a word;
* times come from the segment's word/token timings (`Segment.words`) when the
  backend supplied them and they spell the same text; otherwise each word gets
  an equal share of the segment's span (proportional split);
* the parts' text concatenates back to the original (modulo whitespace), their
  times are monotonic and non-overlapping, and they stay inside the original
  segment's [start, end] — so `clip_to_duration` applied before the split still
  holds after it;
* each part keeps its own word timings, so a later re-split (e.g. a smaller
  cap) works from the checkpoint without re-running ASR.

Idempotent: a segment within both caps is returned unchanged (same object).
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from video2mdnotes.core.transcriber import Segment, Word

__all__ = ["split_long_segments", "split_segment", "DEFAULT_MAX_S", "DEFAULT_MAX_WORDS"]

DEFAULT_MAX_S = 20.0
DEFAULT_MAX_WORDS = 60

_SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
_CLAUSE_END = (",", ";", ":", "—", "–", "、", "，", "；", "：")
_CLOSERS = "\"')]}”’»"
_UNIT_RE = re.compile(r"\s*\S+\s*")

# A unit of text that may not be split further: (text, start, end).
_Unit = Tuple[str, float, float]


def _caps(max_s: Optional[float], max_words: Optional[int]) -> Tuple[float, int]:
    if max_s is None or max_words is None:
        try:
            from video2mdnotes.config import settings

            max_s = settings.asr_max_segment_s if max_s is None else max_s
            max_words = settings.asr_max_segment_words if max_words is None else max_words
        except Exception:  # noqa: BLE001 — config unavailable: built-in caps
            max_s = DEFAULT_MAX_S if max_s is None else max_s
            max_words = DEFAULT_MAX_WORDS if max_words is None else max_words
    return float(max_s), int(max_words)


def _norm(text: str) -> List[str]:
    return text.split()


def _timed_units(seg: Segment) -> Optional[List[_Unit]]:
    """The backend's own word timings, if they spell this segment's text."""
    words = seg.words or []
    if not words:
        return None
    if _norm("".join(w.text for w in words)) != _norm(seg.text):
        # Mismatch (e.g. text post-edited): timings no longer describe it.
        return None
    lo, hi = seg.start, max(seg.end, seg.start)
    units: List[_Unit] = []
    for w in words:
        s = min(max(w.start, lo), hi)
        e = min(max(w.end, s), hi)
        units.append((w.text, s, e))
    return units


def _proportional_units(seg: Segment) -> List[_Unit]:
    """Whitespace words, each given an equal share of the segment's span."""
    texts = _UNIT_RE.findall(seg.text)
    n = len(texts)
    if n == 0:
        return []
    span = max(seg.end - seg.start, 0.0)
    return [(t, seg.start + span * k / n, seg.start + span * (k + 1) / n)
            for k, t in enumerate(texts)]


def _ends_with(text: str, marks: Sequence[str]) -> bool:
    t = text.rstrip().rstrip(_CLOSERS)
    return bool(t) and t.endswith(tuple(marks))


def _choose_cuts(units: Sequence[_Unit], max_s: float, max_words: int) -> List[int]:
    """Exclusive end indices of each part (last == len(units))."""
    n = len(units)
    cuts: List[int] = []
    i = 0
    while i < n:
        # Longest run [i, j) that fits both caps (always at least one unit).
        j = i + 1
        while (j < n and (j + 1 - i) <= max_words
               and units[j][2] - units[i][1] <= max_s):
            j += 1
        if j >= n:
            cuts.append(n)
            break
        # Prefer a natural break, but not one that leaves a sliver: the part
        # must keep at least a third of what would fit.
        floor = i + max(1, (j - i) // 3)
        k = None
        for marks in (_SENTENCE_END, _CLAUSE_END):
            for c in range(j, floor - 1, -1):
                if _ends_with(units[c - 1][0], marks):
                    k = c
                    break
            if k is not None:
                break
        if k is None:
            # No punctuation: cut at the widest pause between words, if timed.
            gaps = [(units[c][1] - units[c - 1][2], c) for c in range(floor, j + 1) if c < n]
            best = max(gaps, default=(0.0, j))
            k = best[1] if best[0] > 0.3 else j
        cuts.append(k)
        i = k
    return cuts


def split_segment(seg: Segment, max_s: Optional[float] = None,
                  max_words: Optional[int] = None) -> List[Segment]:
    """One segment -> one or more segments within the caps (see module doc)."""
    max_s, max_words = _caps(max_s, max_words)
    duration = seg.end - seg.start
    n_words = len(seg.text.split())
    if duration <= max_s and n_words <= max_words:
        return [seg]
    units = _timed_units(seg)
    timed = units is not None
    if not timed:
        units = _proportional_units(seg)
    if len(units) <= 1:
        return [seg]
    cuts = _choose_cuts(units, max_s, max_words)
    if len(cuts) <= 1:
        return [seg]
    out: List[Segment] = []
    lo, hi = seg.start, max(seg.end, seg.start)
    prev_end = lo
    i = 0
    for n, k in enumerate(cuts):
        part = units[i:k]
        text = "".join(u[0] for u in part)
        if n == 0:
            # Keep the original leading whitespace convention (" text").
            lead = seg.text[: len(seg.text) - len(seg.text.lstrip())]
            text = lead + text.lstrip()
        else:
            text = " " + text.strip()
        text = text.rstrip()
        start = lo if n == 0 else max(part[0][1], prev_end)
        end = hi if n == len(cuts) - 1 else max(part[-1][2], start)
        start, end = min(start, hi), min(end, hi)
        words = ([Word(start=u[1], end=u[2], text=u[0]) for u in part]
                 if timed else None)
        out.append(Segment(start=start, end=end, text=text, words=words))
        prev_end = end
        i = k
    return out


def split_long_segments(segments: Sequence[Segment], max_s: Optional[float] = None,
                        max_words: Optional[int] = None) -> List[Segment]:
    """Apply `split_segment` to every segment, preserving order."""
    max_s, max_words = _caps(max_s, max_words)
    out: List[Segment] = []
    for s in segments:
        out.extend(split_segment(s, max_s, max_words))
    return out
