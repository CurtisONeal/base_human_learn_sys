"""Deterministic assembly helpers for document notes (WP-D1). No model calls.

The T8 failure this exists for: four reduce batches were concatenated, and
each batch wrote its own title, `## Overview` and `## Key terms` — so one
8,956-word note had four Overviews and three Key-terms lists. The prompts now
ask for one of each, but a prompt is a request, not a guarantee, so the final
shape is enforced here:

* **At most one Overview.** The first is kept; any later one is dropped and
  logged (it summarizes content that is already below it).
* **Exactly one Key terms section, at the end**, built by merging every
  key-terms list in the text (per-passage lists, a reduce's own list, a stray
  list mid-document) plus any `extra` entries. Duplicates are merged by a
  normalized term; the first definition wins and later entries contribute
  only locators the first did not have.

Headings inside code fences are code, not structure, and are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from video2mdnotes.core.fences import _parse_fence

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_BRACKET = re.compile(r"\[[^\]]*\]")
# Exact titles only: "Overview of Azure RBAC" is a topic, not THE overview.
_OVERVIEW = re.compile(r"^overview$", re.I)
_KEY_TERMS = re.compile(
    r"^(key terms?|key terms and definitions|glossary|terminology|key vocabulary)$", re.I)
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_LOCATOR = re.compile(r"\[[^\]\n]+\](?!\()")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}")

KEY_TERMS_HEADING = "## Key terms"


def _title_kind(title: str) -> Optional[str]:
    bare = _BRACKET.sub("", title).strip(" :*_`").strip()
    bare = re.sub(r"^\d+[.)]\s*", "", bare)
    bare = re.sub(r"\s*\([^)]*\)", "", bare).strip(" :*_`")
    if _OVERVIEW.match(bare):
        return "overview"
    if _KEY_TERMS.match(bare):
        return "terms"
    return None


@dataclass
class _Heading:
    line: int
    level: int
    title: str


def headings(lines: List[str]) -> List[_Heading]:
    """Markdown headings outside code fences."""
    out: List[_Heading] = []
    fence = None
    for i, line in enumerate(lines):
        parsed = _parse_fence(line)
        if fence is not None:
            if (parsed is not None and parsed.char == fence.char
                    and parsed.length >= fence.length and not parsed.info):
                fence = None
            continue
        if parsed is not None:
            fence = parsed
            continue
        m = _HEADING.match(line)
        if m:
            out.append(_Heading(i, len(m.group(1)), m.group(2)))
    return out


def term_key(entry: str) -> str:
    """Normalized term of one `- **Term**: definition` entry."""
    text = entry.strip()
    m = re.match(r"\*\*(.+?)\*\*", text) or re.match(r"`([^`]+)`", text)
    term = m.group(1) if m else re.split(r"\s*(?::|—|–| - )\s*", text, maxsplit=1)[0]
    term = _BRACKET.sub("", term)
    return " ".join(re.sub(r"[^\w\s/.+#-]", " ", term).lower().split()).strip(" .:")


def _table_entries(rows: List[str]) -> List[str]:
    """`| Term | Definition |` rows → bullet entries (header and separator dropped)."""
    out: List[str] = []
    body = rows
    if len(rows) >= 2 and _TABLE_SEP.match(rows[1]):
        body = rows[2:]
    for row in body:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            term = cells[0] if cells[0].startswith("**") else f"**{cells[0].strip('*')}**"
            out.append(f"- {term}: {' — '.join(cells[1:])}")
    return out


def _entries(body: List[str]) -> List[str]:
    """Entries of one key-terms section body, each a single `- ` line."""
    entries: List[str] = []
    table: List[str] = []
    for line in body:
        if line.strip().startswith("|"):
            table.append(line)
            continue
        if table:
            entries.extend(_table_entries(table))
            table = []
        if not line.strip():
            continue
        m = _BULLET.match(line)
        if m and not line.startswith(("    ", "\t")):
            entries.append(f"- {m.group(1).strip()}")
        elif entries:
            entries[-1] += " " + line.strip()      # continuation line
        else:
            entries.append(f"- {line.strip()}")
    if table:
        entries.extend(_table_entries(table))
    return entries


def merge_terms(entries: Iterable[str]) -> List[str]:
    """Dedupe by normalized term, first definition wins, locators are unioned."""
    merged: List[str] = []
    index = {}
    for entry in entries:
        key = term_key(entry.lstrip("-*+ ").strip())
        if not key:
            continue
        if key not in index:
            index[key] = len(merged)
            merged.append(entry.rstrip())
            continue
        first = merged[index[key]]
        have = set(_LOCATOR.findall(first))
        extra = [loc for loc in _LOCATOR.findall(entry) if loc not in have]
        if extra:
            merged[index[key]] = first + " " + " ".join(dict.fromkeys(extra))
    return merged


@dataclass
class Consolidated:
    text: str
    overviews_found: int = 0
    overviews_dropped: int = 0
    term_sections: int = 0
    terms_in: int = 0
    terms_out: int = 0
    log: List[str] = field(default_factory=list)


def _section_end(heads: List[_Heading], k: int, n_lines: int) -> int:
    level = heads[k].level
    for h in heads[k + 1:]:
        if h.level <= level:
            return h.line
    return n_lines


def split_terms(text: str) -> Tuple[str, List[str], int]:
    """Remove every key-terms section.

    Returns (text without them, their entries in order, number of sections).
    """
    lines = (text or "").split("\n")
    heads = headings(lines)
    drop: List[Tuple[int, int]] = []
    entries: List[str] = []
    for k, h in enumerate(heads):
        if _title_kind(h.title) != "terms":
            continue
        if any(a <= h.line < b for a, b in drop):
            continue                       # nested inside a list already taken
        end = _section_end(heads, k, len(lines))
        entries.extend(_entries(lines[h.line + 1:end]))
        drop.append((h.line, end))
    keep = [line for i, line in enumerate(lines)
            if not any(a <= i < b for a, b in drop)]
    return "\n".join(keep), entries, len(drop)


def consolidate(text: str, extra_terms: Iterable[str] = ()) -> Consolidated:
    """One Overview (the first) and one Key terms section (merged, at the end)."""
    extra_terms = list(extra_terms)
    body, entries, n_sections = split_terms(text)
    lines = body.split("\n")
    heads = headings(lines)
    overview = [k for k, h in enumerate(heads) if _title_kind(h.title) == "overview"]
    drop = []
    for k in overview[1:]:
        drop.append((heads[k].line, _section_end(heads, k, len(lines))))
    kept = [line for i, line in enumerate(lines) if not any(a <= i < b for a, b in drop)]
    result = Consolidated(text="", overviews_found=len(overview),
                          overviews_dropped=len(drop))
    result.term_sections = n_sections
    all_entries = entries + extra_terms
    merged = merge_terms(all_entries)
    result.terms_in, result.terms_out = len(all_entries), len(merged)
    out = "\n".join(kept).rstrip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    if merged:
        out += f"\n\n{KEY_TERMS_HEADING}\n\n" + "\n".join(merged)
    result.text = out.strip() + "\n"
    if drop:
        result.log.append(f"dropped {len(drop)} duplicate Overview section(s)")
    if result.terms_in != result.terms_out or result.term_sections > 1:
        result.log.append(f"merged {result.terms_in} key-term entr(ies) from "
                          f"{result.term_sections + (1 if extra_terms else 0)} list(s) "
                          f"into {result.terms_out}")
    return result


def outline(text: str, max_words: int = 3000) -> str:
    """Headings plus the first sentence under each — the overview call's input
    when the assembled notes are too long to send whole."""
    lines = text.split("\n")
    heads = headings(lines)
    out: List[str] = []
    for k, h in enumerate(heads):
        out.append(lines[h.line])
        end = heads[k + 1].line if k + 1 < len(heads) else len(lines)
        para = " ".join(line.strip() for line in lines[h.line + 1:end]
                        if line.strip() and _parse_fence(line) is None)[:400]
        if para:
            first = re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0]
            out.append(first)
        if sum(len(x.split()) for x in out) > max_words:
            break
    return "\n".join(out)


def topic_headings(text: str, level: int = 2) -> List[str]:
    lines = text.split("\n")
    return [h.title for h in headings(lines) if h.level == level
            and _title_kind(h.title) is None]
