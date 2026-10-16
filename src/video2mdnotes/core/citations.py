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
    * **normalized** (``citation:normalized``) — before judging, repair
     commentary the model wrote around a citation (``[5:55:13 is not valid —
     see [4:55:13]]``, ``[8:46:29]–not cited; see [7:46:29]``) collapses to the
     citation(s) it points at that exist in the passage; one entry per change.
   * **ambiguous** (``citation:ambiguous``) — a strong term is missing at the
     citation but no unique better region exists: logged with
     ``changed: false`` for a human; the notes are NOT changed.

   * **fuzzy ok** (``citation:fuzzy_ok``, WP-F8) — before a unit is marked
     unverified, its terms are matched FUZZILY against the caption lines within
     ±``WINDOW_S`` of the citation, in the corrected transcript and (when
     given) the raw one: normalised edit distance ≤ ``FUZZY_MAX_DIST``
     ("get" ~ "git"), stem/prefix matching ("use" ~ "usage"), and compound
     tokens split against adjacent caption words (``.gitignore`` ~ "get
     ignore"). E2b marked two correct citations unverified because captions
     mis-heard the terms. When at least ``FUZZY_MIN_HITS`` content words match
     and the share reaches ``FUZZY_OK_SHARE`` (``FUZZY_OK_SHARE_STRONG`` when a
     strong term matched too), the unit is NOT marked; a mark from an earlier
     audit is removed (logged ``citation:unmarked``). Fuzzy matching is used
     for this decision only — never to retarget.

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
FUZZY_MAX_DIST = 0.34      # normalised edit distance: "get" ~ "git" (1/3)
FUZZY_MIN_HITS = 2
FUZZY_OK_SHARE = 0.4
FUZZY_OK_SHARE_STRONG = 0.3
# Words the notes use ABOUT a claim ("(speaker's statement)", "reported"):
# never evidence for or against a citation.
META_WORDS = frozenset("""
statement claim claims reported reports report mention mentions mentioned
speaker speakers note notes cited citation unverified suggestion
""".split())
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
    evidence: Optional[dict] = None

    def as_dict(self) -> dict:
        d = {
            "rule": self.rule, "original_ts": self.original_ts, "new_ts": self.new_ts,
            "claim": self.claim, "terms": self.terms, "section": self.section,
            "notes_line": self.line + 1, "coverage_cited": round(self.coverage_cited, 2),
            "changed": self.changed,
        }
        if self.coverage_new is not None:
            d["coverage_new"] = round(self.coverage_new, 2)
        if self.evidence is not None:
            d["fuzzy"] = self.evidence
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


# Repair commentary the model wrote around a citation it had second thoughts
# about (E2, WP-F7) — model output, not pipeline text:
#   "[5:55:13 is not valid — see [4:55:13]]"
#   "[8:46:29]–not cited; see [7:46:29] [7:47:50]"
# CITE_RE does not match the nested form, and the outer form leaves the
# commentary in the notes as plain text. Both collapse to the valid citation(s).
_WHY = (r"(?:is\s+)?(?:not\s+(?:a\s+)?valid|invalid|not\s+cited|wrong|incorrect|"
        r"not\s+in\s+(?:this\s+)?passage|does\s*n[o']t\s+exist|out\s+of\s+range)")
_SEE = r"(?:see(?:\s+instead)?|use|should\s+be|actually|→|->|=>)"
_TS_LIST = rf"\[?{_TS}\]?(?:\s*(?:,|;|and|&)?\s*\[?{_TS}\]?)*"
_COMMENT_INSIDE_RE = re.compile(
    rf"\[(?P<bad>{_TS})\s*[,;:—–-]?\s*{_WHY}\b[^\[\]\n]*?"
    rf"(?:{_SEE}\s*(?P<targets>{_TS_LIST})\s*)?\]",
    re.I)
_COMMENT_OUTSIDE_RE = re.compile(
    rf"\[(?P<bad>{_TS})\]\s*(?P<open>\()?\s*(?:[,;:—–-]\s*)?{_WHY}\b[^\[\]\n]*?"
    rf"{_SEE}\s+(?P<targets>\[{_TS}\](?:\s*(?:,|;|and|&)?\s*\[{_TS}\])*)"
    rf"(?(open)\s*\))",
    re.I)
_TS_ONLY_RE = re.compile(_TS)
# Any other commentary INSIDE a citation's brackets (E2c, WP-F9):
#   "[8:58:25 is not used — see below]", "[9:37:41 not used]"
# A timestamp followed by words in the same brackets is never a valid
# citation; it collapses to the timestamp, which the audit then judges like
# any other. Ranges ("[1:00:05–1:00:09]", "[1:00 to 1:05]") are not commentary.
_RANGE_TAIL = re.compile(rf"^\s*(?:-|–|—|to)\s*{_TS}\s*$", re.I)
_COMMENT_GENERIC_RE = re.compile(
    rf"\[(?P<bad>{_TS})\s*[,;:—–-]?\s+(?P<comment>[^\[\]\n]{{1,120}}?)\s*\]")
# A dangling cross-reference with no target after a citation:
#   "[8:58:25] — see below", "[1:02:03] (see above)"
_SEE_DANGLING_RE = re.compile(
    rf"(?P<cite>\[{_TS}\])\s*(?:[—–-]+|,|;)?\s*\(?\s*see\s+(?:below|above|later|earlier|next)\b\s*\)?",
    re.I)


def _short(ts: str) -> str:
    return ts.split(".", 1)[0]


def normalize_citation_commentary(text: str, valid=None) -> Tuple[str, List[dict]]:
    """Collapse model repair commentary around a citation to the citation(s) it
    points at. `valid(seconds) -> bool` (optional) says whether a timestamp is
    a caption line of the source; when given, only valid targets are kept (if
    none is valid but the original is, the original is kept). Returns (text,
    one ``citation:normalized`` log entry per change). Deterministic."""
    entries: List[dict] = []

    def choose(bad: str, targets: List[str]) -> List[str]:
        if valid is None:
            return targets or [bad]
        ok = [t for t in targets if valid(_parse_ts(t))]
        if ok:
            return ok
        if valid(_parse_ts(bad)) or not targets:
            return [bad]
        return targets

    def sub(m):
        bad = _short(m.group("bad"))
        raw_targets = m.group("targets") or ""
        targets = []
        for t in _TS_ONLY_RE.findall(raw_targets):
            t = _short(t)
            if t not in targets:
                targets.append(t)
        chosen = choose(bad, targets)
        new = " ".join(f"[{t}]" for t in chosen)
        entries.append({
            "rule": "citation:normalized", "changed": True,
            "original": m.group(0), "new": new,
            "original_ts": bad, "new_ts": ", ".join(chosen),
            "claim": "repair commentary around a citation collapsed to the valid citation",
        })
        return new

    out = _COMMENT_INSIDE_RE.sub(sub, text or "")
    out = _COMMENT_OUTSIDE_RE.sub(sub, out)

    def generic(m):
        comment = m.group("comment")
        if _RANGE_TAIL.match(comment) or not re.search(r"[A-Za-z]{2}", comment):
            return m.group(0)
        bad = _short(m.group("bad"))
        # Every timestamp in the brackets is a citation the model meant
        # ("[8:12:35 area, 7:50:35]" -> both); keep the valid ones.
        cands = [bad] + [t for t in (_short(x) for x in _TS_ONLY_RE.findall(comment))
                         if t != bad]
        chosen = [t for t in cands if valid is None or valid(_parse_ts(t))] or [bad]
        new = " ".join(f"[{t}]" for t in chosen)
        entries.append({
            "rule": "citation:normalized", "changed": True,
            "original": m.group(0), "new": new,
            "original_ts": bad, "new_ts": ", ".join(chosen),
            "claim": "commentary inside a citation's brackets removed",
        })
        return new

    out = _COMMENT_GENERIC_RE.sub(generic, out)

    def dangling(m):
        entries.append({
            "rule": "citation:normalized", "changed": True,
            "original": m.group(0), "new": m.group("cite"),
            "original_ts": m.group("cite").strip("[]"), "new_ts": m.group("cite").strip("[]"),
            "claim": "dangling 'see below/above' after a citation removed",
        })
        return m.group("cite")

    out = _SEE_DANGLING_RE.sub(dangling, out)
    return out, entries


def caption_validator(times: Sequence[float], tol: float = 1.0):
    """`valid(seconds)`: a caption line starts within [t - 0.5, t + tol) — a
    citation is the caption start truncated to the second."""
    ts = sorted(float(t) for t in times)

    def valid(t: float) -> bool:
        i = bisect.bisect_left(ts, t - 0.5)
        return i < len(ts) and ts[i] < t + tol
    return valid


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


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (small strings: tokens)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_ROOT_SUFFIXES = ("ation", "ment", "ness", "ing", "age", "ers", "er", "ed", "es", "s", "e", "y")


def _root(tok: str) -> str:
    """A crude stem for fuzzy matching: "usage" -> "us", "use" -> "us"."""
    for suf in _ROOT_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 2:
            return tok[: -len(suf)]
    return tok


def fuzzy_token_match(a: str, b: str) -> bool:
    """Same word as heard by a caption engine: equal; the same crude stem
    ("use" ~ "usage"); one a prefix of the other (shorter ≥ 3 letters and ≥
    half the longer); or normalised edit distance ≤ FUZZY_MAX_DIST ("get" ~
    "git"). Tokens under 3 characters only match exactly."""
    if a == b:
        return True
    if min(len(a), len(b)) < 3 or a.isdigit() or b.isdigit():
        return False
    if _root(a) == _root(b):
        return True
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short) and len(short) * 2 >= len(long_):
        return True
    return edit_distance(a, b) / max(len(a), len(b)) <= FUZZY_MAX_DIST


def _fuzzy_in(tok: str, window: Sequence[str]) -> bool:
    """`tok` fuzzily among the window's tokens, or (a compound, ≥ 6 letters)
    fuzzily equal to two or three adjacent window tokens written together
    (".gitignore" -> "gitignore" ~ "get" + "ignore")."""
    if any(fuzzy_token_match(tok, w) for w in window):
        return True
    if len(tok) < 6:
        return False
    for n in (2, 3):
        for k in range(len(window) - n + 1):
            glued = "".join(window[k:k + n])
            if abs(len(glued) - len(tok)) <= 2 and fuzzy_token_match(tok, glued):
                return True
    return False


class PassageIndex:
    """Caption lines of ONE passage: times and token sets, for window lookups.

    `alt_lines` (optional): the same passage in another version — the RAW
    transcript when `lines` are corrected — used only by fuzzy matching."""

    def __init__(self, lines: Sequence[Tuple[float, str]],
                 alt_lines: Optional[Sequence[Tuple[float, str]]] = None):
        self.alt = [(float(t), tokens(x)) for t, x in (alt_lines or [])]
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

    def window_token_runs(self, lo: float, hi: float) -> List[List[str]]:
        """Token sequences of the window [lo, hi]: the lines joined in order,
        once from the main lines and once from the alt (raw) lines."""
        main: List[str] = []
        for i in self.lines_in(lo, hi):
            main += tokens(self.texts[i])
        alt = [tok for t, toks in self.alt if lo <= t <= hi for tok in toks]
        return [run for run in (main, alt) if run]


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


def fuzzy_support(terms: List[Term], unit: Unit, index: PassageIndex) -> Optional[dict]:
    """Fuzzy evidence that the unit's claim is said at its citation, or None.

    Every strong term fuzzily present in a cited window → supported (the
    exact audit never marks a unit whose strong terms are all said there).
    Otherwise at least FUZZY_MIN_HITS content words must match, covering
    FUZZY_OK_SHARE of them (FUZZY_OK_SHARE_STRONG when a strong term matched).
    """
    strong = [t for t in terms if t.strong]
    content = [t for t in terms if not t.strong and t.toks[0] not in META_WORDS]
    best: Optional[dict] = None
    for a, b, _ in unit.cites:
        runs = index.window_token_runs(a - WINDOW_S, b + WINDOW_S)
        if not runs:
            continue

        def present(toks: Sequence[str]) -> bool:
            glued = "".join(toks)
            return any(_fuzzy_in(glued, run) or all(_fuzzy_in(t, run) for t in toks)
                       for run in runs)

        s_hit = [t.text for t in strong if present(t.toks)]
        c_hit = [t.text for t in content if present(t.toks)]
        share = len(c_hit) / len(content) if content else 0.0
        ok = bool(strong) and len(s_hit) == len(strong)
        ok = ok or (len(c_hit) >= FUZZY_MIN_HITS and share >= (
            FUZZY_OK_SHARE_STRONG if s_hit else FUZZY_OK_SHARE))
        if ok and (best is None or share > best["share"]):
            best = {"strong_matched": s_hit, "content_matched": c_hit,
                    "content_total": len(content), "share": share,
                    "window": f"{hms(max(0.0, a - WINDOW_S))}–{hms(b + WINDOW_S)}"}
    return best


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
            fz = fuzzy_support(terms, unit, index)
            if fz is not None:
                ch = Change("citation:fuzzy_ok", unit.line, unit.section, unit.text, orig,
                            None, show, cited.overall, round(fz["share"], 2), changed=False)
                ch.evidence = fz
                return ch
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


def _unmark(line: str) -> str:
    return line.replace(" " + UNVERIFIED_MARK, "").replace(UNVERIFIED_MARK, "")


def audit_notes(notes: str, caption_lines: Sequence[Tuple[float, str]],
                raw_lines: Optional[Sequence[Tuple[float, str]]] = None,
                ) -> Tuple[str, List[dict]]:
    """(audited notes, log entries). `caption_lines` = (start seconds, text) of
    the passage's own (corrected) transcript lines — not the overlap tail.
    `raw_lines` (optional): the same lines from the raw transcript, used only
    by the fuzzy check that runs before a unit is marked unverified."""
    if not caption_lines:
        return notes, []
    index = PassageIndex(caption_lines, raw_lines)
    entries: List[dict] = []
    notes, entries = normalize_citation_commentary(
        notes, caption_validator([t for t, _ in caption_lines]))
    notes, n_markers = normalize_citation_markers(notes)
    lines = notes.split("\n")
    for unit in iter_units(notes):
        ch = judge(unit, index)
        if ch is None:
            line = lines[unit.line]
            if UNVERIFIED_MARK in line and any(t.strong for t in extract_terms(unit.text)):
                # An earlier audit marked it; this one finds the citation fine.
                lines[unit.line] = _unmark(line)
                entries.append({"rule": "citation:unmarked", "changed": True,
                                "original_ts": " ".join(c[2] for c in unit.cites),
                                "new_ts": None, "claim": unit.text, "section": unit.section,
                                "notes_line": unit.line + 1, "fuzzy": None})
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
        elif ch.rule == "citation:fuzzy_ok" and UNVERIFIED_MARK in line:
            # An earlier (exact-only) audit marked it; the fuzzy match clears it.
            lines[unit.line] = _unmark(line)
            entry = ch.as_dict()
            entry.update(rule="citation:unmarked", changed=True)
            entries.append(entry)
            continue
        entries.append(ch.as_dict())
    if n_markers:
        entries.append({"rule": "citation:normalized", "changed": True, "count": n_markers,
                        "original_ts": None, "new_ts": None,
                        "claim": "leftover marker inside a citation removed, e.g. [H:MM:SS n/a]"})
    return "\n".join(lines), entries


__all__ = [
    "AUDIT_FILENAME", "UNVERIFIED_MARK", "WINDOW_S", "PassageIndex", "Term", "Unit",
    "audit_notes", "caption_validator", "extract_terms", "iter_units", "judge",
    "normalize_citation_commentary", "normalize_citation_markers",
    "tokens",
]
