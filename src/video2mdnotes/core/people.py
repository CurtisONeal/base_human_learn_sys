"""The People section: who is who in a source (prompt variant "current"). No model calls.

Every long-form passage (and every document passage) writes a small People
table — person, role in this source, affiliation & background AS STATED,
relevance to the topic, citations. The final notes carry exactly ONE
``## People`` section, merged here deterministically:

* **Same person, one row.** Rows are grouped by a normalised name
  (case, punctuation, honorifics and "(ASR spelling)" hedges removed). A
  bare first or last name ("Nathaniel", "Altman") joins the ONE full-name
  row it matches. For a machine transcript, a surname that sounds the same
  ("Whittemore" / "Whitmore", Soundex + small edit distance — the names
  module's rule) is the same person, and so are an outline speaker and the
  variants the names module found for them.
* **Name shown.** A spelling the source metadata confirms wins; then the
  fullest name; then (machine transcripts) the spelling the transcript uses
  most. Other ASR spellings are kept as "also heard as". An ASR-only name is
  hedged "(ASR spelling)" — the same hedge as the body and the names table.
* **Role** — the most specific: a speaking role (host, interviewer, guest
  …) beats quoted / cited author, which beats person discussed, which beats
  mentioned only. Distinct roles of the top rank are joined ("host /
  interviewer").
* **Background** — the richest statement (longest that is not "not
  stated" / "mentioned only"). Never invented here: only passage text.
* **Relevance** — the richest, plus other distinct statements while the
  cell stays short (passages of one course each see a slice of a person).
* **Citations** — union, in time order (documents: first-seen order), the
  first ``MAX_CITES`` kept.
* **Order** — people who speak first, then people discussed; each group by
  first citation (documents: first appearance).
* **Privacy** — a row whose name is private (names.private_names) or whose
  role is a winner / attendee / audience member is dropped (WP-F9 rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from video2mdnotes.core import names as names_mod

HEADING = "People"
COLUMNS = ("person", "role", "affiliation & background (as stated)", "relevance", "citations")
DOC_COLUMNS = COLUMNS[:4] + ("locators",)
MAX_CITES = 6
RELEVANCE_MAX_WORDS = 70
NOT_STATED = "not stated"
MENTIONED_ONLY = "mentioned only"

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEP_CELL = re.compile(r":?-{2,}:?")
_TS = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
_TIME_CITE = re.compile(rf"\[(?P<a>{_TS})(?:\s*(?:-|–|—|to)\s*{_TS})?\]")
_ANY_CITE = re.compile(r"\[[^\[\]\n]+\](?!\()")
_HEDGE_RE = re.compile(r"\s*\((?:ASR[^()]*|also heard as[^()]*)\)", re.I)
_HONORIFIC = re.compile(r"^(?:dr|prof|professor|mr|mrs|ms|mx|sir|dame)\.?\s+", re.I)
_BOLD_NAME = re.compile(r"^\s*[-*+]\s+\*\*(?P<name>[^*]+)\*\*\s*[:—–-]?\s*(?P<rest>.*)$")
PRIVATE_ROLE_RE = re.compile(
    r"\b(?:winners?|attendees?|audience|caller|raffle|giveaway|prize)\b", re.I)

# Role vocabulary, most specific first within each rank.
ROLE_RANKS = {
    3: ("host", "co-host", "cohost", "interviewer", "moderator", "guest", "interviewee",
        "speaker", "panelist", "panellist", "presenter", "narrator", "instructor",
        "author"),
    2: ("quoted", "cited author", "cited", "quoted author"),
    1: ("person discussed", "discussed", "subject"),
    0: ("mentioned only", "mentioned"),
}


def role_rank(role: str) -> int:
    low = (role or "").lower()
    for rank in (3, 2, 1, 0):
        for word in ROLE_RANKS[rank]:
            if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low):
                # "cited author" is rank 2, not "author" (rank 3)
                if rank == 3 and word == "author" and re.search(r"cited\s+author|quoted\s+author", low):
                    continue
                return rank
    return 1


@dataclass
class PersonRow:
    name: str
    role: str = ""
    background: str = ""
    relevance: str = ""
    cites: List[str] = field(default_factory=list)
    order: int = 0                      # first-seen position (documents)


@dataclass
class Person:
    """One merged person."""
    names: List[str]
    rows: List[PersonRow]
    shown: str = ""
    also: List[str] = field(default_factory=list)
    role: str = ""
    background: str = ""
    relevance: str = ""
    cites: List[str] = field(default_factory=list)


# --- parsing ---------------------------------------------------------------------------


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in _CELL_SPLIT.split(s)]


def _col(header: Sequence[str], *keys: str) -> Optional[int]:
    for i, h in enumerate(header):
        h = h.lower()
        if any(k in h for k in keys):
            return i
    return None


def parse_rows(body: str, start_order: int = 0) -> List[PersonRow]:
    """Rows of one People section body (a table; a bold-name bullet list is
    accepted as a fallback). `- None` and header/separator rows give nothing."""
    rows: List[PersonRow] = []
    header: Optional[List[str]] = None
    order = start_order
    for line in (body or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("|"):
            cells = _cells(s)
            if all(_SEP_CELL.fullmatch(c) for c in cells if c):
                continue
            low = [c.lower() for c in cells]
            if header is None and any(c in ("person", "name") for c in low):
                header = low
                continue
            hdr = header or list(COLUMNS)
            idx = {
                "name": _col(hdr, "person", "name"),
                "role": _col(hdr, "role"),
                "bg": _col(hdr, "affiliation", "background", "bio"),
                "rel": _col(hdr, "relevance", "why"),
                "cite": _col(hdr, "citation", "locator", "timestamp", "where"),
            }

            def get(k):
                i = idx[k]
                return cells[i].strip() if i is not None and i < len(cells) else ""
            name = get("name").strip("*` ")
            if not name or name.lower() in ("none", "-", "—"):
                continue
            cite_text = get("cite") or " ".join(cells)
            rows.append(PersonRow(name=name, role=get("role"), background=get("bg"),
                                  relevance=get("rel"),
                                  cites=_ANY_CITE.findall(cite_text), order=order))
            order += 1
            continue
        m = _BOLD_NAME.match(line)
        if m:
            rest = m.group("rest")
            rows.append(PersonRow(name=m.group("name").strip(), relevance=_ANY_CITE.sub("", rest).strip(" ;,"),
                                  cites=_ANY_CITE.findall(rest), order=order))
            order += 1
    return rows


# --- names -----------------------------------------------------------------------------


def bare_name(name: str) -> str:
    """The name without hedges, parentheticals, honorifics or markup."""
    s = _HEDGE_RE.sub("", name or "")
    s = re.sub(r"\([^()]*\)", " ", s)
    s = s.replace("*", "").replace("`", "").strip(" .,;:")
    s = _HONORIFIC.sub("", s)
    return " ".join(s.split())


def name_key(name: str) -> str:
    return " ".join(re.sub(r"[^\w' -]+", " ", bare_name(name).lower()).replace("’", "'").split())


def _tokens(name: str) -> List[str]:
    return [t for t in name_key(name).split() if t]


def _same(a: str, b: str, asr: bool, aliases: Dict[str, str]) -> bool:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ca, cb = ka.replace(" ", ""), kb.replace(" ", "")
    if ca == cb:                         # "Claire Vaux" / "ClaireVaux"
        return True
    if asr and len(ca) >= 6 and names_mod.sounds_alike(ca, cb) and (
            len(ka.split()) == 1 or len(kb.split()) == 1):
        return True                      # "Claire Vaux" / "Clairvaux" (ASR)
    if aliases.get(ka) and aliases.get(ka) == aliases.get(kb):
        return True
    ta, tb = ka.split(), kb.split()
    if ka.startswith("unnamed") or kb.startswith("unnamed"):
        return False
    if asr and len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0] and len(ta) == len(tb):
        return all(x == y or names_mod.sounds_alike(x, y) for x, y in zip(ta[1:], tb[1:]))
    return False


def _bare_match(single: str, full_names: Sequence[str]) -> bool:
    """A one-word name matches a full name by its first or last token."""
    t = name_key(single)
    for f in full_names:
        toks = name_key(f).split()
        if len(toks) >= 2 and t in (toks[0], toks[-1]):
            return True
    return False


# --- merging ---------------------------------------------------------------------------


def _richest(texts: Iterable[str], skip=(NOT_STATED, MENTIONED_ONLY)) -> str:
    good = [t.strip() for t in texts if t and t.strip() and
            t.strip().lower().rstrip(".") not in skip and t.strip().lower() not in ("-", "—", "none")]
    if not good:
        return ""
    return max(good, key=lambda t: (len(t.split()), -list(good).index(t)))


def _norm_text(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _ANY_CITE.sub(" ", t or "").lower()).split())


def _merge_relevance(texts: Sequence[str]) -> str:
    first = _richest(texts, skip=())
    if not first:
        return ""
    out, seen = [first], {_norm_text(first)}
    for t in texts:
        t = (t or "").strip()
        n = _norm_text(t)
        if not n or n in seen or any(n in s or s in n for s in seen):
            continue
        if sum(len(x.split()) for x in out) + len(t.split()) > RELEVANCE_MAX_WORDS:
            break
        out.append(t)
        seen.add(n)
    return " ".join(x if x.endswith((".", "!", "?")) else x + "." for x in out)


def _merge_role(roles: Sequence[str]) -> str:
    parts: List[Tuple[int, str]] = []
    for r in roles:
        for piece in re.split(r"\s*/\s*|\s*;\s*|\s*,\s*", r or ""):
            piece = piece.strip(" .").lower()
            if piece:
                parts.append((role_rank(piece), piece))
    if not parts:
        return ""
    top = max(rank for rank, _ in parts)
    out: List[str] = []
    for rank, piece in parts:
        if rank == top and piece not in out:
            out.append(piece)
    return " / ".join(out[:3])


def _cite_time(c: str) -> Optional[float]:
    m = _TIME_CITE.fullmatch(c.strip())
    if not m:
        return None
    total = 0.0
    for p in m.group("a").split(":"):
        total = total * 60 + float(p)
    return total


def _merge_cites(cites: Sequence[str], timed: bool) -> List[str]:
    uniq = list(dict.fromkeys(c.strip() for c in cites if c.strip()))
    if timed:
        uniq = [c for c in uniq if _cite_time(c) is not None]
        uniq.sort(key=lambda c: _cite_time(c))
    return uniq[:MAX_CITES]


def merge_people(rows: Sequence[PersonRow], *, asr: bool = False, timed: bool = True,
                 meta_words: Optional[set] = None,
                 count: Optional[Callable[[str], int]] = None,
                 aliases: Optional[Dict[str, str]] = None,
                 private: Sequence[str] = ()) -> List[Person]:
    """Deterministic merge of every passage's rows into one list of people."""
    meta_words = meta_words or set()
    aliases = {name_key(k): name_key(v) for k, v in (aliases or {}).items()}
    for v in list(aliases.values()):
        aliases.setdefault(v, v)
    private_keys = {name_key(p) for p in private if p}
    private_first = {k.split()[0] for k in private_keys if k}

    def is_private(r: PersonRow) -> bool:
        k = name_key(r.name)
        if k in private_keys:
            return True
        return bool(PRIVATE_ROLE_RE.search(r.role or ""))

    rows = [r for r in rows if not is_private(r)]
    full = [r for r in rows if len(_tokens(r.name)) >= 2 or name_key(r.name).startswith("unnamed")]
    single = [r for r in rows if r not in full]
    groups: List[Person] = []
    for r in full:
        g = next((g for g in groups if any(_same(r.name, n, asr, aliases) for n in g.names)), None)
        if g is None:
            groups.append(Person(names=[r.name], rows=[r]))
        else:
            g.rows.append(r)
            if r.name not in g.names:
                g.names.append(r.name)
    for r in single:
        g = next((g for g in groups if any(_same(r.name, n, asr, aliases) for n in g.names)), None)
        if g is None:
            matches = [g for g in groups if _bare_match(r.name, g.names)]
            g = matches[0] if len(matches) == 1 else None
        if g is None:
            groups.append(Person(names=[r.name], rows=[r]))
        else:
            g.rows.append(r)
            if r.name not in g.names:
                g.names.append(r.name)
    # a private first name that only ever appeared bare
    groups = [g for g in groups if not (len(g.names) == 1 and len(_tokens(g.names[0])) == 1
                                         and _tokens(g.names[0])[0] in private_first)]

    for g in groups:
        bares = list(dict.fromkeys(bare_name(n) for n in g.names if bare_name(n)))

        def score(n: str):
            return (names_mod.in_metadata(n, meta_words) if meta_words else False,
                    len(n.split()), count(n.split()[-1]) if (count and asr) else 0,
                    -bares.index(n))
        g.shown = max(bares, key=score) if bares else g.names[0]
        if asr:
            shown_toks = set(name_key(g.shown).split())
            g.also = [n for n in bares if n != g.shown and name_key(n) not in shown_toks]
        g.role = _merge_role([r.role for r in g.rows]) or "person discussed"
        bg = _richest([r.background for r in g.rows])
        if not bg:
            said_mentioned = any((r.background or "").strip().lower().rstrip(".") == MENTIONED_ONLY
                                 for r in g.rows)
            bg = (MENTIONED_ONLY if role_rank(g.role) == 0
                  or (said_mentioned and role_rank(g.role) < 3) else NOT_STATED)
        g.background = bg
        g.relevance = _merge_relevance([r.relevance for r in g.rows])
        g.cites = _merge_cites([c for r in g.rows for c in r.cites], timed)

    def first(g: Person):
        if timed:
            times = [_cite_time(c) for c in g.cites if _cite_time(c) is not None]
            return min(times) if times else float("inf")
        return min(r.order for r in g.rows)
    groups.sort(key=lambda g: (0 if role_rank(g.role) == 3 else 1, first(g)))
    return groups


# --- rendering -------------------------------------------------------------------------


def looks_like_name(name: str, is_common: Optional[Callable[[str], bool]] = None) -> bool:
    """Capitalised words that are not ordinary words ("The president" is a role)."""
    toks = bare_name(name).split()
    if not toks or any(not t[:1].isupper() for t in toks):
        return False
    return not (is_common and any(is_common(t) for t in toks))


def display_name(p: Person, *, asr: bool, meta_words: Optional[set] = None,
                 is_common: Optional[Callable[[str], bool]] = None) -> str:
    name = p.shown
    if name_key(name).startswith("unnamed") or not looks_like_name(name, is_common):
        return name
    if asr and not (meta_words and names_mod.in_metadata(name, meta_words)):
        hedge = names_mod.HEDGE
        if p.also:
            hedge += "; also heard as " + ", ".join(p.also)
        return f"{name} ({hedge})"
    if p.also:
        return f"{name} (also written {', '.join(p.also)})"
    return name


INTRO = ("_Who takes part in or is discussed in this source, as the source presents "
         "them. Background is only what the source states (anything else is labelled "
         "per model knowledge); citations point to where each person is introduced and "
         "makes key points._")


def render(people: Sequence[Person], *, level: int = 2, asr: bool = False,
           meta_words: Optional[set] = None, documents: bool = False,
           intro: bool = True, is_common: Optional[Callable[[str], bool]] = None) -> str:
    from video2mdnotes.core.notes_format import sanitize_cell as cell
    cols = DOC_COLUMNS if documents else COLUMNS
    out = ["#" * level + " " + HEADING, ""]
    if intro:
        out += [INTRO, ""]
    if not people:
        out.append("- None identified in the source.")
        return "\n".join(out) + "\n"
    out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for p in people:
        out.append("| " + " | ".join(cell(x) for x in (
            display_name(p, asr=asr, meta_words=meta_words, is_common=is_common),
            p.role, p.background,
            p.relevance or "-", " ".join(p.cites) or "-")) + " |")
    return "\n".join(out) + "\n"


def names_for_table(people: Sequence[Person]) -> List[str]:
    """The shown names, as lines the names table's name finder can read."""
    return [f"{p.shown} is a person in this source." for p in people
            if not name_key(p.shown).startswith("unnamed")]


# --- documents: split People sections out of notes ----------------------------------------

_HEAD = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _is_people_title(title: str) -> bool:
    bare = re.sub(r"\[[^\]]*\]", "", title or "").strip(" :*_`").strip()
    return bare.lower() in ("people", "people mentioned", "people involved",
                            "people mentioned or involved")


def _looks_like_people_table(body: List[str]) -> bool:
    for line in body:
        s = line.strip()
        if s.startswith("|"):
            low = [c.lower() for c in _cells(s)]
            return any(c in ("person", "name") for c in low) and any("role" in c for c in low)
        if s and not s.startswith(("_", ">")) and s.lower() not in ("- none", "none"):
            if _BOLD_NAME.match(line):
                return True
    return any(line.strip().lower() in ("- none", "- none identified in the source.")
               for line in body)


def split_people(text: str) -> Tuple[str, List[PersonRow], int]:
    """Remove every People section (a `People` heading whose body is a people
    table) from `text`. Returns (text without them, their rows, sections)."""
    lines = (text or "").split("\n")
    heads: List[Tuple[int, int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            m = _HEAD.match(line)
            if m:
                heads.append((i, len(m.group(1)), m.group(2)))
    drop: List[Tuple[int, int]] = []
    rows: List[PersonRow] = []
    for k, (i, level, title) in enumerate(heads):
        if not _is_people_title(title):
            continue
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        body = lines[i + 1:end]
        if not _looks_like_people_table(body):
            continue
        rows.extend(parse_rows("\n".join(body), start_order=len(rows)))
        drop.append((i, end))
    keep = [ln for i, ln in enumerate(lines) if not any(a <= i < b for a, b in drop)]
    return "\n".join(keep), rows, len(drop)


__all__ = ["COLUMNS", "DOC_COLUMNS", "HEADING", "Person", "PersonRow", "bare_name",
           "display_name", "merge_people", "name_key", "names_for_table", "parse_rows",
           "render", "role_rank", "split_people"]
