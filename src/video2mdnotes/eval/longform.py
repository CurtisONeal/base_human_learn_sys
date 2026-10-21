"""Long-form evaluation harness (WP-0): pass criteria P1-P7 + the P8 sampler.

Spec: ``design/LONGFORM_EXECUTION_PLAN.md`` ("Evaluation — pass criteria").
Everything here is deterministic. **No model is called.** P8 (claim tracing) is
judged by an independent reviewer agent from the ``p8_sample.json`` this module
writes; the report records P8 as ``"pending reviewer"``.

Run-directory contract
======================

This is the layout the long-form pipeline must produce. Implementers target it;
the harness reads nothing else.

::

    <run_dir>/
      summaries/<name>.summary.md   notes; one or more files (all are evaluated)
      transcripts/*.md              timestamped transcript
      source_meta.json              yt-dlp-style metadata
      corrections.json              transcript-correction log

``summaries/<name>.summary.md``
    Markdown notes beginning with YAML frontmatter that carries at least
    ``published`` and ``as_of`` (P4)::

        ---
        title: "..."
        published: 2026-07-20
        as_of: 2026-10-15
        ---

    Chapters appear as markdown headings (``#`` .. ``######``) whose text
    contains the chapter title (P1; matching ignores case, punctuation and
    extra words such as a leading number or a trailing timestamp).

    Citations are bracketed timestamps anywhere in the body (P2):
    ``[H:MM:SS]``, ``[H:MM:SS.mmm]``, ``[MM:SS]`` or a range
    ``[H:MM:SS–H:MM:SS]`` (``-``, ``–``, ``—`` or ``to``). A markdown link
    whose text is a timestamp (``[0:10:17](https://...)``) also counts.

    A ``## Time-Sensitive Claims`` section holds a markdown table with the
    columns ``claim | timestamp | as-of | type | verify-by`` (P5). The
    timestamp cell may be bare (``0:10:17``) or bracketed; either is cited.
    The section is required: a notes set without it fails P5.

``transcripts/*.md``
    One line per cue: ``- [H:MM:SS.mmm] text`` (the existing archive format).
    If several files exist, the first one whose name does not contain
    ``corrected`` is used (the raw transcript is the ground truth; a corrected
    copy shares its timestamps). Override with ``transcript=``.

``source_meta.json``
    yt-dlp-style: ``duration`` (seconds) and ``chapters`` — a list of
    ``{"start_time": float, "title": str}``. If absent, ``yt_info.json`` in
    the run dir is read for ``duration`` (older runs saved the raw yt-dlp
    dump under that name).

``corrections.json``
    A JSON list of ``{"timestamp", "original", "corrected", "rule"}``
    (P7). ``timestamp`` is ``H:MM:SS[.mmm]`` or seconds.

Expected chapters (P1) come from an explicit ``expected_chapters`` file when one
is given — it is the evaluator's ground truth, so the pipeline is not graded
against its own claim — else from ``source_meta.json`` ``chapters``, else (a
source that declares no chapters) from the pipeline's own ``structure.json``
sections — reported as self-reported, with the structure rung. The file has
one chapter per line, optionally prefixed by ``- `` and/or a timestamp
(``- 00:10:17 SDK Environment Setup``); blank lines and ``#`` comments ignored.
"""

from __future__ import annotations

import bisect
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GLOSSARY = Path(__file__).with_name("glossary_default.json")
DEFAULT_MAX_GAP_S = 120.0
DEFAULT_COVERAGE = 0.98
DEFAULT_TOLERANCE_S = 30.0
# P3 on SHORT media (WP-M1b): transcript lines carry START times only, and a
# final cue (segments are capped at 20 s, WP-F5) or trailing room tone starts
# seconds before the end — so "last start >= 98 % of duration" is unpassable
# for a 15 s clip (needs a cue starting in its last 0.3 s). A tail this short
# also passes. It only changes the outcome below 25 min (2 % of 25 min = 30 s).
DEFAULT_TAIL_SLACK_S = 30.0
DEFAULT_P8_N = 20
DEFAULT_P8_WINDOW_S = 60.0
DEFAULT_SEED = 0
CRITERIA = ("P1", "P2", "P3", "P4", "P5", "P6", "P7")

# --- timestamp parsing -------------------------------------------------------

_TS = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
_RANGE_SEP = r"\s*(?:-|–|—|to)\s*"
# `[ts]` or `[ts–ts]`; also a markdown link whose text is a timestamp.
CITE_RE = re.compile(rf"\[({_TS})(?:{_RANGE_SEP}({_TS}))?\]")
BARE_TS_RE = re.compile(rf"^\s*\[?({_TS})(?:{_RANGE_SEP}({_TS}))?\]?\s*$")
TRANSCRIPT_LINE_RE = re.compile(r"^\s*-\s*\[(\d+:\d{2}:\d{2}(?:\.\d+)?)\]\s*(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
DATE_RE = re.compile(r"\b(?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?\b")
CHAPTER_LINE_RE = re.compile(rf"^\s*(?:[-*]\s*)?(?:({_TS})\s*[-–—:]?\s*)?(.+?)\s*$")


def parse_ts(text: str) -> float:
    """``H:MM:SS[.mmm]`` or ``MM:SS[.mmm]`` -> seconds. Raises ValueError."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"not a timestamp: {text!r}")
    secs = float(parts[-1])
    mins = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    if secs >= 60 or (len(parts) == 3 and mins >= 60):
        raise ValueError(f"out-of-range field in timestamp: {text!r}")
    return hours * 3600 + mins * 60 + secs


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# --- inputs ------------------------------------------------------------------


@dataclass
class TranscriptLine:
    t: float
    text: str


@dataclass
class Citation:
    raw: str
    start: float
    end: float
    file: str
    line: int


@dataclass
class NotesFile:
    path: Path
    text: str
    frontmatter: dict[str, Any] | None
    body_offset: int  # 1-based line number where the body starts


@dataclass
class RunInputs:
    run_dir: Path
    notes: list[NotesFile] = field(default_factory=list)
    transcript_path: Path | None = None
    transcript: list[TranscriptLine] = field(default_factory=list)
    duration: float | None = None
    duration_source: str | None = None
    chapters: list[str] = field(default_factory=list)
    chapters_source: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Measured silence [(start_s, end_s)] on the source timeline, from the
    # windowed-ASR checkpoints (asr/*.json, ffmpeg silencedetect). Empty when
    # the transcript came from captions or no measurement exists.
    silences: list[tuple[float, float]] = field(default_factory=list)


def load_silences(run_dir: Path) -> list[tuple[float, float]]:
    """Absolute silence intervals from windowed-ASR checkpoints. A window's
    silences are relative to its extracted start (file offset + ext_start)."""
    out: list[tuple[float, float]] = []
    for f in sorted((run_dir / "asr").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        base = float(d.get("file_offset_s") or 0) + float(d.get("ext_start_s") or 0)
        for iv in d.get("silences") or []:
            try:
                a, b = float(iv[0]), float(iv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if b > a:
                out.append((base + a, base + b))
    out.sort()
    return out


def silence_fraction(silences: list[tuple[float, float]], lo: float, hi: float) -> float:
    """Fraction of [lo, hi] covered by measured silence (overlaps merged)."""
    if hi <= lo or not silences:
        return 0.0
    covered, cur = 0.0, lo
    for a, b in silences:
        a, b = max(a, cur), min(b, hi)
        if b > a:
            covered += b - a
            cur = b
    return covered / (hi - lo)


# A gap or tail at least this much measured silence is excused (tape leader,
# blank run-out, a long pause in an MP3) — never an unmeasured stretch.
SILENCE_EXCUSE_FRACTION = 0.8


def parse_transcript(text: str) -> list[TranscriptLine]:
    lines = []
    for raw in text.splitlines():
        m = TRANSCRIPT_LINE_RE.match(raw)
        if m:
            lines.append(TranscriptLine(parse_ts(m.group(1)), m.group(2)))
    return lines


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, int]:
    """Return (frontmatter dict or None, 1-based body start line)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            block = "\n".join(lines[1:i])
            return _load_yaml(block), i + 2
    return None, 1


def _load_yaml(block: str) -> dict[str, Any]:
    try:
        import yaml

        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Fallback: flat `key: value` lines (enough for P4).
        out: dict[str, Any] = {}
        for line in block.splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip().strip("\"'")
        return out


def parse_expected_chapters(text: str) -> list[str]:
    titles = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = CHAPTER_LINE_RE.match(raw)
        if m and m.group(2):
            titles.append(m.group(2))
    return titles


def chapters_from_description(description: str) -> list[tuple[str, str]]:
    """``- HH:MM:SS Title`` lines in a YouTube description -> [(ts, title)]."""
    out = []
    for raw in description.splitlines():
        m = re.match(rf"^\s*[-*]?\s*({_TS})\s+(.+?)\s*$", raw)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def load_run(
    run_dir: Path,
    *,
    expected_chapters: Path | None = None,
    transcript: Path | None = None,
    duration: float | None = None,
) -> RunInputs:
    run_dir = Path(run_dir)
    inp = RunInputs(run_dir=run_dir)
    inp.silences = load_silences(run_dir)

    for path in sorted((run_dir / "summaries").glob("*.summary.md")):
        text = path.read_text(encoding="utf-8")
        fm, offset = split_frontmatter(text)
        inp.notes.append(NotesFile(path, text, fm, offset))

    if transcript is None:
        candidates = sorted((run_dir / "transcripts").glob("*.md"))
        raw = [p for p in candidates if "corrected" not in p.name]
        transcript = (raw or candidates or [None])[0]
        if len(candidates) > 1:
            inp.warnings.append(
                f"{len(candidates)} transcript files; using {transcript.name}"
            )
    if transcript is not None and Path(transcript).exists():
        inp.transcript_path = Path(transcript)
        inp.transcript = parse_transcript(
            inp.transcript_path.read_text(encoding="utf-8")
        )

    meta: dict[str, Any] = {}
    for name in ("source_meta.json", "yt_info.json"):
        p = run_dir / name
        if p.exists():
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                meta["_file"] = name
                break
            except json.JSONDecodeError as exc:
                inp.warnings.append(f"{name} unreadable: {exc}")
    if duration is not None:
        inp.duration, inp.duration_source = float(duration), "--duration"
    elif isinstance(meta.get("duration"), (int, float)):
        inp.duration, inp.duration_source = float(meta["duration"]), meta["_file"]

    meta_chapters = [
        c.get("title", "") for c in (meta.get("chapters") or []) if isinstance(c, dict)
    ]
    if expected_chapters is not None:
        inp.chapters = parse_expected_chapters(
            Path(expected_chapters).read_text(encoding="utf-8")
        )
        inp.chapters_source = str(expected_chapters)
        if meta_chapters and _norm_list(meta_chapters) != _norm_list(inp.chapters):
            inp.warnings.append(
                f"{meta['_file']} lists {len(meta_chapters)} chapters that differ "
                f"from the {len(inp.chapters)} expected; P1 uses the expected file"
            )
    elif meta_chapters:
        inp.chapters, inp.chapters_source = meta_chapters, meta["_file"]
    else:
        own = structure_chapters(run_dir)
        if own is not None:
            inp.chapters, rung = own
            inp.chapters_source = (
                f"{STRUCTURE_FILE} (the pipeline's own sections, rung: {rung} — "
                "self-reported, not ground truth: the source declares no chapters)")
            inp.warnings.append(
                f"P1 graded against the pipeline's own {len(inp.chapters)} section(s) "
                f"from {STRUCTURE_FILE} (rung: {rung}); give --expected-chapters for "
                "an independent check")
    return inp


STRUCTURE_FILE = "structure.json"


def structure_chapters(run_dir: Path) -> tuple[list[str], str] | None:
    """(section titles, rung) from the pipeline's structure.json, the preamble
    excluded (it is not a chapter), or None when there is no such file.
    Display titles are used: they are what the headings show."""
    path = Path(run_dir) / STRUCTURE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    titles = [str(sec.get("display_title") or sec.get("title") or "")
              for sec in data.get("sections") or []
              if isinstance(sec, dict) and not sec.get("preamble")]
    titles = [t for t in titles if t.strip()]
    return (titles, str(data.get("rung") or "unknown")) if titles else None


class RunDirError(ValueError):
    """The run dir cannot be evaluated: missing, or nothing in it to grade."""


def check_run_dir(run_dir: Path, transcript: Path | None = None) -> None:
    """Fail loudly (WP-M3) instead of evaluating nothing: a harness that
    globbed `*let_s_build_gpt*` for a folder named `lets_build_gpt` got a
    clean-looking report over no files at all."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise RunDirError(f"run dir does not exist: {run_dir}")
    if transcript is None and not any((run_dir / "transcripts").glob("*.md")):
        raise RunDirError(f"no transcripts/*.md in {run_dir} — not a run dir "
                          "(read the RUN_DIR= line or _last_run.json the command wrote)")
    if not any((run_dir / "summaries").glob("*.summary.md")):
        raise RunDirError(f"no summaries/*.summary.md in {run_dir} — nothing to evaluate")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def _norm_list(items: list[str]) -> list[str]:
    return [_norm(i) for i in items]


# --- notes parsing -----------------------------------------------------------


def body_lines(nf: NotesFile) -> list[tuple[int, str]]:
    """(1-based line number, text) for every line after the frontmatter."""
    lines = nf.text.splitlines()
    return [(i + 1, line) for i, line in enumerate(lines) if i + 1 >= nf.body_offset]


def find_citations(nf: NotesFile) -> list[Citation]:
    cites: list[Citation] = []
    seen: set[tuple[int, float, float]] = set()

    def add(raw: str, a: str, b: str | None, line: int) -> None:
        try:
            start = parse_ts(a)
            end = parse_ts(b) if b else start
        except ValueError:
            return
        key = (line, start, end)
        if key not in seen:
            seen.add(key)
            cites.append(Citation(raw, start, end, nf.path.name, line))

    for no, line in body_lines(nf):
        for m in CITE_RE.finditer(line):
            add(m.group(0), m.group(1), m.group(2), no)
    for table in claims_tables(nf):
        col = table["columns"].get("timestamp")
        if col is None:
            continue
        for row in table["rows"]:
            cell = row["cells"][col] if col < len(row["cells"]) else ""
            m = BARE_TS_RE.match(cell)
            if m:
                add(cell.strip(), m.group(1), m.group(2), row["line"])
    return cites


def claims_tables(nf: NotesFile) -> list[dict[str, Any]]:
    """Parse every ``## Time-Sensitive Claims`` table in a notes file."""
    tables = []
    lines = body_lines(nf)
    i = 0
    while i < len(lines):
        m = HEADING_RE.match(lines[i][1])
        if not (m and _norm(m.group(2)) == "time sensitive claims"):
            i += 1
            continue
        level = len(m.group(1))
        table: dict[str, Any] = {
            "line": lines[i][0],
            "columns": {},
            "header": [],
            "rows": [],
        }
        i += 1
        while i < len(lines):
            no, text = lines[i]
            h = HEADING_RE.match(text)
            if h and len(h.group(1)) <= level:
                break
            if text.strip().startswith("|"):
                cells = [c.strip() for c in text.strip().strip("|").split("|")]
                if not table["header"]:
                    table["header"] = cells
                    for idx, name in enumerate(cells):
                        # "as-of" / "As of" / "as_of" -> "asof"
                        key = _norm(name).replace(" ", "")
                        table["columns"].setdefault(key, idx)
                elif not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    table["rows"].append({"line": no, "cells": cells})
            i += 1
        tables.append(table)
    return tables


def claim_bullets(nf: NotesFile) -> list[dict[str, Any]]:
    """Bullets (with their indented continuation lines) that cite a timestamp."""
    bullets: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for no, line in body_lines(nf):
        m = BULLET_RE.match(line)
        if m:
            current = {"file": nf.path.name, "line": no, "claim": m.group(1)}
            bullets.append(current)
        elif current and line.startswith((" ", "\t")) and line.strip():
            current["claim"] += " " + line.strip()
        else:
            current = None
    out = []
    for b in bullets:
        cites = find_citations_in_text(b["claim"])
        if cites:
            out.append({**b, "cited": cites})
    return out


def find_citations_in_text(text: str) -> list[dict[str, Any]]:
    out = []
    for m in CITE_RE.finditer(text):
        try:
            start = parse_ts(m.group(1))
            end = parse_ts(m.group(2)) if m.group(2) else start
        except ValueError:
            continue
        out.append({"raw": m.group(0), "start_s": start, "end_s": end})
    return out


# --- glossary ----------------------------------------------------------------


@dataclass
class Glossary:
    entries: list[dict[str, str]]
    pattern: re.Pattern[str] | None

    @classmethod
    def load(cls, path: Path | None = None) -> Glossary:
        data = json.loads(Path(path or DEFAULT_GLOSSARY).read_text(encoding="utf-8"))
        entries = data["entries"] if isinstance(data, dict) else data
        # Longest first so "clawed code" wins over "clawed" at the same spot.
        forms = sorted({e["wrong"] for e in entries}, key=len, reverse=True)
        if not forms:
            return cls(entries, None)
        alt = "|".join(re.escape(f).replace(r"\ ", r"\s+") for f in forms)
        pattern = re.compile(rf"(?<![\w.])(?:{alt})(?![\w])", re.IGNORECASE)
        return cls(entries, pattern)

    def right_for(self, wrong: str) -> str:
        key = " ".join(wrong.lower().split())
        for e in self.entries:
            if e["wrong"].lower() == key:
                return e["right"]
        return "?"

    def scan(self, text: str) -> list[tuple[int, str]]:
        """[(char offset, matched text)] — non-overlapping, case-insensitive."""
        if self.pattern is None:
            return []
        return [(m.start(), m.group(0)) for m in self.pattern.finditer(text)]


# Sections that list mishearings ON PURPOSE (heard -> probable): a glossary
# "wrong" form there is the record of the fix, not a residual mishearing. The
# Stage 3 AIDB run failed P6 on SX9LQD3yINg for exactly this row.
P6_SKIP_SECTIONS = ("transcription uncertainty",)
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _p6_lines(nf: NotesFile) -> list[tuple[int, str]]:
    """Body lines minus the sections in P6_SKIP_SECTIONS (each runs to the
    next heading of the same or a higher level; code fences respected)."""
    out: list[tuple[int, str]] = []
    skip_level: int | None = None
    fenced = False
    for no, line in body_lines(nf):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        elif not fenced and (m := _MD_HEADING.match(line)):
            level = len(m.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            title = re.sub(r"\[[^\]]*\](\([^)]*\))?", "", m.group(2)).strip(" *_`:").lower()
            if skip_level is None and any(title.startswith(s) for s in P6_SKIP_SECTIONS):
                skip_level = level
                continue
        if skip_level is None:
            out.append((no, line))
    return out


def scan_notes_glossary(nf: NotesFile, glossary: Glossary) -> list[dict[str, Any]]:
    hits = []
    for no, line in _p6_lines(nf):
        for _, found in glossary.scan(line):
            hits.append(
                {
                    "file": nf.path.name,
                    "line": no,
                    "found": found,
                    "should_be": glossary.right_for(found),
                    "context": line.strip()[:160],
                }
            )
    return hits


def scan_transcript_glossary(
    lines: list[TranscriptLine], glossary: Glossary
) -> dict[str, Any]:
    """Glossary hits on a transcript, joining cues so split phrases are found.

    Caption cues break mid-phrase ("... cloud" / "code ..."), so the cue texts
    are joined with spaces and each hit is mapped back to the cue it starts in.
    """
    starts, parts, pos = [], [], 0
    for line in lines:
        starts.append(pos)
        parts.append(line.text)
        pos += len(line.text) + 1
    joined = " ".join(parts)
    counts: dict[str, int] = {}
    examples = []
    for offset, found in glossary.scan(joined):
        key = " ".join(found.lower().split())
        counts[key] = counts.get(key, 0) + 1
        if len(examples) < 10:
            idx = bisect.bisect_right(starts, offset) - 1
            examples.append({"timestamp": fmt_ts(lines[idx].t), "found": found})
    return {
        "total": sum(counts.values()),
        "by_form": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "examples": examples,
    }


# --- criteria ----------------------------------------------------------------


def _result(passed: bool, summary: str, **details: Any) -> dict[str, Any]:
    return {"pass": passed, "summary": summary, "details": details}


def check_p1(inp: RunInputs) -> dict[str, Any]:
    if not inp.chapters:
        return _result(
            False, "no expected chapters (source_meta or --expected-chapters)"
        )
    if not inp.notes:
        return _result(False, "no notes files", missing=inp.chapters)
    headings = []
    for nf in inp.notes:
        for _, line in body_lines(nf):
            m = HEADING_RE.match(line)
            if m:
                headings.append(m.group(2))
    padded = [f" {_norm(h)} " for h in headings]
    found, missing = [], []
    for title in inp.chapters:
        key = f" {_norm(title)} "
        (found if any(key in h for h in padded) else missing).append(title)
    return _result(
        not missing,
        f"{len(found)}/{len(inp.chapters)} chapters present as headings",
        source=inp.chapters_source,
        found=found,
        missing=missing,
    )


def check_p2(inp: RunInputs, tolerance: float = DEFAULT_TOLERANCE_S) -> dict[str, Any]:
    if not inp.notes:
        return _result(False, "no notes files")
    if not inp.transcript:
        return _result(False, "no transcript lines to resolve against")
    cites = [c for nf in inp.notes for c in find_citations(nf)]
    if not cites:
        return _result(False, "notes cite no timestamps (nothing is traceable)")
    times = [t.t for t in inp.transcript]
    unresolved = []
    for c in cites:
        for point in sorted({c.start, c.end}):
            reason = None
            if inp.duration is not None and not (0 <= point <= inp.duration):
                reason = f"outside duration {fmt_ts(inp.duration)}"
            else:
                i = bisect.bisect_left(times, point)
                near = [times[j] for j in (i - 1, i) if 0 <= j < len(times)]
                dist = min(abs(t - point) for t in near)
                if dist > tolerance:
                    reason = f"nearest transcript line {dist:.0f}s away"
            if reason:
                unresolved.append(
                    {
                        "citation": c.raw,
                        "point": fmt_ts(point),
                        "file": c.file,
                        "line": c.line,
                        "reason": reason,
                    }
                )
                break
    return _result(
        not unresolved,
        f"{len(cites) - len(unresolved)}/{len(cites)} cited timestamps resolve "
        f"(±{tolerance:.0f}s)",
        cited=len(cites),
        unresolved=unresolved,
        duration_known=inp.duration is not None,
    )


def check_p3(
    inp: RunInputs,
    max_gap: float = DEFAULT_MAX_GAP_S,
    coverage: float = DEFAULT_COVERAGE,
    tail_slack: float = DEFAULT_TAIL_SLACK_S,
) -> dict[str, Any]:
    if not inp.transcript:
        return _result(False, "no transcript lines")
    if inp.duration is None or inp.duration <= 0:
        return _result(False, "duration unknown (source_meta.json or --duration)")
    times = [t.t for t in inp.transcript]
    last = max(times)
    ratio = last / inp.duration
    gaps = []
    prev = 0.0
    backwards = 0
    widest = 0.0
    for t in times:
        if t < prev:
            backwards += 1
        else:
            widest = max(widest, t - prev)
            if t - prev > max_gap and silence_fraction(
                    inp.silences, prev, t) < SILENCE_EXCUSE_FRACTION:
                gaps.append(
                    {
                        "from": fmt_ts(prev),
                        "to": fmt_ts(t),
                        "seconds": round(t - prev, 1),
                    }
                )
        prev = max(prev, t)
    tail = max(0.0, inp.duration - last)
    # The last line's START is what we know; its speech runs on a few seconds.
    tail_silent = (tail > tail_slack and silence_fraction(
        inp.silences, last + tail_slack, inp.duration) >= SILENCE_EXCUSE_FRACTION)
    covered = ratio >= coverage or tail <= tail_slack or tail_silent
    ok = covered and not gaps
    slack_note = (f" (tail {tail:.0f}s <= {tail_slack:.0f}s slack)"
                  if ratio < coverage and tail <= tail_slack else
                  f" (tail {tail:.0f}s is measured silence)" if ratio < coverage and tail_silent
                  else "")
    return _result(
        ok,
        f"last line {fmt_ts(last)} = {ratio:.3%} of {fmt_ts(inp.duration)}{slack_note}; "
        f"{len(gaps)} gap(s) > {max_gap:.0f}s",
        last_timestamp=fmt_ts(last),
        last_timestamp_s=last,
        duration_s=inp.duration,
        coverage=round(ratio, 5),
        coverage_required=coverage,
        tail_s=round(tail, 1),
        tail_slack_s=tail_slack,
        max_gap_s=round(widest, 1),
        gap_limit_s=max_gap,
        gaps_over_limit=gaps,
        non_monotonic_steps=backwards,
        lines=len(times),
    )


def check_p4(inp: RunInputs) -> dict[str, Any]:
    if not inp.notes:
        return _result(False, "no notes files")
    problems, values = [], {}
    for nf in inp.notes:
        fm = nf.frontmatter
        if fm is None:
            problems.append({"file": nf.path.name, "missing": ["frontmatter"]})
            continue
        missing = [k for k in ("published", "as_of") if fm.get(k) in (None, "")]
        values[nf.path.name] = {
            k: None if fm.get(k) is None else str(fm[k]) for k in ("published", "as_of")
        }
        if missing:
            problems.append({"file": nf.path.name, "missing": missing})
    return _result(
        not problems,
        "published/as_of present"
        if not problems
        else f"{len(problems)} file(s) missing fields",
        values=values,
        problems=problems,
    )


def check_p5(inp: RunInputs) -> dict[str, Any]:
    tables = [(nf, t) for nf in inp.notes for t in claims_tables(nf)]
    if not tables:
        return _result(False, "no '## Time-Sensitive Claims' section in notes")
    rows, undated, bad = 0, 0, []
    for nf, table in tables:
        col = table["columns"].get("asof")
        if col is None:
            bad.append(
                {
                    "file": nf.path.name,
                    "line": table["line"],
                    "reason": f"no as-of column in header {table['header']}",
                }
            )
            continue
        for row in table["rows"]:
            rows += 1
            cell = row["cells"][col] if col < len(row["cells"]) else ""
            if not DATE_RE.search(cell):
                undated += 1
                bad.append(
                    {
                        "file": nf.path.name,
                        "line": row["line"],
                        "claim": row["cells"][0] if row["cells"] else "",
                        "as_of": cell,
                        "reason": "as-of is not a date",
                    }
                )
    return _result(
        not bad,
        f"{rows - undated}/{rows} claims dated",
        rows=rows,
        problems=bad,
    )


def check_p6(inp: RunInputs, glossary: Glossary) -> dict[str, Any]:
    if not inp.notes:
        return _result(False, "no notes files")
    hits = [h for nf in inp.notes for h in scan_notes_glossary(nf, glossary)]
    return _result(not hits, f"{len(hits)} residual mishearing(s) in notes", hits=hits)


def check_p7(inp: RunInputs) -> dict[str, Any]:
    path = inp.run_dir / "corrections.json"
    if not path.exists():
        return _result(False, "corrections.json missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _result(False, f"corrections.json unreadable: {exc}")
    if isinstance(data, dict):
        data = data.get("corrections")
    if not isinstance(data, list):
        return _result(False, "corrections.json is not a list")
    bad, warn = [], []
    for i, entry in enumerate(data):
        ts = entry.get("timestamp") if isinstance(entry, dict) else None
        ok = isinstance(ts, (int, float)) and not isinstance(ts, bool)
        if isinstance(ts, str) and ts.strip():
            try:
                parse_ts(ts)
                ok = True
            except ValueError:
                ok = False
        if not ok:
            bad.append(
                {"index": i, "entry": entry, "reason": "missing/invalid timestamp"}
            )
        elif isinstance(entry, dict):
            absent = [k for k in ("original", "corrected", "rule") if not entry.get(k)]
            if absent:
                warn.append({"index": i, "missing": absent})
    return _result(
        not bad,
        f"{len(data) - len(bad)}/{len(data)} corrections carry a timestamp",
        entries=len(data),
        problems=bad,
        warnings=warn,
    )


# --- P8 sampler --------------------------------------------------------------


P8_INSTRUCTIONS = (
    "For each claim, read the transcript excerpt (cited window ±60 s) and judge "
    "whether the transcript at that point supports the claim. Set verdict to "
    "'supported', 'partially_supported', 'not_supported' or 'wrong_timestamp' "
    "(content exists elsewhere), and give a one-line reviewer_note. Judge only "
    "from the excerpt; do not use outside knowledge."
)


def build_p8_sample(
    inp: RunInputs,
    n: int = DEFAULT_P8_N,
    seed: int = DEFAULT_SEED,
    window: float = DEFAULT_P8_WINDOW_S,
) -> dict[str, Any]:
    pool = [b for nf in inp.notes for b in claim_bullets(nf)]
    rng = random.Random(seed)
    picked = rng.sample(pool, min(n, len(pool)))
    claims = []
    for i, b in enumerate(picked, 1):
        lo = min(c["start_s"] for c in b["cited"]) - window
        hi = max(c["end_s"] for c in b["cited"]) + window
        excerpt = [
            {"timestamp": fmt_ts(t.t), "t": t.t, "text": t.text}
            for t in inp.transcript
            if lo <= t.t <= hi
        ]
        claims.append(
            {
                "id": f"C{i:02d}",
                "file": b["file"],
                "line": b["line"],
                "claim": b["claim"],
                "cited": b["cited"],
                "window": [fmt_ts(lo), fmt_ts(hi)],
                "excerpt": excerpt,
                "verdict": None,
                "reviewer_note": None,
            }
        )
    return {
        "criterion": "P8",
        "instructions": P8_INSTRUCTIONS,
        "seed": seed,
        "n_requested": n,
        "n_available": len(pool),
        "n_sampled": len(claims),
        "window_s": window,
        "transcript": str(inp.transcript_path) if inp.transcript_path else None,
        "claims": claims,
    }


# --- driver ------------------------------------------------------------------


def evaluate(
    run_dir: Path,
    *,
    expected_chapters: Path | None = None,
    transcript: Path | None = None,
    duration: float | None = None,
    glossary: Path | None = None,
    max_gap: float = DEFAULT_MAX_GAP_S,
    tolerance: float = DEFAULT_TOLERANCE_S,
    criteria: tuple[str, ...] = CRITERIA,
    p8_n: int = DEFAULT_P8_N,
    seed: int = DEFAULT_SEED,
    scan_transcript: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute the report and the P8 sample. Writes nothing."""
    inp = load_run(
        run_dir,
        expected_chapters=expected_chapters,
        transcript=transcript,
        duration=duration,
    )
    gl = Glossary.load(glossary)
    checks = {
        "P1": lambda: check_p1(inp),
        "P2": lambda: check_p2(inp, tolerance),
        "P3": lambda: check_p3(inp, max_gap),
        "P4": lambda: check_p4(inp),
        "P5": lambda: check_p5(inp),
        "P6": lambda: check_p6(inp, gl),
        "P7": lambda: check_p7(inp),
    }
    results = {k: checks[k]() for k in CRITERIA if k in criteria}
    sample = build_p8_sample(inp, n=p8_n, seed=seed)
    results["P8"] = {
        "pass": None,
        "status": "pending reviewer",
        "summary": f"{sample['n_sampled']} of {sample['n_available']} cited claims "
        f"sampled (seed {seed}) for an independent reviewer",
        "details": {"sample_file": "p8_sample.json", "n_sampled": sample["n_sampled"]},
    }
    report: dict[str, Any] = {
        "run_dir": str(inp.run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "notes": [str(nf.path) for nf in inp.notes],
            "transcript": str(inp.transcript_path) if inp.transcript_path else None,
            "transcript_lines": len(inp.transcript),
            "duration_s": inp.duration,
            "duration_source": inp.duration_source,
            "chapters_source": inp.chapters_source,
            "expected_chapters": len(inp.chapters),
            "glossary": str(glossary or DEFAULT_GLOSSARY),
        },
        "warnings": inp.warnings,
        "criteria_evaluated": [k for k in CRITERIA if k in criteria],
        "partial": tuple(criteria) != CRITERIA,
        "criteria": results,
        "overall_pass": all(results[k]["pass"] for k in CRITERIA if k in results),
        "overall_note": "P1-P7 deterministic; P8 judged separately (pending reviewer)",
    }
    if scan_transcript:
        report["transcript_glossary"] = scan_transcript_glossary(inp.transcript, gl)
    return report, sample


def render_markdown(report: dict[str, Any]) -> str:
    verdict = "PASS" if report["overall_pass"] else "FAIL"
    if report["partial"]:
        verdict += f" (partial: {', '.join(report['criteria_evaluated'])} only)"
    out = [
        "# Long-form eval report",
        "",
        f"- Run dir: `{report['run_dir']}`",
        f"- Generated: {report['generated_at']}",
        f"- **Overall (P1–P7): {verdict}** — {report['overall_note']}",
        "",
        "| # | Result | Summary |",
        "|---|---|---|",
    ]
    for key, res in report["criteria"].items():
        mark = (
            res.get("status")
            if res["pass"] is None
            else ("PASS" if res["pass"] else "FAIL")
        )
        out.append(f"| {key} | {mark} | {res['summary']} |")
    if report["warnings"]:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in report["warnings"]]
    for key, res in report["criteria"].items():
        if res["pass"] is False:
            out += [
                "",
                f"## {key} details",
                "",
                "```json",
                json.dumps(res["details"], indent=2, ensure_ascii=False)[:6000],
                "```",
            ]
    if "transcript_glossary" in report:
        tg = report["transcript_glossary"]
        out += [
            "",
            "## Glossary hits in transcript (informational)",
            "",
            f"Total: {tg['total']}",
            "",
        ]
        out += [f"- `{k}`: {v}" for k, v in tg["by_form"].items()]
    return "\n".join(out) + "\n"


def write_outputs(
    report: dict[str, Any], sample: dict[str, Any], out_dir: Path
) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": out_dir / "eval_report.json",
        "md": out_dir / "eval_report.md",
        "p8": out_dir / "p8_sample.json",
    }
    paths["json"].write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    paths["md"].write_text(render_markdown(report))
    paths["p8"].write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n")
    return paths


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--out", type=Path, help="output dir (default <run_dir>/eval)")
    ap.add_argument("--expected-chapters", type=Path)
    ap.add_argument("--transcript", type=Path)
    ap.add_argument("--duration", type=float, help="override duration (seconds)")
    ap.add_argument("--glossary", type=Path)
    ap.add_argument("--max-gap", type=float, default=DEFAULT_MAX_GAP_S)
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_S)
    ap.add_argument(
        "--criteria",
        default=",".join(CRITERIA),
        help="comma list subset of P1..P7 (report is marked partial)",
    )
    ap.add_argument("--p8-n", type=int, default=DEFAULT_P8_N)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument(
        "--scan-transcript",
        action="store_true",
        help="also count glossary mishearings in the transcript",
    )
    args = ap.parse_args(argv)
    criteria = tuple(c.strip().upper() for c in args.criteria.split(",") if c.strip())
    unknown = set(criteria) - set(CRITERIA)
    if unknown:
        ap.error(f"unknown criteria: {sorted(unknown)}")
    try:
        check_run_dir(args.run_dir, args.transcript)
    except RunDirError as exc:
        import sys
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    report, sample = evaluate(
        args.run_dir,
        expected_chapters=args.expected_chapters,
        transcript=args.transcript,
        duration=args.duration,
        glossary=args.glossary,
        max_gap=args.max_gap,
        tolerance=args.tolerance,
        criteria=criteria,
        p8_n=args.p8_n,
        seed=args.seed,
        scan_transcript=args.scan_transcript,
    )
    paths = write_outputs(report, sample, args.out or args.run_dir / "eval")
    print(render_markdown(report))
    print(f"wrote {paths['json']}, {paths['md']}, {paths['p8']}")
    return 0 if report["overall_pass"] else 1
