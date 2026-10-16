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

Relative anchors (WP-F8). E3b's meetup has no published date, and its only
dated clue is relative: "In May of 26, so just like five months ago or four
months ago" [1:41:30]. An absolute month ("May of 26", "April 23rd") with an
offset phrase within ``RELATIVE_CHARS`` ("N days/weeks/months/years ago",
"last week/month/year") implies the EVENT date = anchor + offset — here
September–October 2026. ``estimate_event`` prefers such evidence (it is
explicit) and falls back to the lag cluster below. It applies when the
publish date is unknown too (reference = the captured date), and is shown
whenever the basis is not ``published``; with a known publish date it is
shown only when it lags by ``MIN_LAG_DAYS``. Always labelled an inference;
``as_of`` is never changed.

A mention is *anchored* when a present-time anchor ("today", "tonight", "this
week", "as of", "right now", "next time", ...) is spoken within
``ANCHOR_CHARS`` of it. An estimate needs lagging mentions (≥ ``MIN_LAG_DAYS``
before the reference), clustered within ``CLUSTER_DAYS``, that include at
least one ANCHORED mention and are the majority of the qualifying mentions.
Unanchored repeats never suffice (WP-F9). Otherwise there is no estimate.
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
RELATIVE_CHARS = 90

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
# "May of 26": a two-digit year only after "of" ("May 26" is a day).
_MONTH_OF_YY = re.compile(rf"\b{_M}\s+of\s+(?P<year>\d{{2}})\b(?!\s*(?:st|nd|rd|th)\b)", re.I)
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "a couple of": 2, "a couple": 2, "couple of": 2}
_AGO_RE = re.compile(
    r"\b(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"an?|(?:a\s+)?couple(?:\s+of)?)\s+(?P<unit>days?|weeks?|months?|years?)\s+ago\b", re.I)
_LAST_RE = re.compile(r"\blast\s+(?P<unit>week|month|year)\b", re.I)
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
    has_day: bool = True
    span: tuple = field(default=(0, 0), repr=False, compare=False)

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
    kind: str = "lag"                   # "lag" | "relative"
    month_end: str = ""                 # "YYYY-MM" when the evidence spans months
    relative: List["RelativeEvidence"] = field(default_factory=list)

    @staticmethod
    def _name(month: str) -> str:
        y, m = month.split("-")
        return f"{MONTHS[int(m) - 1].capitalize()} {y}"

    @property
    def range_name(self) -> str:
        if not self.month_end or self.month_end == self.month:
            return self.month_name
        a, b = self._name(self.month), self._name(self.month_end)
        if a.split()[1] == b.split()[1]:
            return f"{a.split()[0]}–{b}"
        return f"{a}–{b}"

    @property
    def range_value(self) -> str:
        if not self.month_end or self.month_end == self.month:
            return self.month
        return f"{self.month}/{self.month_end}"

    @property
    def month_name(self) -> str:
        y, m = self.month.split("-")
        return f"{MONTHS[int(m) - 1].capitalize()} {y}"

    @property
    def lag_days(self) -> int:
        if self.kind == "relative":
            return (self.reference - statistics.median_low(
                d for r in self.relative for d in r.implied)).days
        return (self.reference - statistics.median_low(m.date for m in self.mentions)).days

    def evidence(self) -> str:
        if self.kind == "relative":
            return "; ".join(r.describe() for r in self.relative)
        return "; ".join(
            f'"{m.text}" [{m.ts}]' + (f" near \"{m.anchor}\"" if m.anchored else "")
            for m in self.mentions)

    def frontmatter_value(self) -> str:
        if self.kind == "relative":
            return f"{self.range_value} (inferred from spoken relative dates: {self.evidence()})"
        return f"{self.month} (inferred from spoken dates: {self.evidence()})"

    def note_line(self) -> str:
        if self.kind == "relative":
            return (f"> Event date (inference): ~{self.range_name}, from a relative date "
                    f"spoken in the transcript — {self.evidence()}. Not a published or "
                    "device date; `as_of` is unchanged (see `recorded_estimate`).")
        ref = "the publish date" if self.basis == "published" else f"the {self.basis} date"
        return (f"> Material appears recorded ~{self.month_name}; time-sensitive claims "
                f"may predate {ref}. (Inference from dates spoken in the transcript — "
                "see `recorded_estimate`.)")

    def as_dict(self) -> dict:
        if self.kind == "relative":
            return {"kind": "relative", "month": self.month, "month_end": self.month_end,
                    "reference": self.reference.isoformat(), "basis": self.basis,
                    "lag_days": self.lag_days, "considered": self.considered,
                    "evidence": [r.as_dict() for r in self.relative]}
        return {"kind": "lag", "month": self.month, "reference": self.reference.isoformat(),
                "basis": self.basis, "lag_days": self.lag_days,
                "considered": self.considered,
                "mentions": [{"ts": m.ts, "text": m.text, "date": m.date.isoformat(),
                              "anchored": m.anchored, "anchor": m.anchor}
                             for m in self.mentions]}


@dataclass
class RelativeEvidence:
    """An absolute month + an offset phrase near it => implied event date(s)."""
    seconds: float
    anchor: DateMention
    offsets: List[str]                  # as spoken: "five months ago", "last month"
    implied: List[dt.date]
    line: str                           # the spoken words, anchor .. offsets

    @property
    def ts(self) -> str:
        s = int(self.seconds)
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def describe(self) -> str:
        return f'"{self.line}" [{self.ts}]'

    def as_dict(self) -> dict:
        return {"ts": self.ts, "line": self.line, "anchor": self.anchor.text,
                "anchor_date": self.anchor.date.isoformat(), "offsets": self.offsets,
                "implied": [d.isoformat() for d in self.implied]}


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
    return _scan(segments, reference)[0]


def _scan(segments: Sequence, reference: dt.date):
    """(mentions, joined text) — `DateMention.span` indexes the joined text."""
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

    def add(m, month: int, day: int, year: Optional[int], has_day: bool = True):
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
                                 bool(anc), anc.group(0) if anc else "", has_day, (a, b)))

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
    for rx in (_MONTH_YEAR, _MONTH_OF_YY):
        for m in rx.finditer(joined):
            name = m.group("month")
            if name.lower() in ("may", "march") and not name[0].isupper():
                continue
            add(m, _MONTH_NO[name.lower()], 1, _year(m.group("year")), has_day=False)
    return sorted(found, key=lambda x: x.seconds), joined


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
    # At least one ANCHORED mention ("April 23rd ... next time"). Repeats of
    # an unanchored date are history told twice ("announced May 22nd"), not
    # the recording date (F9 review (k)).
    if not any(m.anchored for m in best):
        return None
    mid = statistics.median_low(m.date for m in best)
    return RecordingEstimate(f"{mid.year:04d}-{mid.month:02d}", reference, basis,
                             sorted(best, key=lambda x: x.seconds), len(qualifying))


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, min(d.day, 28))


def _offset_date(anchor: dt.date, n: int, unit: str) -> dt.date:
    unit = unit.lower().rstrip("s")
    if unit == "day":
        return anchor + dt.timedelta(days=n)
    if unit == "week":
        return anchor + dt.timedelta(weeks=n)
    if unit == "month":
        return _add_months(anchor, n)
    return _add_months(anchor, 12 * n)


def _count(raw: str) -> Optional[int]:
    raw = " ".join(raw.lower().split())
    if raw.isdigit():
        return int(raw)
    return _NUMBER_WORDS.get(raw)


def _as_date(reference) -> Optional[dt.date]:
    if not reference or reference == "unknown":
        return None
    if isinstance(reference, dt.datetime):
        return reference.date()
    if isinstance(reference, dt.date):
        return reference
    try:
        return dt.date.fromisoformat(str(reference)[:10])
    except ValueError:
        return None


def relative_evidence(segments: Sequence, reference) -> List[RelativeEvidence]:
    """Absolute month mentions with an offset phrase within RELATIVE_CHARS:
    "In May of 26, so just like five months ago or four months ago" =>
    implied event dates May 2026 + 5 months and + 4 months. A month-only
    anchor counts as mid-month. Implied dates outside [reference -
    MAX_LAG_DAYS, reference + FUTURE_DAYS] are dropped."""
    reference = _as_date(reference)
    if reference is None:
        return []
    mentions, joined = _scan(segments, reference)
    lo = reference - dt.timedelta(days=MAX_LAG_DAYS)
    hi = reference + dt.timedelta(days=FUTURE_DAYS)
    out: List[RelativeEvidence] = []
    for m in mentions:
        a, b = m.span
        if a == b == 0:
            continue
        base = m.date if m.has_day else m.date.replace(day=15)
        start = max(0, a - RELATIVE_CHARS)
        window = joined[start: b + RELATIVE_CHARS]
        offsets, implied, ends = [], [], [a, b]
        for rx in (_AGO_RE, _LAST_RE):
            for o in rx.finditer(window):
                oa, ob = start + o.start(), start + o.end()
                if oa < b and a < ob:
                    continue
                n = _count(o.group("n")) if "n" in rx.groupindex else 1
                if not n:
                    continue
                d = _offset_date(base, n, o.group("unit"))
                if lo <= d <= hi:
                    offsets.append(" ".join(o.group(0).split()))
                    implied.append(d)
                    ends += [oa, ob]
        if implied:
            line = " ".join(joined[min(ends): max(ends)].split())
            out.append(RelativeEvidence(m.seconds, m, offsets, sorted(implied), line))
    return out


def estimate_from_relative(segments: Sequence, reference, basis: str = "published"
                           ) -> Optional[RecordingEstimate]:
    """Event-date estimate from relative anchors, or None."""
    ref = _as_date(reference)
    ev = relative_evidence(segments, ref) if ref else []
    if not ev:
        return None
    dates = sorted(d for r in ev for d in r.implied)
    first, last = dates[0], dates[-1]
    est = RecordingEstimate(f"{first.year:04d}-{first.month:02d}", ref, basis, [],
                            len(ev), kind="relative",
                            month_end=f"{last.year:04d}-{last.month:02d}", relative=ev)
    if basis == "published" and est.lag_days < MIN_LAG_DAYS:
        return None          # consistent with the publish date: nothing to warn about
    return est


def estimate_event(segments: Sequence, reference, basis: str = "published"
                   ) -> Optional[RecordingEstimate]:
    """The event/recording date inference used by assembly: explicit
    relative evidence first, else the lag cluster (``estimate_recording``)."""
    return (estimate_from_relative(segments, reference, basis)
            or estimate_recording(segments, reference, basis))


__all__ = ["DateMention", "RecordingEstimate", "RelativeEvidence", "estimate_event",
           "estimate_from_relative", "estimate_recording", "relative_evidence", "scan"]
