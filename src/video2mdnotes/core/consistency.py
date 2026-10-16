"""Cross-section consistency (WP-F8): one model call, deterministic annotation.

Why: sections are summarized independently, so a later section can
contradict an earlier one. In E3b, chapter 1's notes said "The giveaway was
not drawn at the event" and its Time-Sensitive row said "The giveaway winner
will be announced Wednesday" — while chapter 6 recorded the winner being
drawn at the event [1:12:21–1:12:35]. Each passage only saw its own lines.

    extract     every cited bullet / table row (outside Transcription
                Uncertainty, Fact-Checked Notes, Key Concepts and Commands)
                that states an outcome or event (outcome verbs, will / was /
                did not ...), a quantity (numbers, %, units) or a named
                person; one compact line each, with a reference id
                `S<section>.<n>`, its section and its citations
    ONE call    `complete_with_failover` (the subscription chain) with that
                list only — never the full notes; the reply is a JSON array
                of {earlier_ref, later_ref, kind, resolution}
    cache       <out dir>/consistency.json keyed by a hash of the prompt and
                the statement list: a rerun with unchanged notes never re-calls
    apply       deterministic and annotation-only: the superseded statement
                gets " _(superseded: see [t] in ch. N "Title")_"; a
                Time-Sensitive row additionally gets its claim prefixed
                "(superseded)"; a `discrepancy` (two figures disagree, e.g. a
                speaker quoting another speaker's number) annotates the LATER
                statement " _(discrepancy: see [t] in ch. N "Title")_". No other
                text is rewritten. Every pair, applied or rejected, is logged.

Skipped (no call) when the notes have at most one section. A failed call is a
logged warning, never a failed run; subscription / budget exhaustion
propagates like every other call (the run checkpoints and resumes).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core import citations

CONSISTENCY_PROMPT = BASE_DIR / "prompts" / "longform" / "consistency.md"
CACHE_FILENAME = "consistency.json"
CACHE_VERSION = 1
MAX_STATEMENT_CHARS = 240
MAX_INPUT_CHARS = 60_000
SUPERSEDED_PREFIX = "(superseded)"

CompleteFn = Callable[[str, str], Tuple[str, str]]

# Sections that never hold a statement worth comparing.
SKIP_SECTIONS = ("key concepts", "commands", "transcription uncertainty", "fact checked",
                 "fact-checked")
TIME_SENSITIVE = "time-sensitive claims"

OUTCOME_RE = re.compile(
    r"\b(?:was|were|wasn't|weren't|did|didn't|does\s+not|doesn't|will|won't|would|"
    r"is\s+not|isn't|never|not|no\s+longer|announc\w*|drawn|draws?|drew|picked|chosen|"
    r"chose|won|wins?|winner|postpone\w*|delay\w*|cancel\w*|moved|happened|worked|"
    r"failed|fixed|broke|succeeded|blocked|passed|confirmed|launched|released|"
    r"shipped|decided|switched|changed|turned\s+out|instead|actually|later|planned|"
    r"expected|scheduled|tomorrow|tonight|today|next\s+week|wednesday|monday|tuesday|"
    r"thursday|friday|saturday|sunday)\b", re.I)
QUANTITY_RE = re.compile(
    r"(?:\$\s?\d|\b\d[\d,.]*\s*(?:%|percent\b|x\b|×|k\b|m\b|million|billion|trillion|"
    r"thousand|hundred|hours?\b|minutes?\b|days?\b|weeks?\b|months?\b|years?\b|gb\b|tb\b|"
    r"gig\b|tokens?\b|members?\b|events?\b|people\b|submissions?\b|users?\b|"
    r"customers?\b)|\b\d{2,}\b)", re.I)
PERSON_RE = re.compile(
    r"\b[A-Z][a-z]+(?:\s+(?:Mc|Mac|O'|Van\s|De\s)?[A-Z][a-zA-Z'\-]+)+\b|"
    r"\b[A-Z][a-z]+\s+(?:says?|said|cited|mentioned|referenced|quoted|asked|announced)\b")


@dataclass
class Statement:
    ref: str                  # "S3.12"
    section: int              # 1-based position in the notes (recording order)
    chapter: str              # chapter label ("1", "-")
    title: str                # chapter title as in the skeleton
    passage: str              # passage key (chunk notes it lives in)
    line: int                 # 0-based line in that passage's notes
    subsection: str           # "Summary", "Time-Sensitive Claims", ...
    kind: str                 # "bullet" | "row"
    text: str                 # compact claim text (citations stripped)
    cites: List[str] = field(default_factory=list)   # "[H:MM:SS]" as written
    score: int = 0

    @property
    def is_time_sensitive(self) -> bool:
        return self.subsection.lower().startswith(TIME_SENSITIVE)

    def render(self) -> str:
        cites = " ".join(self.cites)
        return f"[{self.ref}] S{self.section} · {self.subsection} · {self.text} {cites}".rstrip()

    def as_dict(self) -> dict:
        return {"ref": self.ref, "chapter": self.chapter, "title": self.title,
                "passage": self.passage, "subsection": self.subsection, "kind": self.kind,
                "text": self.text, "cites": self.cites}


@dataclass
class Section:
    """One chapter of the notes: its label, title and passages' notes."""
    label: str
    title: str
    range: str
    passages: List[Tuple[str, str]]       # (passage key, notes markdown)


def _score(text: str) -> int:
    return (2 * bool(OUTCOME_RE.search(text)) + 2 * bool(QUANTITY_RE.search(text))
            + bool(PERSON_RE.search(text)))


def extract_statements(sections: Sequence[Section]) -> List[Statement]:
    """Cited outcome / event / number / person statements, in recording order."""
    out: List[Statement] = []
    for n, sec in enumerate(sections, 1):
        k = 0
        for key, notes in sec.passages:
            for unit in citations.iter_units(notes):
                sub = unit.section or ""
                low = sub.lower()
                if any(low.startswith(s) for s in SKIP_SECTIONS):
                    continue
                text = " ".join(unit.text.replace(citations.UNVERIFIED_MARK, " ").split())
                text = re.sub(r"\s+([,.;:])", r"\1", text).strip(" -:;")
                if not text:
                    continue
                score = _score(text)
                if not score:
                    continue
                if len(text) > MAX_STATEMENT_CHARS:
                    text = text[:MAX_STATEMENT_CHARS - 1].rstrip() + "…"
                k += 1
                out.append(Statement(
                    ref=f"S{n}.{k}", section=n, chapter=sec.label, title=sec.title,
                    passage=key, line=unit.line, subsection=sub, kind=unit.kind,
                    text=text, cites=[c[2] if c[2].startswith("[") else f"[{c[2]}]"
                                      for c in unit.cites], score=score))
    return out


def _bounded(statements: List[Statement], sections: Sequence[Section],
             max_chars: int) -> List[Statement]:
    """Drop the lowest-scoring statements (latest first on ties) until the
    rendered list fits; order is preserved."""
    keep = list(statements)
    size = sum(len(s.render()) + 1 for s in keep)
    if size <= max_chars:
        return keep
    ranked = sorted(range(len(keep)), key=lambda i: (keep[i].score, -i))
    drop = set()
    for i in ranked:
        if size <= max_chars:
            break
        drop.add(i)
        size -= len(keep[i].render()) + 1
    return [s for i, s in enumerate(keep) if i not in drop]


def render_prompt(sections: Sequence[Section], statements: Sequence[Statement]
                  ) -> Tuple[str, str]:
    system = CONSISTENCY_PROMPT.read_text(encoding="utf-8")
    lines = ["SECTIONS (recording order):"]
    lines += [f"S{n}: ch. {s.label} \"{s.title}\" [{s.range}]"
              for n, s in enumerate(sections, 1)]
    lines += ["", f"STATEMENTS ({len(statements)}):"]
    lines += [s.render() for s in statements]
    return system, "\n".join(lines) + "\n"


# --- reply ---------------------------------------------------------------------------


def parse_reply(text: str) -> Tuple[Optional[List[dict]], str]:
    """The JSON array of contradictions, or (None, reason)."""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b < a:
        return None, "reply has no JSON array"
    try:
        data = json.loads(raw[a:b + 1])
    except json.JSONDecodeError as exc:
        return None, f"reply is not valid JSON ({exc.msg})"
    if not isinstance(data, list):
        return None, "reply is not a JSON array"
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append({
            "earlier_ref": str(item.get("earlier_ref") or "").strip(),
            "later_ref": str(item.get("later_ref") or "").strip(),
            "kind": str(item.get("kind") or "discrepancy").strip().lower(),
            "resolution": " ".join(str(item.get("resolution") or "").split()),
        })
    return out, ""


# --- apply ------------------------------------------------------------------------------


def _short_title(title: str) -> str:
    """Chapter title without a trailing "(Speaker)" from the outline."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", title).strip() or title


KINDS = ("superseded", "discrepancy", "partly contradicted")
_KIND_ALIASES = {"partly_contradicted": "partly contradicted",
                 "partially contradicted": "partly contradicted",
                 "partly-contradicted": "partly contradicted", "partial": "partly contradicted"}


def fragments(line: str) -> List[Tuple[str, List[str]]]:
    """A notes line split into (clause text, its citations): each run of
    citations belongs to the text before it. Text after the last citation
    (e.g. an existing annotation) is a fragment with no citations."""
    out: List[Tuple[str, List[str]]] = []
    pos, text, cites = 0, "", []
    for m in citations.CITE_RE.finditer(line or ""):
        gap = line[pos:m.start()]
        if gap.strip(" .,;:") and cites:
            out.append((text, cites))
            text, cites = "", []
        text += gap
        cites.append(m.group(0))
        pos = m.end()
    if cites:
        out.append((text, cites))
    rest = (line or "")[pos:]
    if rest.strip(" .,;:"):
        out.append((rest, []))
    return out


def _overlap(a: str, b: str) -> int:
    ta = {t for t in citations.tokens(a) if t not in citations.STOPWORDS and len(t) > 2}
    tb = {t for t in citations.tokens(b) if t not in citations.STOPWORDS and len(t) > 2}
    return len(ta & tb)


def best_fragment(line: str, query: str) -> Tuple[int, List[str]]:
    """(index, citations) of the cited clause of `line` that best matches
    `query` (the resolution plus the other statement): ties -> earliest."""
    frags = [(i, f) for i, f in enumerate(fragments(line)) if f[1]]
    if not frags:
        return 0, []
    best = max(frags, key=lambda x: (_overlap(x[1][0], query), -x[0]))
    return best[0], best[1][1]


def _pointer(st: Statement, cite: Optional[str] = None) -> str:
    """'see [t] in ch. N "Title"' — `cite` is the citation of the clause that
    actually contradicts (WP-F9: E2c pointed at a statement's FIRST citation,
    3:59:48, instead of the clause cited 4:01:58)."""
    cite = cite or (st.cites[0] if st.cites else "")
    cite = f"{cite} " if cite else ""
    return f'see {cite}in ch. {st.chapter} "{_short_title(st.title)}"'


_BULLET = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")


def _annotate_line(line: str, st: Statement, note: str, prefix: bool) -> str:
    """Append `note` to a bullet, or to the claim cell of a table row (and
    prefix the claim with "(superseded)" when `prefix`). Nothing else changes."""
    if st.kind != "row":
        if prefix:
            m = _BULLET.match(line)
            head, body = (m.group(1), line[m.end():]) if m else ("", line)
            if not body.lower().startswith(SUPERSEDED_PREFIX):
                line = f"{head}{SUPERSEDED_PREFIX} {body}"
        return line.rstrip() + " " + note
    if st.kind == "row":
        cells = line.rstrip().rstrip("|").split("|")
        for k, c in enumerate(cells):
            if c.strip():
                claim = c.strip()
                if prefix and not claim.lower().startswith(SUPERSEDED_PREFIX):
                    claim = f"{SUPERSEDED_PREFIX} {claim}"
                cells[k] = f" {claim} {note} "
                break
        return "|".join(cells) + "|"
    return line.rstrip() + " " + note


def apply_contradictions(
    notes_by_key: Dict[str, str],
    statements: Sequence[Statement],
    pairs: Sequence[dict],
) -> Tuple[Dict[str, str], List[dict]]:
    """(annotated copy of notes_by_key, one log entry per pair)."""
    by_ref = {s.ref: s for s in statements}
    lines = {k: v.split("\n") for k, v in notes_by_key.items()}
    log: List[dict] = []
    for pair in pairs:
        entry = dict(pair)
        e, l = by_ref.get(pair["earlier_ref"]), by_ref.get(pair["later_ref"])
        raw_kind = str(pair.get("kind") or "").strip().lower()
        kind = _KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in KINDS:
            # An unknown label is not evidence the earlier claim is false:
            # the weaker annotation (WP-F9 — "superseded" was over-used).
            kind = "discrepancy"
        entry["kind"] = kind
        if e is None or l is None:
            entry.update(applied=False, reason="unknown reference id")
            log.append(entry)
            continue
        if e.section == l.section:
            entry.update(applied=False, reason="same section (handled inside the passage)")
            log.append(entry)
            continue
        if e.section > l.section:
            e, l = l, e
            entry["swapped"] = True
        entry["earlier"] = e.as_dict()
        entry["later"] = l.as_dict()
        # superseded / partly contradicted: the EARLIER statement is (partly)
        # wrong and carries the note; discrepancy: the later (quoting) one.
        target, other = (l, e) if kind == "discrepancy" else (e, l)
        text = lines.get(target.passage)
        other_text = lines.get(other.passage)
        if text is None or target.line >= len(text):
            entry.update(applied=False, reason="statement line not found")
            log.append(entry)
            continue
        line = text[target.line]
        # The pointer is the citation of the contradicting CLAUSE of the other
        # statement, not merely its first citation.
        query = " ".join([pair.get("resolution", ""), target.text])
        cite = None
        if other_text is not None and other.line < len(other_text):
            _, cs = best_fragment(other_text[other.line], query)
            cite = cs[0] if cs else None
        note = f"_({kind}: {_pointer(other, cite)})_"
        if f"_({kind}:" in line:
            entry.update(applied=False, reason=f"already annotated {kind}")
            log.append(entry)
            continue
        # A superseded statement whose LEADING clause is the false one gets
        # "(superseded)" up front, so a skimmer sees it before the claim.
        lead = False
        if kind == "superseded" and target.kind != "row":
            idx, _ = best_fragment(line, " ".join([pair.get("resolution", ""), other.text]))
            lead = idx == 0
        prefix = kind == "superseded" and (target.is_time_sensitive or lead)
        text[target.line] = _annotate_line(line, target, note, prefix)
        entry.update(applied=True, target=target.ref, annotation=note,
                     time_sensitive_prefix=prefix and target.is_time_sensitive,
                     superseded_prefix=prefix, pointer=cite)
        log.append(entry)
    return {k: "\n".join(v) for k, v in lines.items()}, log


# --- call + cache ---------------------------------------------------------------------------


@dataclass
class ConsistencyResult:
    notes_by_key: Dict[str, str]
    status: str                          # "applied" | "none" | "skipped" | "failed"
    reason: str = ""
    model: str = ""
    cached: bool = False
    called: bool = False
    statements: int = 0
    log: List[dict] = field(default_factory=list)

    def state_record(self) -> dict:
        return {"status": self.status, "reason": self.reason, "model": self.model,
                "cached": self.cached, "called": self.called, "statements": self.statements,
                "file": CACHE_FILENAME,
                "applied": sum(1 for e in self.log if e.get("applied")),
                "contradictions": self.log}


def check(
    sections: Sequence[Section],
    notes_by_key: Dict[str, str],
    out_dir: Path,
    complete: Optional[CompleteFn],
    *,
    resume: bool = True,
    max_chars: int = MAX_INPUT_CHARS,
) -> ConsistencyResult:
    """Extract → (cached) call → apply. `complete=None` means cache only."""
    from video2mdnotes.core.costs import BudgetExhausted
    from video2mdnotes.core.summarizer import SubscriptionExhausted

    if len(sections) <= 1:
        return ConsistencyResult(dict(notes_by_key), "skipped", "at most one section")
    statements = _bounded(extract_statements(sections), sections, max_chars)
    if not statements:
        return ConsistencyResult(dict(notes_by_key), "skipped", "no statements extracted")
    system, user = render_prompt(sections, statements)
    key = hashlib.sha256(f"v{CACHE_VERSION}\n{system}\n{user}".encode("utf-8")).hexdigest()[:16]
    path = Path(out_dir) / CACHE_FILENAME
    reply, model, cached, called = None, "", False, False
    if resume and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("input_sha256") == key and "reply" in data:
                reply, model, cached = data["reply"], data.get("model", ""), True
        except (OSError, ValueError):
            pass
    if reply is None:
        if complete is None:
            return ConsistencyResult(dict(notes_by_key), "skipped", "not cached; no model given",
                                     statements=len(statements))
        try:
            reply, model = complete(system, user)
            called = True
        except (SubscriptionExhausted, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - a warning, never a failed run
            return ConsistencyResult(dict(notes_by_key), "failed",
                                     f"consistency call failed: {type(exc).__name__}: {str(exc)[:200]}",
                                     called=True, statements=len(statements))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "version": CACHE_VERSION, "input_sha256": key, "model": model,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "statements": [s.as_dict() for s in statements],
            "reply": reply,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    pairs, why = parse_reply(reply)
    if pairs is None:
        return ConsistencyResult(dict(notes_by_key), "failed", f"reply unusable: {why}",
                                 model=model, cached=cached, called=called,
                                 statements=len(statements))
    annotated, log = apply_contradictions(notes_by_key, statements, pairs)
    status = "applied" if any(e.get("applied") for e in log) else "none"
    return ConsistencyResult(annotated, status, "", model, cached, called, len(statements), log)


__all__ = ["CACHE_FILENAME", "ConsistencyResult", "KINDS", "Section", "Statement",
           "apply_contradictions", "best_fragment", "check", "extract_statements",
           "fragments", "parse_reply", "render_prompt"]
