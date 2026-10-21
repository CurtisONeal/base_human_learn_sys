"""The Sources Referenced section: outside sources a source cites (prompt
variant "current"). No model calls.

Every long-form passage (and every document passage) writes a small table of
the outlets, podcasts, papers, reports, books, posts, datasets, videos and
talks it cites — never people (they are in ``## People``; a person's own
podcast or newsletter is a source and the person stays in People). The final
notes carry exactly ONE ``## Sources Referenced`` section, merged here
deterministically:

* **Same source, one row.** Rows are grouped by a normalised name: case,
  punctuation, a leading "the", "(ASR spelling)" / "(per model knowledge …)"
  hedges and a trailing type word ("podcast", "newsletter") removed —
  "The Information" = "the information"; "Hard Fork podcast" = "Hard Fork".
* **Name shown** — the most frequent spelling, then the longest.
* **Type** — the most frequent stated type (first on ties).
* **Cited by** — the distinct roles, in first-seen order.
* **Cited for** — the richest statement, plus other distinct ones while the
  cell stays short (``CITED_FOR_MAX_WORDS``).
* **Date / edition** — the distinct stated values.
* **Citations** — union, time-ordered (documents: first-seen order), the
  first ``MAX_CITES`` kept.
* **Order** — by first citation (documents: first appearance).
* **People are not sources.** A row whose name is a person of the merged
  People table is dropped (counted in the info as ``dropped_people``).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from video2mdnotes.core import people as people_mod

HEADING = "Sources Referenced"
COLUMNS = ("source", "type", "cited by", "cited for", "date / edition", "citations")
DOC_COLUMNS = COLUMNS[:5] + ("locators",)
TYPES = ("outlet", "newsletter", "blog", "podcast", "show", "paper", "report", "book",
         "post", "dataset", "survey", "video", "talk", "announcement", "filing", "other")
MAX_CITES = 6
CITED_FOR_MAX_WORDS = 60

_TYPE_SUFFIX = re.compile(
    r"\s+(?:podcast|newsletter|blog|show|report|paper|survey|substack|magazine|"
    r"newspaper|youtube channel|channel)$")
_HEDGE = re.compile(r"\s*\((?:ASR[^()]*|per model knowledge[^()]*|also heard as[^()]*)\)", re.I)


@dataclass
class SourceRow:
    name: str
    type: str = ""
    cited_by: str = ""
    cited_for: str = ""
    date: str = ""
    cites: List[str] = field(default_factory=list)
    order: int = 0


@dataclass
class Source:
    """One merged source."""
    key: str
    rows: List[SourceRow]
    shown: str = ""
    type: str = ""
    cited_by: str = ""
    cited_for: str = ""
    date: str = ""
    cites: List[str] = field(default_factory=list)


# --- names -----------------------------------------------------------------------------


def bare_name(name: str) -> str:
    s = _HEDGE.sub("", name or "")
    s = s.replace("*", "").replace("`", "").replace("_", " ").strip(" .,;:\"'“”")
    return " ".join(s.split())


def source_key(name: str) -> str:
    """Normalised identity of a source name."""
    s = bare_name(name).lower().replace("’", "'").replace("&", " and ")
    s = re.sub(r"[^\w' ]+", " ", s)
    s = " ".join(s.split())
    s = re.sub(r"^the\s+", "", s)
    s = _TYPE_SUFFIX.sub("", s) or s
    return s.replace("'", "")


def _norm_type(t: str) -> str:
    low = (t or "").strip().lower().strip(" .`*")
    for word in TYPES:
        if re.search(rf"\b{word}s?\b", low):
            return word
    return low or ""


# --- parsing ---------------------------------------------------------------------------


def parse_rows(body: str, start_order: int = 0) -> List[SourceRow]:
    """Rows of one Sources Referenced body. `- None` / header / separator rows
    give nothing; a table without our header is read in our column order."""
    rows: List[SourceRow] = []
    header: Optional[List[str]] = None
    order = start_order
    for line in (body or "").split("\n"):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = people_mod._cells(s)
        if all(people_mod._SEP_CELL.fullmatch(c) for c in cells if c):
            continue
        low = [c.lower() for c in cells]
        if header is None and any(c in ("source", "sources", "name") for c in low):
            header = low
            continue
        hdr = header or list(COLUMNS)
        col = people_mod._col
        idx = {
            "name": col(hdr, "source", "name"),
            "type": col(hdr, "type"),
            "by": col(hdr, "cited by", "who"),
            "for": col(hdr, "cited for", "what", "claim"),
            "date": col(hdr, "date", "edition"),
            "cite": col(hdr, "citation", "locator", "timestamp", "where"),
        }

        def get(k):
            i = idx[k]
            return cells[i].strip() if i is not None and i < len(cells) else ""
        name = get("name").strip("*` ")
        if not name or name.lower() in ("none", "-", "—", "- none"):
            continue
        cite_text = get("cite") or " ".join(cells)
        rows.append(SourceRow(name=name, type=get("type"), cited_by=get("by"),
                              cited_for=people_mod._ANY_CITE.sub("", get("for")).strip(),
                              date=get("date"), cites=people_mod._ANY_CITE.findall(cite_text),
                              order=order))
        order += 1
    return rows


# --- merging ---------------------------------------------------------------------------


def _distinct(values: Iterable[str], limit: int = 4, split: str = r"\s*;\s*|\s+/\s+") -> List[str]:
    out: List[str] = []
    seen = set()
    for v in values:
        for piece in re.split(split, v or ""):
            p = piece.strip(" .")
            k = p.lower()
            if not p or k in ("-", "—", "none", "not stated", "n/a") or k in seen:
                continue
            seen.add(k)
            out.append(p)
    return out[:limit]


def merge_sources(rows: Sequence[SourceRow], *, timed: bool = True,
                  people_names: Sequence[str] = ()) -> Tuple[List[Source], dict]:
    """Deterministic merge of every passage's rows into one list of sources."""
    person_keys = {people_mod.name_key(n) for n in people_names if n}
    groups: dict = {}
    dropped_people: List[str] = []
    for r in rows:
        k = source_key(r.name)
        if not k:
            continue
        if people_mod.name_key(r.name) in person_keys:
            dropped_people.append(r.name)
            continue
        groups.setdefault(k, Source(key=k, rows=[])).rows.append(r)
    out: List[Source] = []
    for k, g in groups.items():
        names = [bare_name(r.name) for r in g.rows if bare_name(r.name)]
        counts = Counter(names)
        g.shown = max(dict.fromkeys(names), key=lambda n: (counts[n], len(n))) if names else k
        types = [_norm_type(r.type) for r in g.rows if _norm_type(r.type)]
        if types:
            tc = Counter(types)
            g.type = max(dict.fromkeys(types), key=lambda t: tc[t])
        g.cited_by = "; ".join(_distinct(r.cited_by for r in g.rows))
        g.cited_for = people_mod._merge_relevance([r.cited_for for r in g.rows])
        words = g.cited_for.split()
        if len(words) > CITED_FOR_MAX_WORDS:
            g.cited_for = " ".join(words[:CITED_FOR_MAX_WORDS]) + " …"
        g.date = "; ".join(_distinct((r.date for r in g.rows), limit=3, split=r"\s*;\s*"))
        g.cites = people_mod._merge_cites([c for r in g.rows for c in r.cites], timed)
        out.append(g)

    def first(g: Source):
        if timed:
            times = [people_mod._cite_time(c) for c in g.cites]
            times = [t for t in times if t is not None]
            return (min(times) if times else float("inf"), 0)
        return (min(r.order for r in g.rows), 0)
    out.sort(key=lambda g: (first(g), g.key))
    return out, {"rows_in": len(rows), "sources": len(out),
                 "dropped_people": dropped_people}


# --- rendering -------------------------------------------------------------------------


INTRO = ("_Outside sources this source cites — outlets, shows and podcasts, papers, "
         "reports, books, posts, datasets, videos and talks — with who cites them and "
         "for what, as stated. People are in the People section._")


def render(sources: Sequence[Source], *, level: int = 2, documents: bool = False,
           intro: bool = True) -> str:
    from video2mdnotes.core.notes_format import sanitize_cell as cell
    cols = DOC_COLUMNS if documents else COLUMNS
    out = ["#" * level + " " + HEADING, ""]
    if intro:
        out += [INTRO, ""]
    if not sources:
        out.append("- None identified in the source.")
        return "\n".join(out) + "\n"
    out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for s in sources:
        out.append("| " + " | ".join(cell(x) for x in (
            s.shown, s.type or "-", s.cited_by or "-", s.cited_for or "-",
            s.date or "-", " ".join(s.cites) or "-")) + " |")
    return "\n".join(out) + "\n"


# --- documents: split Sources sections out of notes ---------------------------------------


def _is_sources_title(title: str) -> bool:
    bare = re.sub(r"\[[^\]]*\](\([^)]*\))?", "", title or "").strip(" :*_`").strip().lower()
    return bare in ("sources referenced", "sources cited", "references cited")


def split_sources(text: str) -> Tuple[str, List[SourceRow], int]:
    """Remove every Sources Referenced section from `text`. Returns (text
    without them, their rows, number of sections)."""
    lines = (text or "").split("\n")
    heads: List[Tuple[int, int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if people_mod._FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            m = people_mod._HEAD.match(line)
            if m:
                heads.append((i, len(m.group(1)), m.group(2)))
    drop: List[Tuple[int, int]] = []
    rows: List[SourceRow] = []
    for k, (i, level, title) in enumerate(heads):
        if not _is_sources_title(title):
            continue
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        rows.extend(parse_rows("\n".join(lines[i + 1:end]), start_order=len(rows)))
        drop.append((i, end))
    keep = [ln for i, ln in enumerate(lines) if not any(a <= i < b for a, b in drop)]
    return "\n".join(keep), rows, len(drop)


__all__ = ["COLUMNS", "DOC_COLUMNS", "HEADING", "Source", "SourceRow", "merge_sources",
           "parse_rows", "render", "source_key", "split_sources"]
