"""Accuracy measures for comparing ASR engines: WER and a domain glossary.

WER against YouTube auto-captions measures *disagreement with another ASR
system*, not error against truth — the captions are themselves machine output
(they write "Cloud Code" for "Claude Code"). So WER is reported alongside a
glossary count: how often each output spells the domain terms correctly versus
the known mishearings. The two together say more than either alone.

Pure functions, no model calls; unit-tested in tests/test_asr_backends.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_CAPTION_LINE = re.compile(r"^- \[(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\]\s?(.*)$")

#: Correct domain terms. Counted on normalised text (lowercase, no punctuation).
#: Possessive/plural "s" allowed because apostrophes are stripped ("Claude's").
GLOSSARY_TERMS: Dict[str, str] = {
    "Claude": r"\bclaudes?\b",
    "Claude Code": r"\bclaude code\b",
    "MCP": r"\bmcps?\b",
    "Anthropic": r"\banthropics?\b",
    "SDK": r"\bsdks?\b",
}

#: Known mishearings of those terms. "cloud" alone is NOT counted — "cloud
#: provider" is real English — only "cloud" directly before a Claude product
#: name. "cloud code" is a subset of `cloud+product`, listed for visibility.
MISHEARINGS: Dict[str, str] = {
    "cloud code": r"\bcloud code\b",
    "clawed/clawd": r"\bclawe?d\b",
    # Observed in mlx-whisper output on CCA-F ("the clod agent").
    "clod/claud": r"\bclauds?\b|\bclods?\b",
    # Observed in parakeet output ("agent STK").
    "STK (for SDK)": r"\bstks?\b",
    "cloud+product": r"\bcloud (?:code|desktop|sonnet|opus)\b",
}


def normalize_words(text: str) -> List[str]:
    """Lowercase, drop punctuation, split. Hyphens and slashes split words.

    Apostrophes are deleted, not split ("it's" -> "its"), so contractions
    compare equal whichever way each engine writes them.
    """
    t = (text or "").lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[-/_.]", " ", t)
    t = _NON_WORD.sub(" ", t)
    return t.split()


@dataclass
class WerResult:
    wer: float
    substitutions: int
    deletions: int
    insertions: int
    ref_words: int
    hyp_words: int
    engine: str  # "jiwer" or "builtin"


def _levenshtein_counts(ref: Sequence[str], hyp: Sequence[str]) -> tuple:
    """(S, D, I) of a minimum-edit alignment. O(len(ref) * len(hyp)) time."""
    n, m = len(ref), len(hyp)
    # Each cell holds (cost, S, D, I); rows rolled to keep memory O(m).
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [None] * m  # type: ignore[list-item]
        r = ref[i - 1]
        for j in range(1, m + 1):
            if r == hyp[j - 1]:
                cur[j] = prev[j - 1]
                continue
            sub, dele, ins = prev[j - 1], prev[j], cur[j - 1]
            best = min(sub[0], dele[0], ins[0])
            if sub[0] == best:
                cur[j] = (sub[0] + 1, sub[1] + 1, sub[2], sub[3])
            elif dele[0] == best:
                cur[j] = (dele[0] + 1, dele[1], dele[2] + 1, dele[3])
            else:
                cur[j] = (ins[0] + 1, ins[1], ins[2], ins[3] + 1)
        prev = cur
    _, s, d, i_ = prev[m]
    return s, d, i_


def word_error_rate(reference: str, hypothesis: str, use_jiwer: bool = True) -> WerResult:
    """WER of `hypothesis` against `reference`, both normalised first."""
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    engine = "builtin"
    counts = None
    if use_jiwer and ref:
        try:
            import jiwer  # optional

            out = jiwer.process_words(" ".join(ref), " ".join(hyp))
            counts = (out.substitutions, out.deletions, out.insertions)
            engine = "jiwer"
        except ImportError:
            counts = None
    if counts is None:
        counts = _levenshtein_counts(ref, hyp)
    s, d, i = counts
    wer = (s + d + i) / len(ref) if ref else (0.0 if not hyp else float("inf"))
    return WerResult(wer, s, d, i, len(ref), len(hyp), engine)


def repeated_ngrams(text: str, n: int = 5) -> int:
    """Surplus repeats of word n-grams: the Whisper looping signature.

    Natural speech repeats a few 5-grams ("talk about it, then use it"); a
    decoder stuck in a loop repeats one phrase many times. Same measure as the
    asr_select calibration ("0 vs 10 repeated 5-grams").
    """
    from collections import Counter

    w = normalize_words(text)
    counts = Counter(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
    return sum(c - 1 for c in counts.values() if c > 1)


def glossary_counts(text: str) -> Dict[str, int]:
    """Counts of each GLOSSARY_TERMS and MISHEARINGS key in `text`."""
    norm = " ".join(normalize_words(text))
    out: Dict[str, int] = {}
    for label, pat in {**GLOSSARY_TERMS, **MISHEARINGS}.items():
        out[label] = len(re.findall(pat, norm))
    return out


def caption_lines_in_window(
    md_path: Path, start_s: float, duration_s: float
) -> List[Tuple[float, str]]:
    """[(t, text)] for `- [H:MM:SS.mmm] text` lines whose START is in the window.

    `t` is relative to the window start. Caption lines carry only a start time,
    so a line straddling either edge is in or out whole: at most one short
    line (~5 words) of boundary error on each side.
    """
    end_s = start_s + duration_s
    out: List[Tuple[float, str]] = []
    for line in Path(md_path).read_text(encoding="utf-8").splitlines():
        m = _CAPTION_LINE.match(line)
        if not m:
            continue
        h, mi, se, ms, text = m.groups()
        t = int(h) * 3600 + int(mi) * 60 + int(se)
        if ms:
            t += int(ms.ljust(3, "0")) / 1000
        if start_s <= t < end_s:
            out.append((t - start_s, text.strip()))
    return out


def caption_text_in_window(md_path: Path, start_s: float, duration_s: float) -> str:
    return " ".join(t for _, t in caption_lines_in_window(md_path, start_s, duration_s))


def timed_words(spans: Sequence[Tuple[float, float, str]]) -> List[Tuple[float, str]]:
    """Spread each (start, end, text) span's words evenly across the span.

    An approximation: neither caption lines nor sentence/segment timestamps
    give per-word times, so word times inside a span are interpolated.
    """
    out: List[Tuple[float, str]] = []
    for start, end, text in spans:
        words = normalize_words(text)
        if not words:
            continue
        step = max(end - start, 0.0) / len(words)
        out.extend((start + k * step, w) for k, w in enumerate(words))
    return out


def caption_spans(lines: Sequence[Tuple[float, str]], window_s: float) -> List[Tuple[float, float, str]]:
    """Caption lines -> (start, end, text), each ending where the next starts."""
    spans = []
    for k, (t, text) in enumerate(lines):
        nxt = lines[k + 1][0] if k + 1 < len(lines) else min(t + 3.0, window_s)
        spans.append((t, nxt, text))
    return spans


def timestamp_offsets(
    ref: Sequence[Tuple[float, str]],
    hyp: Sequence[Tuple[float, str]],
    min_run: int = 4,
) -> Dict[str, float]:
    """|hyp time - ref time| over words in agreeing runs of >= `min_run` words.

    Only long agreeing runs count, so a common word matched by chance far away
    cannot pollute the figure. Returns median, p90 and how many words matched.
    """
    import difflib

    rw = [w for _, w in ref]
    hw = [w for _, w in hyp]
    sm = difflib.SequenceMatcher(None, rw, hw, autojunk=False)
    deltas: List[float] = []
    for a, b, size in sm.get_matching_blocks():
        if size < min_run:
            continue
        deltas.extend(abs(hyp[b + k][0] - ref[a + k][0]) for k in range(size))
    if not deltas:
        return {"median_s": float("nan"), "p90_s": float("nan"), "matched_words": 0}
    deltas.sort()
    return {
        "median_s": deltas[len(deltas) // 2],
        "p90_s": deltas[min(len(deltas) - 1, int(len(deltas) * 0.9))],
        "matched_words": len(deltas),
    }


def parse_hms(value: str) -> float:
    """'0:47:36' / '47:36' / '2856.5' -> seconds."""
    if ":" not in value:
        return float(value)
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total
