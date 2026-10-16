"""Recording-lag detection from dates spoken in the transcript (WP-F7). No model calls.

Why: E2's course was published 2026-07-20, but the speaker says his usage
resets "April 23rd 5:00 pm" (10:17:30) and asks "so what's today date?" —
the material was recorded around April 2026, three months before the publish
date. Time-sensitive claims are dated ``as_of: published`` (P4/P5 semantics,
unchanged); this module only adds an INFERENCE beside it:

    frontmatter   recorded_estimate: "2026-04 (inferred from spoken dates: ...)"
    under title   > Material appears recorded ~April 2026; time-sensitive claims
                  may predate the publish date. (Inference from spoken dates ...)

What counts as evidence:

* a month + day ("April 23rd", "the 23rd of April", optionally with a year),
  an ISO date (2026-04-23), or a month + year ("October of 2024");
* year-only mentions never count — talks cite history ("started in 2010",
  "joined OpenAI in 2019") far more often than the present;
* a day with no year is resolved to the latest such date not after the
  reference date (+7 days) — the recording precedes publication;
* mentions more than ``MAX_LAG_DAYS`` before, or more than ``FUTURE_DAYS``
  after, the reference are history / plans, not the recording date.

A mention is *anchored* when a present-time anchor ("today", "tonight", "this
week", "as of", "right now", "next time", ...) is spoken within
``ANCHOR_CHARS`` of it. An estimate needs lagging mentions (≥ ``MIN_LAG_DAYS``
before the reference) that either include an anchored one or number at least
two within ``CLUSTER_DAYS`` of each other, and that are the majority of the
qualifying mentions. Otherwise there is no estimate.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

MIN_LAG_DAYS = 30
MAX_LAG_DAYS = 365
FUTURE_DAYS = 31
CLUSTER_DAYS = 45
ANCHOR_CHARS = 120

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
_MONTH_NO = {m: i for i, m in enumerate(MONTHS, 1)}
_MONTH_NO.update({"sept": 9})
_M = r"(?P<month>january|february|march|april|may|june|july|august|september|sept|october|november|december)"
_ORD = r"(?:st|nd|rd|th)"
_YEAR = r"(?:,?\s+(?:of\s+)?(?P<year>(?:19|20)\d{2}))"

_MONTH_DAY = re.compile(rf"\b{_M}\s+(?:the\s+)?(?P<day>[0-3]?\d)(?P<ord>{_ORD})?\b{_YEAR}?", re.I)
_DAY_OF_MONTH = re.compile(rf"\b(?:the\s+)?(?P<day>[0-3]?\d){_ORD}\s+of\s+{_M}\b{_YEAR}?", re.I)
_ISO = re.compile(r"\b(?P<year>20\d{2})-(?P<mm>[01]\d)-(?P<dd>[0-3]\d)\b")
_MONTH_YEAR = re.compile(rf"\b{_M}\s+(?:of\s+)?(?P<year>(?:19|20)\d{{2}}|'\d{{2}})\b", re.I)
ANCHOR_RE = re.compile(
    r"\b(?:today(?:'s)?|tonight|this\s+(?:week|month|morning|afternoon|evening)|"
    r"as\s+of|right\s+now|at\s+the\s+time\s+of\s+(?:recording|this)|yesterday|tomorrow|"
    r"next\s+time|recording\s+this)\b",
    re.I)


@dataclass
class DateMention:
    seconds: float
    text: str                 # the words as spoken, e.g. "April 23rd"
    date: dt.date             # resolved (day 1 for a month-year mention)
    year_given: bool
    anchored: bool = False
    anchor: str = ""

    @property
    def ts(self) -> str:
        s = int(self.seconds)
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


@dataclass
class RecordingEstimate:
    month: str                          # "YYYY-MM"
    reference: dt.date
    basis: str                          # "published" | "recorded" | "captured"
    mentions: List[DateMention] = field(default_factory=list)   # the lagging cluster
    considered: int = 0

    @property
    def month_name(self) -> str:
        y, m = self.month.split("-")
        return f"{MONTHS[int(m) - 1].capitalize()} {y}"

    @property
    def lag_days(self) -> int:
        return (self.reference - statistics.median_low(m.date for m in self.mentions)).days

    def evidence(self) -> str:
        return "; ".join(
            f'"{m.text}" [{m.ts}]' + (f" near \"{m.anchor}\"" if m.anchored else "")
            for m in self.mentions)

    def frontmatter_value(self) -> str:
        return f"{self.month} (inferred from spoken dates: {self.evidence()})"

    def note_line(self) -> str:
        ref = "the publish date" if self.basis == "published" else f"the {self.basis} date"
        return (f"> Material appears recorded ~{self.month_name}; time-sensitive claims "
                f"may predate {ref}. (Inference from dates spoken in the transcript — "
                "see `recorded_estimate`.)")

    def as_dict(self) -> dict:
        return {"month": self.month, "reference": self.reference.isoformat(),
                "basis": self.basis, "lag_days": self.lag_days,
                "considered": self.considered,
                "mentions": [{"ts": m.ts, "text": m.text, "date": m.date.isoformat(),
                              "anchored": m.anchored, "anchor": m.anchor}
                             for m in self.mentions]}


def _resolve(month: int, day: int, year: Optional[int], ref: dt.date) -> Optional[dt.date]:
    try:
        if year:
            return dt.date(year, month, day)
        cand = dt.date(ref.year, month, day)
        if cand > ref + dt.timedelta(days=7):
            cand = dt.date(ref.year - 1, month, day)
        return cand
    except ValueError:
        return None


def _year(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    raw = raw.lstrip("'")
    return int(raw) if len(raw) == 4 else 2000 + int(raw)


def scan(segments: Sequence, reference: dt.date) -> List[DateMention]:
    """Every dated mention in the transcript, in order. `segments` have
    ``.start`` and ``.text``; consecutive lines are joined, so "April" at the
    end of one caption and "23rd" at the start of the next still match."""
    parts, starts, offs = [], [], []
    pos = 0
    for s in segments:
        text = " ".join((getattr(s, "text", "") or "").split())
        parts.append(text)
        starts.append(float(getattr(s, "start", 0.0) or 0.0))
        offs.append(pos)
        pos += len(text) + 1
    joined = " ".join(parts)

    def at(i: int) -> float:
        lo, hi = 0, len(offs) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if offs[mid] <= i:
                lo = mid
            else:
                hi = mid - 1
        return starts[lo] if starts else 0.0

    found: List[DateMention] = []
    taken: List[tuple] = []

    def add(m, month: int, day: int, year: Optional[int]):
        a, b = m.span()
        if any(a < y and x < b for x, y in taken):
            return
        d = _resolve(month, day, year, reference)
        if d is None:
            return
        taken.append((a, b))
        window = joined[max(0, a - ANCHOR_CHARS): b + ANCHOR_CHARS]
        anc = ANCHOR_RE.search(window)
        found.append(DateMention(at(a), " ".join(m.group(0).split()), d, year is not None,
                                 bool(anc), anc.group(0) if anc else ""))

    for m in _ISO.finditer(joined):
        add(m, int(m.group("mm")), int(m.group("dd")), int(m.group("year")))
    for rx in (_DAY_OF_MONTH, _MONTH_DAY):
        for m in rx.finditer(joined):
            name = m.group("month")
            # "may"/"march" are also verbs: only with an ordinal or a capital.
            if name.lower() in ("may", "march") and not (
                    name[0].isupper() or (m.groupdict().get("ord") or rx is _DAY_OF_MONTH)):
                continue
            day = int(m.group("day"))
            if not 1 <= day <= 31:
                continue
            add(m, _MONTH_NO[name.lower()], day, _year(m.group("year")))
    for m in _MONTH_YEAR.finditer(joined):
        name = m.group("month")
        if name.lower() in ("may", "march") and not name[0].isupper():
            continue
        add(m, _MONTH_NO[name.lower()], 1, _year(m.group("year")))
    return sorted(found, key=lambda x: x.seconds)


def estimate_recording(segments: Sequence, reference, basis: str = "published"
                       ) -> Optional[RecordingEstimate]:
    """The inferred recording month, or None when the spoken dates do not
    cluster at least ``MIN_LAG_DAYS`` before `reference`."""
    if not reference or reference == "unknown":
        return None
    if isinstance(reference, dt.datetime):
        reference = reference.date()
    if not isinstance(reference, dt.date):
        reference = dt.date.fromisoformat(str(reference)[:10])
    lo = reference - dt.timedelta(days=MAX_LAG_DAYS)
    hi = reference + dt.timedelta(days=FUTURE_DAYS)
    qualifying = [m for m in scan(segments, reference) if lo <= m.date <= hi]
    lagging = [m for m in qualifying
               if (reference - m.date).days >= MIN_LAG_DAYS]
    if not lagging or 2 * len(lagging) <= len(qualifying):
        return None
    # The densest cluster of lagging mentions (within CLUSTER_DAYS of each other).
    best: List[DateMention] = []
    for m in lagging:
        group = [x for x in lagging if abs((x.date - m.date).days) <= CLUSTER_DAYS]
        if len(group) > len(best) or (len(group) == len(best) and
                                      sum(x.anchored for x in group) > sum(x.anchored for x in best)):
            best = group
    if not (any(m.anchored for m in best) or len(best) >= 2):
        return None
    mid = statistics.median_low(m.date for m in best)
    return RecordingEstimate(f"{mid.year:04d}-{mid.month:02d}", reference, basis,
                             sorted(best, key=lambda x: x.seconds), len(qualifying))


__all__ = ["DateMention", "RecordingEstimate", "estimate_recording", "scan"]
