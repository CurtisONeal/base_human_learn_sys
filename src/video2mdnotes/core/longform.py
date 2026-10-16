"""Long recordings → complete, traceable, dated notes (WP-3 + WP-7).

A transcript over ``DEFAULT_MAX_WORDS`` cannot go through the single-call
``generate_summary`` — a 12-hour course is ~120k words. This module wires the
pure pieces built in WP-1/2/4/5/8 into one checkpointed pipeline:

    source_meta.json (or yt_info.json)  ->  SourceMeta          core/metadata.py
    transcript provenance                ->  "auto-captions"     core/quality.py
    glossary correction                  ->  corrected copy + corrections.json
    structure ladder on CORRECTED text   ->  chapters / parts    core/structure.py
    one model call per passage           ->  chunk_summaries/ + ledger/  (resumable)
    one course-map call per batch        ->  ## Course Map rows + candidate themes
    one themes call (candidates only)    ->  ### Cross-chapter themes (<= 8)
    deterministic assembly               ->  summaries/<name>.summary.md
    deterministic lint + fix of notes    ->  corrections.json gets `notes:` entries

Context carry — the reason this is not just "summarize each chunk"
-------------------------------------------------------------------
Every passage prompt carries (a) the dated source header, (b) the course
skeleton, (d) the passage with ``[H:MM:SS]`` on every caption line, (e) a
mishearing hint, (f) the uncertainty block and (g) the time-sensitive
fragment. For part i>1 of a split chapter it also carries (c) the running
LEDGER written by part i-1 and an OVERLAP TAIL (last ~150 s of part i-1)
labelled ``CONTEXT ONLY — do not summarize``. Parts of one chapter therefore
run sequentially; different chapters are independent and may run in parallel.

Assembly never re-summarizes chapter notes (FIXED_BUGS: "Recursive reduce
turned a book into 177 words of chat"). The only model call after the
passages is the course map, which returns a table of one-line gists; the
chapter / time-range columns are rendered from the skeleton, not trusted from
the model, and a failed or collapsed call falls back to a deterministic table.

Output layout is the contract in ``eval/longform.py``'s docstring.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core import (
    citations, consistency, domains, names, notes_format, outline as outline_mod, quality,
    spoken_dates, summarizer,
)
from video2mdnotes.core.chunking import DEFAULT_MAX_WORDS
from video2mdnotes.core.costs import BudgetExhausted, current_run_id
from video2mdnotes.core.metadata import META_FILENAME, SourceMeta, from_ytdlp_info, prompt_header
from video2mdnotes.core.structure import (
    PREAMBLE_TITLE, StructureResult, course_skeleton, media_sections, time_locator,
)
from video2mdnotes.core.transcriber import Segment

PROMPT_DIR = BASE_DIR / "prompts"
PASSAGE_PROMPT = PROMPT_DIR / "longform" / "passage.md"
COURSE_MAP_PROMPT = PROMPT_DIR / "longform" / "course_map.md"
THEMES_PROMPT = PROMPT_DIR / "longform" / "course_map_themes.md"
MISHEARING_FRAGMENT = PROMPT_DIR / "fragments" / "mishearing_hint.md"
LEDGER_FRAGMENT = PROMPT_DIR / "fragments" / "ledger.md"

OVERLAP_S = 150.0
LEDGER_MAX_WORDS = 800
CONTEXT_ONLY_LABEL = "CONTEXT ONLY — do not summarize"
STATE_FILENAME = "longform_state.json"
CHUNK_DIR = "chunk_summaries"
LEDGER_DIR = "ledger"
COURSE_MAP_HEADING = "Course Map"
THEMES_HEADING = "Cross-chapter themes"
CANDIDATE_THEMES_HEADING = "Candidate themes"
MAX_THEMES = 8
# Sections pulled out of every passage's notes and merged once at the end.
UNCERTAINTY = "Transcription Uncertainty"
TIME_SENSITIVE = "Time-Sensitive Claims"
FACT_CHECKED = "Fact-Checked Notes"
CROSS_SECTIONS = (UNCERTAINTY, TIME_SENSITIVE, FACT_CHECKED)

CompleteFn = Callable[[str, str], Tuple[str, str]]
Progress = Callable[[str], None]

_TS_LINE_FMT = "[{ts}] {text}"
_CITE_RE = re.compile(r"\[(\d{1,2}(?::\d{2}){1,2})(?:\.\d+)?(?:\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2}){1,2})?\]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LEDGER_RE = re.compile(r"```[ \t]*ledger[^\n]*\n(.*?)(?:\n```|\Z)", re.S | re.I)
_RANGE_IN_HEADING = re.compile(r"\[(\d+:\d{2}:\d{2})\s*[–-]")


def _default_complete(system_prompt: str, user_message: str) -> Tuple[str, str]:
    # Looked up at call time so tests can monkeypatch the summarizer.
    return summarizer.complete_with_failover(system_prompt, user_message)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split())


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^0-9a-z]+", "_", (text or "").lower()).strip("_")
    return (s[:limit].rstrip("_")) or "untitled"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def hms(seconds: Optional[float]) -> str:
    s = int(max(0.0, seconds or 0.0))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def word_count(segments: Sequence) -> int:
    return sum(len((getattr(s, "text", "") or "").split()) for s in segments)


def needs_longform(segments: Sequence, max_words: int = DEFAULT_MAX_WORDS) -> bool:
    """The length gate: over `max_words` words, the single call is not used."""
    return word_count(segments) > max_words


# --- inputs ------------------------------------------------------------------


def raw_transcript_path(run_dir: Path) -> Path:
    files = sorted((Path(run_dir) / "transcripts").glob("*.md"))
    raw = [p for p in files if "corrected" not in p.name]
    if not raw:
        raise FileNotFoundError(f"no transcript in {Path(run_dir) / 'transcripts'}")
    return raw[0]


def notes_name(transcript: Path) -> str:
    """`<name>` of summaries/<name>.summary.md, from the raw transcript file."""
    stem = transcript.name[:-3] if transcript.name.endswith(".md") else transcript.stem
    return re.sub(r"\.captions$", "", stem)


def load_segments(path: Path) -> List[Segment]:
    """Segments from a `- [H:MM:SS.mmm] text` transcript. `end` = next start."""
    _, cues = quality.parse_transcript_markdown(Path(path).read_text(encoding="utf-8"))
    starts = [quality.parse_ts(m.group("ts")) for _, m in cues]
    out = []
    for k, (_, m) in enumerate(cues):
        text = m.group("text")
        end = starts[k + 1] if k + 1 < len(starts) else starts[k] + 0.4 * len(text.split())
        out.append(Segment(start=starts[k], end=max(end, starts[k]), text=text))
    return out


def _transcript_generated_at(path: Path) -> Optional[dt.datetime]:
    fm = quality._frontmatter(Path(path).read_text(encoding="utf-8")[:2000])
    raw = fm.get("generated_at")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def load_meta(run_dir: Path, name: str, transcript: Optional[Path] = None) -> Tuple[SourceMeta, str]:
    """(SourceMeta, where it came from). source_meta.json > yt_info.json > bare."""
    run_dir = Path(run_dir)
    meta = SourceMeta.read(run_dir)
    if meta is not None:
        return meta, META_FILENAME
    yt = run_dir / "yt_info.json"
    if yt.exists():
        meta, origin = from_ytdlp_info(json.loads(yt.read_text(encoding="utf-8"))), "yt_info.json"
    else:
        meta, origin = SourceMeta(source_id=name, title=name), "none (title from file name)"
    # A derived meta for an OLD run: the capture date is when the transcript
    # was made, not now.
    if transcript is not None:
        made = _transcript_generated_at(transcript)
        if made is not None:
            meta.captured_at = made
    return meta, origin


# --- plan --------------------------------------------------------------------


@dataclass
class Passage:
    key: str
    chapter_no: int          # 0 = preamble
    title: str
    group_id: str
    part: int
    parts: int
    start_s: float
    end_s: float
    span: Tuple[int, int]
    words: int

    @property
    def range(self) -> str:
        return time_locator(self.start_s, self.end_s)


@dataclass
class Plan:
    run_dir: Path
    out_dir: Path
    name: str
    raw_transcript: Path
    meta: SourceMeta
    meta_origin: str
    provenance: Optional[quality.TranscriptProvenance]
    glossary: quality.Glossary
    corrections: List[dict]
    segments: List[Segment]          # CORRECTED
    structure: StructureResult
    skeleton: str
    profile: str
    chapters: List[List[Passage]]    # all chapters, in order
    selected: List[List[Passage]]    # after --only
    structure_mode: str = "auto"
    outline: Optional[outline_mod.OutlineResult] = None   # None: rung not tried
    raw_segments: List[Segment] = field(default_factory=list)   # 1:1 with `segments`

    @property
    def passages(self) -> List[Passage]:
        return [p for ch in self.selected for p in ch]

    @property
    def provenance_label(self) -> str:
        return self.provenance.label if self.provenance else "unknown"


def _chapters(structure: StructureResult) -> List[List[Passage]]:
    out: List[List[Passage]] = []
    n = 0
    k = 0
    for group in structure.groups():
        first = group[0]
        title = first.path[0] if first.path else f"Section {len(out) + 1}"
        preamble = title == PREAMBLE_TITLE and first.group_id == "ch00"
        if not preamble:
            n += 1
        number = 0 if preamble else n
        chapter = []
        for sec in group:
            span = structure.segment_spans[k]
            k += 1
            part = sec.part or 1
            chapter.append(Passage(
                key=f"{number:02d}_{_slug(title)}_p{part}",
                chapter_no=number, title=title, group_id=sec.group_id or f"g{number:02d}",
                part=part, parts=sec.parts or 1,
                start_s=float(sec.start_s or 0.0), end_s=float(sec.end_s or 0.0),
                span=tuple(span), words=sec.word_count,
            ))
        out.append(chapter)
    return out


def select_chapters(chapters: List[List[Passage]], only: Optional[str]) -> List[List[Passage]]:
    """`--only "1,MCP Ecosystem"`: chapter numbers or title substrings."""
    if not only or not only.strip():
        return list(chapters)
    tokens = [t.strip() for t in only.split(",") if t.strip()]
    chosen = []
    unmatched = set(tokens)
    for ch in chapters:
        first = ch[0]
        for tok in tokens:
            hit = (tok.isdigit() and int(tok) == first.chapter_no) or (
                not tok.isdigit() and _norm(tok) and _norm(tok) in _norm(first.title))
            if hit:
                unmatched.discard(tok)
                if ch not in chosen:
                    chosen.append(ch)
    if unmatched:
        raise ValueError(f"--only matched no chapter for: {', '.join(sorted(unmatched))}")
    return chosen


def build_plan(
    run_dir: Path,
    out_dir: Optional[Path] = None,
    *,
    only: Optional[str] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    write: bool = True,
    structure: str = "auto",
    outline_complete: Optional[CompleteFn] = None,
) -> Plan:
    """Everything deterministic: meta, provenance, correction, structure, passages.

    With `write`, the out dir gets source_meta.json, the raw transcript (when
    out != run dir), the corrected transcript and corrections.json. The raw
    transcript is never modified.

    `structure` picks the ladder start (auto/outline/cues/windows). The
    outline rung's one model call goes through `outline_complete`; None means
    cache-only (no call), so a plain build_plan never reaches a model. The
    outline cache (structure_outline.json in the out dir) is the one file
    written even when `write` is False — it is what makes a rerun free.
    """
    run_dir = Path(run_dir)
    out_dir = Path(out_dir) if out_dir else run_dir
    raw = raw_transcript_path(run_dir)
    name = notes_name(raw)
    meta, meta_origin = load_meta(run_dir, name, raw)
    provenance = quality.infer_provenance(run_dir)
    glossary = quality.build_glossary(meta)
    segments = load_segments(raw)
    if meta.duration_s and segments and segments[-1].end > meta.duration_s >= segments[-1].start:
        # The last cue's end is a speech-rate estimate; never let it push the
        # media end (and the last chapter's cited range) past the real duration.
        segments[-1] = segments[-1].model_copy(update={"end": float(meta.duration_s)})
    corrected, corrections = quality.correct_segments(segments, glossary)

    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_raw = out_dir / "transcripts" / raw.name
        if out_raw.resolve() != raw.resolve():
            out_raw.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw, out_raw)
        # Always (re)written: an older source_meta.json gains the yt-dlp
        # aliases the evaluator reads; the content is otherwise unchanged.
        meta.write(out_dir)
        _, _, written = quality.write_corrected_transcript(out_raw, glossary)
        corrections = written

    outline_res = None
    if outline_mod.needs_outline(corrected, meta, structure):
        outline_res = outline_mod.obtain_outline(
            corrected, meta, out_dir, complete=outline_complete,
            extra_cache_dirs=[run_dir] if run_dir.resolve() != out_dir.resolve() else [])
    structure_res = media_sections(
        corrected, meta, max_words=max_words, mode=structure,
        outline=outline_res.entries if outline_res else None,
        outline_source=(outline_res.model or "a model") if outline_res else "a model",
        outline_note=outline_res.note if outline_res and outline_res.entries is None else "")
    chapters = _chapters(structure_res)
    return Plan(
        run_dir=run_dir, out_dir=out_dir, name=name, raw_transcript=raw,
        meta=meta, meta_origin=meta_origin, provenance=provenance,
        glossary=glossary, corrections=corrections, segments=corrected,
        structure=structure_res, skeleton=course_skeleton(structure_res, meta),
        profile=domains.longform_profile(meta),
        chapters=chapters, selected=select_chapters(chapters, only),
        structure_mode=structure, outline=outline_res, raw_segments=list(segments),
    )


# --- prompts -----------------------------------------------------------------


@dataclass
class PassagePrompt:
    system: str
    user: str

    @property
    def text(self) -> str:
        return self.system + "\n\n" + self.user


def _sections_block(profile: str) -> str:
    return "\n".join(
        f"{i}. `### {heading}` — {instruction}"
        for i, (heading, instruction) in enumerate(domains.longform_sections(profile), 1)
    )


def mishearing_hint(provenance: Optional[quality.TranscriptProvenance],
                    glossary: Optional[quality.Glossary] = None) -> str:
    """Empty for a human-authored transcript; the hint otherwise."""
    if provenance is not None and not provenance.is_machine:
        return ""
    text = MISHEARING_FRAGMENT.read_text(encoding="utf-8")
    text = text.replace("{{PROVENANCE}}", provenance.label if provenance else "source unknown")
    if glossary is not None and glossary.terms:
        text = text.rstrip() + (
            "\nNames expected in this source: " + ", ".join(glossary.terms[:30]) + ".\n")
    return text


def passage_system_prompt(plan: Plan) -> str:
    parts = [
        PASSAGE_PROMPT.read_text(encoding="utf-8").replace(
            "{{SECTIONS}}", _sections_block(plan.profile)).rstrip(),
        mishearing_hint(plan.provenance, plan.glossary).rstrip(),
        LEDGER_FRAGMENT.read_text(encoding="utf-8").rstrip(),
        quality.uncertainty_prompt_block().rstrip(),
        notes_format.time_sensitive_fragment(plan.meta).rstrip(),
    ]
    return "\n\n".join(p for p in parts if p) + "\n"


def timestamped_lines(segments: Sequence[Segment], i: int, j: int) -> str:
    return "\n".join(
        _TS_LINE_FMT.format(ts=hms(s.start), text=" ".join(s.text.split()))
        for s in segments[i:j]
    )


def overlap_tail(plan: Plan, prev: Passage, seconds: float = OVERLAP_S) -> str:
    a, b = prev.span
    cutoff = prev.end_s - seconds
    k = b
    while k > a and plan.segments[k - 1].start >= cutoff:
        k -= 1
    return timestamped_lines(plan.segments, k, b)


def render_passage_prompt(
    plan: Plan,
    passage: Passage,
    ledger_in: Optional[dict] = None,
    prev: Optional[Passage] = None,
) -> PassagePrompt:
    """The exact (system, user) pair a passage is summarized with."""
    if passage.chapter_no == 0:
        where = f"the Preamble (before chapter 1) [{passage.range}]"
    else:
        where = f'chapter {passage.chapter_no} "{passage.title}"'
        if passage.parts > 1:
            where += f", part {passage.part}/{passage.parts}"
        where += f" [{passage.range}]"

    blocks = [
        f"SOURCE: {prompt_header(plan.meta, plan.provenance_label)}",
        "COURSE SKELETON (the whole recording; context only):\n" + plan.skeleton,
        f"THIS PASSAGE: {where}.",
    ]
    if passage.part > 1 and ledger_in is not None:
        blocks.append(
            f"RUNNING LEDGER from part {passage.part - 1}/{passage.parts} of this "
            "chapter (already covered — build on it, carry it forward; an entry with "
            '"status": "expected" was not shown to work, "superseded" was later '
            "corrected — never restate either as an observed fact):\n"
            "```json\n" + json.dumps(ledger_in, indent=2, ensure_ascii=False) + "\n```"
        )
    if passage.part > 1 and prev is not None:
        tail = overlap_tail(plan, prev)
        if tail:
            blocks.append(
                f"{CONTEXT_ONLY_LABEL} — the last ~{int(OVERLAP_S)} s of part "
                f"{prev.part}/{prev.parts}:\n{tail}\nEND OF CONTEXT ONLY"
            )
    a, b = passage.span
    blocks.append(
        f"PASSAGE TRANSCRIPT ({passage.words:,} words) — write notes for these lines:\n"
        + timestamped_lines(plan.segments, a, b) + "\nEND OF PASSAGE"
    )
    return PassagePrompt(passage_system_prompt(plan), "\n\n".join(blocks) + "\n")


# --- ledger ------------------------------------------------------------------


def _ledger_words(ledger: dict) -> int:
    n = 0
    for c in ledger.get("concepts", []):
        n += len(str(c.get("term", "")).split()) + len(str(c.get("definition", "")).split())
    for c in ledger.get("claims", []):
        n += len(str(c.get("claim", "")).split()) + 2
    for key in ("terms_and_corrections", "open_threads"):
        n += sum(len(str(x).split()) for x in ledger.get(key, []))
    n += len(str(ledger.get("last_point", "")).split())
    return n


LEDGER_STATUSES = ("observed", "expected", "superseded")
_STATUS_ALIASES = {
    "observed": "observed", "confirmed": "observed", "verified": "observed", "shown": "observed",
    "fact": "observed", "expected": "expected", "attempted": "expected", "predicted": "expected",
    "unverified": "expected", "intended": "expected", "superseded": "superseded",
    "corrected": "superseded", "contradicted": "superseded", "retracted": "superseded",
}


def _status(value) -> Optional[str]:
    """A ledger status, normalized; None when absent or unrecognized (old
    ledgers have none — never invent one)."""
    key = re.sub(r"[^a-z]", "", str(value or "").lower())
    return _STATUS_ALIASES.get(key)


def normalize_ledger(raw) -> dict:
    """Ledger in the current schema. Backward compatible: ledgers written
    before WP-F2 have no `claims` and no `status` — they parse unchanged, and
    no status is invented for their concepts."""
    raw = raw if isinstance(raw, dict) else {}
    concepts = []
    for c in raw.get("concepts") or []:
        if isinstance(c, dict):
            term = str(c.get("term", "")).strip()
            if term:
                entry = {"term": term, "definition": str(c.get("definition", "")).strip()}
                st = _status(c.get("status"))
                if st:
                    entry["status"] = st
                concepts.append(entry)
        elif str(c).strip():
            term, _, definition = str(c).partition(" - ")
            concepts.append({"term": term.strip(), "definition": definition.strip()})
    claims = []
    for c in raw.get("claims") or []:
        if isinstance(c, dict):
            text = str(c.get("claim") or c.get("text") or "").strip()
            if not text:
                continue
            # A claim with no recognizable status was not shown to be observed.
            entry = {"claim": text, "status": _status(c.get("status")) or "expected"}
            ts = str(c.get("ts") or c.get("timestamp") or "").strip()
            if ts:
                entry["ts"] = ts
            if c.get("superseded_by"):
                entry["superseded_by"] = str(c["superseded_by"]).strip()
            claims.append(entry)
        elif str(c).strip():
            claims.append({"claim": str(c).strip(), "status": "expected"})

    def strings(key):
        return [str(x).strip() for x in (raw.get(key) or []) if str(x).strip()]

    return {
        "concepts": concepts,
        "claims": claims,
        "terms_and_corrections": strings("terms_and_corrections"),
        "open_threads": strings("open_threads"),
        "last_point": str(raw.get("last_point") or "").strip(),
    }


def cap_ledger(ledger: dict, max_words: int = LEDGER_MAX_WORDS) -> Tuple[dict, int]:
    """Bound a ledger to `max_words`, dropping the OLDEST entries first.

    Deterministic: superseded claims go first, then concepts from the front,
    then claims, terms_and_corrections, open_threads; if `last_point` alone is still
    over, its words are truncated from the start. Returns (ledger, dropped).
    """
    led = normalize_ledger(ledger)
    dropped = 0
    while _ledger_words(led) > max_words and any(
            c.get("status") == "superseded" for c in led["claims"]):
        idx = next(i for i, c in enumerate(led["claims"]) if c.get("status") == "superseded")
        led["claims"].pop(idx)
        dropped += 1
    for key in ("concepts", "claims", "terms_and_corrections", "open_threads"):
        while _ledger_words(led) > max_words and led[key]:
            led[key].pop(0)
            dropped += 1
    if _ledger_words(led) > max_words:
        words = led["last_point"].split()
        led["last_point"] = " ".join(words[len(words) - max_words:])
        dropped += 1
    return led, dropped


def extract_ledger(output: str) -> Tuple[str, Optional[dict], Optional[str]]:
    """(notes with every ledger block removed, parsed ledger or None, error)."""
    blocks = list(_LEDGER_RE.finditer(output or ""))
    notes = _LEDGER_RE.sub("", output or "").rstrip() + "\n"
    if not blocks:
        return notes, None, "no ```ledger block in the reply"
    body = blocks[-1].group(1).strip()
    try:
        return notes, normalize_ledger(json.loads(body)), None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", body, re.S)
        if m:
            try:
                return notes, normalize_ledger(json.loads(m.group(0))), None
            except json.JSONDecodeError as exc:
                return notes, None, f"ledger is not valid JSON: {exc}"
        return notes, None, "ledger is not valid JSON"


def _fallback_ledger(prev: Optional[dict], notes: str) -> dict:
    """When the model wrote no usable ledger: carry the previous one forward and
    take the last point from this passage's last summary bullet."""
    led = normalize_ledger(prev or {})
    summary = section_body(notes, "Summary") or ""
    bullets = [b.strip("-* ").strip() for b in summary.splitlines() if b.strip().startswith(("-", "*"))]
    led["last_point"] = bullets[-1] if bullets else ""
    return led


# --- markdown helpers ----------------------------------------------------------


def _headings(md: str) -> List[Tuple[int, int, str]]:
    """(line index, level, text) of headings outside code fences."""
    out = []
    fenced = False
    for i, line in enumerate(md.split("\n")):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            m = _HEADING_RE.match(line)
            if m:
                out.append((i, len(m.group(1)), m.group(2)))
    return out


def split_sections(md: str) -> Tuple[str, List[Tuple[str, int, str]]]:
    """Split notes at their top heading level: (lead text, [(title, level, body)])."""
    lines = md.split("\n")
    heads = _headings(md)
    if not heads:
        return md.strip(), []
    top = min(level for _, level, _ in heads)
    tops = [(i, level, text) for i, level, text in heads if level == top]
    lead = "\n".join(lines[: tops[0][0]]).strip()
    out = []
    for k, (i, level, text) in enumerate(tops):
        end = tops[k + 1][0] if k + 1 < len(tops) else len(lines)
        out.append((text, level, "\n".join(lines[i + 1 : end]).strip("\n")))
    return lead, out


def _is(title: str, name: str) -> bool:
    return _norm(title).startswith(_norm(name))


def section_body(md: str, name: str) -> Optional[str]:
    for title, _, body in split_sections(md)[1]:
        if _is(title, name):
            return body
    return None


def shift_headings(md: str, delta: int) -> str:
    if delta == 0:
        return md
    lines = md.split("\n")
    for i, level, text in _headings(md):
        lines[i] = "#" * max(1, min(6, level + delta)) + " " + text
    return "\n".join(lines)


def chapter_body(notes: str, level: int) -> str:
    """A passage's notes without the cross-cutting sections, sections at `level`."""
    lead, sections = split_sections(notes)
    out = []
    if lead and not summarizer._CHAT_OPENER.search(lead):
        out.append(lead)
    for title, lvl, body in sections:
        if any(_is(title, c) for c in CROSS_SECTIONS):
            continue
        out.append("#" * level + " " + title)
        if body.strip():
            out.append(shift_headings(body, level - lvl).strip("\n"))
    return "\n\n".join(out).strip() + "\n"


def _is_none(body: Optional[str]) -> bool:
    text = _norm(body or "")
    return text in {"", "none", "n a", "na", "none found", "nothing to check"}


# --- checkpoint state ----------------------------------------------------------


class State:
    """longform_state.json: status per passage, written atomically, thread-safe."""

    def __init__(self, out_dir: Path):
        self.path = Path(out_dir) / STATE_FILENAME
        self.lock = threading.Lock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"version": 1, "run_id": current_run_id(), "created_at": _now(),
                         "passages": {}, "course_map": {}}
        self.data.setdefault("passages", {})
        self.data.setdefault("course_map", {})

    def save(self) -> None:
        self.data["updated_at"] = _now()
        _atomic_write(self.path, json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")

    def update(self, section: str, key: str, **fields) -> None:
        with self.lock:
            self.data[section].setdefault(key, {}).update(fields)
            self.save()

    def passage(self, key: str) -> dict:
        with self.lock:
            return dict(self.data["passages"].get(key, {}))


def chunk_path(out_dir: Path, p: Passage) -> Path:
    return Path(out_dir) / CHUNK_DIR / f"{p.key}.md"


def ledger_path(out_dir: Path, p: Passage) -> Path:
    return Path(out_dir) / LEDGER_DIR / f"{p.chapter_no:02d}_p{p.part}.json"


def is_done(state: State, out_dir: Path, p: Passage) -> bool:
    rec = state.passage(p.key)
    return (
        rec.get("status") == "done"
        and abs(float(rec.get("start_s", -1)) - p.start_s) < 0.5
        and abs(float(rec.get("end_s", -1)) - p.end_s) < 0.5
        and chunk_path(out_dir, p).exists()
        and ledger_path(out_dir, p).exists()
    )


# --- citation audit (WP-F1) -------------------------------------------------------


def passage_caption_lines(plan: Plan, p: Passage) -> List[Tuple[float, str]]:
    """(start, text) of the passage's own corrected caption lines (no overlap)."""
    a, b = p.span
    return [(s.start, " ".join(s.text.split())) for s in plan.segments[a:b]]


def passage_raw_lines(plan: Plan, p: Passage) -> List[Tuple[float, str]]:
    """The same lines from the RAW transcript (fuzzy audit only, WP-F8)."""
    a, b = p.span
    if len(plan.raw_segments) != len(plan.segments):
        return []
    return [(s.start, " ".join(s.text.split())) for s in plan.raw_segments[a:b]]


def audit_passage_notes(plan: Plan, p: Passage, notes: str) -> Tuple[str, List[dict]]:
    audited, entries = citations.audit_notes(notes, passage_caption_lines(plan, p),
                                             passage_raw_lines(plan, p))
    for e in entries:
        e["passage"] = p.key
    return audited, entries


def _audit_counts(entries: Sequence[dict]) -> dict:
    out: Dict[str, int] = {}
    for e in entries:
        key = str(e.get("rule", "")).split(":", 1)[-1]
        out[key] = out.get(key, 0) + 1
    return out


_AUDIT_LOCK = threading.Lock()


def record_citation_audit(out_dir: Path, p: Passage, entries: List[dict]) -> Path:
    """citation_audit.json: this passage's entries replace any earlier ones."""
    path = Path(out_dir) / citations.AUDIT_FILENAME
    with _AUDIT_LOCK:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        data = [e for e in data if e.get("passage") != p.key] + list(entries)
        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def reaudit_chunks(run_dir: Path, out_dir: Optional[Path] = None, *,
                   max_words: int = DEFAULT_MAX_WORDS, write: bool = True) -> List[dict]:
    """Re-run the citation audit over the chunk notes already on disk (no model
    calls). Rewrites chunk_summaries/<key>.md and citation_audit.json when
    `write`; returns every log entry. Re-auditing audited notes is a no-op."""
    plan = build_plan(run_dir, out_dir, max_words=max_words, write=False)
    entries_all: List[dict] = []
    for ch in plan.chapters:
        for p in ch:
            path = chunk_path(plan.out_dir, p)
            if not path.exists():
                continue
            notes = path.read_text(encoding="utf-8")
            audited, entries = audit_passage_notes(plan, p, notes)
            entries_all.extend(entries)
            if write:
                if audited != notes:
                    _atomic_write(path, audited)
                record_citation_audit(plan.out_dir, p, entries)
    return entries_all


# --- running passages ------------------------------------------------------------


class LongformIncomplete(RuntimeError):
    """Some passages failed (not exhaustion). Completed ones are checkpointed."""


@dataclass
class LongformResult:
    summary_path: Optional[Path]
    notes: str = ""
    models: List[str] = field(default_factory=list)
    passages_run: int = 0
    passages_skipped: int = 0
    course_map_calls: int = 0
    course_map_fallback: List[str] = field(default_factory=list)
    notes_corrections: List[dict] = field(default_factory=list)
    lint_residual: List[dict] = field(default_factory=list)


def _run_chapter(plan: Plan, chapter: List[Passage], state: State, complete: CompleteFn,
                 resume: bool, stop: threading.Event, counters: dict,
                 progress: Progress) -> None:
    ledger: Optional[dict] = None
    prev: Optional[Passage] = None
    for p in chapter:
        if resume and is_done(state, plan.out_dir, p):
            ledger = json.loads(ledger_path(plan.out_dir, p).read_text(encoding="utf-8"))
            prev = p
            with state.lock:
                counters["skipped"] += 1
            progress(f"  skip {p.key} (checkpoint)")
            continue
        if stop.is_set():
            return
        prompt = render_passage_prompt(plan, p, ledger_in=ledger, prev=prev)
        started = _now()
        state.update("passages", p.key, status="running", started_at=started,
                     chapter=p.chapter_no, title=p.title, part=p.part, parts=p.parts,
                     start_s=p.start_s, end_s=p.end_s)
        progress(f"  run  {p.key} [{p.range}] {p.words:,} words")
        try:
            output, model = complete(prompt.system, prompt.user)
        except (summarizer.SubscriptionExhausted, BudgetExhausted) as exc:
            state.update("passages", p.key, status="pending",
                         stopped=f"{type(exc).__name__}: {str(exc)[:200]}")
            stop.set()
            raise
        except Exception as exc:  # noqa: BLE001 - record; other chapters continue
            state.update("passages", p.key, status="failed", error=str(exc)[:400])
            raise
        notes, led, err = extract_ledger(output)
        notes = summarizer._strip_chat_preamble(notes).rstrip() + "\n"
        # Audit citations before the checkpoint, so chunk_summaries/ holds
        # audited notes (WP-F1).
        notes, audit = audit_passage_notes(plan, p, notes)
        record_citation_audit(plan.out_dir, p, audit)
        source = "model"
        if led is None:
            led, source = _fallback_ledger(ledger, notes), f"fallback ({err})"
        led, dropped = cap_ledger(led)
        _atomic_write(chunk_path(plan.out_dir, p), notes)
        _atomic_write(ledger_path(plan.out_dir, p),
                      json.dumps(led, indent=2, ensure_ascii=False) + "\n")
        state.update("passages", p.key, status="done", model=model, finished_at=_now(),
                     ledger_words=_ledger_words(led), ledger_dropped=dropped,
                     citation_audit=_audit_counts(audit),
                     ledger_source=source, notes_words=len(notes.split()),
                     error=None, stopped=None)
        with state.lock:
            counters["run"] += 1
        ledger, prev = led, p


def run_passages(plan: Plan, state: State, complete: CompleteFn, *, resume: bool = True,
                 concurrency: int = 1, progress: Progress = print) -> Tuple[int, int]:
    """Run every selected passage not already checkpointed. Returns (run, skipped).

    Exhaustion (SubscriptionExhausted / BudgetExhausted) stops new passages and
    is re-raised after in-flight chapters finish their current passage; every
    completed passage stays on disk. Ordinary failures are collected and raised
    as LongformIncomplete after all chapters have been attempted.
    """
    stop = threading.Event()
    counters = {"run": 0, "skipped": 0}
    errors: List[Tuple[str, BaseException]] = []

    def one(chapter):
        try:
            _run_chapter(plan, chapter, state, complete, resume, stop, counters, progress)
        except BaseException as exc:  # noqa: BLE001
            errors.append((chapter[0].title, exc))

    if concurrency <= 1:
        for chapter in plan.selected:
            one(chapter)
            if stop.is_set():
                break
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, plan.selected))

    for _, exc in errors:
        if isinstance(exc, (summarizer.SubscriptionExhausted, BudgetExhausted)):
            raise exc
    for _, exc in errors:
        if isinstance(exc, KeyboardInterrupt):
            raise exc
    if errors:
        raise LongformIncomplete(
            f"{len(errors)} chapter(s) failed; completed passages are checkpointed: "
            + "; ".join(f"{t}: {str(e)[:120]}" for t, e in errors)
        )
    return counters["run"], counters["skipped"]


# --- course map --------------------------------------------------------------------


def course_map_batches(chapters: List[List[Passage]], batch: int = None) -> List[List[List[Passage]]]:
    """Chapters grouped so each batch carries at most `_REDUCE_BATCH` passages
    (a chapter is never split across batches)."""
    batch = batch or summarizer._REDUCE_BATCH
    out: List[List[List[Passage]]] = []
    size = 0
    for ch in chapters:
        if out and size + len(ch) <= batch:
            out[-1].append(ch)
            size += len(ch)
        else:
            out.append([ch])
            size = len(ch)
    return out


def _chapter_label(ch: List[Passage]) -> str:
    return "-" if ch[0].chapter_no == 0 else str(ch[0].chapter_no)


def _chapter_range(ch: List[Passage]) -> str:
    return time_locator(ch[0].start_s, ch[-1].end_s)


def _deterministic_gist(notes: List[str]) -> str:
    for md in notes:
        body = section_body(md, "Summary") or ""
        for line in body.splitlines():
            s = line.strip()
            if s.startswith(("-", "*")):
                s = _CITE_RE.sub("", s.strip("-* ")).strip()
                words = s.split()
                return " ".join(words[:25]) + ("…" if len(words) > 25 else "")
    return "(no summary)"


def _course_map_input(batch: List[List[Passage]], notes_by_key: Dict[str, str]) -> str:
    out = ["Chapters given (copy #, chapter and time range exactly):", ""]
    for ch in batch:
        out.append(f"## {_chapter_label(ch)} | {ch[0].title} | [{_chapter_range(ch)}]")
        for p in ch:
            md = notes_by_key[p.key]
            for name in ("Summary", "Key Concepts", "Exam-relevant points"):
                body = section_body(md, name)
                if body and not _is_none(body):
                    out.append(f"### {name}" + (f" (part {p.part}/{p.parts})" if p.parts > 1 else ""))
                    out.append(body.strip())
        out.append("")
    return "\n".join(out)


def parse_course_map(text: str) -> Tuple[Dict[str, str], List[str]]:
    """({chapter label or normalized title: gist}, theme bullets) from a reply."""
    gists: Dict[str, str] = {}
    themes: List[str] = []
    in_themes = False
    for line in (text or "").splitlines():
        s = line.strip()
        h = _HEADING_RE.match(s)
        if h:
            in_themes = _is(h.group(2), THEMES_HEADING) or _is(h.group(2), CANDIDATE_THEMES_HEADING)
            continue
        if re.match(r"^\**(?:cross[- ]chapter|candidate) themes\**:?$", s, re.I):
            in_themes = True
            continue
        if in_themes and s.startswith(("-", "*")):
            themes.append(s.lstrip("-* ").strip())
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 4 or all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if _norm(cells[0]) in {"", "#"} and _norm(cells[1]) == "chapter":
                continue
            label = cells[0].strip("* ")
            gist = cells[-1].strip()
            if gist and _norm(gist) != "gist":
                gists[label] = gist
                gists["t:" + _norm(cells[1])] = gist
    return gists, themes


def _map_collapsed(text: str, batch: List[List[Passage]], gists: Dict[str, str]) -> Optional[str]:
    """Why a course-map reply is unusable, or None. Judged by what it contains
    (rows for the chapters given), not by phrasing — but an offer of further
    work is the conversational-collapse signature from FIXED_BUGS."""
    found = sum(1 for ch in batch
                if _chapter_label(ch) in gists or "t:" + _norm(ch[0].title) in gists)
    if found == 0:
        return f"no table rows for any of {len(batch)} chapters"
    if summarizer._CHAT_OFFER.search(text or "") and found < len(batch):
        return "chat reply instead of a table"
    return None


# Cross-chapter themes (WP-F7). The map is built in batches, so each batch
# names its own themes and E2 printed 24 of them, "Agent SDK vs Anthropic SDK"
# three times. Batches now return CANDIDATE themes; one small final call (input:
# the candidate bullets and chapter titles only — never the chapter notes)
# consolidates them into <= MAX_THEMES, with a deterministic merge as fallback.

_THEME_CH = re.compile(r"\s*\((?:ch(?:apters?|s)?\.?|§)\s*([^)]*)\)\s*\.?\s*$", re.I)


def split_theme(theme: str) -> Tuple[str, List[str]]:
    """("Theme text", ["2", "5"]) from "Theme text (ch. 2, 5)"."""
    m = _THEME_CH.search(theme or "")
    if not m:
        return (theme or "").strip(), []
    chs = [c for c in re.split(r"[,;&\s]+|\band\b", m.group(1)) if c.strip()]
    return theme[: m.start()].strip(), chs


def _ch_key(c: str):
    return (0, int(c)) if c.isdigit() else (1, c)


def render_theme(text: str, chapters: Sequence[str]) -> str:
    chs = sorted(set(chapters), key=_ch_key)
    return f"{text} (ch. {', '.join(chs)})" if chs else text


def merge_themes(candidates: Sequence[str], limit: int = MAX_THEMES,
                 threshold: float = 0.5) -> List[str]:
    """Deterministic consolidation: candidates naming the same idea (overlap
    coefficient of their distinctive word stems >= `threshold`) merge into one
    theme citing the union of chapters; themes spanning the most chapters are
    kept, at most `limit`, in order of first appearance."""
    items = []
    for raw in candidates:
        text, chs = split_theme(raw)
        if not text:
            continue
        toks = {t for t in citations.tokens(text)
                if len(t) >= 3 and not t.isdigit() and t not in citations.STOPWORDS}
        items.append((text, chs, toks))
    if not items:
        return []
    # Words in most candidates ("claude", "agent" in an agents course) do not
    # distinguish one theme from another.
    df: Dict[str, int] = {}
    for _, _, toks in items:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    common = {t for t, n in df.items() if len(items) >= 5 and n / len(items) > 0.4}
    clusters: List[dict] = []
    for idx, (text, chs, toks) in enumerate(items):
        key = toks - common or toks
        best, best_score = None, 0.0
        for c in clusters:
            score = max((len(key & k) / min(len(key), len(k)) for k in c["keys"] if k and key),
                        default=0.0)
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= threshold:
            best["members"].append((text, chs))
            best["keys"].append(key)
            best["chapters"].update(chs)
        else:
            clusters.append({"first": idx, "members": [(text, chs)], "keys": [key],
                             "chapters": set(chs)})
    for c in clusters:
        # The member citing the most chapters names the theme (earliest on ties).
        c["text"] = max(c["members"], key=lambda m: len(set(m[1])))[0]
    ranked = sorted(clusters, key=lambda c: (-len(c["chapters"]), c["first"]))[:limit]
    ranked.sort(key=lambda c: c["first"])
    return [render_theme(c["text"], c["chapters"]) for c in ranked]


# Theme tagging (WP-F8): E2b's themes named topics ("hooks", "subagents") but
# cited only some of the chapters that teach them — the themes call saw
# chapter TITLES only. It now gets a short deterministic topic list per chapter
# (vocabulary terms + sub-headings of that chapter's notes), and a post-check
# adds every chapter whose topic list holds a term the theme names.

MAX_TOPICS = 8
_TOPIC_BULLET = re.compile(
    r"^\s*[-*+]\s+(?:`([^`]+)`|\*\*([^*]+)\*\*|([^:—–\[`*]{2,60}?)(?:\s*:|\s+[-—–])\s)")
_STANDARD_HEADINGS = None


def _standard_headings() -> set:
    global _STANDARD_HEADINGS
    if _STANDARD_HEADINGS is None:
        names = {h for prof in domains.LONGFORM_SECTIONS.values() for h, _ in prof}
        _STANDARD_HEADINGS = {_norm(h) for h in names} | {"part"}
    return _STANDARD_HEADINGS


def _clean_topic(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", text or "")
    text = _CITE_RE.sub(" ", text)
    return " ".join(text.strip(" -*_`'\"").split())


def chapter_topics(ch: List[Passage], notes_by_key: Dict[str, str],
                   limit: int = MAX_TOPICS) -> List[str]:
    """Key topics of one chapter, deterministic: the vocabulary terms of its
    notes (in order), then its non-standard sub-headings; at most `limit`."""
    out: List[str] = []
    seen = set()

    def add(t: str):
        t = _clean_topic(t)
        key = _norm(t)
        if t and len(key) >= 3 and len(t.split()) <= 6 and key not in seen:
            seen.add(key)
            out.append(t)

    for p in ch:
        md = notes_by_key.get(p.key, "")
        body = section_body(md, "Key Concepts") or ""
        for line in body.splitlines():
            m = _TOPIC_BULLET.match(line)
            if m:
                add(next(g for g in m.groups() if g))
    for p in ch:
        for _, _, text in _headings(notes_by_key.get(p.key, "")):
            if not any(_norm(text).startswith(s) for s in _standard_headings()):
                add(text)
    return out[:limit]


def _topic_toks(topic: str) -> Tuple[str, ...]:
    return tuple(citations.tokens(topic))


def _contains(seq: Sequence[str], sub: Sequence[str]) -> bool:
    n = len(sub)
    return any(tuple(seq[i:i + n]) == tuple(sub) for i in range(len(seq) - n + 1))


GENERIC_TOPIC_DF = 0.4


def generic_topic_words(chapter_texts: Dict[str, str], max_df: float = GENERIC_TOPIC_DF) -> set:
    """Words said in the notes of more than `max_df` of the chapters ("agent"
    in an agents course): as a ONE-word topic they name nothing specific."""
    if len(chapter_texts) < 3:
        return set()
    df: Dict[str, int] = {}
    for text in chapter_texts.values():
        for tok in set(citations.tokens(text)):
            df[tok] = df.get(tok, 0) + 1
    n = len(chapter_texts)
    return {t for t, k in df.items() if k / n > max_df}


def chapter_topic_lists(plan: Plan, notes_by_key: Dict[str, str]
                        ) -> Tuple[Dict[str, List[str]], List[str]]:
    """({chapter label: key topics}, one-word topics dropped as generic). The
    same lists go to the themes call and to the tagging post-check."""
    raw = {_chapter_label(ch): chapter_topics(ch, notes_by_key, limit=3 * MAX_TOPICS)
           for ch in plan.selected}
    generic = generic_topic_words({
        _chapter_label(ch): "\n".join(chapter_body(notes_by_key[p.key], 3) for p in ch)
        for ch in plan.selected})
    dropped: List[str] = []
    topics: Dict[str, List[str]] = {}
    for label, ts in raw.items():
        keep = []
        for t in ts:
            tt = _topic_toks(t)
            if len(tt) == 1 and tt[0] in generic:
                if t not in dropped:
                    dropped.append(t)
                continue
            keep.append(t)
        topics[label] = keep[:MAX_TOPICS]
    return topics, dropped


def tag_theme_chapters(themes: Sequence[str], topics: Dict[str, List[str]],
                       generic: Optional[set] = None) -> Tuple[List[str], List[dict]]:
    """Every chapter whose topic list holds a term a theme names is tagged on
    that theme. Returns (themes, additions log). Tags are only ever added.
    A one-word topic in `generic` (said across most chapters) is not a term."""
    generic = generic or set()
    out, log = [], []
    for theme in themes:
        text, chs = split_theme(theme)
        toks = citations.tokens(text)
        have = list(chs)
        for label, tlist in topics.items():
            if label in have or label == "-":
                continue
            for t in tlist:
                tt = _topic_toks(t)
                meaningful = [x for x in tt if x not in citations.STOPWORDS]
                if not meaningful or (len(tt) == 1 and (len(tt[0]) < 4 or tt[0] in generic)):
                    continue
                if _contains(toks, tt):
                    have.append(label)
                    log.append({"theme": text, "chapter": label, "topic": t})
                    break
        out.append(render_theme(text, have) if have != list(chs) else theme)
    return out, log


def _themes_input(plan: Plan, candidates: Sequence[str],
                  topics: Optional[Dict[str, List[str]]] = None) -> str:
    out = ["Chapters (for reference only):"]
    for ch in plan.selected:
        label = _chapter_label(ch)
        line = f"- {label}: {ch[0].title}"
        if topics and topics.get(label):
            line += " — key topics: " + "; ".join(topics[label])
        out.append(line)
    if topics:
        out += ["", "Tag a theme with EVERY chapter whose key topics include what the "
                "theme names, not only the chapters of the candidates."]
    out += ["", f"Candidate themes from the batches ({len(candidates)}):"]
    out += [f"- {c}" for c in candidates]
    return "\n".join(out) + "\n"


def consolidate_themes(plan: Plan, candidates: List[str], state: State, complete: CompleteFn,
                       resume: bool = True, progress: Progress = print,
                       topics: Optional[Dict[str, List[str]]] = None,
                       ) -> Tuple[List[str], Optional[str], int, Optional[str]]:
    """(themes, model, calls made, fallback reason). One model call over the
    candidate bullets only; a failed, empty or chatty reply falls back to
    `merge_themes`. With a single batch the candidates are already one view:
    no call, just the deterministic dedupe and cap."""
    if not candidates:
        return [], None, 0, None
    if len(course_map_batches(plan.selected)) <= 1:
        return merge_themes(candidates), None, 0, None
    cache = Path(plan.out_dir) / CHUNK_DIR / "_course_map_themes.md"
    rec = state.data["course_map"].get("themes", {})
    reply, model, calls = None, None, 0
    if resume and rec.get("candidates") == list(candidates) and cache.exists():
        reply, model = cache.read_text(encoding="utf-8"), rec.get("model")
        progress("  skip course map themes (checkpoint)")
    else:
        progress(f"  run  course map themes ({len(candidates)} candidates)")
        try:
            reply, model = complete(THEMES_PROMPT.read_text(encoding="utf-8"),
                                    _themes_input(plan, candidates, topics))
            calls = 1
            _atomic_write(cache, reply)
            state.update("course_map", "themes", candidates=list(candidates), model=model,
                         finished_at=_now())
        except (summarizer.SubscriptionExhausted, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - deterministic merge instead
            return merge_themes(candidates), None, calls, f"themes: call failed ({str(exc)[:120]})"
    _, themes = parse_course_map(reply or "")
    if not themes or (summarizer._CHAT_OFFER.search(reply or "") and len(themes) < 3):
        return merge_themes(candidates), None, calls, "themes: no theme bullets in reply"
    # Cap and drop duplicates the model left in; it already merged the rest.
    return merge_themes(themes, threshold=0.8), model, calls, None


def build_course_map(plan: Plan, notes_by_key: Dict[str, str], state: State,
                     complete: CompleteFn, resume: bool = True,
                     progress: Progress = print) -> Tuple[str, List[str], int, List[str]]:
    """(markdown section, models used, calls made, fallback reasons)."""
    system = COURSE_MAP_PROMPT.read_text(encoding="utf-8")
    rows: List[str] = []
    candidates: List[str] = []
    models: List[str] = []
    calls = 0
    fallbacks: List[str] = []
    for b, batch in enumerate(course_map_batches(plan.selected), 1):
        keys = [p.key for ch in batch for p in ch]
        bid = f"batch{b:02d}"
        cache = Path(plan.out_dir) / CHUNK_DIR / f"_course_map_{bid}.md"
        rec = state.data["course_map"].get(bid, {})
        reply, model = None, None
        if resume and rec.get("keys") == keys and cache.exists():
            reply, model = cache.read_text(encoding="utf-8"), rec.get("model")
            progress(f"  skip course map {bid} (checkpoint)")
        else:
            progress(f"  run  course map {bid} ({len(batch)} chapters)")
            try:
                reply, model = complete(system, _course_map_input(batch, notes_by_key))
                calls += 1
                _atomic_write(cache, reply)
                state.update("course_map", bid, keys=keys, model=model, finished_at=_now())
            except (summarizer.SubscriptionExhausted, BudgetExhausted):
                raise
            except Exception as exc:  # noqa: BLE001 - deterministic table instead
                fallbacks.append(f"{bid}: call failed ({str(exc)[:120]})")
        gists, batch_themes = parse_course_map(reply or "")
        if reply is not None:
            why = _map_collapsed(reply, batch, gists)
            if why:
                fallbacks.append(f"{bid}: {why}")
                gists, batch_themes = {}, []
            elif model:
                models.append(model)
        for ch in batch:
            label = _chapter_label(ch)
            gist = gists.get(label) or gists.get("t:" + _norm(ch[0].title))
            if not gist:
                gist = _deterministic_gist([notes_by_key[p.key] for p in ch])
            gist = notes_format.sanitize_cell(gist)
            rows.append(f"| {label} | {notes_format.sanitize_cell(display_title(plan, ch[0].title))} "
                        f"| [{_chapter_range(ch)}] | {gist} |")
        candidates.extend(t for t in batch_themes if t not in candidates)

    topics, generic_dropped = chapter_topic_lists(plan, notes_by_key)
    themes, theme_model, theme_calls, theme_fallback = consolidate_themes(
        plan, candidates, state, complete, resume=resume, progress=progress, topics=topics)
    themes, tags_added = tag_theme_chapters(themes, topics)
    with state.lock:
        state.data["course_map"]["theme_tags"] = {
            "topics": topics, "added": tags_added,
            "generic_one_word_topics_dropped": generic_dropped}
        state.save()
    calls += theme_calls
    if theme_model:
        models.append(theme_model)
    if theme_fallback:
        fallbacks.append(theme_fallback)

    out = [f"## {COURSE_MAP_HEADING}", "", "| # | chapter | time range | gist |",
           "|---|---|---|---|", *rows]
    if fallbacks:
        out += ["", "> Course map: some parts are deterministic (gists: first summary "
                "point; themes: merged candidates) — " + "; ".join(fallbacks)]
    out += ["", f"### {THEMES_HEADING}", ""]
    out += [f"- {t}" for t in themes] if themes else ["- None identified"]
    return "\n".join(out) + "\n", models, calls, fallbacks


# --- assembly -----------------------------------------------------------------------


def outline_speakers(plan: Plan) -> List[Tuple[str, List[str]]]:
    """(speaker, spelling variants) of the outline entries, in order, unique."""
    out: List[Tuple[str, List[str]]] = []
    entries = plan.outline.entries if plan.outline is not None and plan.outline.entries else []
    for e in entries:
        if e.speaker and all(e.speaker != s for s, _ in out):
            out.append((e.speaker, list(e.speaker_variants or [])))
    return out


def _asr_names(plan: Plan) -> bool:
    """Names in this source rest on machine transcription (WP-F8)."""
    return names.is_machine(plan.provenance)


def display_title(plan: Plan, title: str) -> str:
    """The chapter title as shown in headings and the Course Map: an outline
    speaker name the metadata does not confirm is hedged when the transcript
    is machine-made. Passage keys keep the plain title."""
    if not _asr_names(plan):
        return title
    speakers = [s for s, _ in outline_speakers(plan)]
    if not speakers:
        return title
    return names.hedge_title(title, speakers, names.metadata_words(plan.meta))


def _merge_uncertainty(plan: Plan, notes_by_key: Dict[str, str]) -> Tuple[str, dict]:
    items: Dict[Tuple[str, str], dict] = {}
    omitted = known = 0
    asr = _asr_names(plan)
    vocab = names.TranscriptVocab.build(plan.segments) if asr else None
    meta_words = names.metadata_words(plan.meta)
    respellings: List[Tuple[str, str]] = []
    for p in plan.passages:
        parsed = quality.parse_uncertainty_section(notes_by_key[p.key])
        if parsed is None:
            omitted += 1
            continue
        for it in parsed:
            # A form the glossary already fixes deterministically is logged in
            # corrections.json; repeating it here would re-print the mishearing.
            if quality.correct_notes(it["heard"], plan.glossary)[1]:
                known += 1
                continue
            # WP-F8: "Curtis O'Neill -> O'Neal" is not a correction the audio
            # supports; it moves to the names subsection, unapplied.
            if asr and names.is_name_respelling(it["heard"], it["probable"], vocab, meta_words):
                if (it["heard"], it["probable"]) not in respellings:
                    respellings.append((it["heard"], it["probable"]))
                continue
            key = (it["heard"].lower(), it["probable"].lower())
            if key not in items or quality.parse_ts(it["timestamp"]) < quality.parse_ts(items[key]["timestamp"]):
                items[key] = it
    rows = sorted(items.values(), key=lambda it: quality.parse_ts(it["timestamp"]))
    out = [f"## {UNCERTAINTY}", ""]
    if rows:
        out += ["| timestamp | heard | probable | confidence |", "|---|---|---|---|"]
        cell = notes_format.sanitize_cell
        out += [f"| [{it['timestamp']}] | {cell(it['heard'])} | "
                f"{cell(it['probable'])} | {cell(it['confidence'])} |" for it in rows]
    else:
        out.append("- None flagged by the passages.")
    notes = []
    if known:
        notes.append(f"{known} flagged form(s) already fixed by the glossary pass "
                     "(see corrections.json)")
    if omitted:
        notes.append(f"{omitted} passage(s) returned no {UNCERTAINTY} section")
    if notes:
        out += ["", "> " + "; ".join(notes) + "."]
    name_rows: List[names.NameRow] = []
    if asr:
        name_rows = names.names_rows(
            speakers=outline_speakers(plan),
            notes=[chapter_body(notes_by_key[p.key], 3) for p in plan.passages],
            respellings=respellings, vocab=vocab, meta_words=meta_words)
        block = names.render_names(name_rows, plan.provenance_label)
        if block:
            out += ["", block.rstrip("\n")]
    return "\n".join(out) + "\n", {
        "rows": len(rows), "glossary_known": known, "omitted": omitted,
        "names_unverified": [{"name": r.name, "spellings": r.spellings, "basis": r.source}
                             for r in name_rows],
        "name_respellings_not_applied": [{"heard": h, "probable": p} for h, p in respellings],
    }


MODEL_KNOWLEDGE = "per model knowledge (cutoff uncertain)"


def fix_fact_checked_wording(text: str, meta: SourceMeta) -> str:
    """Model-knowledge checks never claim "correct as of <published date>"
    (WP-F3): the model cannot know what was true on that date. Rewrites that
    phrase (and its usual tails) to `correct per model knowledge (cutoff
    uncertain)`. Other dates — a date the speaker gave — are left alone."""
    dates = set()
    as_of = notes_format.resolve_as_of(meta)
    if as_of.date:
        dates.add(str(as_of.date))
    if meta.published_at:
        dates.add(str(meta.published_at)[:10])
    if not dates:
        return text
    alt = "|".join(re.escape(d) for d in sorted(dates))
    pat = re.compile(
        rf"\b(?:is\s+)?correct\s+as\s+of\s+(?:{alt})(?:\s*\(captured\))?"
        rf"(?:\s+(?:according\s+to\s+my\s+knowledge|as\s+far\s+as\s+I\s+can\s+tell))?"
        rf"(?:\s*[;,]\s*may\s+have\s+changed\s+since)?",
        re.I)

    def sub(m):
        lead = "is " if m.group(0).lower().startswith("is") else ""
        return f"{lead}correct {MODEL_KNOWLEDGE}"
    return pat.sub(sub, text)


def _merge_fact_checked(plan: Plan, notes_by_key: Dict[str, str]) -> str:
    out = [f"## {FACT_CHECKED}", ""]
    any_body = False
    for ch in plan.selected:
        bodies = []
        for p in ch:
            body = section_body(notes_by_key[p.key], FACT_CHECKED)
            if body and not _is_none(body):
                body = fix_fact_checked_wording(body, plan.meta)
                lead, secs = split_sections(body)
                bodies.append(shift_headings(body, 4 - (secs[0][1] if secs else 4)).strip())
        if bodies:
            any_body = True
            out += [f"### {display_title(plan, ch[0].title)} [{_chapter_range(ch)}]", "",
                    "\n\n".join(bodies), ""]
    if not any_body:
        out.append("- None")
    return "\n".join(out).rstrip() + "\n"


def load_citation_audit(out_dir: Path, keys: Optional[Sequence[str]] = None) -> List[dict]:
    path = Path(out_dir) / citations.AUDIT_FILENAME
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [e for e in data if keys is None or e.get("passage") in keys]


def _audit_line(audit: Sequence[dict]) -> Optional[str]:
    changed = [e for e in audit if e.get("changed", True)]
    if not changed:
        return None
    counts = _audit_counts(changed)
    parts = []
    if counts.get("retarget"):
        parts.append(f"{counts['retarget']} citation(s) retargeted to the caption line that states the claim")
    if counts.get("unverified"):
        parts.append(f"{counts['unverified']} marked _(unverified citation)_")
    if counts.get("normalized"):
        parts.append(f"{counts['normalized']} with model commentary collapsed to the valid citation")
    if counts.get("unmarked"):
        parts.append(f"{counts['unmarked']} earlier _(unverified citation)_ mark(s) cleared "
                     "by fuzzy caption matching")
    if not parts:
        return None
    return "> Citation audit: " + "; ".join(parts) + f" — see `{citations.AUDIT_FILENAME}`."


def _chapter_counts(plan: Plan) -> dict:
    """The preamble (chapter_no 0) is not a chapter (WP-F3/F4): one definition
    for the frontmatter and the header line — E1b said "4 of 14" for 13."""
    return {
        "chapters_total": sum(1 for ch in plan.chapters if ch[0].chapter_no),
        "chapters_included": sum(1 for ch in plan.selected if ch[0].chapter_no),
        "preamble_included": any(ch[0].chapter_no == 0 for ch in plan.selected),
    }


def _source_block(plan: Plan, models: Sequence[str], partial: bool,
                  audit: Sequence[dict] = (),
                  recorded: Optional[spoken_dates.RecordingEstimate] = None) -> str:
    m = plan.meta
    corrected = quality.corrected_path_for(plan.out_dir / "transcripts" / plan.raw_transcript.name).name
    total = sum(len(ch) for ch in plan.chapters)
    counts = _chapter_counts(plan)
    pre = " + preamble" if counts["preamble_included"] else ""
    lines = [
        f"# {m.title or plan.name}",
        "",
        *([recorded.note_line(), ">"] if recorded else []),
        f"> Source: {m.url or 'unknown'}",
        f"> {prompt_header(m, plan.provenance_label)}",
        f"> Structure: {plan.structure.provenance} (rung: {plan.structure.rung}"
        f"{', INFERRED' if plan.structure.structure_is_inferred else ''})",
        f"> Notes: {len(plan.passages)} passage(s) over {counts['chapters_included']} of "
        f"{counts['chapters_total']} chapter(s){pre} ({total} passages in all); "
        f"profile: {plan.profile}; "
        f"models: {', '.join(sorted(set(models))) or 'none'}",
        f"> Transcript: `transcripts/{corrected}` ({len(plan.corrections)} glossary "
        f"corrections, logged in `corrections.json`); raw `transcripts/{plan.raw_transcript.name}` "
        "is kept unmodified. The full transcript is not appended to these notes.",
    ]
    audit_line = _audit_line(audit)
    if audit_line:
        lines.append(audit_line)
    if partial:
        lines.append("> **Partial run** (`--only`): only the chapters below were summarized.")
    return "\n".join(lines) + "\n"


def assemble(plan: Plan, notes_by_key: Dict[str, str], course_map_md: str,
             models: Sequence[str], run_id: str,
             audit: Sequence[dict] = ()) -> Tuple[str, dict]:
    """Final notes (before lint). Chapter notes are concatenated, never re-summarized."""
    partial = len(plan.selected) < len(plan.chapters)
    # Recording lag (WP-F7): an INFERENCE from dates spoken in the transcript.
    # as_of stays the published date (P4/P5 unchanged); this only warns.
    # WP-F8: relative anchors ("In May of 26 ... five months ago") first, and
    # also when the publish date is unknown (reference = the captured date).
    as_of = notes_format.resolve_as_of(plan.meta)
    recorded = spoken_dates.estimate_event(plan.segments, as_of.date, as_of.basis)
    blocks = [_source_block(plan, models, partial, audit, recorded), course_map_md]
    for ch in plan.selected:
        blocks.append(f"## {display_title(plan, ch[0].title)} [{_chapter_range(ch)}]\n")
        if len(ch) == 1:
            blocks.append(chapter_body(notes_by_key[ch[0].key], 3))
        else:
            for p in ch:
                blocks.append(f"### Part {p.part}/{p.parts} [{p.range}]\n")
                blocks.append(chapter_body(notes_by_key[p.key], 4))
    uncertainty_md, uncertainty_info = _merge_uncertainty(plan, notes_by_key)
    blocks.append(uncertainty_md)
    blocks.append(_merge_fact_checked(plan, notes_by_key))
    body = "\n".join(b.rstrip() + "\n" for b in blocks)
    # Chunks audited before WP-F4 can still carry `[7:55:55 n/a]`, and before
    # WP-F7 `[5:55:13 is not valid — see [4:55:13]]` (both model output).
    body, normalized = citations.normalize_citation_commentary(
        body, citations.caption_validator([s.start for s in plan.segments]))
    body = citations.normalize_citation_markers(body)[0]

    # Time-sensitive claims: merge -> validate(fill) -> stamp. validate MUST
    # come first: a model row without a date fails P5 otherwise.
    merged = notes_format.merge_time_sensitive(
        notes_format.parse_time_sensitive(notes_by_key[p.key]) for p in plan.passages)
    # Personal session state ("resets in 9 minutes", "hit 100% usage") is not a
    # claim about the product: moved to a dropped list logged in the state file.
    merged, ts_dropped = notes_format.filter_ephemeral(merged)
    check = notes_format.validate_time_sensitive(merged, as_of.date, basis=as_of.basis, fill=True)
    fm = notes_format.frontmatter(
        plan.meta,
        provenance=plan.provenance_label,
        structure_rung=plan.structure.rung,
        captured_at=plan.meta.captured_at,
        model=", ".join(sorted(set(models))) or None,
        run_id=run_id,
        extra={
            "notes_kind": "longform",
            "structure_detail": plan.structure.provenance,
            "structure_is_inferred": plan.structure.structure_is_inferred,
            "longform_profile": plan.profile,
            # The preamble is not a chapter (WP-F3): E1 said 14 for 13 chapters.
            **_chapter_counts(plan),
            "passages": len(plan.passages),
            "partial": partial,
            **({"recorded_estimate": recorded.frontmatter_value()} if recorded else {}),
        },
    )
    final = notes_format.stamp_notes(body, fm, check.rows)
    info = {
        "uncertainty": uncertainty_info,
        "time_sensitive_rows": len(check.rows),
        "time_sensitive_dropped": ts_dropped,
        "recorded_estimate": recorded.as_dict() if recorded else None,
        "time_sensitive_findings": [f"{f.level}: {f.claim[:60]} — {f.message}" for f in check.findings],
        "citation_audit": _audit_counts(audit),
        "citations_unverified": [
            f"{e.get('passage')}: {e.get('original_ts')} {str(e.get('claim', ''))[:80]}"
            for e in audit if e.get("rule") == "citation:unverified"],
        "citations_normalized_at_assembly": [
            f"{e['original']} -> {e['new']}" for e in normalized],
    }
    return final, info


def _nearest_citation(lines: List[str], idx: int) -> Optional[str]:
    """Timestamp of the nearest citation at/above line `idx` within its chapter,
    else the chapter heading's start time."""
    for k in range(idx, -1, -1):
        line = lines[k]
        cites = _CITE_RE.findall(line)
        if cites and not line.startswith("## "):
            return hms(quality.parse_ts(cites[-1]))
        if line.startswith("## "):
            m = _RANGE_IN_HEADING.search(line)
            return m.group(1) if m else None
    return None


def lint_and_fix(plan: Plan, notes: str, summary_name: str) -> Tuple[str, List[dict], List[dict]]:
    """Deterministic correction of residual mishearings in the notes.

    Returns (fixed notes, logged corrections, residual findings). Each fix is
    logged with rule prefix `notes:` and the nearest cited timestamp (or the
    chapter start), so P7 can account for it like a transcript fix.
    """
    logged: List[dict] = []
    text = notes
    for _ in range(3):
        fixed, findings = quality.correct_notes(text, plan.glossary)
        if not findings:
            break
        lines = text.split("\n")
        for f in findings:
            logged.append({
                "timestamp": _nearest_citation(lines, f["line"] - 1) or "0:00:00",
                "original": f["found"],
                "corrected": f["suggested"],
                "rule": f"notes:{f['rule']}",
                "file": summary_name,
                "notes_line": f["line"],
            })
        text = fixed
    residual = quality.lint_notes(text, plan.glossary)
    return text, logged, residual


def _append_corrections(out_dir: Path, entries: List[dict]) -> None:
    path = Path(out_dir) / quality.CORRECTIONS_FILENAME
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    data = [e for e in data if not str(e.get("rule", "")).startswith("notes:")] + entries
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def consistency_sections(plan: Plan, notes_by_key: Dict[str, str]) -> List[consistency.Section]:
    return [consistency.Section(_chapter_label(ch), ch[0].title, _chapter_range(ch),
                                [(p.key, notes_by_key[p.key]) for p in ch])
            for ch in plan.selected]


def run_consistency(plan: Plan, notes_by_key: Dict[str, str], state: State,
                    complete: Optional[CompleteFn], *, resume: bool = True,
                    progress: Progress = print,
                    ) -> Tuple[Dict[str, str], consistency.ConsistencyResult]:
    """The consistency step: (annotated notes_by_key, result). Logged to the
    state file under "consistency"; a failed call is a warning there."""
    sections = consistency_sections(plan, notes_by_key)
    if len(sections) > 1:
        progress(f"  run  consistency check ({len(sections)} sections)")
    res = consistency.check(sections, notes_by_key, plan.out_dir, complete, resume=resume)
    if res.status == "failed":
        progress(f"  WARNING consistency check skipped: {res.reason}")
    elif res.cached:
        progress("  skip consistency call (cached)")
    with state.lock:
        state.data["consistency"] = {**res.state_record(), "finished_at": _now()}
        state.save()
    return res.notes_by_key, res


# Notes-own arithmetic must carry "Check:" (WP-F8). Deterministic lint only:
# findings are logged in the state file, the notes are not rewritten.
_ARITH_RE = re.compile(
    r"\d[\d,.]*\s*(?:[×x*/÷+]|−|-(?=\s*\d))\s*\d[\d,.]*\s*(?:=|≈|is about|is ~)|"
    r"(?<![\w-])=\s*~?\d", re.I)


def check_rule_findings(notes: str) -> List[str]:
    """Bullets that show a computation (`2 × 12 = 24`, `= 71.7%`) without a
    `Check:` label."""
    out = []
    fenced = False
    for line in notes.split("\n"):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        s = line.strip()
        if fenced or not s.startswith(("-", "*", "|")):
            continue
        if _ARITH_RE.search(_CITE_RE.sub(" ", s)) and "check:" not in s.lower():
            out.append(s[:160])
    return out


# --- entry points ---------------------------------------------------------------------


def estimated_calls(plan: Plan, state: Optional[State] = None) -> Tuple[int, int]:
    """(passage calls, course-map calls) still to make. With more than one
    batch the course map adds one themes-consolidation call."""
    todo = [p for p in plan.passages
            if state is None or not is_done(state, plan.out_dir, p)]
    batches = len(course_map_batches(plan.selected))
    return len(todo), batches + (1 if batches > 1 else 0)


def describe_plan(plan: Plan, state: Optional[State] = None) -> str:
    """Human-readable plan (for --plan-only): chapters, parts, passages, calls."""
    split = sum(1 for ch in plan.chapters if len(ch) > 1)
    passage_calls, map_calls = estimated_calls(plan, state)
    lines = [
        f"Source: {plan.meta.title or plan.name}",
        f"Meta: {plan.meta_origin} · provenance: {plan.provenance_label} · "
        f"profile: {plan.profile}",
        f"Structure: {plan.structure.provenance} (rung: {plan.structure.rung})",
        f"Glossary corrections: {len(plan.corrections)} · words: {word_count(plan.segments):,}",
    ]
    if plan.outline is not None:
        o = plan.outline
        how = ("cached" if o.cached else "1 call made" if o.called else "no call made")
        lines.append(f"Outline: {how}"
                     + (f" · model: {o.model}" if o.model else "")
                     + (f" · {o.note}" if o.note else ""))
    lines += [
        "",
        "  #  key                                              range                  words",
    ]
    for ch in plan.chapters:
        mark = "*" if ch in plan.selected else " "
        for p in ch:
            done = " done" if state is not None and is_done(state, plan.out_dir, p) else ""
            lines.append(f"{mark} {_chapter_label(ch):>2} {p.key:<48} [{p.range}] {p.words:>7,}{done}")
    lines += [
        "",
        f"Chapters: {len(plan.chapters)} ({sum(1 for c in plan.chapters if c[0].chapter_no)} "
        f"+ preamble)" if any(c[0].chapter_no == 0 for c in plan.chapters)
        else f"Chapters: {len(plan.chapters)}",
        f"Passages: {sum(len(c) for c in plan.chapters)} in all, {split} chapter(s) split; "
        f"selected: {len(plan.passages)} passage(s) in {len(plan.selected)} chapter(s)",
        f"Estimated model calls: {passage_calls} passage + {map_calls} course map = "
        f"{passage_calls + map_calls}"
        + (" (+1 outline)" if plan.outline is not None and plan.outline.entries is None
           and not plan.outline.cached and not plan.outline.called else "")
        + (" (+1 consistency check unless cached)" if len(plan.selected) > 1 else ""),
        "",
        plan.skeleton,
    ]
    return "\n".join(lines)


def run_longform(
    run_dir: Path,
    out_dir: Optional[Path] = None,
    *,
    only: Optional[str] = None,
    resume: bool = True,
    concurrency: int = 1,
    complete: Optional[CompleteFn] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    progress: Progress = print,
    structure: str = "auto",
) -> LongformResult:
    """The long-form path on a run dir that already has a transcript.

    Raises SubscriptionExhausted / BudgetExhausted (checkpoints intact; rerun
    resumes) and LongformIncomplete (some passages failed for other reasons).
    """
    complete = complete or _default_complete
    plan = build_plan(run_dir, out_dir, only=only, max_words=max_words, write=True,
                      structure=structure, outline_complete=complete)
    state = State(plan.out_dir)
    if plan.outline is not None:
        with state.lock:
            state.data["structure_outline"] = {
                "rung": plan.structure.rung, "model": plan.outline.model,
                "cached": plan.outline.cached, "called": plan.outline.called,
                "note": plan.outline.note, "file": outline_mod.CACHE_FILENAME,
            }
            state.save()
    progress(f"Long-form: {len(plan.passages)} passage(s) in {len(plan.selected)} chapter(s); "
             f"{plan.structure.provenance}")
    ran, skipped = run_passages(plan, state, complete, resume=resume,
                                concurrency=concurrency, progress=progress)

    notes_by_key = {p.key: chunk_path(plan.out_dir, p).read_text(encoding="utf-8")
                    for p in plan.passages}
    models = [state.passage(p.key).get("model") for p in plan.passages]
    models = [m for m in models if m]

    course_map_md, map_models, map_calls, map_fallbacks = build_course_map(
        plan, notes_by_key, state, complete, resume=resume, progress=progress)
    models += map_models

    # Cross-section consistency (WP-F8): one call over extracted statements;
    # annotations only, on an in-memory copy (checkpoints stay as written).
    notes_by_key, cons = run_consistency(plan, notes_by_key, state, complete,
                                         resume=resume, progress=progress)
    if cons.model and cons.called:
        models.append(cons.model)

    run_id = state.data.get("run_id") or current_run_id()
    audit = load_citation_audit(plan.out_dir, [p.key for p in plan.passages])
    final, info = assemble(plan, notes_by_key, course_map_md, models, run_id, audit=audit)
    summary_name = f"{plan.name}.summary.md"
    fixed, logged, residual = lint_and_fix(plan, final, summary_name)
    _append_corrections(plan.out_dir, logged)
    if residual:
        raise RuntimeError(
            f"notes lint left {len(residual)} residual mishearing(s) after fixing: "
            + "; ".join(f"line {r['line']}: {r['found']}" for r in residual[:5]))

    summary_path = plan.out_dir / "summaries" / summary_name
    _atomic_write(summary_path, fixed)
    with state.lock:
        state.data["assembly"] = {
            "finished_at": _now(), "summary": str(summary_path.relative_to(plan.out_dir)),
            "passages": [p.key for p in plan.passages], "notes_corrections": len(logged),
            "lint_residual": len(residual), "course_map_calls": map_calls,
            "course_map_fallbacks": map_fallbacks, **info,
            "consistency": {"status": cons.status, "applied": sum(
                1 for e in cons.log if e.get("applied")), "reason": cons.reason},
            "check_rule_findings": check_rule_findings(fixed),
        }
        state.save()
    progress(f"Long-form notes: {summary_path} ({len(fixed.split()):,} words; "
             f"{ran} passage call(s), {skipped} from checkpoint, {map_calls} course-map call(s); "
             f"{len(logged)} notes correction(s))")
    return LongformResult(
        summary_path=summary_path, notes=fixed, models=sorted(set(models)),
        passages_run=ran, passages_skipped=skipped, course_map_calls=map_calls,
        course_map_fallback=map_fallbacks, notes_corrections=logged, lint_residual=residual,
    )
