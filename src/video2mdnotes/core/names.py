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


def hedge_title(title: str, speakers: Sequence[str], meta_words: set) -> str:
    """"Title (Harsha)" -> "Title — Harsha (ASR spelling)" when "Harsha" is an
    outline speaker that the metadata does not confirm."""
    m = _TRAILING_PAREN.search(title or "")
    if not m:
        return title
    name = " ".join(m.group(1).split())
    if name not in speakers or in_metadata(name, meta_words):
        return title
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


def _strip_for_names(md: str) -> str:
    md = re.sub(r"```.*?```", " ", md or "", flags=re.S)
    md = re.sub(r"`[^`\n]*`", " ", md)
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
               meta_words: set) -> List[NameRow]:
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

    for name, variants in speakers:
        if in_metadata(name, meta_words):
            continue
        var = [v for v in variants if v and v != name]
        rows[name.lower()] = NameRow(name, spellings(name, var),
                                     "outline speaker" + (f"; variants given: {', '.join(var)}"
                                                          if var else ""))
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


__all__ = ["HEDGE", "NAMES_HEADING", "NameRow", "TranscriptVocab", "hedge_title",
           "in_metadata", "is_machine", "is_name_respelling", "metadata_words",
           "names_rows", "note_names", "render_names"]
