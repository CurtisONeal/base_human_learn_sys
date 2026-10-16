"""Person names that rest only on machine transcription (WP-F8). No model calls.

Why: in E3b (parakeet ASR, no metadata naming anyone) the headings and the
Course Map asserted "(Zachary Hune)" and "(Archer)" — a one-minute sponsor
speaker, and a speaker heard as "Harsha" twice and "Archer" once — and the
notes gave surnames (Hune, Tabling, Matucci, Novick, McCaffrey, Nash) with no
hedge. A passage even "corrected" Curtis O'Neill -> O'Neal, a difference the
audio cannot carry. A reader looking these people up is misled.

When the transcript provenance is machine (``auto-captions`` or ``asr:*``):

* ``hedge_title`` — a section title that ends in an outline speaker name,
  "Talk title (Harsha)", is displayed as "Talk title — Harsha (ASR spelling)"
  in the section heading, the Course Map row and the Fact-Checked heading.
  Display only: passage keys and checkpoints keep the plain title.
* ``names_rows`` — every ASR-only name (outline speakers and their variants,
  multi-word names in the notes, and name "corrections" the passages
  proposed) goes into a ``### Names (spelling unverified)`` subsection of
  Transcription Uncertainty, with how often each spelling (and close
  variants) occurs in the transcript.
* ``is_name_respelling`` — an uncertainty row that only re-spells a name
  ("Curtis O'Neill" -> "Curtis O'Neal") is NOT a correction unless the
  proposed spelling appears in the source metadata (title, description,
  tags); it moves to the names subsection as an unapplied suggestion.

A name whose every word appears in the metadata is treated as verified and
never hedged.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from video2mdnotes.core import citations, quality

NAMES_HEADING = "Names (spelling unverified)"
HEDGE = "ASR spelling"

_TOK = r"(?:Mc|Mac|O')?[A-Z][a-z]+(?:-[A-Z][a-z]+)?"
NAME_RE = re.compile(rf"\b{_TOK}(?:\s+(?:(?:Van|Von|De|Le|La)\s+)?{_TOK}){{1,2}}\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
_TRAILING_PAREN = re.compile(r"\s*\(([^()]+)\)\s*$")


def is_machine(provenance) -> bool:
    """auto-captions or asr:* (an unknown provenance counts as machine)."""
    if provenance is None:
        return True
    return getattr(provenance, "kind", "") in ("auto-captions", "asr")


def _words(text: str) -> List[str]:
    out = []
    for w in _WORD_RE.findall(text or ""):
        w = w.strip("'")
        if w.endswith("'s"):
            w = w[:-2]
        if w:
            out.append(w)
    return out


def metadata_words(meta) -> set:
    return {w.lower() for w in _words(quality.metadata_text(meta))}


def in_metadata(name: str, meta_words: set) -> bool:
    """Every word of the name appears in the metadata (title/description/tags)."""
    ws = [w.lower() for w in _words(name)]
    return bool(ws) and all(w in meta_words for w in ws)


@dataclass
class TranscriptVocab:
    """Word counts of the transcript, case-sensitive and folded."""
    folded: Counter = field(default_factory=Counter)
    capitalized: Counter = field(default_factory=Counter)
    lowercase: set = field(default_factory=set)

    @classmethod
    def build(cls, segments: Iterable) -> "TranscriptVocab":
        v = cls()
        for s in segments:
            for w in _words(getattr(s, "text", "") or ""):
                v.folded[w.lower()] += 1
                if w[0].isupper():
                    v.capitalized[w] += 1
                elif w.islower():
                    v.lowercase.add(w)
        return v

    def common_word(self, word: str) -> bool:
        """Spoken in lowercase somewhere: an ordinary word, not a name."""
        return word.lower() in self.lowercase

    def count(self, word: str) -> int:
        return self.folded.get(word.lower(), 0)

    def variants(self, word: str, min_len: int = 5) -> List[Tuple[str, int]]:
        """Capitalized transcript spellings close to `word` (itself first)."""
        out: Dict[str, int] = {}
        low = word.lower()
        for w, n in self.capitalized.items():
            wl = w.lower()
            if wl == low:
                out[w] = out.get(w, 0) + n
                continue
            if len(low) < min_len or len(wl) < min_len - 1 or wl[0] != low[0]:
                continue
            if self.common_word(w):
                continue
            d = citations.edit_distance(wl, low)
            if d <= 2 and d / max(len(wl), len(low)) <= 0.34:
                out[w] = out.get(w, 0) + n
        return sorted(out.items(), key=lambda kv: (kv[0].lower() != low, -kv[1], kv[0]))


# --- headings ------------------------------------------------------------------------------


def hedge_title(title: str, speakers: Sequence[str], meta_words: set,
                preferred: Optional[Dict[str, str]] = None) -> str:
    """"Title (Harsha)" -> "Title — Harsha (ASR spelling)" when "Harsha" is an
    outline speaker that the metadata does not confirm. With `preferred`
    ({outline name: most-used transcript spelling}, WP-F9) the heading uses
    the better-supported spelling and keeps the outline's as "also heard as"."""
    m = _TRAILING_PAREN.search(title or "")
    if not m:
        return title
    name = " ".join(m.group(1).split())
    if name not in speakers or in_metadata(name, meta_words):
        return title
    shown = (preferred or {}).get(name, name)
    if shown != name:
        return f"{title[: m.start()].rstrip()} — {shown} ({HEDGE}; also heard as {name})"
    return f"{title[: m.start()].rstrip()} — {name} ({HEDGE})"


# --- uncertainty rows that only re-spell a name ------------------------------------------


def _name_tokens(text: str) -> Optional[List[str]]:
    ws = (text or "").split()
    if not 1 <= len(ws) <= 3:
        return None
    toks = [w.strip(".,;:") for w in ws]
    if all(re.fullmatch(_TOK, t) for t in toks):
        return toks
    return None


_SOUNDEX = {c: d for d, cs in {"1": "bfpv", "2": "cgjkqsxz", "3": "dt", "4": "l",
                                 "5": "mn", "6": "r"}.items() for c in cs}


def soundex(word: str) -> str:
    """American Soundex: "O'Neill" and "O'Neal" are both O540."""
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if not w:
        return ""
    out, last = w[0].upper(), _SOUNDEX.get(w[0], "")
    for c in w[1:]:
        d = _SOUNDEX.get(c, "")
        if d and d != last:
            out += d
        if c not in "hw":
            last = d
    return (out + "000")[:4]


def sounds_alike(a: str, b: str) -> bool:
    """Spellings a listener (or ASR) cannot tell apart: same Soundex code and
    a small spelling difference (normalised edit distance <= 0.34)."""
    x, y = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())
    if not x or not y:
        return False
    return soundex(x) == soundex(y) and citations.edit_distance(x, y) / max(len(x), len(y)) <= 0.34


def is_name_respelling(heard: str, probable: str, vocab: TranscriptVocab,
                       meta_words: set) -> bool:
    """A passage's "correction" that only re-spells a PERSON's surname
    ("Curtis O'Neill" -> "Curtis O'Neal"): first name identical, one other
    word changed to a spelling that sounds the same. True means: do not
    apply it — the audio cannot distinguish them and the metadata does not
    name the person. Single words (companies, products: "Gardner" ->
    "Gartner") are not judged here."""
    h, p = _name_tokens(heard), _name_tokens(probable)
    if not h or not p or len(h) != len(p) or len(h) < 2 or h == p or h[0] != p[0]:
        return False
    if vocab.common_word(h[0]):
        return False                     # "When Tim Nash": not a first name
    diff = [(a, b) for a, b in zip(h, p) if a != b]
    if len(diff) != 1:
        return False
    a, b = diff[0]
    if vocab.common_word(a) or vocab.common_word(b) or not sounds_alike(a, b):
        return False
    return b.lower() not in meta_words


# --- the names subsection ------------------------------------------------------------------


@dataclass
class NameRow:
    name: str
    spellings: List[Tuple[str, int]]
    source: str

    def render(self) -> str:
        spell = ", ".join(f"{w} ×{n}" for w, n in self.spellings) or "not in transcript as spelled"
        cell = quality_cell
        return f"| {cell(self.name)} | {cell(spell)} | {cell(self.source)} |"


def quality_cell(text: str) -> str:
    from video2mdnotes.core.notes_format import sanitize_cell
    return sanitize_cell(text)


_PERSON_HINT = re.compile(
    r"\b(?:he|she|his|her|him|they|person|MC|host|organi[sz]er|organi[sz]ing|speaker|"
    r"co-?founder|founder|engineer|manager|lead|head|director|handles|joined|team|"
    r"member|staff|CEO|CTO|introduced|thanked|role)\b")


def _strip_for_names(md: str) -> str:
    md = re.sub(r"```.*?```", " ", md or "", flags=re.S)
    # A backticked term that IS a person ("`Ted Shoxal` - the evening's MC",
    # E3c Key Concepts) stays; any other code span ("`Amazon Bedrock`: a
    # model-provider service") is removed.
    def keep(line: str) -> str:
        def sub(m):
            term = m.group(1).strip()
            if NAME_RE.fullmatch(term) and _PERSON_HINT.search(line[m.end():]):
                return f" {term} "
            return " "
        return re.sub(r"`([^`\n]*)`", sub, line)

    md = "\n".join(keep(line) for line in md.split("\n"))
    md = citations.CITE_RE.sub(" ", md)
    return md


def note_names(notes: Iterable[str], vocab: TranscriptVocab, meta_words: set) -> List[str]:
    """Multi-word person-like names in the notes whose words are never spoken
    in lowercase (so not ordinary words), with the surname in the transcript,
    and not confirmed by the metadata."""
    found: List[str] = []
    for md in notes:
        for m in NAME_RE.finditer(_strip_for_names(md)):
            name = " ".join(m.group(0).split())
            toks = name.split()
            # A leading capitalised ordinary word ("The host", "Hune says") is
            # dropped by the lowercase test; try without it first.
            while toks and vocab.common_word(toks[0]):
                toks = toks[1:]
            if len(toks) < 2 or any(vocab.common_word(t) for t in toks):
                continue
            name = " ".join(toks)
            # ASR-only: every word is the transcript's own (a name the notes
            # re-spelled, e.g. "their 4 Ultra" -> "Thor Ultra", is not).
            if in_metadata(name, meta_words) or not all(vocab.count(t) for t in toks):
                continue
            if name not in found:
                found.append(name)
    return found


def names_rows(*, speakers: Sequence[Tuple[str, Sequence[str]]], notes: Iterable[str],
               respellings: Sequence[Tuple[str, str]], vocab: TranscriptVocab,
               meta_words: set,
               found: Optional[Dict[str, Sequence[Tuple[str, str]]]] = None) -> List[NameRow]:
    """Rows of the names subsection: outline speakers (with variants), names
    from the notes, and unapplied re-spellings. Spellings are counted in the
    transcript; a name the metadata confirms is left out."""
    rows: Dict[str, NameRow] = {}

    def spellings(name: str, extra: Sequence[str] = ()) -> List[Tuple[str, int]]:
        toks = name.split()
        key = toks[-1] if toks else name
        out = vocab.variants(key) if key else []
        seen = {w.lower() for w, _ in out}
        for x in extra:
            for w in x.split()[-1:]:
                if w.lower() not in seen:
                    out.append((w, vocab.count(w)))
                    seen.add(w.lower())
        if len(toks) > 1 and vocab.count(toks[0]) and toks[0].lower() not in seen:
            out.append((toks[0], vocab.count(toks[0])))
        return out

    found = found or {}
    for name, variants in speakers:
        if in_metadata(name, meta_words):
            continue
        heard = [(w, how) for w, how in found.get(name, ())]
        var = [v for v in variants if v and v != name and v not in {w for w, _ in heard}]
        basis = "outline speaker" + (f"; variants given: {', '.join(var)}" if var else "")
        if heard:
            basis += "; transcript calls this speaker " + ", ".join(
                f"{w} ({how})" for w, how in heard)
        rows[name.lower()] = NameRow(name, spellings(name, var + [w for w, _ in heard]), basis)
    for heard, probable in respellings:
        # "Chris Halasey" -> "Chris Halasi" where "Chris Halasi" already has a
        # row: one row for the person, carrying the note.
        key = probable.lower() if probable.lower() in rows else heard.lower()
        rows.setdefault(key, NameRow(heard, spellings(heard, [probable]), ""))
        row = rows[key]
        other = heard if key == probable.lower() else probable
        row.source = "; ".join(x for x in (
            row.source, f'"{heard}" -> "{probable}" suggested by the notes but not applied — '
                        "ASR cannot distinguish the spellings") if x)
        if all(w.lower() != other.split()[-1].lower() for w, _ in row.spellings):
            row.spellings.append((other.split()[-1], vocab.count(other.split()[-1])))
    def known_variant(name: str) -> bool:
        """Same first name as a row, and a surname that row already counts
        among its spellings ("Alice Exampel" for "Alice Example")."""
        toks = name.split()
        for row in rows.values():
            rt = row.name.split()
            if len(rt) > 1 and rt[0] == toks[0] and any(
                    w.lower() == toks[-1].lower() for w, _ in row.spellings):
                return True
        return False

    for name in note_names(notes, vocab, meta_words):
        low = name.lower()
        if low in rows or any(low in k for k in rows) or known_variant(name):
            continue
        # A one-word outline speaker ("Louis") whose full name the notes give
        # ("Louis Matucci"): the row becomes the full name.
        short = next((k for k in rows if " " not in k and low.split()[0] == k), None)
        if short is not None:
            row = rows.pop(short)
            row.name = name
            row.spellings = spellings(name, [w for w, _ in row.spellings])
            rows[low] = row
            continue
        rows[low] = NameRow(name, spellings(name), "notes")
    return list(rows.values())


def render_names(rows: Sequence[NameRow], provenance_label: str) -> str:
    if not rows:
        return ""
    out = [f"### {NAMES_HEADING}", "",
           f"These names come only from machine transcription ({provenance_label}); the "
           "spellings are the transcript's and are not confirmed by the source "
           "metadata. Counts are occurrences in the transcript.", "",
           "| name in notes | transcript spellings (count) | basis |", "|---|---|---|"]
    out += [r.render() for r in rows]
    return "\n".join(out) + "\n"


# --- WP-F9: speaker variants from the transcript ------------------------------------------
#
# E3c: the outline named the ch. 4 speaker "Archer" (heard once, "Archer's
# work focuses on ...") while the transcript says "Harsha" twice — in the
# roll call "Morgan, Harsha, Jason, Chris, and Louie" [0:06:20] and in the
# introduction "So this is Harsha" [0:28:05]. Neither is a spelling variant
# (different first letter, different Soundex), so only the CONTEXT links
# them: the roll call lists the speakers in talk order, and an introduction
# names the speaker just as their section starts.

_INTRO_RE = re.compile(
    rf"\b(?:this\s+is|i'?m|i\s+am|my\s+name\s+is|please\s+welcome|welcome|"
    rf"introduce|introducing|give\s+it\s+up\s+for|hand\s+it\s+(?:over\s+)?to)\s+"
    rf"(?P<name>{_TOK})", re.I)
INTRO_BEFORE_S = 30.0
INTRO_AFTER_S = 90.0


def _first(name: str) -> str:
    toks = (name or "").split()
    return toks[0] if toks else ""


def _same_person(word: str, speaker: str, variants: Sequence[str]) -> bool:
    w = word.lower()
    forms = [speaker] + list(variants)
    return any(w == _first(f).lower() or sounds_alike(word, _first(f)) for f in forms if f)


def transcript_speaker_variants(
        speakers: Sequence[Tuple[str, Sequence[str], float]], segments: Sequence,
        vocab: TranscriptVocab) -> Dict[str, List[Tuple[str, str]]]:
    """{speaker: [(variant, how found)]} — spellings of an outline speaker's
    name that the transcript uses in a roll call or an introduction.

    `speakers` = (speaker, outline variants, section start seconds), in talk
    order. Deterministic; a candidate must be a capitalised word the
    transcript never uses in lowercase, and not another speaker's name."""
    order: List[Tuple[str, List[str], float]] = []
    for sp, var, start in speakers:
        if sp and all(_first(sp) != _first(o[0]) for o in order):
            order.append((sp, list(var or []), float(start)))
    out: Dict[str, List[Tuple[str, str]]] = {sp: [] for sp, _, _ in order}
    if not order:
        return out

    def taken(word: str, me: Optional[str] = None) -> bool:
        return any(_same_person(word, sp, var) for sp, var, _ in order if sp != me)

    def add(sp: str, word: str, how: str) -> None:
        if (word.lower() != _first(sp).lower() and not vocab.common_word(word)
                and len(word) >= 3 and all(word != w for w, _ in out[sp])):
            out[sp].append((word, how))

    # 1. Roll call: a list of >= 3 capitalised names, >= 2 of them speakers.
    #    Between two matched anchors, ONE unmatched name and ONE unmatched
    #    speaker pair up (talk order = list order).
    for s in segments:
        text = getattr(s, "text", "") or ""
        for m in re.finditer(rf"{_TOK}(?:\s*,\s*(?:and\s+)?{_TOK}){{2,}}", text):
            toks = re.findall(_TOK, m.group(0))
            if any(vocab.common_word(t) for t in toks):
                continue
            match: List[Optional[int]] = []
            for t in toks:
                j = next((k for k, (sp, var, _) in enumerate(order)
                          if _same_person(t, sp, var)), None)
                match.append(j)
            if sum(1 for j in match if j is not None) < 2:
                continue
            anchors = [(-1, -1)] + [(i, j) for i, j in enumerate(match) if j is not None] + [
                (len(toks), len(order))]
            for (i0, j0), (i1, j1) in zip(anchors, anchors[1:]):
                free_t = list(range(i0 + 1, i1))
                free_s = list(range(j0 + 1, j1))
                if len(free_t) == 1 and len(free_s) == 1:
                    sp = order[free_s[0]][0]
                    if not taken(toks[free_t[0]], sp):
                        add(sp, toks[free_t[0]], f"roll call [{_hms(getattr(s, 'start', 0.0))}]")
    # 2. Introduction just as the speaker's first section starts.
    for sp, var, start in order:
        for s in segments:
            t0 = float(getattr(s, "start", 0.0) or 0.0)
            if not (start - INTRO_BEFORE_S <= t0 <= start + INTRO_AFTER_S):
                continue
            for m in _INTRO_RE.finditer(getattr(s, "text", "") or ""):
                w = m.group("name")
                if not taken(w, sp):
                    add(sp, w, f"introduction [{_hms(t0)}]")
    return out


def _hms(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def preferred_spelling(speaker: str, variants: Sequence[str], vocab: TranscriptVocab) -> str:
    """The spelling of a speaker's first name the transcript uses most
    (outline spelling on ties)."""
    first = _first(speaker)
    best, best_n = first, vocab.count(first)
    for v in variants:
        v = _first(v)
        n = vocab.count(v)
        if n > best_n:
            best, best_n = v, n
    if best == first:
        return speaker
    rest = speaker.split()[1:]
    return " ".join([best] + rest)


# --- WP-F9: hedging names in the notes body -------------------------------------------------


def hedge_names_in_body(md: str, names: Sequence[str]) -> Tuple[str, int]:
    """First mention of each name in `md` gets "(ASR spelling)" unless it is
    already hedged. A backticked name is hedged after the closing backtick.
    Text inside citations and headings is left alone."""
    count = 0
    for name in names:
        pat = re.compile(rf"(`?){re.escape(name)}(`?)(?!\s*\((?:ASR|{re.escape(HEDGE)}))")
        lines = md.split("\n")
        done = False
        for i, line in enumerate(lines):
            if done or line.lstrip().startswith("#") or line.lstrip().startswith("|"):
                continue
            m = next((x for x in pat.finditer(line)
                      if line[:x.start()].count('"') % 2 == 0          # not inside a quote
                      and not re.match(r"\s+[A-Z]", line[x.end():])), None)  # not "X Y Corp"
            if not m:
                continue
            if f"{name} ({HEDGE}" in line or f"{name}` ({HEDGE}" in line:
                done = True
                continue
            end = m.end()
            lines[i] = f"{line[:end]} ({HEDGE}){line[end:]}"
            done = True
            count += 1
        md = "\n".join(lines)
    return md, count


# --- WP-F9: private individuals (giveaway winners, attendees) --------------------------------

PRIVATE_CONTEXT_RE = re.compile(
    r"\b(?:winners?|won|raffle|giveaway|prize|drawing|drawn|draw|picked\s+at\s+random|"
    r"attendees?|audience\s+member|from\s+the\s+audience)\b", re.I)
WITHHELD = "an attendee (name withheld)"


def private_names(chapter_notes: Iterable[str], candidates: Sequence[str],
                  protected: Sequence[str]) -> List[str]:
    """Candidate names (people in the notes) that are not speakers/organisers
    and appear ONLY in giveaway/winner/attendee context — private
    individuals the notes must not name."""
    texts = list(chapter_notes)
    prot = {_first(p).lower() for p in protected if p} | {p.lower() for p in protected if p}
    out = []
    for name in candidates:
        if name.lower() in prot or _first(name).lower() in prot:
            continue
        lines = [ln for t in texts for ln in t.split("\n") if name in ln]
        if lines and all(PRIVATE_CONTEXT_RE.search(ln) for ln in lines):
            out.append(name)
    return out


def redact_private(md: str, private: Sequence[str]) -> Tuple[str, List[dict]]:
    """Replace each private name (and an appositive after it — ", Senior
    Business Analyst at Clear result") with WITHHELD; "a winner from <Org>"
    -> "a winner". Returns (md, log entries)."""
    log: List[dict] = []
    for name in private:
        pat = re.compile(
            rf"`?{re.escape(name)}`?(?:,\s*[^,\[\]\n.;|]{{1,80}}?(?=\s*\[|[.;|]|$))?", re.M)
        md, n = pat.subn(WITHHELD, md)
        if n:
            log.append({"rule": "names:private-redacted", "name": name, "count": n,
                        "why": "named only in giveaway/winner/attendee context"})
    md2, n = re.subn(
        r"\b((?:an?|the)\s+(?:winner|attendee)s?)\s+from\s+[A-Z][\w&.'-]*"
        r"(?:\s+(?![\[(])[A-Za-z][\w&.'-]*){0,2}(?=\s*[\[.,;)]|$)",
        r"\1", md, flags=re.M)
    if n:
        log.append({"rule": "names:private-redacted", "name": "<organisation of a winner>",
                    "count": n, "why": "'a winner from <org>' identifies a private person"})
    return md2, log


__all__ = ["HEDGE", "NAMES_HEADING", "NameRow", "TranscriptVocab", "hedge_names_in_body",
           "hedge_title", "in_metadata", "is_machine", "is_name_respelling",
           "metadata_words", "names_rows", "note_names", "preferred_spelling",
           "private_names", "redact_private", "render_names",
           "transcript_speaker_variants"]
