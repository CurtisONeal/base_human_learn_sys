"""annotate-people: a durable person index and sources index for a collection.

    annotate-people <run_dir | digest.md | collection name> [--collection NAME]
                    [--max-calls N] [--refresh] [--no-web] [--max-web-calls N]

An agentic follow-up AFTER the summaries. It reads finished episode notes
(packages or run dirs) and digests — never transcripts — and writes, in the
collection folder AND the vault subject folder:

* ``people/_index.md`` and one ``people/<slug>.md`` per unique person:
  frontmatter (name, aliases incl. ASR variants, role_in_series, first_seen,
  last_seen, appearances); a minimal **description per model knowledge
  (cutoff uncertain)**, or "No reliable model knowledge; known only from
  sources"; **as stated in sources** (backgrounds from the People tables,
  linked); **appearances** (episode / digest links, dates, relevance).
* ``people/_resolutions.md``: every vague role reference resolved ("the
  Secretary of State" → name, as of the episode date, confidence, or
  "verify — after model knowledge cutoff").
* ``sources/_index.md``: the Sources Referenced tables of every episode,
  merged (times cited, first/last seen, who cites, episode links) with a
  "listen / read next" queue — candidate inputs for interests and listening.

**What the model does.** Only two things, in the SAME batched calls
(``BATCH`` people + ``BATCH`` role references per call): a 2–3 sentence
description of each NEW person (or "not known"; never invented; private
individuals flagged), and who held each vague role on the episode's date.
Everything else — who the unique people are (``core.people.merge_people``:
normalised names, ASR sounds-alike variants), appearances, stated
backgrounds, the sources index, every link — is deterministic.

**Idempotent.** Descriptions and resolutions are kept in
``<primary root>/people/.annotate_state.json``; a person or role already
there is never sent again unless ``--refresh``. A rerun with nothing new
makes 0 calls and leaves every file byte-identical.

**Web lookup (WP-P3).** Only for people the model marked "no reliable model
knowledge" and for items flagged "verify — after model knowledge cutoff":
one subscription ``claude -p`` call per ``WEB_BATCH`` targets with only
WebSearch/WebFetch, at most 3 authoritative sources per target (3 more only
when all 3 fail). Results are cached on the state record (``"web"``), shown
as "per web lookup <date> (sources: …)"; "verify" clears only when the
lookup confirms; a contradiction with model knowledge is shown, not
overwritten. Never for private / unnamed / withheld rows or for confident
pre-cutoff model knowledge. ``--no-web`` disables; ``--max-web-calls``
caps (default ``MAX_WEB_CALLS``).

**WP-P4.** Unknown quoted people get a quote-first "voice" lookup (queries
from the attributed quote/claim + 7-day window + topic; tiers confirmed /
probable / unknown decided here from the evidence); roles are asked only
"Who held <role> at <org> on <date>?" and confirmed only on dated evidence
(≤90 days before the episode, or a live official page), with a change
check; ``sources/_index.md`` gains a ranked "AI voices" table
(``signal_score``). See design/ANNOTATE_PEOPLE.md.

**Privacy.** Rows whose role is a winner / attendee / audience member, names
withheld by packaging, unnamed speakers, and anyone the model flags as a
private individual get no person note (only a count in the index).

**Notes annotation.** A resolved role's first mention in each episode's
notes body (and a People row whose name is that role) gets a minimal
annotation: ``the Secretary of State (→ [Marco Rubio](../people/marco_rubio.md),
as of 2026-10-14, per model knowledge)``. Digests strip these annotations
before packing an episode (core/digest.py), so their cache keys do not move.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core import cited_sources, digest as dg, people as people_mod
from video2mdnotes.core.package import link_path, slugify

PROMPT = BASE_DIR / "prompts" / "annotate" / "people.md"
WEB_PROMPT = BASE_DIR / "prompts" / "annotate" / "web_lookup.md"
GENERATOR = "video2mdnotes.annotate-people/1"
STATE_FILE = ".annotate_state.json"
BATCH = 20
MAX_ROLES = 40
MODEL_KNOWLEDGE = "per model knowledge (cutoff uncertain)"
NO_KNOWLEDGE = "No reliable model knowledge; known only from sources."
PENDING = "Description pending (call limit reached; rerun annotate-people)."
VERIFY = "verify — after model knowledge cutoff"
WEB_BATCH = 10                 # targets per web-lookup call
MAX_WEB_CALLS = 5              # default --max-web-calls
WEB_MAX_SOURCES = 3            # sources kept per target ...
WEB_MAX_FALLBACK = 6           # ... unless the 3 most authoritative all failed
VOICE_PROMPT = BASE_DIR / "prompts" / "annotate" / "voice_lookup.md"
ROLE_PROMPT = BASE_DIR / "prompts" / "annotate" / "role_on_date.md"
QUOTE_WINDOW_DAYS = 7          # a quoted post most likely appeared in the 7 days before
ROLE_EVIDENCE_DAYS = 90        # a role source must be dated within 90 days before the episode
VOICE_TIERS = ("confirmed", "probable", "unknown")
AFF_TYPES = ("lab", "founder", "investor", "journalist", "researcher", "other")
# signal score = times quoted × episode breadth × recency × affiliation weight
AFF_WEIGHT = {"lab": 1.5, "researcher": 1.4, "founder": 1.3, "investor": 1.2,
              "journalist": 1.1, "other": 1.0}
RECENCY_HALF_LIFE = 30         # days

CompleteFn = Callable[[str, str], Tuple[str, str]]


class AnnotateError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

@dataclass
class DigestDoc:
    path: Path
    span: str
    start: Optional[dt.date]
    people_rows: List[people_mod.PersonRow] = field(default_factory=list)


@dataclass
class Scope:
    episodes: List[dg.Episode]
    digests: List[DigestDoc]
    roots: List[Path]                       # output roots; [0] holds the state
    show: str = ""


def load_digest(path: Path) -> DigestDoc:
    text = Path(path).read_text(encoding="utf-8")
    fm, body = dg._frontmatter(text)
    start = dg._to_date(fm.get("span_start"))
    doc = DigestDoc(Path(path), fm.get("span") or Path(path).stem, start)
    for lv, h, t, _ in dg._sections(body):
        if lv == 2 and dg._bare(h).lower().startswith("people of the"):
            doc.people_rows.extend(people_mod.parse_rows(t))
    return doc


def _digest_episode_paths(path: Path) -> List[Path]:
    out = []
    text = Path(path).read_text(encoding="utf-8")
    sec = text.split("\n## Episodes", 1)[-1].split("\n## ", 1)[0]
    for m in re.finditer(r"\[notes\]\(([^)\s]+)\)", sec):
        p = (Path(path).parent / m.group(1)).resolve()
        if p.exists():
            out.append(p)
    return out


def _episodes_from(paths: Iterable[Path]) -> List[dg.Episode]:
    eps: Dict[str, dg.Episode] = {}
    for p in paths:
        ep = dg.load_episode(p)
        if ep is None:
            continue
        if ep.key not in eps or p.name == "notes.md":
            eps[ep.key] = ep
    return sorted(eps.values(), key=lambda e: (e.published, e.title))


def _run_dir_notes(d: Path) -> List[Path]:
    if (d / "notes.md").exists():
        return [d / "notes.md"]
    return [c for c in sorted((d / "summaries").glob("*.summary.md"))
            if not c.name.startswith("no_summary_")]


def resolve_scope(target: str, *, collection: Optional[str] = None,
                  collection_root: Optional[Path] = None) -> Scope:
    """Episodes, digests and output roots for a target (see module doc)."""
    from video2mdnotes.config import settings
    from video2mdnotes.core import collection as cm

    root = Path(collection_root) if collection_root else settings.output_dir / "collections"
    eps_paths: List[Path] = []
    digests: List[Path] = []
    roots: List[Path] = []
    col_name = collection
    p = Path(target).expanduser()
    if p.is_dir():
        eps_paths += _run_dir_notes(p)
        if not eps_paths:
            raise AnnotateError(f"{p}: no notes.md or summaries/*.summary.md")
        if not col_name:
            roots = [p]
    elif p.is_file() and p.suffix == ".md":
        digests.append(p)
        eps_paths += _digest_episode_paths(p)
        if not col_name:
            roots = [p.parent.parent if p.parent.name == "digests" else p.parent]
    else:
        col_name = col_name or target
    if col_name:
        try:
            col = cm.Collection.load(root, col_name)
        except cm.CollectionError as exc:
            raise AnnotateError(str(exc)) from exc
        vault = Path(col.data["vault_subject_dir"]) if col.data.get("vault_subject_dir") else None
        roots = [col.dir] + ([vault] if vault else [])
        src = vault if vault and vault.is_dir() else col.runs_dir
        eps_paths += dg.find_notes(src)
        if vault and (vault / "digests").is_dir():
            digests += sorted((vault / "digests").glob("*.md"))
    episodes = _episodes_from(eps_paths)
    if not episodes:
        raise AnnotateError(f"{target}: no episode notes found")
    docs = [load_digest(d) for d in dict.fromkeys(Path(x).resolve() for x in digests)]
    for i, e in enumerate(episodes, 1):
        e.label = f"E{i}"
    show = dg._show_name(episodes, roots[0])
    return Scope(episodes, docs, roots, show)


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #

_WITHHELD = re.compile(r"withheld|\[attendee", re.I)


@dataclass
class Appearance:
    episode: Optional[dg.Episode]
    digest: Optional[DigestDoc]
    row: people_mod.PersonRow
    via_role: str = ""                     # "the Secretary of State" (role resolution)


@dataclass
class PersonEntry:
    slug: str
    name: str
    aliases: List[str]
    role: str
    apps: List[Appearance]
    keys: set

    def episode_dates(self) -> List[dt.date]:
        return sorted({a.episode.published for a in self.apps if a.episode})


def _skip_row(r: people_mod.PersonRow) -> bool:
    k = people_mod.name_key(r.name)
    return (not k or k.startswith("unnamed") or bool(_WITHHELD.search(r.name))
            or bool(people_mod.PRIVATE_ROLE_RE.search(r.role or "")))


def _is_role_name(name: str) -> bool:
    """A People row whose 'name' is a role ("The president", "Meta's CEO")."""
    bare = people_mod.bare_name(name)
    if ROLE_A.fullmatch(bare) or ROLE_B.fullmatch(bare):
        return True
    toks = bare.split()
    return bool(toks) and (toks[0].lower() in ("the", "a", "an") or not toks[0][:1].isupper())


def collect_people(scope: Scope) -> Tuple[List[PersonEntry], int]:
    """Unique people across episodes + digests (merge_people with ASR
    variants). Returns (entries, rows skipped as private/unnamed)."""
    rows: List[people_mod.PersonRow] = []
    owner: Dict[int, Tuple[Optional[dg.Episode], Optional[DigestDoc]]] = {}
    skipped = 0
    order = 0
    for e in scope.episodes:
        for r in e.people_rows:
            if _skip_row(r):
                skipped += 1
                continue
            if _is_role_name(r.name):
                continue
            r2 = people_mod.PersonRow(r.name, r.role, r.background, r.relevance, r.cites, order)
            owner[order] = (e, None)
            rows.append(r2)
            order += 1
    for d in scope.digests:
        for r in d.people_rows:
            if _skip_row(r) or _is_role_name(r.name):
                continue
            r2 = people_mod.PersonRow(r.name, r.role, r.background, r.relevance, [], order)
            owner[order] = (None, d)
            rows.append(r2)
            order += 1
    merged = people_mod.merge_people(rows, asr=True, timed=False)
    out: List[PersonEntry] = []
    used: Dict[str, int] = {}
    for p in merged:
        name = people_mod.bare_name(p.shown)
        slug = slugify(name, 60) or "person"
        if slug in used:
            used[slug] += 1
            slug = f"{slug}_{used[slug]}"
        else:
            used[slug] = 1
        aliases = list(dict.fromkeys(
            x for x in (people_mod.bare_name(n) for n in p.names) if x and x != name))
        apps = [Appearance(*owner[r.order], row=r) for r in p.rows]
        ep_role_rows = [a.row.role for a in apps if a.episode]
        role = people_mod._merge_role(ep_role_rows) or p.role
        keys = {people_mod.name_key(n) for n in p.names} | {people_mod.name_key(name)}
        out.append(PersonEntry(slug, name, aliases, role, apps, keys))
    out.sort(key=lambda e: (-len({a.episode.key for a in e.apps if a.episode}), e.name.lower()))
    return out, skipped


# --------------------------------------------------------------------------- #
# Vague role references
# --------------------------------------------------------------------------- #

_TITLES = (
    r"Secretary of State|Secretary of (?:Defense|War|Commerce|the Treasury|Energy|Labor|"
    r"Education|Homeland Security|Health and Human Services)|Treasury Secretary|Commerce Secretary|"
    r"Defense Secretary|Attorney General|Fed chair(?:man|woman)?|Federal Reserve chair(?:man|woman)?|"
    r"chair(?:man|woman)? of the Federal Reserve|FBI [Dd]irector|CIA [Dd]irector|"
    r"Speaker of the House|House Speaker|Senate Majority Leader|Vice President|President|"
    r"Prime Minister|Chancellor|SEC [Cc]hair(?:man|woman)?|FTC [Cc]hair(?:man|woman)?|"
    r"(?:White House )?AI czar|Surgeon General|Pope")
_NATION = r"(?:U\.S\.|US|American|British|UK|German|French|Japanese|Chinese|Indian|Canadian|Korean|Taiwanese) "
# not followed by a name, and not a possessive ("the President's Council" is
# an organisation; annotating before "'s" would break the phrase)
_NOT_NAMED = r"(?![\w-])(?!['’]s\b)(?!,?\s+(?:[A-Z][a-z'’-]+\s+){1,2}[A-Z][a-z'’-]+)"
ROLE_A = re.compile(rf"\b(?P<p>[Tt]he (?:{_NATION})?(?:{_TITLES})(?: of (?:the )?[A-Z][\w&.-]+(?: [A-Z][\w&.-]+)?)?){_NOT_NAMED}")
ROLE_B = re.compile(
    r"\b(?P<p>(?P<org>[A-Z][\w&.-]+(?: (?:of |and |& )?[A-Z][\w&.-]+){0,3})['’]s (?:CEO|CTO|CFO|COO|chief executive|"
    r"chief scientist|founder|co-founder|cofounder|president|chair(?:man|woman)?|head of [a-z]+))"
    + _NOT_NAMED)
_NAME = r"[A-Z][\w'’.-]+(?: [A-Z][\w'’.-]+)+"
# "Brian Moynihan is Bank of America's CEO" / "the CEO, Jane Roe" — already named.
_NAMED_BEFORE = re.compile(rf"{_NAME},? (?:is|was|as|became|remains) (?:now |still )?$")
_NAMED_AFTER = re.compile(rf"^,? (?:is|was|remains) (?:now |still )?{_NAME}|^ \({_NAME}\)")
_ORG_STOP = {"The", "It", "This", "That", "He", "She", "They", "Its", "Their", "His", "Her", "One"}
ANNOT = dg._ANNOTATION          # "(→ …, per model knowledge|per web lookup …)"


@dataclass
class RoleRef:
    id: str
    phrase: str
    date: dt.date
    context: str
    episodes: List[dg.Episode] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{role_key(self.phrase)}|{self.date.isoformat()}"


def role_key(phrase: str) -> str:
    s = re.sub(r"^the\s+", "", phrase.strip().lower())
    return " ".join(re.sub(r"[^\w ]+", " ", s.replace("’", "'")).split())


def _body_lines(text: str) -> Iterable[Tuple[int, str]]:
    fenced = False
    lines = text.split("\n")
    start = 0
    if lines and lines[0].startswith("---"):
        for i in range(1, len(lines)):
            if lines[i].startswith("---"):
                start = i + 1
                break
    for i in range(start, len(lines)):
        line = lines[i]
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if not fenced and not line.lstrip().startswith("#"):
            yield i, line


def _role_matches(line: str) -> List[re.Match]:
    out = []
    if line.lstrip().startswith("|"):
        cells = people_mod._cells(line)
        # a People row whose name cell is a role: only that cell
        name = cells[0] if cells else ""
        for rx in (ROLE_A, ROLE_B):
            m = rx.fullmatch(name.strip()) or (rx.match(name.strip()) if name else None)
            if m and m.group("p") == name.strip():
                out.append(m)
        return out
    for rx in (ROLE_A, ROLE_B):
        for m in rx.finditer(line):
            if rx is ROLE_B and m.group("org").split()[0] in _ORG_STOP:
                continue
            if _NAMED_BEFORE.search(line[:m.start()]) or _NAMED_AFTER.search(line[m.end():]):
                continue
            out.append(m)
    return out


def find_role_refs(episodes: Sequence[dg.Episode], limit: int = MAX_ROLES) -> List[RoleRef]:
    refs: Dict[str, RoleRef] = {}
    for e in episodes:
        text = e.notes_path.read_text(encoding="utf-8")
        for _, line in _body_lines(text):
            clean = ANNOT.sub("", dg.unlink_citations(line))
            for m in _role_matches(clean):
                phrase = m.group("p")
                r = RoleRef("", phrase, e.published, " ".join(ANNOT.sub("", clean).split())[:300])
                if r.key in refs:
                    if e not in refs[r.key].episodes:
                        refs[r.key].episodes.append(e)
                    continue
                r.episodes.append(e)
                refs[r.key] = r
    out = sorted(refs.values(), key=lambda r: (r.date, role_key(r.phrase)))[:limit]
    for i, r in enumerate(out, 1):
        r.id = f"R{i}"
    return out


# --------------------------------------------------------------------------- #
# State + model calls
# --------------------------------------------------------------------------- #

def _load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("people", {})
                data.setdefault("roles", {})
                return data
        except ValueError:
            pass
    return {"version": 1, "people": {}, "roles": {}, "knowledge_cutoff": None}


def _write_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _state_person(state: Dict[str, Any], p: PersonEntry) -> Optional[Dict[str, Any]]:
    rec = state["people"].get(p.slug)
    if rec and set(rec.get("keys", [])) & p.keys:
        return rec
    for rec in state["people"].values():
        if set(rec.get("keys", [])) & p.keys:
            return rec
    return None


def _person_payload(p: PersonEntry, show: str, pid: str) -> Dict[str, Any]:
    stated = list(dict.fromkeys(
        a.row.background for a in p.apps if a.row.background
        and a.row.background.lower() not in (people_mod.NOT_STATED, people_mod.MENTIONED_ONLY)))
    eps = [a.episode for a in p.apps if a.episode]
    return {"id": pid, "names": [p.name, *p.aliases], "role_in_series": p.role,
            "stated": stated[:3], "show": show,
            "first_episode": (f"{eps[0].published.isoformat()} \"{eps[0].title}\"" if eps else "")}


def _role_payload(r: RoleRef) -> Dict[str, Any]:
    return {"id": r.id, "reference": r.phrase, "episode_date": r.date.isoformat(),
            "sentence": r.context}


@dataclass
class CallLog:
    calls: int = 0
    models: List[str] = field(default_factory=list)
    stopped: str = ""


def _ym(s: Optional[str]) -> Optional[Tuple[int, int]]:
    m = re.match(r"^(\d{4})-(\d{2})", str(s or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def run_batches(people_todo: List[Tuple[PersonEntry, str]], roles_todo: List[RoleRef],
                show: str, complete: CompleteFn, max_calls: Optional[int], log: CallLog
                ) -> Tuple[Dict[str, Dict], Dict[str, Dict], Optional[str]]:
    """ceil(max(people, roles) / BATCH) calls; each carries up to BATCH of each."""
    n = math.ceil(max(len(people_todo), len(roles_todo)) / BATCH) if (people_todo or roles_todo) else 0
    system = PROMPT.read_text(encoding="utf-8")
    pres: Dict[str, Dict] = {}
    rres: Dict[str, Dict] = {}
    cutoff = None
    for i in range(n):
        if max_calls is not None and log.calls >= max_calls:
            log.stopped = f"--max-calls {max_calls} reached"
            break
        pb = people_todo[i * BATCH:(i + 1) * BATCH]
        rb = roles_todo[i * BATCH:(i + 1) * BATCH]
        user = ("PEOPLE:\n" + json.dumps([_person_payload(p, show, pid) for p, pid in pb],
                                         ensure_ascii=False, indent=1)
                + "\n\nROLES:\n" + json.dumps([_role_payload(r) for r in rb],
                                              ensure_ascii=False, indent=1) + "\n")
        text, model = complete(system, user)
        log.calls += 1
        if model and model not in log.models:
            log.models.append(model)
        try:
            data = dg.parse_reply(text)
        except dg.DigestError as exc:
            raise AnnotateError(f"call {i + 1}: {exc}") from exc
        cutoff = cutoff or data.get("knowledge_cutoff")
        for x in data.get("people") or []:
            if isinstance(x, dict) and x.get("id"):
                pres[str(x["id"])] = {**x, "model": model}
        for x in data.get("roles") or []:
            if isinstance(x, dict) and x.get("id"):
                rres[str(x["id"])] = {**x, "model": model}
    return pres, rres, cutoff


# --------------------------------------------------------------------------- #
# Web lookup (WP-P3, WP-P4) — only where model knowledge is missing or post-cutoff
# --------------------------------------------------------------------------- #
#
# Three target kinds, each with its own prompt and cache key on the state record:
#   role   prompts/annotate/role_on_date.md  rec["role_check"]  (WP-P4: holder ON the date)
#   person prompts/annotate/web_lookup.md    rec["web"]         (WP-P3: post-cutoff model bio)
#   voice  prompts/annotate/voice_lookup.md  rec["voice"]       (WP-P4: quote-first identity)
# A WP-P3 rec["web"] on a person / role is kept as history.

@dataclass
class WebTarget:
    kind: str                               # role | person | voice
    rec: Dict[str, Any]                     # the state record the result is cached on
    payload: Dict[str, Any]
    meta: Dict[str, Any] = field(default_factory=dict)


CACHE_KEY = {"role": "role_check", "person": "web", "voice": "voice"}
KIND_ORDER = ("role", "person", "voice")


def _needs_web_person(rec: Optional[Dict[str, Any]]) -> bool:
    """A model description flagged post-cutoff (WP-P3 lookup). Never:
    private, pending, already looked up. Unknown people get the voice lookup."""
    if not rec or rec.get("private") or rec.get("pending") or "web" in rec:
        return False
    return bool(rec.get("known")) and bool(rec.get("verify"))


def _needs_voice(rec: Optional[Dict[str, Any]]) -> bool:
    """No reliable model knowledge and no adopted WP-P3 identity. Never:
    private, pending, a person created from a role, already looked up."""
    if not rec or rec.get("private") or rec.get("pending") or rec.get("from_role") \
            or "voice" in rec or rec.get("known"):
        return False
    w = rec.get("web") or {}
    return not (w.get("status") == "found" and w.get("confidence") in ("high", "medium"))


def _needs_web_role(rec: Optional[Dict[str, Any]]) -> bool:
    """Post-cutoff ("verify") and not yet checked for the date. A WP-P3
    rec["web"] does not count: its sources carried no dates."""
    return bool(rec) and bool(rec.get("verify")) and "role_check" not in rec


# -- quote-first queries ----------------------------------------------------- #

_QUOTE_RE = re.compile(r"[\"“]([^\"“”]{8,200})[\"”]")
_STOP = set("""a an the and or but of to in on for with at by from as is are was were be been it its
this that these those he she they his her their him them i you we our not no so than then too very
just also says said say calls called call argues argued thinks think about into over more most
some any all can could would should will may might has have had do does did if when what which who
reads read jokes joked claimed claims describes described got gets praises praised estimated estimates
optimistic pessimistic view views blown away complains complained criticized criticised rejects
rejected idea notes noted wrote writes post posted posts tweeted tweet like using via one
""".split())
_HANDLE_OK = re.compile(r"^[A-Za-z0-9_]{2,15}$")


def exact_quotes(texts: Iterable[str], limit: int = 3) -> List[str]:
    out: List[str] = []
    for t in texts:
        for m in _QUOTE_RE.finditer(t or ""):
            q = " ".join(m.group(1).split()).strip(" .,;:")
            if len(q.split()) >= 3 and q not in out:
                out.append(q)
    return out[:limit]


def claim_keywords(text: str, n: int = 6) -> List[str]:
    words = re.findall(r"[A-Za-z0-9][\w.+'’-]*", _QUOTE_RE.sub(" ", text or ""))
    out: List[str] = []
    for w in words:
        w2 = w.strip(".'’-")
        if len(w2) < 3 or w2.lower() in _STOP or w2.lower() in (x.lower() for x in out):
            continue
        out.append(w2)
    return out[:n]


def handle_candidates(names: Sequence[str]) -> List[str]:
    """Handle spellings for handle-like names: one token ("Kyo",
    "Pleometric"), or a spoken handle ("B on X", "Faw for AI")."""
    out: List[str] = []
    for n in names:
        bare = people_mod.bare_name(n).strip()
        toks = bare.split()
        if not toks:
            continue
        spoken = re.sub(r"\s+on\s+(?:X|Twitter)$", "", bare, flags=re.I)
        cands = []
        if len(toks) == 1:
            cands.append(toks[0])
        elif spoken != bare or any(t.lower() in ("for", "ai", "the") or re.search(r"\d", t) for t in toks):
            cands.append("".join(spoken.split()))
        for c in cands:
            c = re.sub(r"[^A-Za-z0-9_]", "", c).lower()
            if _HANDLE_OK.match(c) and c not in out:
                out.append(c)
    return out


def _body_mentions(ep: dg.Episode, names: Sequence[str], limit: int = 2) -> List[str]:
    keys = [people_mod.bare_name(n) for n in names if people_mod.bare_name(n)]
    out = []
    for _, line in _body_lines(ep.notes_path.read_text(encoding="utf-8")):
        if line.lstrip().startswith("|"):
            continue
        clean = " ".join(ANNOT.sub("", dg.unlink_citations(line)).split()).lstrip("-* ")
        clean = re.sub(r"\s*\[\d+:\d{2}(?::\d{2})?\]", "", clean)
        if any(re.search(rf"\b{re.escape(k)}\b", clean) for k in keys):
            for s in re.split(r"(?<=[.!?;])\s+", clean):
                if any(re.search(rf"\b{re.escape(k)}\b", s) for k in keys) and s not in out:
                    out.append(s[:240])
    return out[:limit]


def voice_queries(name: str, quotes: Sequence[str], claim: str, topic: str,
                  window: Tuple[dt.date, dt.date], handles: Sequence[str]) -> List[str]:
    """Quote first, then the claim, then the handle; the name last."""
    after = window[0].isoformat()
    before = (window[1] + dt.timedelta(days=1)).isoformat()
    span = f"after:{after} before:{before}"
    qs: List[str] = []
    for q in quotes[:2]:
        qs.append(f'site:x.com "{q}" {span}')
    kw = " ".join(claim_keywords(claim))
    spoken = re.sub(r"\s+on\s+(?:X|Twitter)$", "", name, flags=re.I)
    who = spoken if len(spoken.split()) > 1 else ""
    if kw:
        qs.append(" ".join(x for x in (f"site:x.com", who, kw, span) if x))
    for h in handles[:2]:
        qs.append(f"x.com/{h}")
    qs.append(" ".join(x for x in (f'"{name}"', topic, "AI") if x))
    return list(dict.fromkeys(qs))


def voice_payload(p: PersonEntry, show: str) -> Dict[str, Any]:
    names = [p.name, *p.aliases]
    eps = []
    texts: List[str] = []
    seen = set()
    for a in p.apps:
        if not a.episode or a.episode.key in seen:
            continue
        seen.add(a.episode.key)
        e = a.episode
        mentions = _body_mentions(e, names)
        claim = " ".join(x for x in [a.row.relevance] if x).strip()
        texts += [a.row.relevance or "", a.row.background or "", *mentions]
        start = e.published - dt.timedelta(days=QUOTE_WINDOW_DAYS)
        eps.append({"date": e.published.isoformat(), "topic": e.title, "claim": claim,
                    "context": mentions, "window": [start.isoformat(), e.published.isoformat()]})
    eps = eps[:3]
    quotes = exact_quotes(texts)
    handles = handle_candidates(names)
    first = p.apps[0].episode if p.apps and p.apps[0].episode else None
    e0 = next((a.episode for a in p.apps if a.episode), first)
    queries: List[str] = []
    if e0 is not None:
        claim0 = eps[0]["claim"] if eps else ""
        queries = voice_queries(p.name, quotes, claim0, e0.title,
                                (e0.published - dt.timedelta(days=QUOTE_WINDOW_DAYS), e0.published),
                                handles)
    stated = [s for s in dict.fromkeys(a.row.background for a in p.apps if a.row.background)
              if s.lower().rstrip(".") not in (people_mod.NOT_STATED, people_mod.MENTIONED_ONLY)]
    return {"kind": "voice", "names": names, "role_in_series": p.role, "stated": stated[:3],
            "show": show, "episodes": eps, "exact_quotes": quotes,
            "handle_candidates": handles, "queries": queries}


# -- role on date -------------------------------------------------------------- #

def role_parts(phrase: str) -> Tuple[str, str]:
    """("CEO", "OpenAI") for "OpenAI's CEO"; ("Secretary of State", "") for
    "the Secretary of State"; ("Prime Minister", "UK") for "the UK Prime Minister"."""
    p = " ".join(phrase.split())
    m = ROLE_B.fullmatch(p)
    if m:
        org = m.group("org")
        return re.sub(r"^['’]s\s+", "", p[len(org):]).strip(), org
    bare = re.sub(r"^[Tt]he\s+", "", p)
    nm = re.match(_NATION, bare)
    if nm:
        return bare[nm.end():].strip(), nm.group(0).strip()
    return bare, ""


def role_question(role: str, org: str, date: dt.date) -> str:
    if org:
        return f"Who held {role} at {org} on {date.isoformat()}?"
    return f"Who held the office of {role} on {date.isoformat()}?"


def _cutoff_date(cutoff: Optional[str]) -> Optional[dt.date]:
    ym = _ym(cutoff)
    return dt.date(ym[0], ym[1], 1) if ym else None


def role_payload(r: RoleRef, rec: Dict[str, Any], cutoff: Optional[str], show: str) -> Dict[str, Any]:
    role, org = role_parts(r.phrase)
    start = r.date - dt.timedelta(days=ROLE_EVIDENCE_DAYS)
    lc = _cutoff_date(cutoff)
    return {"kind": "role", "question": role_question(role, org, r.date), "role": role,
            "org": org or "(as implied by the context; the show is US-based unless stated)",
            "date": r.date.isoformat(),
            "last_confirmed": {"name": rec.get("name", ""),
                               "as_of": (cutoff or "unknown"),
                               "basis": "model knowledge (cutoff)"},
            "evidence_window": [start.isoformat(), r.date.isoformat()],
            "change_window": [(lc or start).isoformat(), r.date.isoformat()],
            "context_sentence": r.context,
            "context_note": "context only: identifies which org/role; not the question",
            "show": show}


def select_web_targets(entries: Sequence[PersonEntry], refs: Sequence[RoleRef],
                       state: Dict[str, Any], show: str) -> List[WebTarget]:
    """Roles flagged "verify — after cutoff" (asked for the holder ON the
    date), then post-cutoff model bios, then people with no reliable model
    knowledge (quote-first voice lookup, most appearances first). Private /
    unnamed / withheld rows never get here."""
    out: List[WebTarget] = []
    cutoff = state.get("knowledge_cutoff")
    for r in refs:
        rec = state["roles"].get(r.key)
        if _needs_web_role(rec):
            out.append(WebTarget("role", rec, role_payload(r, rec, cutoff, show),
                                 {"date": r.date, "last_confirmed": _cutoff_date(cutoff),
                                  "last_name": rec.get("name", "")}))
    seen: set = set()
    for p in entries:
        rec = _state_person(state, p)
        if rec is None or id(rec) in seen:
            continue
        if _needs_web_person(rec):
            seen.add(id(rec))
            payload = _person_payload(p, show, "")
            payload.pop("id")
            payload["kind"] = "person"
            payload["model_description"] = rec.get("description", "")
            out.append(WebTarget("person", rec, payload))
    for p in entries:
        rec = _state_person(state, p)
        if rec is None or id(rec) in seen or not _needs_voice(rec):
            continue
        seen.add(id(rec))
        out.append(WebTarget("voice", rec, voice_payload(p, show)))
    return out


# -- normalisation (the code, not the model, decides what is accepted) -------- #

def _sentences(text: str, n: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split()))
    return " ".join(parts[:n]).strip()


def _url_ok(u: Any) -> bool:
    return isinstance(u, str) and bool(re.match(r"^https?://\S+$", u.strip()))


def _iso_date(v: Any) -> Optional[dt.date]:
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None


def normalize_web(x: Dict[str, Any], kind: str, as_of: dt.date, model: str) -> Dict[str, Any]:
    """WP-P3 contract: ≤3 sources (≤6 only on fallback), real URLs,
    "found" only with a supporting source and an answer — never invented."""
    fallback = bool(x.get("fallback_used"))
    srcs = [str(u).strip() for u in (x.get("sources") or []) if isinstance(u, str)]
    srcs = list(dict.fromkeys(u for u in srcs if re.match(r"^https?://\S+$", u)))
    srcs = srcs[:WEB_MAX_FALLBACK if fallback else WEB_MAX_SOURCES]
    name = " ".join(str(x.get("name") or "").split())
    desc = _sentences(x.get("description") or "")
    conf = str(x.get("confidence") or "low").lower()
    conf = conf if conf in ("high", "medium", "low") else "low"
    note = " ".join(str(x.get("note") or "").split())[:200]
    found = str(x.get("status") or "").lower() == "found"
    if found and not srcs:
        found, note = False, note or "no supporting source returned"
    if found and not (name if kind == "role" else (desc or name)):
        found = False
    agrees = x.get("agrees_with_model")
    return {"status": "found" if found else "not_found",
            "name": name if found else "", "description": desc if found else "",
            "sources": srcs if found else [], "confidence": conf if found else "",
            "fallback_used": fallback and found,
            "agrees_with_model": agrees if isinstance(agrees, bool) else None,
            "note": note, "looked_up_at": as_of.isoformat(), "model": model}


_AFF_RX = [
    # an AI lab by name (a big-tech company only through its AI arm)
    ("lab", re.compile(r"\b(OpenAI|Anthropic|DeepMind|Google AI|Google Brain|Meta AI|FAIR|Microsoft AI|"
                       r"xAI|Mistral|Cohere|Hugging ?Face|Ai2|Allen Institute|Cognition|Perplexity|"
                       r"Character\.?AI|Safe Superintelligence|Thinking Machines)\b")),
    ("researcher", re.compile(r"\b(research(?:er)?|professor|scientist|academic|PhD|lecturer|economist)\b", re.I)),
    ("founder", re.compile(r"\b(founder|co-?founder|CEO|chief executive|entrepreneur|builder|"
                           r"engineer|developer)\b", re.I)),
    ("investor", re.compile(r"\b(investor|venture|VC|capitalist|general partner|fund)\b", re.I)),
    ("journalist", re.compile(r"\b(journalist|reporter|editor|writer|columnist|newsletter|"
                              r"podcast|host|analyst|commentator|YouTuber|creator)\b", re.I)),
]
AI_RE = re.compile(r"\bAI\b|artificial intelligence|machine learning|\bML\b|\bLLMs?\b|language model|"
                   r"OpenAI|Anthropic|DeepMind|Gemini|Claude|ChatGPT|GPT|neural|deep learning|"
                   r"robotics|\bagents?\b|Nvidia|chips?\b|data cent(?:er|re)|compute|Hugging ?Face|"
                   r"Mistral|xAI|Cognition|Perplexity|prompt|vibe.?cod", re.I)


_ABBREV_END = re.compile(r"(?:\b(?:[A-Z]\.){1,3}|\b(?:Mr|Ms|Mrs|Dr|Jr|Sr|St|Inc|Co|Corp|Ltd|vs|No)\.)$")


def first_sentence(text: str) -> str:
    """The first sentence, not split after "U.S." / "D.A." / "Inc."."""
    t = " ".join(str(text or "").split())
    for m in re.finditer(r"[.!?](?=\s+[A-Z\"“(])", t):
        if not _ABBREV_END.search(t[:m.end()]):
            return t[:m.end()]
    return t


def classify_affiliation(text: str) -> str:
    """lab | researcher | founder | investor | journalist | other — the first
    rule that matches the affiliation / description text."""
    for kind, rx in _AFF_RX:
        if rx.search(text or ""):
            return kind
    return "other"


def _clean_handle(h: Any) -> str:
    s = str(h or "").strip()
    s = re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/", "", s, flags=re.I).lstrip("@").split("/")[0]
    s = s.split("?")[0]
    return s if re.fullmatch(r"[A-Za-z0-9_]{1,15}", s) else ""


def own_post(url: str, handle: str) -> bool:
    """The URL is the person's own x.com / twitter.com profile or post."""
    return bool(handle) and bool(re.match(
        rf"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/{re.escape(handle)}(?:[/?#]|$)", url or "", re.I))


def enforce_voice_rules(v: Dict[str, Any]) -> Dict[str, Any]:
    """Rules applied to a stored voice result (also to results cached before
    a rule existed — deterministic, no web call): "confirmed" needs the quote
    on their OWN post/profile; a quote seen on someone else's post or in an
    article is "probable"."""
    if v.get("tier") == "confirmed" and not own_post(v.get("quote_url", ""), v.get("handle", "")):
        v = dict(v, tier="probable")
        host = _url_label(v.get("quote_url") or "") or "another page"
        why = f"quote seen on {host}, not on their own post — probable"
        if why not in (v.get("note") or ""):
            v["note"] = ((v.get("note") or "") + "; " if v.get("note") else "") + why
    return v


def normalize_voice(x: Dict[str, Any], as_of: dt.date, model: str) -> Dict[str, Any]:
    """Two signals must agree, decided here from the evidence (not the
    model's own tier): AI bio + the quoted topic/post near the date.
      confirmed  quote seen (quote_url in the evidence, supports "quote") + AI bio
      probable   handle/profile + AI bio + topic — e.g. only x.com snippets
                 behind the login: never more than probable without the quote
      unknown    anything less
    The model's tier is a ceiling: never upgraded, only downgraded."""
    fallback = bool(x.get("fallback_used"))
    ev: List[Dict[str, Any]] = []
    for e in x.get("evidence") or []:
        if isinstance(e, str):
            e = {"url": e}
        if not isinstance(e, dict) or not _url_ok(e.get("url")):
            continue
        u = e["url"].strip()
        if any(u == y["url"] for y in ev):
            continue
        sup = e.get("supports") or []
        sup = [sup] if isinstance(sup, str) else sup
        d = _iso_date(e.get("date"))
        ev.append({"url": u, "kind": str(e.get("kind") or "other").lower(),
                   "date": d.isoformat() if d else "", "login_wall": bool(e.get("login_wall")),
                   "supports": [s for s in (str(z).lower() for z in sup) if s in ("bio", "topic", "quote")]})
    ev = ev[:WEB_MAX_FALLBACK if fallback else WEB_MAX_SOURCES]
    handle = _clean_handle(x.get("handle")) or _clean_handle(x.get("profile_url"))
    profile = str(x.get("profile_url") or "").strip()
    if not _url_ok(profile):
        profile = f"https://x.com/{handle}" if handle else ""
    has = lambda tag: any(tag in e["supports"] for e in ev)          # noqa: E731
    bio = bool(x.get("bio_ai_related")) and has("bio")
    quote_url = str(x.get("quote_url") or "").strip()
    quote = (bool(x.get("quote_found")) and _url_ok(quote_url)
             and any(e["url"] == quote_url and "quote" in e["supports"] for e in ev))
    topic = (bool(x.get("topic_match")) and has("topic")) or quote
    ident = bool(handle or profile)
    if ident and bio and quote:
        tier = "confirmed"
    elif ident and bio and topic:
        tier = "probable"
    else:
        tier = "unknown"
    claimed = str(x.get("tier") or "unknown").lower()
    claimed = claimed if claimed in VOICE_TIERS else "unknown"
    if VOICE_TIERS.index(claimed) > VOICE_TIERS.index(tier):
        tier = claimed                                       # model's own tier is the ceiling
    note = " ".join(str(x.get("note") or "").split())[:200]
    if claimed != tier and VOICE_TIERS.index(claimed) < VOICE_TIERS.index(tier):
        why = ("quote not seen in the evidence" if claimed == "confirmed" and tier == "probable"
               else "the two signals (AI bio + quoted topic) did not both hold in the evidence")
        note = (note + "; " if note else "") + f"downgraded from {claimed}: {why}"
    login_only = bool(ev) and all(e["login_wall"] or e["kind"] == "snippet" for e in ev)
    aff = " ".join(str(x.get("affiliation") or "").split())[:120]
    atype = str(x.get("affiliation_type") or "").lower()
    if atype not in AFF_TYPES:
        atype = classify_affiliation(aff + " " + str(x.get("description") or ""))
    keep = tier != "unknown"
    candidate = {}
    if not keep and (handle or profile):
        candidate = {"handle": handle, "profile_url": profile, "claimed_tier": claimed,
                     "display_name": " ".join(str(x.get("display_name") or "").split())[:80]}
    return enforce_voice_rules({
            "tier": tier, "candidate": candidate,
            "handle": handle if keep else "", "profile_url": profile if keep else "",
            "display_name": " ".join(str(x.get("display_name") or "").split())[:80] if keep else "",
            "real_name": " ".join(str(x.get("real_name") or "").split())[:80] if keep else "",
            "affiliation": aff if keep else "", "affiliation_type": atype if keep else "",
            "quote_url": quote_url if (keep and quote) else "",
            "evidence": ev if keep else [],
            "login_wall_only": login_only and keep,
            "description": _sentences(x.get("description") or "", 2) if keep else "",
            "fallback_used": fallback and keep, "note": note,
            "looked_up_at": as_of.isoformat(), "model": model})


def normalize_role_date(x: Dict[str, Any], date: dt.date, last_confirmed: Optional[dt.date],
                        last_name: str, as_of: dt.date, model: str) -> Dict[str, Any]:
    """Accept "confirmed" only on dated evidence for the date: ≥1 source
    dated within ROLE_EVIDENCE_DAYS before the episode date, or a live
    official leadership page (only while the episode is ≤90 days old — a
    live page shows today's holder). Otherwise "last_confirmed <newest
    source date>" and verify stays. A change of holder found between the
    last confirmation and the episode date wins; both names are recorded."""
    fallback = bool(x.get("fallback_used"))
    srcs: List[Dict[str, Any]] = []
    for s in x.get("sources") or []:
        if isinstance(s, str):
            s = {"url": s}
        if not isinstance(s, dict) or not _url_ok(s.get("url")):
            continue
        u = s["url"].strip()
        if any(u == y["url"] for y in srcs):
            continue
        d = _iso_date(s.get("date"))
        srcs.append({"url": u, "date": d.isoformat() if d else "",
                     "kind": str(s.get("kind") or "other").lower(), "live": bool(s.get("live")),
                     "states": " ".join(str(s.get("states") or "").split())[:160]})
    srcs = srcs[:WEB_MAX_FALLBACK if fallback else WEB_MAX_SOURCES]
    start = date - dt.timedelta(days=ROLE_EVIDENCE_DAYS)
    changes = []
    for c in x.get("changes") or []:
        if not isinstance(c, dict) or not _url_ok(c.get("url")):
            continue
        d = _iso_date(c.get("date"))
        new = " ".join(str(c.get("new_holder") or "").split())
        if not d or not new or d > date or (last_confirmed and d < last_confirmed):
            continue
        changes.append({"date": d.isoformat(), "event": " ".join(str(c.get("event") or "").split())[:40],
                        "new_holder": new,
                        "previous_holder": " ".join(str(c.get("previous_holder") or "").split()) or last_name,
                        "url": c["url"].strip()})
    changes.sort(key=lambda c: c["date"])
    change = changes[-1] if changes else None
    name = " ".join(str(x.get("name") or "").split())
    answered = name
    if change:
        name = change["new_holder"]                         # a found change wins
    found = str(x.get("status") or "").lower() == "found" or bool(change)
    note = " ".join(str(x.get("note") or "").split())[:200]
    if found and not name:
        found = False
    if found and not srcs and not change:
        found, note = False, note or "no supporting source returned"
    dated = [dt.date.fromisoformat(s["date"]) for s in srcs if s["date"]]
    recent = [d for d in dated if start <= d <= date]
    official = [s for s in srcs if s["kind"] == "official_leadership_page" and s["live"]]
    official_ok = bool(official) and (as_of - date).days <= ROLE_EVIDENCE_DAYS
    change_recent = bool(change) and start <= dt.date.fromisoformat(change["date"]) <= date
    basis = ""
    if recent:
        basis = f"a source dated {max(recent).isoformat()}"
    elif change_recent:
        basis = f"a change dated {change['date']}"
    elif official_ok:
        basis = f"the live official leadership page ({_url_label(official[0]['url'])}, {as_of.isoformat()})"
    status = "not_found"
    if found:
        status = "confirmed" if basis else "last_confirmed"
    before = [d for d in dated if d <= date]
    if change:
        before.append(dt.date.fromisoformat(change["date"]))
    conf = str(x.get("confidence") or "low").lower()
    conf = conf if conf in ("high", "medium", "low") else "low"
    if change and answered and people_mod.name_key(answered) != people_mod.name_key(name):
        note = (note + "; " if note else "") + f"answer named {answered}; the change found wins"
    return {"status": status, "name": name if found else "",
            "description": _sentences(x.get("description") or "", 2) if found else "",
            "sources": srcs if found else [], "change": change if found else None,
            "basis": basis if status == "confirmed" else "",
            "evidence_date": (max(before).isoformat() if (found and before) else ""),
            "confidence": conf if found else "", "fallback_used": fallback and found,
            "note": note, "looked_up_at": as_of.isoformat(), "model": model}


def _normalize(t: WebTarget, x: Dict[str, Any], as_of: dt.date, model: str) -> Dict[str, Any]:
    if t.kind == "voice":
        return normalize_voice(x, as_of, model)
    if t.kind == "role":
        return normalize_role_date(x, t.meta["date"], t.meta.get("last_confirmed"),
                                   t.meta.get("last_name", ""), as_of, model)
    return normalize_web(x, t.kind, as_of, model)


def _hit(kind: str, r: Dict[str, Any]) -> bool:
    if kind == "voice":
        return r.get("tier") in ("confirmed", "probable")
    return r.get("status") in ("found", "confirmed", "last_confirmed")


@dataclass
class WebLog:
    calls: int = 0
    models: List[str] = field(default_factory=list)
    targets: int = 0
    sent: int = 0
    found: int = 0
    not_found: int = 0
    stopped: str = ""


PROMPTS = {"role": ROLE_PROMPT, "person": WEB_PROMPT, "voice": VOICE_PROMPT}


def run_web_lookups(targets: List[WebTarget], complete: CompleteFn, max_calls: Optional[int],
                    as_of: dt.date, log: WebLog) -> None:
    """Per kind (roles, then post-cutoff bios, then voices): ceil(n / WEB_BATCH)
    calls, all kinds together capped by max_calls. Each result is cached on its
    state record (CACHE_KEY); a target the reply omits is not cached (retried
    next run)."""
    log.targets = len(targets)
    for kind in KIND_ORDER:
        group = [t for t in targets if t.kind == kind]
        if not group:
            continue
        system = PROMPTS[kind].read_text(encoding="utf-8")
        key = CACHE_KEY[kind]
        for i in range(math.ceil(len(group) / WEB_BATCH)):
            if max_calls is not None and log.calls >= max_calls:
                log.stopped = f"--max-web-calls {max_calls} reached"
                return
            batch = group[i * WEB_BATCH:(i + 1) * WEB_BATCH]
            ids = {f"W{j + 1}": t for j, t in enumerate(batch)}
            user = ("TARGETS:\n" + json.dumps([{"id": k, **t.payload} for k, t in ids.items()],
                                              ensure_ascii=False, indent=1) + "\n")
            try:
                text, model = complete(system, user)
            except Exception as exc:  # noqa: BLE001 - keep finished batches
                log.stopped = f"web lookup ({kind}) call {i + 1} failed: {str(exc)[:200]}"
                if type(exc).__name__ in ("SubscriptionExhausted", "BudgetExhausted"):
                    raise
                return
            log.calls += 1
            log.sent += len(batch)
            if model and model not in log.models:
                log.models.append(model)
            try:
                data = dg.parse_reply(text)
            except dg.DigestError as exc:
                log.stopped = f"web lookup ({kind}) call {i + 1}: {exc}"
                return
            for x in data.get("targets") or []:
                t = ids.get(str(x.get("id"))) if isinstance(x, dict) else None
                if t is None or key in t.rec:
                    continue
                t.rec[key] = _normalize(t, x, as_of, model)
                if _hit(kind, t.rec[key]):
                    log.found += 1
                else:
                    log.not_found += 1


def _url_label(u: str) -> str:
    host = re.sub(r"^https?://(?:www\.)?", "", u).split("/", 1)[0]
    return host or u


def _md_url(u: str) -> str:
    return u.replace("(", "%28").replace(")", "%29").replace(" ", "%20").replace("|", "%7C")


def web_sources_md(w: Dict[str, Any]) -> str:
    return ", ".join(f"[{_url_label(u)}]({_md_url(u)})" for u in w.get("sources") or [])


def web_label(w: Dict[str, Any]) -> str:
    """'per web lookup 2026-10-21 (sources: [a](u), [b](u))'."""
    return f"per web lookup {w.get('looked_up_at')} (sources: {web_sources_md(w)})"


def dated_sources_md(rc: Dict[str, Any]) -> str:
    """Role sources with their own dates: [reuters.com 2026-09-30](u)."""
    out = []
    for s in rc.get("sources") or []:
        tag = s.get("date") or ("live" if s.get("live") else "undated")
        out.append(f"[{_url_label(s['url'])} {tag}]({_md_url(s['url'])})")
    c = rc.get("change")
    if c and not any(s["url"] == c["url"] for s in rc.get("sources") or []):
        out.append(f"[{_url_label(c['url'])} {c['date']}]({_md_url(c['url'])})")
    return ", ".join(out)


def apply_web_role(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The effective resolution.
    * role_check (WP-P4, dated evidence): its holder is shown; verify is
      cleared only when "confirmed" (a source dated ≤90 days before the
      episode, or a live official page) and not contradicting model knowledge
      without a recorded change.
    * WP-P3 rec["web"] only (undated sources): its name is shown but verify
      stays — undated sources cannot confirm a holder on the date."""
    res = dict(rec)
    model_name = (rec.get("name") or "").strip()
    rc = rec.get("role_check")
    if rc and rc.get("status") in ("confirmed", "last_confirmed") and rc.get("name"):
        same = (not model_name) or people_mod.name_key(model_name) == people_mod.name_key(rc["name"])
        res.update(name=rc["name"], confidence=rc.get("confidence") or "low", source="role_check",
                   description=rc.get("description") or (rec.get("description", "") if same else ""))
        if not same:
            res["model_name"] = model_name
            res["contradiction"] = not rc.get("change")
        res["verify"] = rc["status"] != "confirmed" or bool(res.get("contradiction"))
        return res
    w = rec.get("web")
    if not w or w.get("status") != "found":
        return res
    same = (not model_name) or people_mod.name_key(model_name) == people_mod.name_key(w["name"])
    res.update(name=w["name"], confidence=w.get("confidence") or "low", source="web",
               description=w.get("description") or rec.get("description", ""))
    if not same:
        res["model_name"] = model_name
        res["contradiction"] = True
    res["verify"] = True
    res["undated"] = True
    return res


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _rel(target: Path, from_dir: Path) -> str:
    return link_path(os.path.relpath(Path(target).resolve(), Path(from_dir).resolve()))


def _cell(s: str) -> str:
    return dg._clean_cell(s or "")


def _q(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _web_found(rec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A lookup good enough to BE the description: found, high/medium
    confidence. A low-confidence identity match is shown only as a possible
    match next to "No reliable model knowledge" (never adopted)."""
    w = (rec or {}).get("web")
    return (w if w and w.get("status") == "found" and w.get("description")
            and w.get("confidence") in ("high", "medium") else None)


def _description(rec: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(text, kind) kind: model | web | none | pending. A web description is
    used only where the lookup was triggered (unknown, or post-cutoff)."""
    if not rec:
        return PENDING, "pending"
    v = rec.get("voice") or {}
    if v.get("tier") == "confirmed" and v.get("description"):
        return v["description"], "voice"
    w = _web_found(rec)
    if w:
        return w["description"], "web"
    if rec.get("known") and (rec.get("description") or "").strip():
        return rec["description"].strip(), "model"
    return NO_KNOWLEDGE, "none"


TIER_TEXT = {
    "confirmed": "confirmed — the quote was found on their post/profile, and the bio is AI-related",
    "probable": "probable — handle + AI-related bio + a post on the quoted topic around the date; "
                "the quote was not found on their own post",
}


def _handle_md(v: Dict[str, Any]) -> str:
    if v.get("handle") and v.get("profile_url"):
        return f"[@{v['handle']}]({_md_url(v['profile_url'])})"
    if v.get("profile_url"):
        return f"[{_url_label(v['profile_url'])}]({_md_url(v['profile_url'])})"
    return f"@{v['handle']}" if v.get("handle") else "-"


def voice_evidence_md(v: Dict[str, Any]) -> str:
    out = []
    for e in v.get("evidence") or []:
        tag = e.get("kind") or "page"
        if e.get("login_wall"):
            tag += ", login wall"
        if e.get("date"):
            tag += f", {e['date']}"
        out.append(f"[{_url_label(e['url'])} ({tag})]({_md_url(e['url'])})")
    return ", ".join(out)


def voice_section(v: Dict[str, Any]) -> List[str]:
    """The quote-first identity (WP-P4), as its own section."""
    d = v.get("looked_up_at")
    if v.get("tier") not in ("confirmed", "probable"):
        c = v.get("candidate") or {}
        cand = (f" Possible candidate, not adopted: {_handle_md(c)}." if c else "")
        return [f"_Quote-first web lookup {d}: unknown"
                f"{' (' + v['note'] + ')' if v.get('note') else ''}.{cand}_", ""]
    md = [f"## Identity — per web lookup {d} ({v['tier']})", "",
          f"- Handle: {_handle_md(v)}"
          + (f" · display name: {_cell(v['display_name'])}" if v.get("display_name") else "")
          + (f" · real name: {_cell(v['real_name'])}" if v.get("real_name") else ""),
          f"- Affiliation: {_cell(v.get('affiliation')) or '-'} ({v.get('affiliation_type') or 'other'})"]
    if v.get("quote_url"):
        lbl = "Quoted post" if v.get("tier") == "confirmed" else "Quote seen at (not their own post)"
        md.append(f"- {lbl}: [{_url_label(v['quote_url'])}]({_md_url(v['quote_url'])})")
    md.append(f"- Evidence: {voice_evidence_md(v) or '-'}")
    md.append(f"- Tier: {TIER_TEXT[v['tier']]}"
              + (" (x.com login wall: search snippets only)" if v.get("login_wall_only") else ""))
    if v.get("tier") == "probable" and v.get("description"):
        md.append(f"- Profile summary (not adopted as the description): {v['description']}")
    if v.get("note"):
        md.append(f"- Note: {v['note']}")
    md += ["", f"_Web lookup ({v.get('model') or 'model'}), not from the episodes; check the "
           "evidence before relying on it._", ""]
    return md


def render_person(p: PersonEntry, rec: Optional[Dict[str, Any]], pdir: Path,
                  role_apps: Sequence[Tuple[RoleRef, Dict]]) -> str:
    desc, kind = _description(rec)
    dates = p.episode_dates() + [r.date for r, _ in role_apps]
    eps = {a.episode.key for a in p.apps if a.episode} | {
        e.key for r, _ in role_apps for e in r.episodes}
    canonical = (rec or {}).get("canonical_name") or ""
    w = _web_found(rec)
    canon_src = MODEL_KNOWLEDGE
    if not canonical and w and w.get("name") and \
            people_mod.name_key(w["name"]) != people_mod.name_key(p.name):
        canonical, canon_src = w["name"], f"per web lookup {w.get('looked_up_at')}"
    aliases = list(dict.fromkeys(p.aliases + ([canonical] if canonical and canonical != p.name else [])))
    fm = ["---", f"name: {_q(p.name)}",
          "aliases: [" + ", ".join(_q(a) for a in aliases) + "]",
          f"role_in_series: {_q(p.role or 'referred to by role')}",
          f"first_seen: {min(dates).isoformat() if dates else 'null'}",
          f"last_seen: {max(dates).isoformat() if dates else 'null'}",
          f"appearances: {len(eps)}",
          f"description_source: {kind}",
          f"generated_by: {GENERATOR}",
          "tags: [person]", "---"]
    md = [f"# {p.name}", ""]
    if canonical and people_mod.name_key(canonical) != people_mod.name_key(p.name):
        md += [f"_Probable spelling, {canon_src}: **{canonical}**. The notes use the "
               f"transcript's spelling._", ""]
    v = (rec or {}).get("voice") or {}
    label = {"model": f"Description — {MODEL_KNOWLEDGE}",
             "web": f"Description — {web_label(w) if w else ''}",
             "voice": f"Description — per web lookup {v.get('looked_up_at')} (confirmed identity)"
             }.get(kind, "Description")
    md += [f"## {label}", "", desc, ""]
    if kind == "model":
        md += [f"_Model knowledge ({(rec or {}).get('model', 'model')}), not from the sources; "
               "may be outdated. Verify before relying on it._", ""]
    elif kind == "web" and w:
        md += [f"_Web lookup ({w.get('model') or 'model'}, {w.get('confidence') or 'low'} confidence"
               f"{', fallback sources' if w.get('fallback_used') else ''}), not from the episodes; "
               "check the linked sources before relying on it._", ""]
        if rec.get("known") and rec.get("description"):
            if w.get("agrees_with_model") is False:
                md += [f"_Contradiction noted — model knowledge said: {rec['description']}_", ""]
            else:
                md += [f"_Model knowledge, for comparison: {rec['description']}_", ""]
    elif kind in ("none", "voice") and (rec or {}).get("web"):
        wn = rec["web"]
        if wn.get("status") == "found":
            md += [f"_Possible match, {web_label(wn)} — low confidence, not adopted: "
                   f"{wn.get('description')}_", ""]
        else:
            earlier = "Earlier name-only web lookup" if v else "Web lookup"
            md += [f"_{earlier} {wn.get('looked_up_at')}: not found"
                   f"{' (' + wn['note'] + ')' if wn.get('note') else ''}._", ""]
    if v:
        md += voice_section(v)
    md += ["## As stated in sources", ""]
    stated = []
    for a in p.apps:
        bg = (a.row.background or "").strip()
        if not bg or bg.lower().rstrip(".") in (people_mod.NOT_STATED, people_mod.MENTIONED_ONLY):
            continue
        if a.episode:
            link = f"[{_cell(a.episode.title)}]({_rel(a.episode.notes_path, pdir)})"
            stated.append(f"- {bg} — {link} ({a.episode.published.isoformat()})")
    stated = list(dict.fromkeys(stated))
    md += (stated or ["- Nothing stated beyond the name."]) + [""]
    md += ["## Appearances", "", "| date | where | role | relevance |", "|---|---|---|---|"]
    rows = []
    for a in p.apps:
        if a.episode:
            e = a.episode
            rows.append((e.published.isoformat(), 0,
                         f"| {e.published.isoformat()} | [{_cell(e.title)}]({_rel(e.notes_path, pdir)}) | "
                         f"{_cell(a.row.role) or '-'} | {_cell(a.row.relevance) or '-'} |"))
        elif a.digest:
            d = a.digest
            rows.append(((d.start.isoformat() if d.start else d.span), 1,
                         f"| {d.span} | [digest {d.span}]({_rel(d.path, pdir)}) | "
                         f"{_cell(a.row.role) or '-'} | {_cell(a.row.relevance) or '-'} |"))
    for r, res in role_apps:
        for e in r.episodes:
            rows.append((e.published.isoformat(), 2,
                         f"| {e.published.isoformat()} | [{_cell(e.title)}]({_rel(e.notes_path, pdir)}) | "
                         f"referred to as \"{_cell(r.phrase)}\" | {_cell(_resolution_text(r, res, None))} |"))
    md += [x for _, _, x in sorted(dict.fromkeys(rows))] + [""]
    return "\n".join(fm) + "\n" + "\n".join(md)


def _web_tail(res: Dict[str, Any]) -> str:
    if res.get("source") == "role_check":
        rc = res["role_check"]
        parts = [f"per web lookup {rc.get('looked_up_at')}"]
        c = rc.get("change")
        if c:
            parts.append(f"changed {c['date']}: {c['new_holder']} ({c.get('event') or 'new holder'}; "
                         f"previously {c.get('previous_holder') or 'unknown'})")
        if rc.get("status") == "confirmed":
            parts.append(f"confirmed by {rc.get('basis')}")
        else:
            parts.append(f"last confirmed {rc.get('evidence_date') or 'never (undated sources)'}")
        parts.append(f"sources: {dated_sources_md(rc)}")
        return "; ".join(parts)
    w = res.get("web") or {}
    und = "; undated sources" if res.get("undated") else ""
    return (f"per web lookup {w.get('looked_up_at')}{und}; sources: {web_sources_md(w)}")


def _looked_up(res: Dict[str, Any]) -> bool:
    return res.get("source") in ("web", "role_check")


def _not_found_tail(res: Dict[str, Any]) -> str:
    rc = res.get("role_check") or {}
    if rc.get("status") == "not_found":
        return f"; web lookup {rc.get('looked_up_at')}: not found for the date"
    if (res.get("web") or {}).get("status") == "not_found":
        return f"; web lookup {res['web'].get('looked_up_at')}: not found"
    return ""


def _resolution_text(r: RoleRef, res: Dict[str, Any], link: Optional[str]) -> str:
    name = (res.get("name") or "").strip()
    who = f"[{name}]({link})" if (name and link) else (name or "unresolved")
    d = r.date.isoformat()
    if _looked_up(res):
        if res.get("contradiction"):
            return (f"→ {who}?, as of {d}, {_web_tail(res)}; contradicts model knowledge "
                    f"({res.get('model_name')}) — verify")
        if res.get("verify"):
            return f"→ {who}?, as of {d}, {_web_tail(res)} ({res.get('confidence')} confidence) — verify"
        return f"→ {who}, as of {d}, {_web_tail(res)} ({res.get('confidence')} confidence)"
    nf = _not_found_tail(res)
    if res.get("verify"):
        return f"→ {who}?, as of {d}, per model knowledge — {VERIFY}{nf}"
    return f"→ {who}, as of {d}, per model knowledge ({res.get('confidence', 'low')} confidence){nf}"


def annotation(r: RoleRef, res: Dict[str, Any], link: Optional[str]) -> str:
    """The minimal in-notes annotation (ANNOT matches it)."""
    name = (res.get("name") or "").strip()
    who = f"[{name}]({link})" if (name and link) else (name or "unresolved")
    q = "?" if res.get("verify") and name else ""
    if _looked_up(res):
        tail = ""
        if res.get("contradiction"):
            tail = f"; model knowledge: {res.get('model_name')} — verify"
        elif res.get("verify"):
            tail = " — verify"
        return f" (→ {who}{q}, as of {r.date.isoformat()}, {_web_tail(res)}{tail})"
    tail = f" — {VERIFY}" if res.get("verify") else ""
    return f" (→ {who}{q}, as of {r.date.isoformat()}, per model knowledge{tail})"


_KIND_TAG = {"model": " _(model knowledge)_", "web": " _(web lookup)_",
             "voice": " _(web lookup, confirmed)_", "probable": " _(web lookup, probable)_"}


def render_people_index(entries: Sequence[PersonEntry], recs: Dict[str, Optional[Dict]],
                        resolved: Sequence[Tuple[RoleRef, Dict]], skipped_private: int,
                        role_apps: Dict[str, List[Tuple[RoleRef, Dict]]],
                        show: str, n_eps: int, n_digests: int, cutoff: Optional[str]) -> str:
    md = ["---", f"title: {_q(f'{show} — people')}", f"people: {len(entries)}",
          f"episodes: {n_eps}", f"digests: {n_digests}",
          f"model_knowledge_cutoff: {_q(cutoff) if cutoff else 'null'}",
          f"generated_by: {GENERATOR}", "tags: [people-index]", "---",
          f"# {show} — people", "",
          f"> {len(entries)} people from the People sections of {n_eps} episode(s) and "
          f"{n_digests} digest(s). Descriptions are model knowledge (cutoff uncertain) or, "
          "where the model had none or the date is past its cutoff, a web lookup — labelled in "
          "each note with its sources; everything else is as the sources state. "
          f"{skipped_private} private individual(s) / unnamed speaker row(s) have no note.", "",
          "| person | role in series | appearances | first seen | last seen | description |",
          "|---|---|---:|---|---|---|"]
    for p in entries:
        desc, kind = _description(recs.get(p.slug))
        first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]
        v = (recs.get(p.slug) or {}).get("voice") or {}
        if kind == "none" and v.get("tier") == "probable":
            first, kind = (f"Probable: {_handle_md(v)}"
                           + (f" — {v['affiliation']}" if v.get("affiliation") else "")), "probable"
        ra = role_apps.get(p.slug, [])
        dates = sorted(set(p.episode_dates()) | {r.date for r, _ in ra})
        n = len({a.episode.key for a in p.apps if a.episode} | {e.key for r, _ in ra for e in r.episodes})
        md.append(f"| [{_cell(p.name)}]({p.slug}.md) | {_cell(p.role or 'referred to by role')} | {n} | "
                  f"{dates[0].isoformat() if dates else '-'} | {dates[-1].isoformat() if dates else '-'} | "
                  f"{_cell(first)}{_KIND_TAG.get(kind, '')} |")
    md += ["", "## Vague role references", ""]
    if not resolved:
        md.append("- None found in the notes.")
    else:
        md.append(f"_Resolved for each episode's date from model knowledge (or a web lookup "
                  f"where past the cutoff); full log in "
                  f"[_resolutions](_resolutions.md)._")
        md.append("")
        for r, res in resolved:
            link = f"{res['slug']}.md" if res.get("slug") else None
            md.append(f"- \"{r.phrase}\" ({r.date.isoformat()}) {_resolution_text(r, res, link)}")
    return "\n".join(md) + "\n"


def _status(res: Dict[str, Any]) -> str:
    if _looked_up(res):
        s = f"{_web_tail(res)}"
        if res.get("contradiction"):
            return f"{s}; contradicts model knowledge ({res.get('model_name')}) — verify"
        return s + (" — verify" if res.get("verify") else "; confirmed")
    return (VERIFY if res.get("verify") else "as of episode date, per model knowledge") + _not_found_tail(res)


def render_resolutions(resolved: Sequence[Tuple[RoleRef, Dict]], pdir: Path) -> str:
    md = ["---", "title: \"Vague role references — resolution log\"", f"generated_by: {GENERATOR}",
          "tags: [people-index]", "---", "# Vague role references — resolution log", "",
          "_Every role reference found in the notes (\"the Secretary of State\", \"Meta's CEO\"), "
          "resolved for the episode's date from model knowledge — never from the sources — "
          "or, when the date is after the model's knowledge cutoff, from a web lookup (sources "
          "linked). \"verify\" means not confirmed: after the cutoff and not (yet) confirmed "
          "by a lookup, or the lookup contradicts model knowledge._", "",
          "| reference | episode date | resolved | confidence | status | episodes | context |",
          "|---|---|---|---|---|---|---|"]
    for r, res in resolved:
        name = res.get("name") or "-"
        if res.get("slug"):
            name = f"[{name}]({res['slug']}.md)"
        eps = ", ".join(f"[{_cell(e.title)[:40]}]({_rel(e.notes_path, pdir)})" for e in r.episodes)
        md.append(f"| {_cell(r.phrase)} | {r.date.isoformat()} | {_cell(name)} | "
                  f"{res.get('confidence', '-')} | {_status(res)} | "
                  f"{eps} | {_cell(r.context[:160])} |")
    if not resolved:
        md.append("| - | - | - | - | - | - | - |")
    return "\n".join(md) + "\n"


# --------------------------------------------------------------------------- #
# Sources index
# --------------------------------------------------------------------------- #

LISTEN_TYPES = {"podcast", "show", "video", "talk"}
QUEUE_MAX = 15


QUOTED_ROLE = re.compile(r"quot|cited|guest|speaker|interview|author|post", re.I)
HOST_ROLE = re.compile(r"\bhost\b|co-?host", re.I)
VOICES_MAX = 100


def _cite_link(e: dg.Episode, cite: str) -> Optional[str]:
    """[0:21:21] in episode e -> a YouTube timestamp link."""
    t = dg.parse_ts(cite)
    if t is None:
        return None
    base = f"https://youtu.be/{e.youtube_id}" if e.youtube_id else (e.url or "")
    if not base:
        return None
    return f"[{dg.hms(t)}]({_md_url(base + ('&' if '?' in base else '?') + f't={int(t)}')})"


def signal_score(times: int, episodes: int, days_since_last: int, aff_type: str) -> float:
    """quotes × episode breadth × recency × affiliation weight
       quotes   = times quoted (citations on the person's quoted rows)
       breadth  = 1 + ln(distinct episodes)
       recency  = 0.5 ** (days since last quoted / RECENCY_HALF_LIFE)
       weight   = AFF_WEIGHT[lab|researcher|founder|investor|journalist|other]"""
    breadth = 1 + math.log(max(1, episodes))
    recency = 0.5 ** (max(0, days_since_last) / RECENCY_HALF_LIFE)
    return round(times * breadth * recency * AFF_WEIGHT.get(aff_type, 1.0), 2)


def ai_voices(entries: Sequence[PersonEntry], recs: Dict[str, Optional[Dict]],
              as_of: dt.date) -> Tuple[List[Dict[str, Any]], List[str], int]:
    """People the episodes QUOTE (not the host), with an identity, whose bio is
    AI-related — ranked by signal_score. Returns (voices, unidentified names,
    identified-but-not-AI count)."""
    voices: List[Dict[str, Any]] = []
    unidentified: List[str] = []
    not_ai = 0
    for p in entries:
        rows = [a for a in p.apps if a.episode and QUOTED_ROLE.search(a.row.role or "")
                and not HOST_ROLE.search(a.row.role or "")]
        if not rows:
            continue
        rec = recs.get(p.slug) or {}
        v = rec.get("voice") or {}
        desc, kind = _description(rec)
        if v.get("tier") in ("confirmed", "probable"):
            tier, aff, atype = v["tier"], v.get("affiliation") or "", v.get("affiliation_type") or "other"
            real = v.get("real_name") or v.get("display_name") or ""
            ai = True                                       # two-signal acceptance includes an AI bio
        elif kind in ("model", "web"):
            tier = "known (model knowledge)" if kind == "model" else "known (web lookup)"
            aff = first_sentence(desc)
            aff = aff if len(aff) <= 110 else aff[:107].rstrip() + "…"
            # the bio decides (not what the notes say they talked about)
            atype = classify_affiliation(desc)
            real = rec.get("canonical_name") or ((_web_found(rec) or {}).get("name") or "")
            ai = bool(AI_RE.search(desc))
        else:
            unidentified.append(p.name)
            continue
        if not ai:
            not_ai += 1
            continue
        times = sum(max(1, len(a.row.cites)) for a in rows)
        eps = sorted({a.episode.key: a.episode for a in rows}.values(), key=lambda e: e.published)
        last = eps[-1].published
        days = (as_of - last).days
        posts = []
        if v.get("quote_url"):
            posts.append(v["quote_url"])
        posts += [e["url"] for e in v.get("evidence") or [] if e.get("kind") == "post"
                  and e["url"] not in posts]
        cites = []
        for a in rows:
            for c in a.row.cites:
                link = _cite_link(a.episode, c)
                if link and link not in cites:
                    cites.append(link)
        voices.append({"entry": p, "tier": tier, "handle": v.get("handle") if v.get("tier") else "",
                       "profile_url": v.get("profile_url") if v.get("tier") else "",
                       "real_name": real if people_mod.name_key(real) != people_mod.name_key(p.name) else "",
                       "affiliation": aff, "aff_type": atype, "times": times, "episodes": eps,
                       "first": eps[0].published, "last": last, "days": days, "posts": posts[:3],
                       "cites": cites[:3],
                       "score": signal_score(times, len(eps), days, atype)})
    voices.sort(key=lambda x: (-x["score"], -x["times"], x["entry"].name.lower()))
    return voices, unidentified, not_ai


def render_voices(voices: Sequence[Dict[str, Any]], unidentified: Sequence[str], not_ai: int,
                  sdir: Path) -> List[str]:
    w = ", ".join(f"{k} {v}" for k, v in AFF_WEIGHT.items())
    md = ["## AI voices — listen / follow queue", "",
          "_People the episodes quote whose bio is AI-related, ranked by **signal score = times quoted "
          f"× episode breadth × recency × affiliation weight** — breadth = 1 + ln(distinct episodes); "
          f"recency = 0.5^(days since last quoted / {RECENCY_HALF_LIFE}); weight: {w}. Tier: "
          "confirmed (quote found on their post/profile) · probable (handle + AI bio + a post on the "
          "quoted topic around the date) · known (model knowledge or an earlier web lookup). "
          "Identities are candidates — check the evidence before following; what each said is as "
          "the episodes state._", ""]
    if not voices:
        md += ["- None yet.", ""]
    else:
        md += ["| # | person / handle | real name | affiliation | tier | times quoted | episodes | "
               "first quoted | last quoted | recency (days) | quoted posts | where quoted | score |",
               "|---:|---|---|---|---|---:|---:|---|---|---:|---|---|---:|"]
        for i, x in enumerate(voices[:VOICES_MAX], 1):
            p = x["entry"]
            who = f"[{_cell(p.name)}](../people/{p.slug}.md)"
            if x.get("handle") or x.get("profile_url"):
                who += " · " + _handle_md(x)
            posts = ", ".join(f"[{_url_label(u)}]({_md_url(u)})" for u in x["posts"]) or "-"
            md.append(f"| {i} | {who} | {_cell(x['real_name']) or '-'} | {_cell(x['affiliation']) or '-'} "
                      f"({x['aff_type']}) | {x['tier']} | {x['times']} | {len(x['episodes'])} | "
                      f"{x['first'].isoformat()} | {x['last'].isoformat()} | {x['days']} | {posts} | "
                      f"{', '.join(x['cites']) or '-'} | {x['score']:.2f} |")
        md.append("")
    if len(voices) > VOICES_MAX:
        md += [f"_{len(voices) - VOICES_MAX} more below the top {VOICES_MAX} (see the people index)._", ""]
    if unidentified:
        md += [f"_Not ranked — quoted but unidentified (tier unknown), {len(unidentified)}: "
               + ", ".join(_cell(n) for n in unidentified) + "._", ""]
    if not_ai:
        md += [f"_Not ranked — {not_ai} identified quoted people whose description is not AI-related._", ""]
    return md


def sources_index(scope: Scope, sdir: Path, as_of: dt.date,
                  voices_md: Optional[List[str]] = None) -> Tuple[str, List[Dict[str, Any]]]:
    rows: List[cited_sources.SourceRow] = []
    owner: Dict[int, dg.Episode] = {}  # row order -> episode
    order = 0
    for e in scope.episodes:
        for r in e.source_rows:
            owner[order] = e
            rows.append(cited_sources.SourceRow(r.name, r.type, r.cited_by, r.cited_for, r.date,
                                                r.cites or ["-"], order))
            order += 1
    merged, _ = cited_sources.merge_sources(rows, timed=False)
    items = []
    for s in merged:
        eps = sorted({owner[r.order].key: owner[r.order] for r in s.rows}.values(),
                     key=lambda e: (e.published, e.title))
        times = sum(max(1, len([c for c in r.cites if c != "-"])) for r in s.rows)
        items.append({"source": s, "episodes": eps, "times": times,
                      "first": eps[0].published, "last": eps[-1].published})
    items.sort(key=lambda x: (-x["times"], -len(x["episodes"]), -x["last"].toordinal(),
                              x["source"].key))
    md = ["---", f"title: {_q(f'{scope.show} — sources referenced')}", f"sources: {len(items)}",
          f"episodes: {len(scope.episodes)}", f"generated_by: {GENERATOR}",
          "tags: [sources-index, candidate-inputs]", "---",
          f"# {scope.show} — sources referenced", "",
          "> Outside sources the episodes cite (outlets, podcasts, papers, reports, posts …), merged "
          "from every episode's Sources Referenced section. **Candidate inputs for interests and "
          "listening** — not endorsements; what each was cited for is as the episodes state.", ""]
    md += ["## Listen / read next", "",
           "_Ranked by times cited, then recency. Shows and podcasts are to listen to; the rest "
           "to read._", ""]
    def score(x):
        age = max(0, (as_of - x["last"]).days)
        return (-(x["times"] + len(x["episodes"]) + max(0.0, 1 - age / 30)), x["source"].key)
    queue = sorted(items, key=score)[:QUEUE_MAX]
    if not queue:
        md.append("- Nothing yet: no episode in scope has a Sources Referenced section.")
    for i, x in enumerate(queue, 1):
        s = x["source"]
        verb = "listen" if s.type in LISTEN_TYPES else "read"
        last = x["episodes"][-1]
        md.append(f"{i}. **{s.shown}** ({s.type or 'source'}; {verb}) — cited {x['times']}× in "
                  f"{len(x['episodes'])} episode(s), last {x['last'].isoformat()} "
                  f"([{_cell(last.title)}]({_rel(last.notes_path, sdir)})): {s.cited_for or '-'}")
    md += [""] + (voices_md or [])
    md += ["## All sources", "",
           "| source | type | times cited | episodes | first seen | last seen | cited by | episode links |",
           "|---|---|---:|---:|---|---|---|---|"]
    for x in items:
        s = x["source"]
        links = ", ".join(f"[{e.published.isoformat()}]({_rel(e.notes_path, sdir)})" for e in x["episodes"])
        md.append(f"| {_cell(s.shown)} | {s.type or '-'} | {x['times']} | {len(x['episodes'])} | "
                  f"{x['first'].isoformat()} | {x['last'].isoformat()} | {_cell(s.cited_by) or '-'} | {links} |")
    if not items:
        md.append("| - | - | 0 | 0 | - | - | - | - |")
    return "\n".join(md) + "\n", [{"source": x["source"].shown, "times": x["times"],
                                   "episodes": len(x["episodes"])} for x in items]


# --------------------------------------------------------------------------- #
# Notes annotation
# --------------------------------------------------------------------------- #

def annotate_notes(ep: dg.Episode, refs: Sequence[Tuple[RoleRef, Dict]],
                   people_dir_for: Callable[[Path], Optional[Path]]) -> int:
    """First mention of each resolved role in the notes body + a People row
    whose name is that role. Already-annotated mentions are left alone."""
    path = ep.notes_path
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    changed = 0
    pdir = people_dir_for(path)
    for r, res in refs:
        if ep not in r.episodes:
            continue
        link = (_rel(pdir / f"{res['slug']}.md", path.parent) if (pdir and res.get("slug")) else None)
        note = annotation(r, res, link)
        done = False
        for i, line in _body_lines("\n".join(lines)):
            for m in _role_matches(dg.unlink_citations(line)):
                if role_key(m.group("p")) != role_key(r.phrase):
                    continue
                # locate the phrase in the raw line (links unchanged before it)
                pos = line.find(m.group("p"))
                if pos < 0:
                    continue
                end = pos + len(m.group("p"))
                old = ANNOT.match(line[end:])
                if old and old.group(0) == note:
                    done = True
                    break
                # a stale annotation (e.g. model knowledge, now looked up) is replaced
                rest = line[end + len(old.group(0)):] if old else line[end:]
                lines[i] = line[:end] + note + rest
                changed += 1
                done = True
                break
            if done:
                break
    if changed:
        _write_if_changed(path, "\n".join(lines))
    return changed


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class AnnotateResult:
    roots: List[Path]
    people: int
    new_described: int
    calls: int
    models: List[str]
    roles: List[Dict[str, Any]]
    skipped_private: int
    sources: List[Dict[str, Any]]
    notes_annotated: int
    written: List[Path]
    stopped: str = ""
    link_problems: List[str] = field(default_factory=list)
    web: Dict[str, Any] = field(default_factory=dict)


def run(scope: Scope, complete: Optional[CompleteFn], *, max_calls: Optional[int] = None,
        refresh: bool = False, annotate: bool = True, as_of: Optional[dt.date] = None,
        progress: Callable[[str], None] = lambda m: None,
        web_complete: Optional[CompleteFn] = None, web: bool = True,
        max_web_calls: Optional[int] = MAX_WEB_CALLS) -> AnnotateResult:
    as_of = as_of or dt.date.today()
    state_path = scope.roots[0] / "people" / STATE_FILE
    state = _load_state(state_path)
    for prec in state["people"].values():           # rules added after a lookup was cached
        if prec.get("voice"):
            prec["voice"] = enforce_voice_rules(prec["voice"])
    entries, skipped = collect_people(scope)
    refs = find_role_refs(scope.episodes)
    progress(f"{len(scope.episodes)} episode(s), {len(scope.digests)} digest(s): "
             f"{len(entries)} unique people, {len(refs)} role reference(s)")

    # -- what needs the model
    people_todo: List[Tuple[PersonEntry, str]] = []
    for p in entries:
        rec = _state_person(state, p)
        if rec is None or refresh or rec.get("pending"):
            people_todo.append((p, f"P{len(people_todo) + 1}"))
    roles_todo = [r for r in refs if refresh or r.key not in state["roles"]]
    log = CallLog()
    if (people_todo or roles_todo) and complete is None:
        raise AnnotateError("model calls needed but no model configured")
    pres, rres, cutoff = run_batches(people_todo, roles_todo, scope.show, complete,
                                     max_calls, log) if (people_todo or roles_todo) else ({}, {}, None)
    if cutoff:
        state["knowledge_cutoff"] = cutoff
    cut = _ym(state.get("knowledge_cutoff"))
    new_described = 0
    for p, pid in people_todo:
        res = pres.get(pid)
        if res is None:
            continue                             # not sent (call limit): stays pending
        prev = _state_person(state, p) or {}
        state["people"][p.slug] = {
            "name": p.name, "keys": sorted(p.keys),
            "known": bool(res.get("known")) and not res.get("private"),
            "description": (res.get("description") or "").strip() if res.get("known") else "",
            "canonical_name": (res.get("canonical_name") or "").strip(),
            "private": bool(res.get("private")), "model": res.get("model", ""),
            "described_at": as_of.isoformat()}
        for key in ("web", "voice"):             # a lookup is cached, not repeated by --refresh
            if key in prev:
                state["people"][p.slug][key] = prev[key]
        new_described += 1
    for r in roles_todo:
        res = rres.get(r.id)
        if res is None:
            continue
        after = bool(res.get("after_cutoff")) or bool(cut and (r.date.year, r.date.month) > cut)
        prev = state["roles"].get(r.key) or {}
        state["roles"][r.key] = {
            "phrase": r.phrase, "date": r.date.isoformat(), "name": (res.get("name") or "").strip(),
            "confidence": str(res.get("confidence") or "low").lower(), "verify": after,
            "description": (res.get("description") or "").strip(), "model": res.get("model", ""),
            "resolved_at": as_of.isoformat()}
        for key in ("web", "role_check"):
            if key in prev:
                state["roles"][r.key][key] = prev[key]

    # -- web lookup: only unknown people and post-cutoff ("verify") items
    wlog = WebLog()
    if web and web_complete is not None:
        targets = select_web_targets(entries, refs, state, scope.show)
        if targets:
            progress(f"web lookup: {len(targets)} target(s), {WEB_BATCH} per call, "
                     f"max {max_web_calls} call(s)")
            run_web_lookups(targets, web_complete, max_web_calls, as_of, wlog)

    # -- role resolutions → person entries
    by_key: Dict[str, PersonEntry] = {}
    for p in entries:
        for k in p.keys:
            by_key.setdefault(k, p)
    resolved: List[Tuple[RoleRef, Dict]] = []
    role_apps: Dict[str, List[Tuple[RoleRef, Dict]]] = {}
    extra: List[PersonEntry] = []
    for r in refs:
        rec = state["roles"].get(r.key)
        if not rec:
            continue
        res = apply_web_role(rec)
        name = res.get("name") or ""
        if name and res.get("confidence") in ("high", "medium"):
            k = people_mod.name_key(name)
            p = by_key.get(k)
            if p is None:
                p = PersonEntry(slugify(name, 60), name, [], "", [], {k})
                by_key[k] = p
                extra.append(p)
                if p.slug not in state["people"] and res.get("description"):
                    if res.get("source") == "role_check":
                        rc = res["role_check"]
                        state["people"][p.slug] = {
                            "name": name, "keys": [k], "known": False, "description": "",
                            "canonical_name": "", "private": False, "model": "",
                            "described_at": rc["looked_up_at"], "from_role": True,
                            "web": {"status": "found", "name": name, "description": res["description"],
                                    "sources": [s["url"] for s in rc.get("sources") or []],
                                    "confidence": rc.get("confidence") or "low",
                                    "fallback_used": rc.get("fallback_used", False),
                                    "agrees_with_model": None, "note": "",
                                    "looked_up_at": rc["looked_up_at"], "model": rc.get("model", "")}}
                    elif res.get("source") == "web":
                        state["people"][p.slug] = {
                            "name": name, "keys": [k], "known": False, "description": "",
                            "canonical_name": "", "private": False, "model": "",
                            "described_at": res["web"]["looked_up_at"], "web": dict(res["web"]),
                            "from_role": True}
                    else:
                        state["people"][p.slug] = {
                            "name": name, "keys": [k], "known": True,
                            "description": res["description"], "canonical_name": "",
                            "private": False, "model": res.get("model", ""),
                            "described_at": res.get("resolved_at", as_of.isoformat())}
            res["slug"] = p.slug
            role_apps.setdefault(p.slug, []).append((r, res))
        resolved.append((r, res))
    all_entries = entries + extra

    recs: Dict[str, Optional[Dict]] = {}
    shown: List[PersonEntry] = []
    private = skipped
    for p in all_entries:
        rec = _state_person(state, p)
        if rec and rec.get("private"):
            private += 1
            continue
        recs[p.slug] = rec
        shown.append(p)

    voices, unidentified, not_ai = ai_voices(shown, recs, as_of)

    # -- write
    written: List[Path] = []
    problems: List[str] = []
    to_check: List[Tuple[Path, str]] = []
    for root in scope.roots:
        pdir = root / "people"
        sdir = root / "sources"
        for p in shown:
            text = render_person(p, recs.get(p.slug), pdir, role_apps.get(p.slug, []))
            if _write_if_changed(pdir / f"{p.slug}.md", text):
                written.append(pdir / f"{p.slug}.md")
            to_check.append((pdir / f"{p.slug}.md", text))
        idx = render_people_index(shown, recs, resolved, private, role_apps, scope.show,
                                  len(scope.episodes), len(scope.digests), state.get("knowledge_cutoff"))
        for name, text in (("_index.md", idx), ("_resolutions.md", render_resolutions(resolved, pdir))):
            if _write_if_changed(pdir / name, text):
                written.append(pdir / name)
            to_check.append((pdir / name, text))
        stext, sitems = sources_index(scope, sdir, as_of,
                                      render_voices(voices, unidentified, not_ai, sdir))
        if _write_if_changed(sdir / "_index.md", stext):
            written.append(sdir / "_index.md")
        to_check.append((sdir / "_index.md", stext))
    for path, text in to_check:
        problems += [f"{path}: {x}" for x in _check_links(text, path.parent)]

    # -- annotate the notes (a link to the person note in the root that holds them)
    annotated = 0
    if annotate and resolved:
        def people_dir_for(notes: Path) -> Optional[Path]:
            for root in scope.roots:
                try:
                    notes.resolve().relative_to(root.resolve())
                    return root / "people"
                except ValueError:
                    continue
            return scope.roots[0] / "people"
        for e in scope.episodes:
            annotated += annotate_notes(e, resolved, people_dir_for)

    state["last_run"] = {"calls": log.calls, "models": log.models, "people": len(shown),
                         "stopped": log.stopped or None, "web_calls": wlog.calls,
                         "web_stopped": wlog.stopped or None}
    stable = {k: v for k, v in state.items() if k != "last_run"}
    old = _load_state(state_path)
    if {k: v for k, v in old.items() if k != "last_run"} != stable or log.calls or wlog.calls:
        _write_if_changed(state_path, json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    return AnnotateResult(
        roots=scope.roots, people=len(shown), new_described=new_described, calls=log.calls,
        models=log.models,
        roles=[{"reference": r.phrase, "date": r.date.isoformat(), "name": res.get("name"),
                "confidence": res.get("confidence"), "verify": res.get("verify")} for r, res in resolved],
        skipped_private=private, sources=sitems, notes_annotated=annotated, written=written,
        stopped=log.stopped, link_problems=problems, web=_web_summary(state, wlog))


def _web_summary(state: Dict[str, Any], wlog: WebLog) -> Dict[str, Any]:
    """This run's calls + the state's totals over every eligible item:
    people = WP-P3 bio lookups, voices = quote-first tiers, roles = holder-on-date."""
    people = [r for r in state["people"].values()
              if not r.get("private") and not r.get("from_role") and ((not r.get("known")) or r.get("verify") or "web" in r)]
    roles = [r for r in state["roles"].values() if r.get("verify") or "web" in r or "role_check" in r]
    voices = [r for r in state["people"].values() if "voice" in r or _needs_voice(r)]
    def tally(recs):
        st = [(r.get("web") or {}).get("status") for r in recs]
        low = sum(1 for r in recs if (r.get("web") or {}).get("status") == "found"
                  and r["web"].get("confidence") == "low")
        return {"eligible": len(recs), "found": st.count("found") - low, "low_confidence": low,
                "not_found": st.count("not_found"), "pending": st.count(None)}
    vt = [(r.get("voice") or {}).get("tier") for r in voices]
    rt = [(r.get("role_check") or {}).get("status") for r in roles]
    return {"calls": wlog.calls, "models": wlog.models, "targets": wlog.targets,
            "sent": wlog.sent, "found": wlog.found, "not_found": wlog.not_found,
            "stopped": wlog.stopped, "people": tally(people),
            "voices": {"eligible": len(voices), "confirmed": vt.count("confirmed"),
                       "probable": vt.count("probable"), "unknown": vt.count("unknown"),
                       "pending": vt.count(None)},
            "roles": {"eligible": len(roles), "confirmed": rt.count("confirmed"),
                      "last_confirmed": rt.count("last_confirmed"), "not_found": rt.count("not_found"),
                      "pending": rt.count(None)}}


def _check_links(text: str, from_dir: Path) -> List[str]:
    from urllib.parse import unquote
    from video2mdnotes.core.package import _LINK_RE
    out = []
    for m in _LINK_RE.finditer(text):
        t = m.group(1)
        if re.match(r"^[a-z]+:", t):
            continue
        path = t.partition("#")[0]
        if path and not (from_dir / unquote(path)).exists():
            out.append(f"missing {t}")
    return out


__all__ = ["AnnotateError", "AnnotateResult", "BATCH", "MAX_WEB_CALLS", "Scope", "WEB_BATCH",
           "annotation", "apply_web_role", "collect_people", "find_role_refs", "normalize_web",
           "resolve_scope", "run", "select_web_targets"]
