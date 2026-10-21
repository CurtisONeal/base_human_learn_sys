"""Long recordings → complete, traceable, dated notes (WP-3 + WP-7).

Every media transcript comes here, however short (WP-M2): a 12-hour course
(~120k words) and a 70-second clip alike. This module wires the pure pieces
built in WP-1/2/4/5/8 into one checkpointed pipeline:

    source_meta.json (or yt_info.json)  ->  SourceMeta          core/metadata.py
    transcript provenance                ->  "auto-captions"     core/quality.py
    glossary correction                  ->  corrected copy + corrections.json
    structure ladder on CORRECTED text   ->  chapters / parts    core/structure.py
    one model call per PACK              ->  chunk_summaries/ + ledger/  (resumable)
      (a pack = adjacent whole chapters, one call, split back per chapter;
       the whole source when it fits; each part of a split chapter alone)
    one course-map call per batch        ->  ## Course Map rows + candidate themes
      (none when there is a single call: a deterministic table instead)
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
    citations, consistency, domains, names, notes_format, outline as outline_mod, people,
    quality, spoken_dates, summarizer, variants,
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
MULTI_CHAPTER_FRAGMENT = PROMPT_DIR / "fragments" / "multi_chapter.md"
PEOPLE_FRAGMENT = PROMPT_DIR / "fragments" / "people.md"

OVERLAP_S = 150.0
LEDGER_MAX_WORDS = 800
CONTEXT_ONLY_LABEL = "CONTEXT ONLY — do not summarize"
STATE_FILENAME = "longform_state.json"
STRUCTURE_FILENAME = "structure.json"
# Packing (WP-M2): consecutive whole chapters share ONE passage call until the
# pack reaches PACK_MIN_WORDS words or PACK_MIN_S seconds (never over the word
# budget, never splitting a chapter). Each chapter still gets its own `##`
# section, range and citations. T3 (Karpathy) made 30 calls for 30 chapters,
# several of them 1-3 minutes long.
PACK_MIN_WORDS = 3000
PACK_MIN_S = 8 * 60
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
# Prompt variant "current": every passage writes a People table; assembly
# merges them into ONE `## People` section (core/people.py).
PEOPLE = people.HEADING
CROSS_SECTIONS = (UNCERTAINTY, TIME_SENSITIVE, FACT_CHECKED, PEOPLE)

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
    # Model-call units over `selected` (WP-M2): each pack is consecutive whole
    # chapters summarized in ONE call, or one part of a split chapter.
    packs: List[List[Passage]] = field(default_factory=list)
    # Summary prompt variant (core/variants.py): "current" (with People) or
    # "legacy" (the prompts before it). Checkpoints are per variant.
    prompt_variant: str = variants.CURRENT

    @property
    def passages(self) -> List[Passage]:
        return [p for ch in self.selected for p in ch]

    @property
    def calls_units(self) -> List[List[Passage]]:
        """`packs`, or one pack per passage when none were computed."""
        return self.packs or [[p] for p in self.passages]

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


def _adjacent(a: Passage, b: Passage) -> bool:
    return b.chapter_no == a.chapter_no + 1


def _pack_words(pack: Sequence[Passage]) -> int:
    return sum(p.words for p in pack)


def _pack_full(pack: Sequence[Passage], min_words: int, min_s: float) -> bool:
    return bool(pack) and (_pack_words(pack) >= min_words
                           or pack[-1].end_s - pack[0].start_s >= min_s)


def pack_passages(
    chapters: Sequence[List[Passage]],
    max_words: int = DEFAULT_MAX_WORDS,
    *,
    min_words: int = PACK_MIN_WORDS,
    min_s: float = PACK_MIN_S,
    enabled: bool = True,
) -> List[List[Passage]]:
    """Model-call units (WP-M2). Deterministic.

    * A chapter split into parts keeps one call per part (the parts are over
      budget together; they carry the ledger from part to part).
    * Whole chapters are packed: when ALL selected chapters are whole, adjacent
      and fit in `max_words` together, they are ONE call; otherwise
      consecutive whole chapters are packed greedily until a pack reaches
      `min_words` words or `min_s` seconds, never over `max_words` and never
      splitting a chapter. An under-target pack left at the end of a run joins
      the pack before it when it fits.
    * Packs never join chapters that are not adjacent (`--only 1,5`).
    """
    if not enabled:
        return [[p] for ch in chapters for p in ch]
    chapters = [ch for ch in chapters if ch]
    whole = [ch[0] for ch in chapters if len(ch) == 1]
    if (chapters and len(whole) == len(chapters)
            and all(_adjacent(a, b) for a, b in zip(whole, whole[1:]))
            and _pack_words(whole) <= max_words):
        return [whole]
    packs: List[List[Passage]] = []
    mergeable: List[bool] = []          # pack of whole chapters (not a part)
    cur: List[Passage] = []

    def flush():
        nonlocal cur
        if cur:
            if (packs and mergeable[-1] and not _pack_full(cur, min_words, min_s)
                    and _adjacent(packs[-1][-1], cur[0])
                    and _pack_words(packs[-1]) + _pack_words(cur) <= max_words):
                packs[-1].extend(cur)
            else:
                packs.append(cur)
                mergeable.append(True)
        cur = []

    for ch in chapters:
        if len(ch) > 1:
            flush()
            for p in ch:
                packs.append([p])
                mergeable.append(False)
            continue
        p = ch[0]
        if cur and (not _adjacent(cur[-1], p) or _pack_full(cur, min_words, min_s)
                    or _pack_words(cur) + p.words > max_words):
            flush()
        cur.append(p)
    flush()
    return packs


def pack_key(pack: Sequence[Passage]) -> str:
    """State/cache key of one model call: the passage key, or a range key."""
    if len(pack) == 1:
        return pack[0].key
    return f"pack_{pack[0].chapter_no:02d}-{pack[-1].chapter_no:02d}"


def build_plan(
    run_dir: Path,
    out_dir: Optional[Path] = None,
    *,
    only: Optional[str] = None,
    max_words: int = DEFAULT_MAX_WORDS,
    write: bool = True,
    structure: str = "auto",
    outline_complete: Optional[CompleteFn] = None,
    pack: bool = True,
    prompt_variant: Optional[str] = None,
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
    segments = load_segments(raw)
    glossary = quality.build_glossary(
        meta, transcript_text=" ".join(s.text for s in segments))
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
    selected = select_chapters(chapters, only)
    return Plan(
        run_dir=run_dir, out_dir=out_dir, name=name, raw_transcript=raw,
        meta=meta, meta_origin=meta_origin, provenance=provenance,
        glossary=glossary, corrections=corrections, segments=corrected,
        structure=structure_res, skeleton=course_skeleton(structure_res, meta),
        profile=domains.longform_profile(meta),
        chapters=chapters, selected=selected,
        structure_mode=structure, outline=outline_res, raw_segments=list(segments),
        packs=pack_passages(selected, max_words, enabled=pack),
        prompt_variant=variants.normalize(prompt_variant or variants.active()),
    )


# --- prompts -----------------------------------------------------------------


@dataclass
class PassagePrompt:
    system: str
    user: str

    @property
    def text(self) -> str:
        return self.system + "\n\n" + self.user


def _sections_block(profile: str, variant: Optional[str] = None) -> str:
    return "\n".join(
        f"{i}. `### {heading}` — {instruction}"
        for i, (heading, instruction) in enumerate(
            domains.longform_sections(profile, variant), 1)
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
    variant = getattr(plan, "prompt_variant", variants.CURRENT)
    parts = [
        PASSAGE_PROMPT.read_text(encoding="utf-8").replace(
            "{{SECTIONS}}", _sections_block(plan.profile, variant)).rstrip(),
        mishearing_hint(plan.provenance, plan.glossary).rstrip(),
        (PEOPLE_FRAGMENT.read_text(encoding="utf-8").rstrip()
         if variants.has_people(variant) else ""),
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


def pack_marker(p: Passage) -> str:
    """The `##` line a multi-chapter reply opens each chapter's notes with."""
    return "## Preamble" if p.chapter_no == 0 else f"## Chapter {p.chapter_no}"


def pack_span(pack: Sequence[Passage]) -> str:
    """"chapters 3–7", "the Preamble and chapter 1"."""
    nums = [p.chapter_no for p in pack if p.chapter_no]
    pre = any(p.chapter_no == 0 for p in pack)
    span = (f"chapter {nums[0]}" if len(nums) == 1
            else f"chapters {nums[0]}–{nums[-1]}" if nums else "")
    return "the Preamble and " + span if pre and span else "the Preamble" if pre else span


def _pack_where(pack: Sequence[Passage]) -> str:
    return (f"{pack_span(pack)} together ({len(pack)} sections, summarized in one call) "
            f"[{time_locator(pack[0].start_s, pack[-1].end_s)}]")


def render_pack_prompt(plan: Plan, pack: Sequence[Passage]) -> PassagePrompt:
    """The (system, user) pair for one model call. A one-passage pack is the
    ordinary passage prompt; a multi-chapter pack lists its chapters with the
    marker each must open with, and marks every chapter's start in the
    transcript, so the reply splits back into per-chapter notes."""
    if len(pack) == 1:
        return render_passage_prompt(plan, pack[0])
    system = (passage_system_prompt(plan).rstrip() + "\n\n"
              + MULTI_CHAPTER_FRAGMENT.read_text(encoding="utf-8").rstrip() + "\n")
    listing = "\n".join(
        f"{pack_marker(p)} — {display_title(plan, p.title)} [{p.range}]" for p in pack)
    body = []
    for p in pack:
        label = ("PREAMBLE" if p.chapter_no == 0
                 else f'CHAPTER {p.chapter_no} "{display_title(plan, p.title)}"')
        body.append(f"--- {label} [{p.range}] ---")
        a, b = p.span
        body.append(timestamped_lines(plan.segments, a, b) if b > a
                    else "(no transcript lines in this chapter)")
    blocks = [
        f"SOURCE: {prompt_header(plan.meta, plan.provenance_label)}",
        "COURSE SKELETON (the whole recording; context only):\n" + plan.skeleton,
        f"THIS PASSAGE: {_pack_where(pack)}.",
        "Write one block per chapter, in this order, each opening with its marker "
        "line exactly as given:\n" + listing,
        f"PASSAGE TRANSCRIPT ({_pack_words(pack):,} words; each chapter starts at its "
        "`--- CHAPTER` line) — write notes for these lines:\n"
        + "\n".join(body) + "\nEND OF PASSAGE",
    ]
    return PassagePrompt(system, "\n\n".join(blocks) + "\n")


_PACK_HEAD = re.compile(r"^#{1,2}\s*\**\s*(?:chapter\s+(\d+)\b|(preamble)\b)", re.I)


def split_pack_output(notes: str, pack: Sequence[Passage]) -> Tuple[Dict[str, str], List[str]]:
    """({passage key: that chapter's notes}, keys with no block) from a
    multi-chapter reply. Blocks are found by their `## Chapter <n>` /
    `## Preamble` marker lines outside code fences; anything before the first
    marker (a chat opener) is dropped; a chapter written twice is joined."""
    by_no = {p.chapter_no: p for p in pack}
    chunks: Dict[str, List[str]] = {}
    current: Optional[str] = None
    fenced = False
    for line in (notes or "").split("\n"):
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _PACK_HEAD.match(line.strip())
            if m:
                no = 0 if m.group(2) else int(m.group(1))
                if no in by_no:
                    current = by_no[no].key
                    chunks.setdefault(current, [])
                    continue
        if current is not None:
            chunks[current].append(line)
    out = {}
    for key, lines in chunks.items():
        text = "\n".join(lines).strip()
        if text:
            out[key] = text + "\n"
    return out, [p.key for p in pack if p.key not in out]


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


def is_done(state: State, out_dir: Path, p: Passage,
            variant: Optional[str] = None) -> bool:
    """Checkpointed under the same prompt variant (a record without one was
    written before variants existed: legacy)."""
    rec = state.passage(p.key)
    return (
        rec.get("status") == "done"
        and (variant is None or rec.get("prompt_variant", variants.LEGACY) == variant)
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
    consistency_calls: int = 0
    outline_calls: int = 0
    course_map_fallback: List[str] = field(default_factory=list)
    notes_corrections: List[dict] = field(default_factory=list)
    lint_residual: List[dict] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        """Model calls THIS run made (checkpointed/cached ones excluded)."""
        return (self.outline_calls + self.passages_run + self.course_map_calls
                + self.consistency_calls)


def _run_chapter(plan: Plan, chapter: List[Passage], state: State, complete: CompleteFn,
                 resume: bool, stop: threading.Event, counters: dict,
                 progress: Progress) -> None:
    ledger: Optional[dict] = None
    prev: Optional[Passage] = None
    for p in chapter:
        if resume and is_done(state, plan.out_dir, p, plan.prompt_variant):
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
                     error=None, stopped=None, prompt_variant=plan.prompt_variant)
        with state.lock:
            counters["run"] += 1
        ledger, prev = led, p


def _run_pack(plan: Plan, pack: List[Passage], state: State, complete: CompleteFn,
              resume: bool, stop: threading.Event, counters: dict,
              progress: Progress) -> None:
    """One model call for several whole chapters; the reply is split back into
    per-chapter notes, each audited against its OWN chapter's lines and
    checkpointed under its own passage key. A chapter the reply has no block
    for is re-run on its own (one extra call), so every chapter's notes come
    from the model, never from a neighbour."""
    key = pack_key(pack)
    if resume and all(is_done(state, plan.out_dir, p, plan.prompt_variant) for p in pack):
        with state.lock:
            counters["skipped"] += 1
        progress(f"  skip {key} (checkpoint, {len(pack)} chapters)")
        return
    if stop.is_set():
        return
    prompt = render_pack_prompt(plan, pack)
    started = _now()
    for p in pack:
        state.update("passages", p.key, status="running", started_at=started,
                     chapter=p.chapter_no, title=p.title, part=p.part, parts=p.parts,
                     start_s=p.start_s, end_s=p.end_s, pack=key, pack_size=len(pack))
    progress(f"  run  {key} [{time_locator(pack[0].start_s, pack[-1].end_s)}] "
             f"{_pack_words(pack):,} words, {len(pack)} chapters in one call")
    try:
        output, model = complete(prompt.system, prompt.user)
    except (summarizer.SubscriptionExhausted, BudgetExhausted) as exc:
        for p in pack:
            state.update("passages", p.key, status="pending",
                         stopped=f"{type(exc).__name__}: {str(exc)[:200]}")
        stop.set()
        raise
    except Exception as exc:  # noqa: BLE001 - record; other units continue
        for p in pack:
            state.update("passages", p.key, status="failed", error=str(exc)[:400])
        raise
    with state.lock:
        counters["run"] += 1
    _atomic_write(Path(plan.out_dir) / CHUNK_DIR / f"_{key}.raw.txt", output)
    notes_all, led, err = extract_ledger(output)
    notes_all = summarizer._strip_chat_preamble(notes_all)
    blocks, missing = split_pack_output(notes_all, pack)
    source = "model"
    if led is None:
        led, source = _fallback_ledger(None, next(iter(blocks.values()), "")), f"fallback ({err})"
    led, dropped = cap_ledger(led)
    for p in pack:
        if p.key not in blocks:
            continue
        notes, audit = audit_passage_notes(plan, p, blocks[p.key].rstrip() + "\n")
        record_citation_audit(plan.out_dir, p, audit)
        _atomic_write(chunk_path(plan.out_dir, p), notes)
        _atomic_write(ledger_path(plan.out_dir, p),
                      json.dumps(led, indent=2, ensure_ascii=False) + "\n")
        state.update("passages", p.key, status="done", model=model, finished_at=_now(),
                     ledger_words=_ledger_words(led), ledger_dropped=dropped,
                     citation_audit=_audit_counts(audit), ledger_source=source,
                     notes_words=len(notes.split()), error=None, stopped=None,
                     pack=key, pack_size=len(pack), prompt_variant=plan.prompt_variant)
    if missing:
        progress(f"  WARNING {key}: no block for {', '.join(missing)} — re-running "
                 f"{'it' if len(missing) == 1 else 'them'} on {'its' if len(missing) == 1 else 'their'} own")
        for p in pack:
            if p.key in missing:
                state.update("passages", p.key, status="pending", pack=key,
                             pack_missing=True)
        for p in pack:
            if p.key in missing:
                _run_chapter(plan, [p], state, complete, False, stop, counters, progress)


def work_units(plan: Plan) -> List[List[List[Passage]]]:
    """Packs grouped into units that run in order: the parts of one split
    chapter are one unit (each part's ledger feeds the next); every other
    pack is a unit of its own. Units are independent of each other."""
    units: List[List[List[Passage]]] = []
    for pk in plan.calls_units:
        p = pk[0]
        if (len(pk) == 1 and p.parts > 1 and units and len(units[-1][-1]) == 1
                and units[-1][-1][0].group_id == p.group_id):
            units[-1].append(pk)
        else:
            units.append([pk])
    return units


def run_passages(plan: Plan, state: State, complete: CompleteFn, *, resume: bool = True,
                 concurrency: int = 1, progress: Progress = print) -> Tuple[int, int]:
    """Run every selected pack not already checkpointed. Returns (calls made,
    calls skipped) — a multi-chapter pack is one call.

    Exhaustion (SubscriptionExhausted / BudgetExhausted) stops new passages and
    is re-raised after in-flight chapters finish their current passage; every
    completed passage stays on disk. Ordinary failures are collected and raised
    as LongformIncomplete after all chapters have been attempted.
    """
    stop = threading.Event()
    counters = {"run": 0, "skipped": 0}
    errors: List[Tuple[str, BaseException]] = []

    def one(unit):
        try:
            if len(unit[0]) > 1:
                _run_pack(plan, unit[0], state, complete, resume, stop, counters, progress)
            else:
                _run_chapter(plan, [pk[0] for pk in unit], state, complete, resume, stop,
                             counters, progress)
        except BaseException as exc:  # noqa: BLE001
            errors.append((unit[0][0].title, exc))

    units = work_units(plan)
    if concurrency <= 1:
        for unit in units:
            one(unit)
            if stop.is_set():
                break
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(one, units))

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

_THEME_CH = re.compile(
    r"\s*\((?P<pre>preamble\s*[,;]\s*)?(?:ch(?:apters?|s)?\.?|§)\s*([^)]*)\)\s*\.?\s*$", re.I)


def split_theme(theme: str) -> Tuple[str, List[str]]:
    """("Theme text", ["2", "5"]) from "Theme text (ch. 2, 5)"; the preamble
    ("(preamble, ch. 1, 2)", as the model writes it) is label "-"."""
    m = _THEME_CH.search(theme or "")
    if not m:
        return (theme or "").strip(), []
    chs = [c for c in re.split(r"[,;&\s]+|\band\b", m.group(2)) if c.strip()]
    if m.group("pre"):
        chs = ["-"] + chs
    return theme[: m.start()].strip(), chs


def _ch_key(c: str):
    return (0, int(c)) if c.isdigit() else (1, c)


def render_theme(text: str, chapters: Sequence[str]) -> str:
    chs = sorted(set(chapters) - {"-"}, key=_ch_key)
    pre = "preamble, " if "-" in chapters else ""
    if not chs:
        return f"{text} (preamble)" if pre else text
    return f"{text} ({pre}ch. {', '.join(chs)})"


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
        names.add(domains.PEOPLE_SECTION[0])
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


def chapter_topic_lists(plan: Plan, notes_by_key: Dict[str, str], full: bool = False
                        ) -> Tuple[Dict[str, List[str]], List[str]]:
    """({chapter label: key topics}, one-word topics dropped as generic).

    full=False: at most MAX_TOPICS per chapter — what the themes call sees.
    full=True: EVERY Key Concepts term and sub-heading plus the chapter title
    — what the tagging post-check matches against. (E2c: ch. 7 teaches plan
    mode, but "plan mode" was its 11th term, cut from the 8-topic list, so
    the plan-mode theme never got ch. 7 — F9 (m).)"""
    limit = 10 ** 6 if full else 3 * MAX_TOPICS
    raw = {_chapter_label(ch): chapter_topics(ch, notes_by_key, limit=limit)
           for ch in plan.selected}
    if full:
        for ch in plan.selected:
            # The title, and its distinctive words ("permissions", "safety"):
            # a theme naming one of them is about this chapter too.
            title = _clean_topic(ch[0].title)
            if title:
                raw[_chapter_label(ch)].append(title)
                raw[_chapter_label(ch)] += [
                    w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", title)
                    if w.lower() not in citations.STOPWORDS]
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
        topics[label] = keep if full else keep[:MAX_TOPICS]
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


ALL_CHAPTERS_FRACTION = 0.9


def mark_all_chapter_themes(themes: Sequence[str], labels: Sequence[str],
                            fraction: float = ALL_CHAPTERS_FRACTION
                            ) -> Tuple[List[str], List[str]]:
    """A theme tagged on >= `fraction` of the chapters keeps its text but its
    tag list becomes "(all chapters)": a list of every chapter number tells a
    skimmer nothing. Returns (themes, texts so marked)."""
    real = [lb for lb in labels if lb != "-"]
    out, marked = [], []
    for theme in themes:
        text, chs = split_theme(theme)
        hit = {c for c in chs if c in real}
        if real and len(real) >= 3 and len(hit) >= fraction * len(real):
            out.append(f"{text} (all chapters)")
            marked.append(text)
        else:
            out.append(theme)
    return out, marked


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
        if (resume and rec.get("keys") == keys and cache.exists()
                and rec.get("prompt_variant", variants.LEGACY) == plan.prompt_variant):
            reply, model = cache.read_text(encoding="utf-8"), rec.get("model")
            progress(f"  skip course map {bid} (checkpoint)")
        else:
            progress(f"  run  course map {bid} ({len(batch)} chapters)")
            try:
                reply, model = complete(system, _course_map_input(batch, notes_by_key))
                calls += 1
                _atomic_write(cache, reply)
                state.update("course_map", bid, keys=keys, model=model, finished_at=_now(),
                             prompt_variant=plan.prompt_variant)
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
    tag_terms, _ = chapter_topic_lists(plan, notes_by_key, full=True)
    themes, tags_added = tag_theme_chapters(themes, tag_terms)
    themes, all_chapter_themes = mark_all_chapter_themes(
        themes, [_chapter_label(ch) for ch in plan.selected])
    with state.lock:
        state.data["course_map"]["theme_tags"] = {
            "topics": topics, "added": tags_added,
            "generic_one_word_topics_dropped": generic_dropped,
            "all_chapters": all_chapter_themes}
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


def deterministic_course_map(plan: Plan, notes_by_key: Dict[str, str]) -> str:
    """The Course Map with NO model call (WP-M2): used when the whole source
    was one passage call, so a course-map call would only re-read what that
    one call already wrote. Chapter and range columns come from the skeleton,
    the gist is each chapter's first summary point."""
    rows = []
    for ch in plan.selected:
        gist = notes_format.sanitize_cell(
            _deterministic_gist([notes_by_key[p.key] for p in ch]))
        rows.append(f"| {_chapter_label(ch)} | "
                    f"{notes_format.sanitize_cell(display_title(plan, ch[0].title))} "
                    f"| [{_chapter_range(ch)}] | {gist} |")
    out = [f"## {COURSE_MAP_HEADING}", "", "| # | chapter | time range | gist |",
           "|---|---|---|---|", *rows, "",
           "> Course map: deterministic — the whole source was summarized in one "
           "passage call, so no course-map call was made; each gist is the "
           "chapter's first summary point.",
           "", f"### {THEMES_HEADING}", "",
           "- Not computed: a single passage call has no cross-call themes to consolidate."]
    return "\n".join(out) + "\n"


def structure_record(plan: Plan) -> dict:
    """structure.json: the sections the notes are organised by, and how they
    were found. The evaluator's P1 falls back to it when the source declares
    no chapters — graded as the pipeline's own claim, labelled as such."""
    return {
        "rung": plan.structure.rung,
        "structure_is_inferred": plan.structure.structure_is_inferred,
        "provenance": plan.structure.provenance,
        "fallback_reason": plan.structure.fallback_reason,
        "sections": [
            {"label": _chapter_label(ch), "title": ch[0].title,
             "display_title": display_title(plan, ch[0].title),
             "start_s": ch[0].start_s, "end_s": ch[-1].end_s,
             "range": _chapter_range(ch), "parts": len(ch),
             "preamble": ch[0].chapter_no == 0}
            for ch in plan.chapters],
        "packs": [{"key": pack_key(pk), "passages": [p.key for p in pk],
                   "words": _pack_words(pk)} for pk in plan.calls_units],
    }


# --- assembly -----------------------------------------------------------------------


def outline_speakers(plan: Plan) -> List[Tuple[str, List[str]]]:
    """(speaker, spelling variants) of the outline entries, in order, unique.

    WP-F9: for a machine transcript the variants also include spellings the
    TRANSCRIPT uses for that speaker in a roll call or an introduction
    ("Harsha" for the outline's "Archer", E3c) — see names.py."""
    info = speaker_info(plan)
    return [(sp, list(var)) for sp, var in info["variants"].items()]


def speaker_info(plan: Plan) -> dict:
    """{"variants": {speaker: [variants]}, "preferred": {speaker: spelling},
    "found": {speaker: [(variant, how)]}} — computed once per plan."""
    cached = getattr(plan, "_speaker_info", None)
    if cached is not None:
        return cached
    entries = plan.outline.entries if plan.outline is not None and plan.outline.entries else []
    order: List[Tuple[str, List[str], float]] = []
    for e in entries:
        if e.speaker and all(e.speaker != sp for sp, _, _ in order):
            order.append((e.speaker, list(e.speaker_variants or []), float(e.start_s)))
    variants = {sp: list(var) for sp, var, _ in order}
    preferred: Dict[str, str] = {}
    found: Dict[str, list] = {}
    if order and names.is_machine(plan.provenance) and plan.segments:
        vocab = names.TranscriptVocab.build(plan.segments)
        found = names.transcript_speaker_variants(order, plan.segments, vocab)
        for sp, extra in found.items():
            for w, _ in extra:
                if w not in variants[sp]:
                    variants[sp].append(w)
            best = names.preferred_spelling(sp, variants[sp], vocab)
            if best != sp:
                preferred[sp] = best
    info = {"variants": variants, "preferred": preferred,
            "found": {k: v for k, v in found.items() if v}}
    try:
        plan._speaker_info = info
    except Exception:  # noqa: BLE001 - a frozen plan just recomputes
        pass
    return info


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
    return names.hedge_title(title, speakers, names.metadata_words(plan.meta),
                             preferred=speaker_info(plan)["preferred"])


def _merge_uncertainty(plan: Plan, notes_by_key: Dict[str, str],
                       private: Sequence[str] = (),
                       people_names: Sequence[str] = ()) -> Tuple[str, dict]:
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
        info = speaker_info(plan)
        name_rows = names.names_rows(
            speakers=outline_speakers(plan),
            notes=[chapter_body(notes_by_key[p.key], 3) for p in plan.passages]
            + (["\n".join(people_names)] if people_names else []),
            respellings=respellings, vocab=vocab, meta_words=meta_words,
            found=info["found"])
        # WP-F9: private individuals are not listed (they are not named at all).
        name_rows = [r for r in name_rows if r.name not in private]
        block = names.render_names(name_rows, plan.provenance_label)
        if block:
            out += ["", block.rstrip("\n")]
    return "\n".join(out) + "\n", {
        "rows": len(rows), "glossary_known": known, "omitted": omitted,
        "names_unverified": [{"name": r.name, "spellings": r.spellings, "basis": r.source}
                             for r in name_rows],
        "name_respellings_not_applied": [{"heard": h, "probable": p} for h, p in respellings],
    }


def merge_people_section(plan: Plan, notes_by_key: Dict[str, str],
                         private: Sequence[str] = ()) -> Tuple[str, dict, List[people.Person]]:
    """ONE `## People` section from every passage's People table (core/people.py).

    Same person across passages = one row (normalised name; for a machine
    transcript also sounds-alike surnames and the outline speaker variants
    the names table uses). Private individuals (WP-F9) are dropped."""
    rows: List[people.PersonRow] = []
    missing = 0
    for p in plan.passages:
        body = section_body(notes_by_key[p.key], PEOPLE)
        if body is None:
            missing += 1
            continue
        rows.extend(people.parse_rows(body, start_order=len(rows)))
    asr = _asr_names(plan)
    meta_words = names.metadata_words(plan.meta)
    vocab = names.TranscriptVocab.build(plan.segments) if asr else None
    aliases: Dict[str, str] = {}
    if asr:
        info = speaker_info(plan)
        for sp, var in info["variants"].items():
            for v in [sp, *var]:
                aliases[v] = info["preferred"].get(sp, sp)
    merged = people.merge_people(
        rows, asr=asr, timed=True, meta_words=meta_words,
        count=(vocab.count if vocab else None), aliases=aliases, private=private)
    md = people.render(merged, level=2, asr=asr, meta_words=meta_words)
    if missing:
        md = md.rstrip("\n") + (f"\n\n> {missing} passage(s) returned no {PEOPLE} section.\n")
    return md, {"rows_in": len(rows), "people": len(merged), "passages_without": missing,
                "names": [p.shown for p in merged]}, merged


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
        f"> Notes: {len(plan.calls_units)} passage call(s) for {len(plan.passages)} "
        f"section(s) over {counts['chapters_included']} of "
        f"{counts['chapters_total']} chapter(s){pre} ({total} sections in all); "
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


def _about_block(plan: Plan) -> str:
    """Event facts supplied with the source (organiser page, publisher text),
    shown verbatim and labelled — not inferred from the recording. Skipped for
    YouTube, whose descriptions are mostly links, chapters and sponsor reads.
    E3e review (2026-10-16): the notes never gave the meetup's date, venue,
    agenda or sponsors although source_meta.json had all of them."""
    meta = plan.meta
    desc = (getattr(meta, "description", "") or "").strip()
    url = getattr(meta, "url", "") or ""
    if not desc or re.search(r"youtube\.com|youtu\.be", url):
        return ""
    out = ["## About this source", "",
           "_From the source's own metadata (e.g. the organiser's event page), "
           "reproduced as provided — not derived from the recording._", ""]
    if getattr(meta, "published_at", None):
        out.append(f"- **Date:** {meta.published_at}")
    if getattr(meta, "published_by", None):
        out.append(f"- **Organiser / publisher:** {meta.published_by}")
    if url.startswith("http"):
        out.append(f"- **Link:** {url}")
    out.append("")
    out += ["> " + ln if ln.strip() else ">" for ln in desc.splitlines()]
    return "\n".join(out) + "\n"


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
    blocks = [_source_block(plan, models, partial, audit, recorded)]
    about = _about_block(plan)
    if about:
        blocks.append(about)
    blocks.append(course_map_md)
    # WP-F9 names/privacy (machine transcripts only): people named only in
    # giveaway/winner/attendee context are private — redacted, not listed;
    # every other ASR-only name in the notes is hedged at its first mention
    # in each chapter and listed in the names table.
    private: List[str] = []
    hedge: List[str] = []
    if _asr_names(plan):
        vocab = names.TranscriptVocab.build(plan.segments)
        meta_words = names.metadata_words(plan.meta)
        bodies = [chapter_body(notes_by_key[p.key], 3) for p in plan.passages]
        found = names.note_names(bodies, vocab, meta_words)
        protected = [sp for sp, var in outline_speakers(plan)] + [
            v for _, var in outline_speakers(plan) for v in var]
        private = names.private_names(bodies, found, protected)
        hedge = [n for n in found if n not in private]
    people_md, people_info, merged_people = "", None, []
    if variants.has_people(plan.prompt_variant):
        people_md, people_info, merged_people = merge_people_section(plan, notes_by_key, private)
        blocks.append(people_md)
    uncertainty_md, uncertainty_info = _merge_uncertainty(
        plan, notes_by_key, private, people.names_for_table(merged_people))
    hedged_total = 0
    for ch in plan.selected:
        blocks.append(f"## {display_title(plan, ch[0].title)} [{_chapter_range(ch)}]\n")
        chapter_blocks = []
        if len(ch) == 1:
            chapter_blocks.append(chapter_body(notes_by_key[ch[0].key], 3))
        else:
            for p in ch:
                chapter_blocks.append(f"### Part {p.part}/{p.parts} [{p.range}]\n")
                chapter_blocks.append(chapter_body(notes_by_key[p.key], 4))
        text = "\n".join(b.rstrip() + "\n" for b in chapter_blocks)
        if hedge:
            text, n = names.hedge_names_in_body(text, hedge)
            hedged_total += n
        blocks.append(text)
    blocks.append(uncertainty_md)
    blocks.append(_merge_fact_checked(plan, notes_by_key))
    body = "\n".join(b.rstrip() + "\n" for b in blocks)
    # Chunks audited before WP-F4 can still carry `[7:55:55 n/a]`, and before
    # WP-F7 `[5:55:13 is not valid — see [4:55:13]]` (both model output).
    body, normalized = citations.normalize_citation_commentary(
        body, citations.caption_validator([s.start for s in plan.segments]))
    body = citations.normalize_citation_markers(body)[0]
    redactions: List[dict] = []
    if private:
        body, redactions = names.redact_private(body, private)

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
            "passage_calls": len(plan.calls_units),
            "partial": partial,
            "prompt_variant": plan.prompt_variant,
            **({"recorded_estimate": recorded.frontmatter_value()} if recorded else {}),
        },
    )
    final = notes_format.stamp_notes(body, fm, check.rows)
    info = {
        "names": {"hedged_in_body": hedged_total, "private_redacted": redactions,
                  "speaker_variants_from_transcript": speaker_info(plan)["found"],
                  "heading_spelling": speaker_info(plan)["preferred"]},
        "uncertainty": uncertainty_info,
        "prompt_variant": plan.prompt_variant,
        **({"people": people_info} if people_info is not None else {}),
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
    """(passage calls, course-map calls) still to make. A multi-chapter pack
    is one passage call. With more than one batch the course map adds one
    themes-consolidation call; with a single passage call there is no
    course-map call at all."""
    todo = [pk for pk in plan.calls_units
            if state is None or not all(is_done(state, plan.out_dir, p) for p in pk)]
    if len(plan.calls_units) <= 1:
        return len(todo), 0          # deterministic course map (WP-M2)
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
    call_of = {p.key: n for n, pk in enumerate(plan.calls_units, 1) for p in pk}
    lines += [
        "",
        "  #  call  key                                              range                  words",
    ]
    for ch in plan.chapters:
        mark = "*" if ch in plan.selected else " "
        for p in ch:
            done = " done" if state is not None and is_done(state, plan.out_dir, p) else ""
            call = f"c{call_of[p.key]:02d}" if p.key in call_of else "  - "
            lines.append(f"{mark} {_chapter_label(ch):>2} {call:>5} {p.key:<48} "
                         f"[{p.range}] {p.words:>7,}{done}")
    packed = [pk for pk in plan.calls_units if len(pk) > 1]
    lines += [
        "",
        f"Chapters: {len(plan.chapters)} ({sum(1 for c in plan.chapters if c[0].chapter_no)} "
        f"+ preamble)" if any(c[0].chapter_no == 0 for c in plan.chapters)
        else f"Chapters: {len(plan.chapters)}",
        f"Passages: {sum(len(c) for c in plan.chapters)} in all, {split} chapter(s) split; "
        f"selected: {len(plan.passages)} passage(s) in {len(plan.selected)} chapter(s)",
        f"Packed: {len(plan.calls_units)} passage call(s) for {len(plan.passages)} "
        f"passage(s); {len(packed)} call(s) cover several chapters "
        f"(whole chapters packed until >= {PACK_MIN_WORDS:,} words or "
        f">= {PACK_MIN_S / 60:.0f} min; a chapter is never split)",
        *[f"  {pack_key(pk)}: {pack_span(pk)} "
          f"[{time_locator(pk[0].start_s, pk[-1].end_s)}] {_pack_words(pk):,} words, "
          f"{len(pk)} sections" for pk in packed],
        f"Estimated model calls: {passage_calls} passage + {map_calls} course map = "
        f"{passage_calls + map_calls}"
        + (" (+1 outline)" if plan.outline is not None and plan.outline.entries is None
           and not plan.outline.cached and not plan.outline.called else "")
        + (" (+1 consistency check unless cached)"
           if len(plan.selected) > 1 and len(plan.calls_units) > 1 else "")
        + (" (single passage call: deterministic course map, no consistency call)"
           if len(plan.calls_units) <= 1 else ""),
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
    pack: bool = True,
) -> LongformResult:
    """The long-form path on a run dir that already has a transcript.

    Every media transcript comes here (WP-M2), however short: one passage call
    when the whole source fits, with the course map rendered deterministically
    and no consistency call; packed calls otherwise.

    Raises SubscriptionExhausted / BudgetExhausted (checkpoints intact; rerun
    resumes) and LongformIncomplete (some passages failed for other reasons).
    """
    complete = complete or _default_complete
    plan = build_plan(run_dir, out_dir, only=only, max_words=max_words, write=True,
                      structure=structure, outline_complete=complete, pack=pack)
    _atomic_write(plan.out_dir / STRUCTURE_FILENAME,
                  json.dumps(structure_record(plan), indent=2, ensure_ascii=False) + "\n")
    state = State(plan.out_dir)
    with state.lock:
        state.data["prompt_variant"] = plan.prompt_variant
        state.save()
    if plan.outline is not None:
        with state.lock:
            state.data["structure_outline"] = {
                "rung": plan.structure.rung, "model": plan.outline.model,
                "cached": plan.outline.cached, "called": plan.outline.called,
                "note": plan.outline.note, "file": outline_mod.CACHE_FILENAME,
            }
            state.save()
    progress(f"Long-form: {len(plan.calls_units)} passage call(s) for {len(plan.passages)} "
             f"passage(s) in {len(plan.selected)} chapter(s); {plan.structure.provenance}")
    ran, skipped = run_passages(plan, state, complete, resume=resume,
                                concurrency=concurrency, progress=progress)

    notes_by_key = {p.key: chunk_path(plan.out_dir, p).read_text(encoding="utf-8")
                    for p in plan.passages}
    models = [state.passage(p.key).get("model") for p in plan.passages]
    models = [m for m in models if m]

    single_call = len(plan.calls_units) <= 1
    if single_call:
        # WP-M2: one call wrote every section; a course-map call would only
        # re-read it, and there is no second call to be inconsistent with.
        course_map_md, map_models, map_calls, map_fallbacks = (
            deterministic_course_map(plan, notes_by_key), [], 0, [])
        cons = consistency.ConsistencyResult(
            dict(notes_by_key), "skipped", "single passage call: one call wrote every section")
        with state.lock:
            state.data["consistency"] = {**cons.state_record(), "finished_at": _now()}
            state.save()
        progress("  skip course map + consistency calls (single passage call)")
    else:
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

    # WP-D1: never leave notes with an unclosed code fence (deterministic).
    from video2mdnotes.core.fences import repair_fences
    fixed, fence_repairs = repair_fences(fixed, label=summary_name)
    summary_path = plan.out_dir / "summaries" / summary_name
    _atomic_write(summary_path, fixed)
    with state.lock:
        state.data["assembly"] = {
            "finished_at": _now(), "summary": str(summary_path.relative_to(plan.out_dir)),
            "passages": [p.key for p in plan.passages], "notes_corrections": len(logged),
            "lint_residual": len(residual), "course_map_calls": map_calls,
            "passage_calls": ran, "passage_calls_skipped": skipped,
            "packs": [pack_key(pk) for pk in plan.calls_units],
            "course_map_fallbacks": map_fallbacks, **info,
            "consistency": {"status": cons.status, "applied": sum(
                1 for e in cons.log if e.get("applied")), "reason": cons.reason},
            "check_rule_findings": check_rule_findings(fixed),
            "fence_repairs": fence_repairs,
        }
        state.save()
    progress(f"Long-form notes: {summary_path} ({len(fixed.split()):,} words; "
             f"{ran} passage call(s), {skipped} from checkpoint, {map_calls} course-map call(s); "
             f"{len(logged)} notes correction(s))")
    return LongformResult(
        summary_path=summary_path, notes=fixed, models=sorted(set(models)),
        passages_run=ran, passages_skipped=skipped, course_map_calls=map_calls,
        consistency_calls=1 if cons.called else 0,
        outline_calls=1 if plan.outline is not None and plan.outline.called else 0,
        course_map_fallback=map_fallbacks, notes_corrections=logged, lint_residual=residual,
    )
