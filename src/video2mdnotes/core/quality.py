"""Transcript quality (WP-4): provenance, glossary correction, notes lint.

Pure, deterministic, no model calls. WP-3 wires it into the pipeline.

Why this exists
===============

YouTube auto-captions (and Whisper without a vocabulary hint) hear "Claude" as
"cloud" / "clawed" (see ``core/captions.py``). The CCA-F course transcript has
dozens of "cloud code" lines; a summarizer faithfully copies them into notes.
This module fixes the *known* forms before the model sees them, logs every fix,
and lints the generated notes for anything that slipped through.

Hard rules (from ``design/LONGFORM_EXECUTION_PLAN.md``)
-------------------------------------------------------

* **The raw transcript is never edited in place.** Corrections go to a separate
  ``transcripts/<name>.corrected.md`` (same line format, same timestamps) plus a
  ``corrections.json`` log — one entry per fix, with the timestamp of the line
  where the phrase *starts*.
* **A bare "cloud" is never rewritten.** Every rule names an explicit wrong form
  in a product context ("cloud code", "cloud agent sdk", ...). "cloud provider",
  "cloud computing", "AWS cloud", "in the cloud" are real usage.

Glossary sources
================

The correction glossary is built from (a) the source's own metadata — title,
description and tags — which selects domain glossaries by keyword and supplies
brand casing; and (b) domain glossaries in ``core/glossaries/*.json``. It is
deliberately independent of ``eval/glossary_default.json``: that file is the
grader, and a pass graded by its own answer key proves nothing.

Caption cues break mid-phrase ("... cloud" / "code ..."), so matching runs on
the cue texts joined by newlines. A replacement with the same number of words
as the match is swapped word-for-word, so a phrase split across two cues stays
split across the same two cues; otherwise the whole replacement lands on the
starting cue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from video2mdnotes.core.transcriber import Segment

GLOSSARY_DIR = Path(__file__).with_name("glossaries")
CORRECTIONS_FILENAME = "corrections.json"
UNCERTAINTY_HEADING = "Transcription Uncertainty"

# Spoken fillers that may sit between the words of a product name
# ("cloud uh code"). They are kept in place when the words are swapped.
FILLERS = frozenset({"uh", "um", "er", "uh,", "um,", "er,"})
_GAP = r"(?:\s+(?:uh|um|er),?)*\s+"
_PRE = r"(?<![\w@/.\-])"
_POST = r"(?![\w\-])"

_TRANSCRIPT_LINE = re.compile(
    r"^(?P<prefix>\s*-\s*\[)(?P<ts>\d+:\d{2}:\d{2}(?:\.\d+)?)(?P<mid>\]\s?)(?P<text>.*)$"
)


# --- provenance ---------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptProvenance:
    """Where transcript text came from.

    ``label`` is one of ``manual-captions``, ``auto-captions`` or
    ``asr:<engine>/<model>``. Manual captions are human-authored evidence;
    auto-captions and ASR are machine guesses and get the correction pass.
    """

    kind: str  # "manual-captions" | "auto-captions" | "asr"
    engine: Optional[str] = None
    model: Optional[str] = None

    @property
    def label(self) -> str:
        if self.kind == "asr":
            return f"asr:{self.engine or 'unknown'}/{self.model or 'unknown'}"
        return self.kind

    @property
    def is_machine(self) -> bool:
        return self.kind != "manual-captions"

    def __str__(self) -> str:
        return self.label

    @classmethod
    def parse(cls, label: str) -> "TranscriptProvenance":
        label = (label or "").strip()
        if label in ("manual-captions", "auto-captions"):
            return cls(label)
        if label.startswith("asr:"):
            engine, _, model = label[4:].partition("/")
            return cls("asr", engine or None, model or None)
        raise ValueError(f"not a provenance label: {label!r}")


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            out[key.strip()] = value.strip().strip("\"'")
    return out


def provenance_from_fields(
    transcript_source: Optional[str] = None, model: Optional[str] = None
) -> Optional[TranscriptProvenance]:
    """Map transcript frontmatter (``transcript_source`` / ``model``) to a label.

    Recognised: ``model: youtube-auto-captions`` (older caption runs),
    ``transcript_source: captions (manual, en)`` (core/captions.py), and
    ``transcript_source: whisper`` with the Whisper model name.
    """
    src = (transcript_source or "").strip().lower()
    mdl = (model or "").strip()
    low = mdl.lower()
    if src.startswith("asr:"):
        return TranscriptProvenance.parse(src)
    if "auto" in low and "caption" in low or src.startswith("captions (auto"):
        return TranscriptProvenance("auto-captions")
    if src.startswith("captions (manual") or ("manual" in low and "caption" in low):
        return TranscriptProvenance("manual-captions")
    if src == "whisper" or (not src and mdl and "caption" not in low):
        return TranscriptProvenance("asr", "faster-whisper", mdl or None)
    if src and src != "whisper":
        return TranscriptProvenance("asr", src, mdl or None)
    return None


def infer_provenance(run_dir: Path) -> Optional[TranscriptProvenance]:
    """Provenance of an existing run, from its artifacts. None if undeterminable.

    Order: the raw transcript's frontmatter (``transcript_source``/``model``),
    its ``> Transcript source:`` banner, then ``caption_source.txt`` (a yt-dlp
    caption filename; ``*.<lang>-orig.*`` is YouTube's original-language ASR
    track, i.e. auto-captions).
    """
    run_dir = Path(run_dir)
    transcripts = sorted((run_dir / "transcripts").glob("*.md"))
    raw = [p for p in transcripts if "corrected" not in p.name]
    for path in raw[:1]:
        text = path.read_text(encoding="utf-8")
        fm = _frontmatter(text)
        prov = provenance_from_fields(fm.get("transcript_source"), fm.get("model"))
        if prov is not None:
            return prov
        banner = re.search(r"^>\s*Transcript source:\s*(.+)$", text, re.M | re.I)
        if banner:
            value = banner.group(1).lower()
            if "automatic" in value or "auto" in value:
                return TranscriptProvenance("auto-captions")
            if "manual" in value:
                return TranscriptProvenance("manual-captions")
    caption_file = run_dir / "caption_source.txt"
    if caption_file.exists():
        name = caption_file.read_text(encoding="utf-8").strip().lower()
        if re.search(r"\.[a-z]{2,3}(?:-[a-z]+)?-orig\.", name) or "auto" in name:
            return TranscriptProvenance("auto-captions")
        if name:
            return TranscriptProvenance("manual-captions")
    return None


# --- glossary -----------------------------------------------------------------


@dataclass
class Rule:
    name: str
    pattern: re.Pattern
    replace: str
    not_before: Optional[re.Pattern] = None
    not_after: Optional[re.Pattern] = None
    require_after: Optional[re.Pattern] = None
    kind: str = "mishearing"  # or "casing" (metadata brand casing)
    source: str = ""

    def accepts(self, text: str, start: int, end: int) -> bool:
        before = text[max(0, start - 40) : start]
        after = text[end : end + 40]
        if self.not_before is not None and self.not_before.search(before):
            return False
        if self.not_after is not None and self.not_after.search(after):
            return False
        if self.require_after is not None and not self.require_after.search(after):
            return False
        return True


@dataclass
class Glossary:
    rules: List[Rule] = field(default_factory=list)
    terms: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def mishearing_rules(self) -> List[Rule]:
        return [r for r in self.rules if r.kind == "mishearing"]


def _compile_rule(spec: dict, source: str) -> Rule:
    body = spec["match"].replace("{_}", _GAP)
    flags = re.IGNORECASE

    def opt(key: str) -> Optional[re.Pattern]:
        return re.compile(spec[key], flags) if spec.get(key) else None

    return Rule(
        name=spec["name"],
        pattern=re.compile(f"{_PRE}(?:{body}){_POST}", flags),
        replace=spec["replace"],
        not_before=opt("not_before"),
        not_after=opt("not_after"),
        require_after=opt("require_after"),
        kind=spec.get("kind", "mishearing"),
        source=source,
    )


def load_domain_glossary(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.setdefault("domain", Path(path).stem)
    return data


def available_domains(directory: Path = GLOSSARY_DIR) -> List[dict]:
    return [load_domain_glossary(p) for p in sorted(Path(directory).glob("*.json"))]


def _meta_fields(meta: Any) -> Tuple[str, str, List[str]]:
    if meta is None:
        return "", "", []
    get = meta.get if isinstance(meta, dict) else lambda k, d=None: getattr(meta, k, d)
    return get("title", "") or "", get("description", "") or "", list(get("tags", []) or [])


def metadata_text(meta: Any) -> str:
    title, description, tags = _meta_fields(meta)
    return "\n".join([title, description, " ".join(tags)])


def select_domains(meta: Any, directory: Path = GLOSSARY_DIR) -> List[dict]:
    """Domain glossaries whose ``match_keywords`` appear in title/description/tags."""
    text = metadata_text(meta).lower()
    chosen = []
    for domain in available_domains(directory):
        for kw in domain.get("match_keywords", []):
            if re.search(rf"(?<![\w-]){re.escape(kw.lower())}(?![\w-])", text):
                chosen.append(domain)
                break
    return chosen


# Words that look like brand casing in metadata but are ordinary English when
# lowercased in speech. Casing rules never target these.
_COMMON = frozenset(
    "the and for are was you not all can has its our out new now use how why who "
    "api sdk app web via get set run see top pro max big off one two ten end yes "
    "ask aws gpt ceo cto faq diy pdf url llm".split()
)


def metadata_terms(meta: Any) -> List[str]:
    """Distinctively-cased proper nouns from title/description/tags.

    Only forms whose casing carries information: CamelCase (``ExamPro``,
    ``freeCodeCamp``) and acronyms of 3+ letters (``CCA-F``). A plain
    capitalised word ("Prepare", "Foundations") is title case, not evidence.
    URLs and @handles are skipped.
    """
    title, description, tags = _meta_fields(meta)
    text = "\n".join([title, description, "\n".join(tags)])
    text = re.sub(r"https?://\S+|@\S+", " ", text)
    found: List[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", text):
        letters = re.sub(r"[^A-Za-z]", "", tok)
        camel = bool(re.search(r"[a-z][A-Z]", tok))
        acronym = len(letters) >= 3 and letters.isupper()
        if not (camel or acronym):
            continue
        if tok.lower() in _COMMON or tok in found:
            continue
        found.append(tok)
    return found


def _casing_rule(term: str) -> Rule:
    # Case-SENSITIVE on the all-lowercase form only: "exampro" -> "ExamPro".
    # A transcript that already has any capital letter is left alone.
    pattern = re.compile(rf"{_PRE}{re.escape(term.lower())}{_POST}")
    return Rule(
        name=f"metadata-casing:{term}",
        pattern=pattern,
        replace=term,
        kind="casing",
        source="metadata",
    )


def build_glossary(
    meta: Any = None,
    directory: Path = GLOSSARY_DIR,
    extra_domains: Sequence[dict] = (),
    include_casing: bool = True,
) -> Glossary:
    """Correction glossary for one source: selected domains + metadata casing."""
    domains = list(select_domains(meta, directory)) + list(extra_domains)
    glossary = Glossary()
    seen_terms = set()
    for domain in domains:
        src = f"domain:{domain.get('domain', '?')}"
        glossary.sources.append(src)
        for spec in domain.get("rules", []):
            glossary.rules.append(_compile_rule(spec, src))
        for term in domain.get("terms", []):
            if term not in seen_terms:
                glossary.terms.append(term)
                seen_terms.add(term)
    if include_casing:
        terms = metadata_terms(meta)
        if terms:
            glossary.sources.append("metadata")
        for term in terms:
            if term not in seen_terms:
                glossary.terms.append(term)
                seen_terms.add(term)
            glossary.rules.append(_casing_rule(term))
    return glossary


def glossary_for_domain(name: str, directory: Path = GLOSSARY_DIR) -> Glossary:
    """One named domain glossary, no metadata (for tests and notes lint)."""
    data = load_domain_glossary(Path(directory) / f"{name}.json")
    return build_glossary(None, directory, extra_domains=[data], include_casing=False)


# --- matching engine ----------------------------------------------------------


@dataclass
class Match:
    start: int
    end: int
    original: str
    corrected: str
    rule: Rule
    order: int


def _apply_case(original_words: List[str], replacement: str) -> str:
    letters = "".join(c for w in original_words for c in w if c.isalpha())
    if len(letters) >= 2 and letters.isupper():
        return replacement.upper()
    return replacement


def _render(rule: Rule, m: re.Match) -> str:
    """Replacement text for match ``m``, preserving gaps (incl. line breaks)."""
    raw = m.group(0)
    canonical = m.expand(rule.replace) if "\\" in rule.replace else rule.replace
    pieces = re.split(r"(\s+)", raw)
    words_idx = [
        i for i in range(0, len(pieces), 2) if pieces[i].lower() not in FILLERS
    ]
    content = [pieces[i] for i in words_idx]
    canonical = _apply_case(content, canonical)
    repl_words = canonical.split(" ")
    if len(repl_words) == len(words_idx):
        out = list(pieces)
        for i, word in zip(words_idx, repl_words):
            out[i] = word
        return "".join(out)
    # Different word count ("cloud md" -> "CLAUDE.md"): the replacement lands
    # on the starting line; line breaks inside the match are kept so every
    # later line keeps its index and timestamp.
    return canonical + "\n" * raw.count("\n")


def _find(text: str, rules: Sequence[Rule]) -> List[Match]:
    candidates: List[Match] = []
    for order, rule in enumerate(rules):
        for m in rule.pattern.finditer(text):
            if not rule.accepts(text, m.start(), m.end()):
                continue
            corrected = _render(rule, m)
            if corrected == m.group(0):
                continue
            candidates.append(
                Match(m.start(), m.end(), m.group(0), corrected, rule, order)
            )
    # Earliest start wins; at the same start, the earlier rule (rules are
    # ordered most specific first), then the longer match.
    candidates.sort(key=lambda c: (c.start, c.order, -(c.end - c.start)))
    chosen: List[Match] = []
    last_end = -1
    for c in candidates:
        if c.start >= last_end:
            chosen.append(c)
            last_end = c.end
    return chosen


def fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _line_of(offset: int, starts: List[int]) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _correct_lines(
    texts: List[str], rules: Sequence[Rule]
) -> Tuple[List[str], List[Tuple[Match, int, int]]]:
    """Correct a list of line texts. Returns (new texts, [(match, first, last)])."""
    flat = [t.replace("\n", " ").replace("\r", " ") for t in texts]
    joined = "\n".join(flat)
    starts, pos = [], 0
    for t in flat:
        starts.append(pos)
        pos += len(t) + 1
    matches = _find(joined, rules)
    pieces, cursor, located = [], 0, []
    for mt in matches:
        pieces.append(joined[cursor : mt.start])
        pieces.append(mt.corrected)
        cursor = mt.end
        first = _line_of(mt.start, starts)
        last = _line_of(max(mt.start, mt.end - 1), starts)
        located.append((mt, first, last))
    pieces.append(joined[cursor:])
    new_lines = "".join(pieces).split("\n")
    assert len(new_lines) == len(texts), "line count changed during correction"
    touched = {i for _, a, b in located for i in range(a, b + 1)}
    out = []
    for i, original in enumerate(texts):
        if i not in touched:
            out.append(original)
        else:
            out.append(re.sub(r"\s{2,}", " ", new_lines[i]).strip())
    return out, located


def correct_segments(
    segments: Sequence[Segment], glossary: Glossary
) -> Tuple[List[Segment], List[dict]]:
    """Apply the glossary to transcript segments. Input is not mutated.

    Returns ``(corrected_segments, corrections)``; each correction is
    ``{timestamp "H:MM:SS", original, corrected, rule, line_index}`` (plus
    ``line_end_index`` when the phrase spans cues). ``timestamp`` and
    ``line_index`` are those of the cue where the phrase starts.
    """
    segs = list(segments)
    texts = [s.text for s in segs]
    new_texts, located = _correct_lines(texts, glossary.rules)
    corrected = [
        s if new == s.text else s.model_copy(update={"text": new})
        for s, new in zip(segs, new_texts)
    ]
    corrections = []
    for mt, first, last in located:
        entry = {
            "timestamp": fmt_hms(segs[first].start),
            "original": " ".join(mt.original.split()),
            "corrected": " ".join(mt.corrected.split()),
            "rule": mt.rule.name,
            "line_index": first,
        }
        if last != first:
            entry["line_end_index"] = last
        corrections.append(entry)
    return corrected, corrections


# --- transcript files ---------------------------------------------------------


def parse_ts(ts: str) -> float:
    parts = ts.split(":")
    h, m, s = (["0"] * (3 - len(parts)) + parts)[-3:]
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_transcript_markdown(text: str) -> Tuple[List[str], List[Tuple[int, re.Match]]]:
    """(all lines, [(line number, match)] for each ``- [H:MM:SS.mmm] text`` cue)."""
    lines = text.split("\n")
    cues = []
    for i, line in enumerate(lines):
        m = _TRANSCRIPT_LINE.match(line)
        if m:
            cues.append((i, m))
    return lines, cues


def corrected_path_for(raw_path: Path) -> Path:
    """``transcripts/<name>.md`` -> ``transcripts/<name>.corrected.md``."""
    raw_path = Path(raw_path)
    name = raw_path.name[:-3] if raw_path.name.endswith(".md") else raw_path.name
    return raw_path.with_name(f"{name}.corrected.md")


def run_dir_for(raw_path: Path) -> Path:
    raw_path = Path(raw_path)
    parent = raw_path.parent
    return parent.parent if parent.name == "transcripts" else parent


def correct_transcript_text(text: str, glossary: Glossary) -> Tuple[str, List[dict]]:
    """Corrected copy of a transcript markdown string + corrections.

    Only the text of cue lines that changed is rewritten; every other byte
    (frontmatter, headings, timestamps) is carried over. Frontmatter gains
    ``corrected: true`` and ``corrections: <n>``.
    """
    lines, cues = parse_transcript_markdown(text)
    segments = []
    for _, m in cues:
        t = parse_ts(m.group("ts"))
        segments.append(Segment(start=t, end=t, text=m.group("text")))
    corrected, corrections = correct_segments(segments, glossary)
    out = list(lines)
    for (line_no, m), seg in zip(cues, corrected):
        if seg.text != m.group("text"):
            out[line_no] = f"{m.group('prefix')}{m.group('ts')}{m.group('mid')}{seg.text}"
    if out and out[0].strip() == "---":
        for i in range(1, len(out)):
            if out[i].strip() == "---":
                out[i:i] = ["corrected: true", f"corrections: {len(corrections)}"]
                break
    return "\n".join(out), corrections


def write_corrected_transcript(
    raw_path: Path,
    glossary: Glossary,
    out_path: Optional[Path] = None,
    corrections_path: Optional[Path] = None,
) -> Tuple[Path, Path, List[dict]]:
    """Write ``<name>.corrected.md`` and ``corrections.json``; never touch raw.

    ``corrections.json`` goes to the run dir (the parent of ``transcripts/``)
    unless ``corrections_path`` is given. It is always written, even empty, so
    P7 can tell "nothing to correct" from "pass never ran".
    """
    raw_path = Path(raw_path)
    out_path = Path(out_path) if out_path else corrected_path_for(raw_path)
    if out_path.resolve() == raw_path.resolve():
        raise ValueError("refusing to overwrite the raw transcript")
    corrections_path = (
        Path(corrections_path)
        if corrections_path
        else run_dir_for(raw_path) / CORRECTIONS_FILENAME
    )
    text = raw_path.read_text(encoding="utf-8")
    new_text, corrections = correct_transcript_text(text, glossary)
    for c in corrections:
        c["file"] = out_path.name
    out_path.write_text(new_text, encoding="utf-8")
    corrections_path.parent.mkdir(parents=True, exist_ok=True)
    corrections_path.write_text(
        json.dumps(corrections, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out_path, corrections_path, corrections


# --- notes lint ---------------------------------------------------------------


def lint_notes(markdown: str, glossary: Glossary) -> List[dict]:
    """Residual mishearings in generated notes (deterministic; no fixing).

    Paragraphs are scanned as units so a phrase hard-wrapped across two lines
    is still caught; a blank line or heading ends a paragraph. Casing rules are
    not lint findings. Each finding: ``{line (1-based), found, suggested,
    rule, context}``.
    """
    rules = glossary.mishearing_rules()
    lines = markdown.split("\n")
    findings = []
    block: List[int] = []

    def flush() -> None:
        if not block:
            return
        texts = [lines[i] for i in block]
        _, located = _correct_lines(texts, rules)
        for mt, first, _ in located:
            line_no = block[first]
            findings.append(
                {
                    "line": line_no + 1,
                    "found": " ".join(mt.original.split()),
                    "suggested": " ".join(mt.corrected.split()),
                    "rule": mt.rule.name,
                    "context": lines[line_no].strip()[:160],
                }
            )
        block.clear()

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            flush()
            if line.strip():
                block.append(i)
                flush()
            continue
        block.append(i)
    flush()
    return findings


def correct_notes(markdown: str, glossary: Glossary) -> Tuple[str, List[dict]]:
    """Apply the glossary's mishearing rules to generated notes.

    The fixing twin of ``lint_notes``: same paragraph blocks, same rules, so
    ``lint_notes(correct_notes(md)[0])`` finds nothing the rules can fix.
    Returns ``(fixed markdown, findings)``; findings have the ``lint_notes``
    shape (``line`` is 1-based, in the INPUT). Line count is preserved.
    """
    rules = glossary.mishearing_rules()
    lines = markdown.split("\n")
    out = list(lines)
    findings: List[dict] = []
    block: List[int] = []

    def flush() -> None:
        if not block:
            return
        texts = [lines[i] for i in block]
        new_texts, located = _correct_lines(texts, rules)
        for mt, first, _ in located:
            line_no = block[first]
            findings.append(
                {
                    "line": line_no + 1,
                    "found": " ".join(mt.original.split()),
                    "suggested": " ".join(mt.corrected.split()),
                    "rule": mt.rule.name,
                    "context": lines[line_no].strip()[:160],
                }
            )
        for i, new in zip(block, new_texts):
            if new != lines[i]:
                # _correct_lines strips touched lines; keep the indentation
                # (nested bullets) of the original.
                indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
                out[i] = indent + new.lstrip()
        block.clear()

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            flush()
            if line.strip():
                block.append(i)
                flush()
            continue
        block.append(i)
    flush()
    return "\n".join(out), findings


# --- per-passage uncertainty flags --------------------------------------------


def uncertainty_prompt_block() -> str:
    """Instruction WP-3 appends to each per-passage prompt."""
    return (
        f"## {UNCERTAINTY_HEADING}\n"
        "The transcript is machine-generated and may mis-hear names, products "
        "and technical terms. After your notes, add a section headed exactly "
        f"`## {UNCERTAINTY_HEADING}` listing words or phrases in THIS passage "
        "that are probably mis-transcribed. Use a markdown table with exactly "
        "these columns:\n\n"
        "| timestamp | heard | probable | confidence |\n"
        "|---|---|---|---|\n"
        "| 0:12:34 | cloud code | Claude Code | high |\n\n"
        "- `timestamp`: H:MM:SS of the transcript line where the phrase starts.\n"
        "- `heard`: the exact words as they appear in the transcript.\n"
        "- `probable`: what was most likely said.\n"
        "- `confidence`: high, medium or low.\n"
        "Only list items you can tie to a transcript line. In your notes, use "
        "the probable form when confidence is high. If there are none, write "
        "`None.` under the heading. Do not guess at words that are merely "
        "unusual; list only likely mishearings."
    )


_CONFIDENCE = {"high", "medium", "low"}


def _section(markdown: str, heading: str) -> Optional[str]:
    pat = re.compile(rf"^(#{{1,6}})\s+{re.escape(heading)}\s*#*\s*$", re.M | re.I)
    m = pat.search(markdown)
    if not m:
        return None
    level = len(m.group(1))
    rest = markdown[m.end() :]
    nxt = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _norm_ts(raw: str) -> Optional[str]:
    raw = raw.strip().strip("[]`")
    if not re.fullmatch(r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?", raw):
        return None
    return fmt_hms(parse_ts(raw))


_WRAPPERS = (("`", "`"), ('"', '"'), ("'", "'"), ("**", "**"), ("*", "*"), ("“", "”"))


def _clean(cell: str) -> str:
    """Strip wrapping quotes / backticks / emphasis — only when they wrap the
    WHOLE cell. Stripping them one-sidedly cut `` `__init__`, formatter `` to
    ``init__`, formatter`` and broke the E1 uncertainty table."""
    s = cell.strip()
    changed = True
    while changed and len(s) >= 2:
        changed = False
        for a, b in _WRAPPERS:
            if len(s) > len(a) + len(b) and s.startswith(a) and s.endswith(b):
                inner = s[len(a):len(s) - len(b)]
                if a == "`" and "`" in inner:
                    continue          # two code spans, not one wrapped cell
                if a in ('"', "'") and a in inner:
                    continue
                s = inner.strip()
                changed = True
                break
    return s


def parse_uncertainty_section(markdown: str) -> Optional[List[dict]]:
    """Parse ``## Transcription Uncertainty`` from model output.

    Returns None when the section is absent (so WP-3 can tell "model ignored
    the instruction" from "model found nothing", which is ``[]``). Accepts the
    requested table and a tolerant bullet form
    ``- [0:12:34] "cloud code" -> "Claude Code" (high)``. Rows without a valid
    timestamp are dropped; unknown confidence becomes ``"unknown"``.
    """
    body = _section(markdown, UNCERTAINTY_HEADING)
    if body is None:
        return None
    items: List[dict] = []
    header: Optional[List[str]] = None
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            lowered = [c.lower() for c in cells]
            if "timestamp" in lowered and "heard" in lowered:
                header = lowered
                continue
            cols = header or ["timestamp", "heard", "probable", "confidence"]
            row = dict(zip(cols, cells))
            item = _make_item(
                row.get("timestamp", ""),
                row.get("heard", ""),
                row.get("probable", ""),
                row.get("confidence", ""),
            )
            if item:
                items.append(item)
            continue
        m = re.match(
            r"^[-*+]\s*\[?(?P<ts>\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?)\]?\s*[:\-–—]?\s*"
            r"(?:heard\s*:?\s*)?(?P<heard>.+?)\s*(?:->|→|=>|—>|\bprobably\b|\bprobable\b:?)"
            r"\s*(?P<probable>.+?)\s*(?:\((?P<conf>[A-Za-z]+)(?:\s+confidence)?\))?\s*$",
            s,
            re.I,
        )
        if m:
            item = _make_item(m["ts"], m["heard"], m["probable"], m["conf"] or "")
            if item:
                items.append(item)
    return items


def _make_item(ts: str, heard: str, probable: str, conf: str) -> Optional[dict]:
    t = _norm_ts(ts)
    heard, probable = _clean(heard), _clean(probable)
    if t is None or not heard or not probable:
        return None
    c = _clean(conf).lower()
    return {
        "timestamp": t,
        "heard": heard,
        "probable": probable,
        "confidence": c if c in _CONFIDENCE else "unknown",
    }


def iter_uncertainty(markdowns: Iterable[str]) -> List[dict]:
    """All uncertainty items across passages (absent sections skipped)."""
    out: List[dict] = []
    for md in markdowns:
        out.extend(parse_uncertainty_section(md) or [])
    return out
