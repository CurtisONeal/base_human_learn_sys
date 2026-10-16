"""Dated notes: YAML frontmatter and the `## Time-Sensitive Claims` table (WP-5).

Pure functions — no model calls, no network, no config reads. WP-3 wires them
into the summarize path; this module only formats, parses and checks.

Why: course material about a fast-moving product ("the exam has 5 domains",
"Claude Code now supports X") is true *as of a date*. The notes used to carry
only the date they were generated, so a reader months later could not tell
how old a claim was. Two things fix that, and the long-form evaluator checks
both (``eval/longform.py``):

P4  frontmatter carries ``published`` and ``as_of``.
P5  every row of every ``## Time-Sensitive Claims`` table has an as-of cell
    containing ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``.

The as-of date is the SOURCE's date, never the model's knowledge date:

    published known       as_of = published               as_of_basis: published
    else plausible        as_of = recorded (device clock)  as_of_basis: recorded
      recorded_at
    else                  as_of = captured                as_of_basis: captured

``published`` is never back-filled from a capture date — when unknown it is
the literal string ``unknown`` (the key is still present, so P4 passes on
presence while the basis says plainly what the date is).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core.metadata import SourceMeta, format_duration

FRAGMENT_DIR = BASE_DIR / "prompts" / "fragments"
TIME_SENSITIVE_FRAGMENT = FRAGMENT_DIR / "time_sensitive.md"

HEADING = "Time-Sensitive Claims"
COLUMNS = ("claim", "timestamp", "as-of", "type", "verify-by")
CLAIM_TYPES = ("version", "feature", "pricing", "exam", "news", "other")
# Model drift we can map without guessing: synonyms of an allowed type.
_TYPE_ALIASES = {
    "price": "pricing", "cost": "pricing", "quota": "pricing", "limit": "pricing",
    "limits": "pricing", "rate limit": "pricing", "versions": "version",
    "features": "feature", "availability": "feature", "announcement": "news",
}

# Same date rule the evaluator's P5 uses.
DATE_RE = re.compile(r"\b(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TS_RE = re.compile(r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?")
_SEPARATOR_CELL = re.compile(r":?-{2,}:?")

# Device clocks that were never set report 1970/1980/2000-01-01. Before this
# year a "recorded" date is treated as unset rather than cited.
_MIN_PLAUSIBLE_RECORDED_YEAR = 2005

_CORE_KEYS = (
    "title", "source", "published", "published_by", "as_of", "as_of_basis",
    "captured", "material_age_days", "duration", "duration_s",
    "transcript_source", "structure", "generated_by", "run_id",
)


# --- dating -----------------------------------------------------------------


@dataclass(frozen=True)
class AsOf:
    date: dt.date
    basis: str  # "published" | "recorded" | "captured"
    note: Optional[str] = None


def _as_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def resolve_as_of(meta: SourceMeta, captured_at: Any = None) -> AsOf:
    """The date a source's claims are true as of, and which date that is."""
    captured = _as_date(captured_at) or _as_date(meta.captured_at)
    if meta.published_at:
        return AsOf(meta.published_at, "published")
    recorded = _as_date(meta.recorded_at)
    if (
        recorded
        and recorded.year >= _MIN_PLAUSIBLE_RECORDED_YEAR
        and (captured is None or recorded <= captured)
    ):
        return AsOf(recorded, "recorded", "recording device clock, unverified")
    return AsOf(captured, "captured", "publish date unknown; date the source was captured")


# --- frontmatter ------------------------------------------------------------


def _yaml_scalar(value: Any) -> str:
    """A YAML scalar that round-trips. Strings are always double-quoted (JSON
    strings are valid YAML) so `12:38:36` is not read as base-60 and titles
    with colons survive. Dates are bare so YAML loads them as dates."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter(
    meta: SourceMeta,
    *,
    provenance: Optional[str] = None,
    structure_rung: Optional[str] = None,
    captured_at: Any = None,
    model: Optional[str] = None,
    run_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """YAML frontmatter block (with the `---` fences and a trailing newline).

    `provenance` is the transcript source label (manual captions / auto-caption
    / ASR engine); `structure_rung` the structure-ladder rung used. `extra`
    keys are appended after the core keys and may not override them.
    """
    captured = _as_date(captured_at) or _as_date(meta.captured_at)
    as_of = resolve_as_of(meta, captured)
    fields: Dict[str, Any] = {
        "title": meta.title or meta.source_id,
        "source": meta.url or None,
        "published": meta.published_at if meta.published_at else "unknown",
        "published_by": meta.published_by or "unknown",
        "as_of": as_of.date,
        "as_of_basis": as_of.basis,
    }
    if as_of.note:
        fields["as_of_note"] = as_of.note
    fields["captured"] = captured
    if meta.published_at and captured:
        fields["material_age_days"] = (captured - meta.published_at).days
    fields["duration"] = format_duration(meta.duration_s)
    if meta.duration_s is not None:
        fields["duration_s"] = meta.duration_s
    fields["transcript_source"] = provenance or "unknown"
    fields["structure"] = structure_rung or "unknown"
    fields["generated_by"] = model or "unknown"
    if run_id:
        fields["run_id"] = run_id
    for key, value in (extra or {}).items():
        if key in fields or key in _CORE_KEYS or key == "as_of_note":
            raise ValueError(f"extra frontmatter key {key!r} collides with a core key")
        fields[key] = value
    body = "\n".join(f"{k}: {_yaml_scalar(v)}" for k, v in fields.items())
    return f"---\n{body}\n---\n"


# --- prompt fragment ----------------------------------------------------------


def time_sensitive_fragment(meta: Optional[SourceMeta] = None, captured_at: Any = None) -> str:
    """The time-sensitivity prompt fragment; with `meta`, ends with the concrete
    as-of date the model must use (so the model never has to infer it)."""
    text = TIME_SENSITIVE_FRAGMENT.read_text(encoding="utf-8").rstrip() + "\n"
    if meta is None:
        return text
    as_of = resolve_as_of(meta, captured_at)
    if as_of.basis == "published":
        line = f"For this source, write as-of as `{as_of.date.isoformat()}` (the published date)."
    else:
        line = (
            f"For this source the published date is unknown: write as-of as "
            f"`{as_of.date.isoformat()} ({as_of.basis})`."
        )
    return text + "\n" + line + "\n"


# --- claims table: parse ------------------------------------------------------


@dataclass
class ClaimRow:
    claim: str
    timestamp: str = ""
    as_of: str = ""
    type: str = ""
    verify_by: str = ""
    flags: List[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def _column_key(name: str) -> str:
    # "as-of" / "As of" / "as_of" -> "asof"; "verify-by" -> "verifyby"
    return _norm(name).replace(" ", "")


_FIELD_FOR_COLUMN = {
    "claim": "claim", "timestamp": "timestamp", "ts": "timestamp", "time": "timestamp",
    "asof": "as_of", "type": "type", "verifyby": "verify_by", "verify": "verify_by",
}


def _split_row(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_none_marker(text: str) -> bool:
    return _norm(text) in {"none", "n a", "na", "no time sensitive claims"}


def _claims_sections(lines: Sequence[str]) -> List[tuple]:
    """(start, end, level) line spans of every Time-Sensitive Claims section."""
    spans = []
    i = 0
    while i < len(lines):
        m = _HEADING_RE.match(lines[i])
        if not (m and _norm(m.group(2)) == "time sensitive claims"):
            i += 1
            continue
        level = len(m.group(1))
        j = i + 1
        while j < len(lines):
            h = _HEADING_RE.match(lines[j])
            if h and len(h.group(1)) <= level:
                break
            j += 1
        spans.append((i, j, level))
        i = j
    return spans


def _strip_frontmatter(markdown: str) -> tuple:
    lines = markdown.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[: i + 1], lines[i + 1 :]
    return [], lines


def parse_time_sensitive(markdown: str) -> List[ClaimRow]:
    """Every row of every `Time-Sensitive Claims` table, in document order.

    Columns are matched by name (case/punctuation-insensitive), not position.
    `None` rows and `<placeholder>` rows copied from the prompt are skipped.
    """
    _, lines = _strip_frontmatter(markdown)
    rows: List[ClaimRow] = []
    for start, end, _level in _claims_sections(lines):
        columns: Optional[Dict[int, str]] = None
        for line in lines[start + 1 : end]:
            if not line.strip().startswith("|"):
                continue
            cells = _split_row(line)
            if columns is None:
                columns = {
                    idx: _FIELD_FOR_COLUMN[_column_key(name)]
                    for idx, name in enumerate(cells)
                    if _column_key(name) in _FIELD_FOR_COLUMN
                }
                continue
            if all(_SEPARATOR_CELL.fullmatch(c) for c in cells if c):
                continue
            values = {f: "" for f in _FIELD_FOR_COLUMN.values()}
            for idx, fname in columns.items():
                if idx < len(cells) and not values[fname]:
                    values[fname] = cells[idx]
            claim = values["claim"]
            if not claim or _is_none_marker(claim) or claim.startswith("<"):
                continue
            rows.append(ClaimRow(**values))
    return rows


# --- claims table: validate ---------------------------------------------------


@dataclass
class Finding:
    level: str  # "filled" | "warning" | "error"
    claim: str
    message: str


@dataclass
class TimeSensitiveCheck:
    rows: List[ClaimRow]
    findings: List[Finding]

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.findings)


def _first_date(text: str) -> Optional[dt.date]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0)
    parts = raw.split("-")
    try:
        return dt.date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1,
                       int(parts[2]) if len(parts) > 2 else 1)
    except ValueError:
        return None


def validate_time_sensitive(
    rows: Iterable[ClaimRow],
    as_of: Any,
    *,
    basis: str = "published",
    fill: bool = True,
) -> TimeSensitiveCheck:
    """Check (and, with `fill`, repair) rows against the source's as-of date.

    - as-of missing or not a date -> filled with `as_of` and flagged
      (`level="filled"`), or an `error` when `fill` is False or no as-of is known.
      The filled cell is labelled in the artifact, so a reader can tell a
      pipeline-supplied date from one the model wrote:
      `2026-07-20 (filled: published)`, `2026-10-15 (filled: captured)`.
    - as-of later than the source's date -> `warning`: that is the shape of the
      model citing its own knowledge date.
    - type outside {version, feature, pricing, exam, news, other} -> mapped
      from a known synonym, else `other`; flagged.
    - no timestamp / no verify-by -> `warning`.
    Input rows are not mutated.
    """
    source_date = _as_date(as_of) if as_of not in (None, "", "unknown") else None
    fill_text = None
    if source_date:
        fill_text = f"{source_date.isoformat()} (filled: {basis})"
    out: List[ClaimRow] = []
    findings: List[Finding] = []
    for original in rows:
        row = replace(original, flags=list(original.flags))
        if not DATE_RE.search(row.as_of or ""):
            was = row.as_of or "(empty)"
            if fill and fill_text:
                row.as_of = fill_text
                row.flags.append(f"as-of filled from {basis} date (was {was})")
                findings.append(Finding("filled", row.claim, f"as-of {was!r} -> {fill_text!r}"))
            else:
                findings.append(Finding("error", row.claim, f"as-of {was!r} is not a date"))
        else:
            row_date = _first_date(row.as_of)
            if source_date and row_date and row_date > source_date:
                findings.append(Finding(
                    "warning", row.claim,
                    f"as-of {row.as_of!r} is after the source date {source_date.isoformat()}"
                    " (model knowledge date?)",
                ))
        kind = _norm(row.type)
        if kind not in CLAIM_TYPES:
            mapped = _TYPE_ALIASES.get(kind, "other")
            findings.append(Finding("warning", row.claim, f"type {row.type!r} -> {mapped!r}"))
            row.flags.append(f"type {row.type or '(empty)'} -> {mapped}")
            row.type = mapped
        else:
            row.type = kind
        if not _TS_RE.search(row.timestamp or ""):
            findings.append(Finding("warning", row.claim, "no timestamp"))
        if not (row.verify_by or "").strip():
            findings.append(Finding("warning", row.claim, "no verify-by check"))
        out.append(row)
    return TimeSensitiveCheck(out, findings)


# --- claims table: merge (reduce step) ----------------------------------------


def _ts_seconds(text: str) -> Optional[float]:
    m = _TS_RE.search(text or "")
    if not m:
        return None
    parts = m.group(0).split(":")
    secs = float(parts[-1])
    mins = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return hours * 3600 + mins * 60 + secs


def merge_time_sensitive(tables: Iterable[Iterable[ClaimRow]]) -> List[ClaimRow]:
    """Combine per-passage tables: identical claims (case/punctuation-
    insensitive) collapse to one row at the earliest timestamp; distinct
    claims are all kept. Output is ordered by timestamp (rows without one
    last, in input order). Empty cells are filled from later duplicates."""
    merged: Dict[str, ClaimRow] = {}
    order: List[str] = []
    for table in tables:
        for row in table:
            key = _norm(row.claim)
            if not key:
                continue
            if key not in merged:
                merged[key] = replace(row, flags=list(row.flags))
                order.append(key)
                continue
            kept = merged[key]
            new_t, old_t = _ts_seconds(row.timestamp), _ts_seconds(kept.timestamp)
            if new_t is not None and (old_t is None or new_t < old_t):
                kept.timestamp = row.timestamp
            for name in ("as_of", "type", "verify_by"):
                if not getattr(kept, name) and getattr(row, name):
                    setattr(kept, name, getattr(row, name))
            kept.flags.extend(f for f in row.flags if f not in kept.flags)
    rows = [merged[k] for k in order]
    return sorted(
        rows,
        key=lambda r: (_ts_seconds(r.timestamp) is None, _ts_seconds(r.timestamp) or 0.0),
    )


# --- rendering + stamping ------------------------------------------------------


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_time_sensitive(rows: Sequence[ClaimRow], level: int = 2) -> str:
    """The section as markdown. An empty table keeps its header (P5 needs the
    as-of column to exist) and says `- None` beneath it."""
    out = [f"{'#' * level} {HEADING}", "", "| " + " | ".join(COLUMNS) + " |",
           "|" + "---|" * len(COLUMNS)]
    for r in rows:
        out.append("| " + " | ".join(_cell(v) for v in (
            r.claim, r.timestamp, r.as_of, r.type, r.verify_by)) + " |")
    if not rows:
        out += ["", "- None"]
    return "\n".join(out) + "\n"


def stamp_notes(
    notes_md: str,
    frontmatter_str: str,
    claims_rows: Optional[Sequence[ClaimRow]] = None,
) -> str:
    """Final notes: `frontmatter_str` on top (replacing any existing block),
    every existing Time-Sensitive Claims section removed, and one section with
    `claims_rows` inserted (rows parsed from `notes_md` itself when None).

    Placement: before the Fact-Checked Notes heading when there is exactly one
    (same heading level); otherwise before a `Transcript` heading (the
    pipeline appends the raw transcript after the model's notes); otherwise at
    the end.
    """
    if claims_rows is None:
        claims_rows = merge_time_sensitive([parse_time_sensitive(notes_md)])
    _, lines = _strip_frontmatter(notes_md)
    for start, end, _level in reversed(_claims_sections(lines)):
        del lines[start:end]

    def headings(name: str) -> List[tuple]:
        found = []
        for i, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if m and _norm(m.group(2)) == name:
                found.append((i, len(m.group(1))))
        return found

    fact_checked = headings("fact checked notes")
    transcript = headings("transcript")
    if len(fact_checked) == 1:
        at, level = fact_checked[0]
    elif transcript:
        at, level = transcript[0]
    else:
        at, level = len(lines), 2
    section = render_time_sensitive(claims_rows, level=level).rstrip("\n").split("\n")

    before = lines[:at]
    while before and not before[-1].strip():
        before.pop()
    after = lines[at:]
    body = before + ([""] if before else []) + section + ([""] + after if after else [])
    fm = frontmatter_str if frontmatter_str.endswith("\n") else frontmatter_str + "\n"
    return fm + "\n".join(body).rstrip("\n") + "\n"


__all__ = [
    "AsOf", "ClaimRow", "Finding", "TimeSensitiveCheck", "CLAIM_TYPES", "COLUMNS",
    "TIME_SENSITIVE_FRAGMENT", "frontmatter", "merge_time_sensitive",
    "parse_time_sensitive", "render_time_sensitive", "resolve_as_of", "stamp_notes",
    "time_sensitive_fragment", "validate_time_sensitive",
]
