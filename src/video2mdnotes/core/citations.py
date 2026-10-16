"""Deterministic citation audit of passage notes (WP-F1). No model calls.

Why: in E1 the model restated an item in a later section (Special Attention)
and approximated its timestamp — "Reading the file directly instead of jq
[8:09:43]" for an event at 7:59:3x. The P8 reviewer marked it UNSUPPORTED.
The passage transcript is right there, so the check is mechanical:

1. For every bullet and table row that cites ``[H:MM:SS]`` (or a range),
   extract its *salient terms*:

   * **strong** — inline code spans, identifier-like tokens (CamelCase,
     snake_case, ``--flags``, dotted names / paths), tool and command names
     (``jq``, ``ls``, ``Glob``, ``WebFetch`` ...), numbers with units;
   * **content words** — every other non-stopword, weighted by how RARE it is
     in this passage's caption lines (idf over the passage, so "file" in a
     settings-file chapter counts for little and "directly" for a lot).

   Terms that never occur in the passage transcript are dropped from scoring
   (the model paraphrased; absence proves nothing about the citation).

2. A term is *present* at a citation when it occurs in a caption line that
   starts within ``±WINDOW_S`` (60 s) of the cited time (a range widens the
   window). Two coverages are scored separately — the weighted share of
   STRONG terms present, and of CONTENT words present — because one strong
   term said nearby is not enough: in E1, `jq` is also said at 8:09:51, right
   next to the wrong citation, while "file" and "directly" are only said at
   the real event.

3. Verdicts (only single-citation units are retargeted):

   * **ok** — every strong term present and content coverage ≥ ``OK_CONTENT``
     (or the unit has no content words).
   * **retarget** (``citation:retarget``) — not ok, and exactly ONE region of
     the passage away from the citation (a cluster of ±60 s windows holding
     every strong term and no fewer content words than the cited window)
     reaches overall coverage ≥ ``RETARGET_MIN`` and beats the cited window
     by ≥ ``RETARGET_MARGIN``. The new timestamp is the caption line in that
     region with the most term weight within ±``LOCAL_S`` (latest on ties —
     where the statement is complete).

     Precision guards (WP-F4 — E1b moved a CORRECT "`/sandbox` didn't detect
     them until he started a new session" from 8:23:35, where "sandbox" is
     said, to 8:30:19 on the generic words "start" and "session"). Only
     *justifying* terms count here — strong terms plus RARE content words
     (said on at most ``RARE_DF`` caption lines of the passage); generic words
     never justify a move:

     - never retarget when the ORIGINAL location (caption lines within
       ±``ORIGIN_S`` of the cited time: the cited line and its neighbours)
       already holds any justifying term of the claim;
     - the new line must hold a justifying term within ±``LOCAL_S``, and the
       target window every strong term of the bullet — so the justifying
       evidence at the target is strictly stronger than at the origin (which
       has none), not merely the overall / content-word score.
   * **unverified** (``citation:unverified``) — none of the unit's strong
     terms occurs anywhere in the passage and content coverage at the
     citation is < ``UNVERIFIED_MAX_COVERAGE``. `` _(unverified citation)_``
     is appended.
   * **ambiguous** (``citation:ambiguous``) — a strong term is missing at the
     citation but no unique better region exists: logged with
     ``changed: false`` for a human; the notes are NOT changed.

Not audited: Transcription Uncertainty and Fact-Checked Notes (see
``SKIP_SECTIONS``), code fences, and lines that are neither bullets nor table
rows. Generic bullets (no strong term) are never retargeted or marked.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

WINDOW_S = 60.0
LOCAL_S = 10.0
OK_CONTENT = 0.4
RETARGET_MIN = 0.6
RETARGET_MARGIN = 0.25
UNVERIFIED_MAX_COVERAGE = 0.3
# The "original location" for the no-retarget guard: the cited caption line and
# its immediate neighbours (lines are ~2 s long). Deliberately NOT the ±60 s
# scoring window: E1's wrong jq citation [8:09:43] has an unrelated "JQ" 8 s
# later, while E1b's correct [8:23:35] has "sandbox" on the cited line itself.
ORIGIN_S = 5.0
RARE_DF = 2            # a content word said on <= 2 caption lines is "rare"
CLUSTER_GAP_S = 60.0
UNVERIFIED_MARK = "_(unverified citation)_"
AUDIT_FILENAME = "citation_audit.json"

# Sections whose citations are not about the claim in the cell text.
# Transcription Uncertainty cites raw-caption mishearings; Fact-Checked Notes
# mix the cited claim with model knowledge, whose terms are not in the
# transcript by design (E1: both produced only false alarms).
SKIP_SECTIONS = ("transcription uncertainty", "fact checked", "fact-checked")

_TS = r"\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?"
CITE_RE = re.compile(rf"\[(?P<a>{_TS})(?:\s*(?:-|–|—|to)\s*(?P<b>{_TS}))?\]")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_SEPARATOR_CELL = re.compile(r":?-{2,}:?")

# Tool / command names that are not ordinary English words: strong in any case.
TOOL_NAMES = frozenset("""
bash glob grep jq ls curl wget npx npm pip pipx uv git sed awk cat sudo docker
kubectl webfetch websearch todowrite notebookedit multiedit socat bubblewrap
seccomp brew winget apt yarn pnpm node nvm python python3 pytest tmux ssh scp
chmod chown mkdir rm cp mv echo
""".split())
# Tool names that are also English words: strong only when Capitalized and
# not the first word of the unit.
AMBIGUOUS_TOOLS = frozenset({"read", "write", "edit", "task", "agent", "skill", "find"})

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren as at be because
been before being below between both but by can cannot could did do does doing don
down during each else even ever every few for from further had has have having he
her here hers him his how i if in into is isn it its itself just me more most my no
nor not now of off on once only or other our ours out over own same she should so
some such than that the their theirs them then there these they this those through
to too under until up very was we were what when where which while who whom why
will with would you your yours yourself one two three first second next last
okay ok yeah yes uh um like right really actually basically going gonna wanna kind
sort thing things stuff something anything way want wants wanted need needs let
lets see say says said saying make makes made get gets got go goes went use uses
used using also still back just well much many lot lots here there now then say
speaker he him his shows show shown e g eg ie i e etc via per vs versus within
without instead rather another same different new old part again
""".split())

_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(gb|mb|kb|tb|ms|s|sec|secs|seconds?|min|mins|minutes?|hours?|h|"
    r"days?|weeks?|months?|years?|k|m|%|percent|tokens?|words?|lines?|files?|questions?|usd|dollars?)\b",
    re.I,
)


def _stem(tok: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(tok) > len(suf) + 3 and tok.endswith(suf):
            return tok[: -len(suf)]
    return tok


def tokens(text: str) -> List[str]:
    """Normalized, lightly stemmed tokens; camelCase is split."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])", " ", text or "")
    return [_stem(t) for t in re.sub(r"[^0-9a-z]+", " ", text.lower()).split()]


@dataclass
class Term:
    text: str
    toks: Tuple[str, ...]
    strong: bool
    weight: float = 0.0
    df: int = 0


@dataclass
class Unit:
    line: int              # 0-based line index in the notes
    kind: str              # "bullet" | "row"
    section: str
    text: str              # claim text used for terms (citations stripped)
    cites: List[Tuple[float, float, str]]  # (start, end, raw "[...]")


@dataclass
class Change:
    rule: str
    line: int
    section: str
    claim: str
    original_ts: str
    new_ts: Optional[str]
    terms: List[str]
    coverage_cited: float
    coverage_new: Optional[float] = None
    changed: bool = True

    def as_dict(self) -> dict:
        d = {
            "rule": self.rule, "original_ts": self.original_ts, "new_ts": self.new_ts,
            "claim": self.claim, "terms": self.terms, "section": self.section,
            "notes_line": self.line + 1, "coverage_cited": round(self.coverage_cited, 2),
            "changed": self.changed,
        }
        if self.coverage_new is not None:
            d["coverage_new"] = round(self.coverage_new, 2)
        return d


# Leftover markers inside a citation, e.g. ``[7:55:55 n/a]`` (E1b: written by the
# model; CITE_RE does not match them, so they reached the notes as plain text).
_MARKER_RE = re.compile(
    rf"\[\s*(?P<ts>{_TS}(?:\s*(?:-|–|—|to)\s*{_TS})?)\s*[,;:]?\s*"
    r"(?:n\s*/\s*a|n\.a\.?|na|\?+|tbd|unknown|unclear|approx\.?|est\.?|~)\s*\]",
    re.I)


def normalize_citation_markers(text: str) -> Tuple[str, int]:
    """``[7:55:55 n/a]`` -> ``[7:55:55]``. Returns (text, number changed)."""
    return _MARKER_RE.subn(lambda m: f"[{m.group('ts')}]", text or "")


def _parse_ts(ts: str) -> float:
    parts = ts.split(":")
    h, m, s = (["0"] * (3 - len(parts)) + parts)[-3:]
    return int(h) * 3600 + int(m) * 60 + float(s)


def hms(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# --- term extraction -------------------------------------------------------------


def extract_terms(text: str) -> List[Term]:
    """Strong terms and content words of one claim (citations already removed)."""
    out: Dict[Tuple[str, ...], Term] = {}

    def add(raw: str, toks: Sequence[str], strong: bool):
        toks = tuple(t for t in toks if t and (t not in STOPWORDS or strong))
        toks = tuple(t for t in toks if len(t) > 1 or t.isdigit())
        if not toks:
            return
        key = toks
        if key in out:
            out[key].strong = out[key].strong or strong
        else:
            out[key] = Term(raw, toks, strong)

    text = _LINK_RE.sub(r"\1", text)
    covered = []
    for m in _CODE_RE.finditer(text):
        span = m.group(1)
        toks = [t for t in tokens(span) if t not in STOPWORDS]
        if toks:
            add(span, toks, True)
        covered.append(m.span())
    plain = text
    for a, b in reversed(covered):
        plain = plain[:a] + " " + plain[b:]
    for m in _UNIT_RE.finditer(plain):
        add(m.group(0), tokens(m.group(0)), True)
    words = re.findall(r"[A-Za-z0-9_./~-]*[A-Za-z0-9]", plain)
    first_alpha = next((w for w in words if re.match(r"[A-Za-z]", w)), None)
    for w in words:
        bare = w.strip("./~-")
        if not bare:
            continue
        low = bare.lower()
        identifier = bool(
            re.search(r"[a-z][A-Z]", bare)            # camelCase / CamelCase
            or ("_" in bare.strip("_"))               # snake_case
            or w.startswith("--")                     # flags
            or re.search(r"[a-z]\.[a-z]", bare, re.I)  # dotted / file names
            or "/" in bare.strip("/")                 # paths
        )
        tool = low in TOOL_NAMES or (
            low in AMBIGUOUS_TOOLS and bare[0].isupper() and w is not first_alpha)
        if identifier or tool:
            add(bare, tokens(bare), True)
        else:
            for t in tokens(bare):
                if t not in STOPWORDS and len(t) >= 3 and not t.isdigit():
                    add(t, [t], False)
    # Words of a strong term also count as content words, so `spinnerTipsEnabled`
    # still scores where the speaker says "spinner tips".
    for term in list(out.values()):
        if term.strong and len(term.toks) > 1:
            for t in term.toks:
                if t not in STOPWORDS and len(t) >= 3 and not t.isdigit() and (t,) not in out:
                    out[(t,)] = Term(t, (t,), False)
    return list(out.values())


# --- the passage index -----------------------------------------------------------


class PassageIndex:
    """Caption lines of ONE passage: times and token sets, for window lookups."""

    def __init__(self, lines: Sequence[Tuple[float, str]]):
        self.times = [float(t) for t, _ in lines]
        self.texts = [x for _, x in lines]
        self.toks = [set(tokens(x)) for x in self.texts]
        self.n = len(lines)
        self._occ: Dict[str, List[float]] = {}
        for t, tk in zip(self.times, self.toks):
            for tok in tk:
                self._occ.setdefault(tok, []).append(t)
        # Glued text, so `WebFetch` also matches a line that says "webfetch".
        self._raw =[re.sub(r"[^0-9a-z]+", "", x.lower()) for x in self.texts]

    def occurrences(self, term: Term) -> List[float]:
        """Line times where the term occurs: single token → that token; multi-token
        → lines where its tokens all occur within LOCAL_S of each other, or
        the tokens written together (``webfetch``)."""
        if len(term.toks) == 1:
            tok = term.toks[0]
            occ = self._occ.get(tok)
            if occ is not None:
                return occ
            # stemmed form may differ from the glued raw text ("jQ" -> "jq")
            return [t for t, r in zip(self.times, self._raw) if len(tok) >= 4 and tok in r]
        per = [self._occ.get(tok, []) for tok in term.toks]
        glued = "".join(term.toks)
        hits = {t for t, r in zip(self.times, self._raw) if glued in r}
        if all(per):
            anchor = min(per, key=len)
            for t in anchor:
                if all(self._near(lst, t, LOCAL_S) for lst in per):
                    hits.add(t)
        return sorted(hits)

    @staticmethod
    def _near(lst: List[float], t: float, d: float) -> bool:
        i = bisect.bisect_left(lst, t - d)
        return i < len(lst) and lst[i] <= t + d

    def lines_in(self, lo: float, hi: float) -> range:
        return range(bisect.bisect_left(self.times, lo), bisect.bisect_right(self.times, hi))


def _weigh(terms: List[Term], index: PassageIndex) -> Dict[int, List[float]]:
    occ: Dict[int, List[float]] = {}
    n = max(index.n, 2)
    for k, term in enumerate(terms):
        times = index.occurrences(term)
        term.df = len(times)
        if not times:
            term.weight = 0.0
            continue
        idf = math.log(n / term.df) / math.log(n)          # 1.0 = said once
        term.weight = (2.0 + idf) if term.strong else max(0.0, idf - 0.2)
        occ[k] = times
    return occ


# --- notes parsing ---------------------------------------------------------------


def _row_claim(cells: List[str], header: Optional[List[str]]) -> str:
    if header:
        for i, name in enumerate(header):
            if re.sub(r"[^a-z]", "", name.lower()) == "claim" and i < len(cells):
                return cells[i]
    return " ".join(c for c in cells if not CITE_RE.fullmatch(c.strip()) and
                    not re.fullmatch(rf"`?{_TS}`?", c.strip()))


def iter_units(notes: str) -> List[Unit]:
    units: List[Unit] = []
    section = ""
    fenced = False
    header: Optional[List[str]] = None
    for i, line in enumerate(notes.split("\n")):
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = _HEADING_RE.match(line)
        if h:
            section = h.group(2).strip()
            header = None
            continue
        if any(section.lower().startswith(s) for s in SKIP_SECTIONS):
            continue
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(_SEPARATOR_CELL.fullmatch(c) for c in cells if c):
                continue
            if header is None and not CITE_RE.search(s):
                header = cells
                continue
            kind, text = "row", _row_claim(cells, header)
            # Unbracketed timestamp cells count as citations in tables.
            raw_cites = [(m.group("a"), m.group("b"), m.group(0)) for m in CITE_RE.finditer(s)]
            if not raw_cites:
                for c in cells:
                    if re.fullmatch(_TS, c.strip()):
                        raw_cites.append((c.strip(), None, c.strip()))
        elif _BULLET_RE.match(line):
            header = None
            kind, text = "bullet", _BULLET_RE.sub("", line)
            raw_cites = [(m.group("a"), m.group("b"), m.group(0)) for m in CITE_RE.finditer(line)]
        else:
            if s:
                header = None if not s.startswith("|") else header
            continue
        if not raw_cites:
            continue
        cites = []
        for a, b, raw in raw_cites:
            ta = _parse_ts(a)
            tb = _parse_ts(b) if b else ta
            cites.append((min(ta, tb), max(ta, tb), raw))
        claim = CITE_RE.sub(" ", text).replace(UNVERIFIED_MARK, " ")
        units.append(Unit(i, kind, section, " ".join(claim.split()), cites))
    return units


# --- audit -----------------------------------------------------------------------


def _cluster(times: List[float], gap: float = CLUSTER_GAP_S) -> List[Tuple[float, float]]:
    out: List[List[float]] = []
    for t in sorted(times):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b) for a, b in out]


@dataclass
class _Score:
    strong: float          # weighted share of strong terms present (1.0 = all)
    content: float         # weighted share of content words present
    overall: float
    present: set


class _Scorer:
    def __init__(self, terms: List[Term], occ: Dict[int, List[float]]):
        self.terms, self.occ = terms, occ
        self.strong_w = sum(t.weight for k, t in enumerate(terms) if t.strong and k in occ)
        self.content_w = sum(t.weight for k, t in enumerate(terms) if not t.strong and k in occ)

    def justifying(self, k: int) -> bool:
        """Strong term, or a content word RARE in this passage."""
        t = self.terms[k]
        return t.strong or (0 < t.df <= RARE_DF and len(t.text) >= 4)

    def justify_count(self, t: float, d: float) -> int:
        """Justifying terms said within ±d of t (generic words never count)."""
        return sum(1 for k, times in self.occ.items()
                   if self.justifying(k) and PassageIndex._near(times, t, d))

    def at(self, lo: float, hi: float) -> _Score:
        mid, half = (lo + hi) / 2, (hi - lo) / 2
        present = {k for k, times in self.occ.items() if PassageIndex._near(times, mid, half)}
        s = sum(self.terms[k].weight for k in present if self.terms[k].strong)
        c = sum(self.terms[k].weight for k in present if not self.terms[k].strong)
        strong = s / self.strong_w if self.strong_w else 0.0
        content = c / self.content_w if self.content_w else 0.0
        if self.strong_w and self.content_w:
            overall = 0.5 * strong + 0.5 * content
        else:
            overall = strong if self.strong_w else content
        return _Score(strong, content, overall, present)


def judge(unit: Unit, index: PassageIndex) -> Optional[Change]:
    """The audit verdict for one unit, or None when its citation is fine (or the
    unit is too generic to judge)."""
    terms = extract_terms(unit.text)
    if not any(t.strong for t in terms):
        return None                        # too generic: never touched
    occ = _weigh(terms, index)
    sc = _Scorer(terms, occ)
    show = [t.text for t in terms if t.strong] + [
        t.text for t in sorted((t for t in terms if not t.strong and t.weight > 0),
                               key=lambda t: -t.weight)][:6]
    orig = " ".join(c[2] for c in unit.cites)

    # Cited score: the best of the unit's citations (a range widens the window).
    cited = max((sc.at(a - WINDOW_S, b + WINDOW_S) for a, b, _ in unit.cites),
                key=lambda s: s.overall)

    if not sc.strong_w:
        # None of its strong terms is ever said in this passage.
        if cited.content < UNVERIFIED_MAX_COVERAGE:
            return Change("citation:unverified", unit.line, unit.section, unit.text, orig,
                          None, show, cited.overall)
        return None
    if cited.strong >= 1.0 and (not sc.content_w or cited.content >= OK_CONTENT):
        return None

    # Candidate regions: ±60 s windows around caption lines that hold every
    # strong term and no fewer content words than the cited window.
    scores = []
    for t in index.times:
        s = sc.at(t - WINDOW_S, t + WINDOW_S)
        if s.strong >= 1.0 and s.content >= cited.content:
            scores.append((t, s.overall))
    best = max((o for _, o in scores), default=0.0)
    # WP-F4: the cited line already says a strong / rare term of the claim ->
    # the citation is defensible; generic-word coverage elsewhere never wins.
    origin_holds = any(sc.justify_count(t, ORIGIN_S) > 0
                       for a, b, _ in unit.cites for t in {a, b})
    if (len(unit.cites) == 1 and not origin_holds and best >= RETARGET_MIN
            and best >= cited.overall + RETARGET_MARGIN):
        good = [t for t, o in scores if o >= best - 0.1]
        cited_lo = unit.cites[0][0] - WINDOW_S
        cited_hi = unit.cites[0][1] + WINDOW_S
        regions = _cluster(good)
        far = [r for r in regions if not (r[1] >= cited_lo and r[0] <= cited_hi)]
        if len(regions) == 1 and len(far) == 1:
            new_t = _pick_line(sc, index, far[0])
            if new_t is not None and sc.justify_count(new_t, LOCAL_S) > 0:
                new = sc.at(new_t - WINDOW_S, new_t + WINDOW_S)
                return Change("citation:retarget", unit.line, unit.section, unit.text, orig,
                              hms(new_t), show, cited.overall, new.overall)
    if cited.strong >= 1.0:
        return None                        # strong terms are there; wording differs
    return Change("citation:ambiguous", unit.line, unit.section, unit.text, orig, None,
                  show, cited.overall, best if scores else None, changed=False)


def _pick_line(sc: "_Scorer", index: PassageIndex, region) -> Optional[float]:
    """The caption line inside a region that best states the claim: most term
    weight within ±LOCAL_S; among ties the LATEST line, where the statement is complete (it must carry a term itself)."""
    lo, hi = region[0] - WINDOW_S, region[1] + WINDOW_S
    best_key, best_t = None, None
    for i in index.lines_in(lo, hi):
        t = index.times[i]
        own = sum(sc.terms[k].weight for k, times in sc.occ.items()
                  if PassageIndex._near(times, t, 0.01))
        if own <= 0:
            continue
        local = sc.at(t - LOCAL_S, t + LOCAL_S)
        key = (round(local.strong, 6), round(local.overall, 6), t)
        if best_key is None or key > best_key:
            best_key, best_t = key, t
    return best_t


def _mark_unverified(line: str, kind: str) -> str:
    if UNVERIFIED_MARK in line:
        return line
    if kind == "row":
        cells = line.rstrip().rstrip("|").split("|")
        # first non-empty cell after the leading pipe = the claim
        for k, c in enumerate(cells):
            if c.strip():
                cells[k] = c.rstrip() + " " + UNVERIFIED_MARK + " "
                break
        return "|".join(cells) + "|"
    return line.rstrip() + " " + UNVERIFIED_MARK


def audit_notes(notes: str, caption_lines: Sequence[Tuple[float, str]]) -> Tuple[str, List[dict]]:
    """(audited notes, log entries). `caption_lines` = (start seconds, text) of
    the passage's own (corrected) transcript lines — not the overlap tail."""
    if not caption_lines:
        return notes, []
    index = PassageIndex(caption_lines)
    entries: List[dict] = []
    notes, n_markers = normalize_citation_markers(notes)
    lines = notes.split("\n")
    for unit in iter_units(notes):
        ch = judge(unit, index)
        if ch is None:
            continue
        line = lines[unit.line]
        if ch.rule == "citation:retarget":
            raw = unit.cites[0][2]
            new = f"[{ch.new_ts}]" if raw.startswith("[") else ch.new_ts
            lines[unit.line] = line.replace(raw, new, 1)
        elif ch.rule == "citation:unverified":
            if UNVERIFIED_MARK in line:
                continue
            lines[unit.line] = _mark_unverified(line, unit.kind)
        entries.append(ch.as_dict())
    if n_markers:
        entries.append({"rule": "citation:normalized", "changed": True, "count": n_markers,
                        "original_ts": None, "new_ts": None,
                        "claim": "leftover marker inside a citation removed, e.g. [H:MM:SS n/a]"})
    return "\n".join(lines), entries


__all__ = [
    "AUDIT_FILENAME", "UNVERIFIED_MARK", "WINDOW_S", "PassageIndex", "Term", "Unit",
    "audit_notes", "extract_terms", "iter_units", "judge", "normalize_citation_markers",
    "tokens",
]
