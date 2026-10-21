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
# Web lookup (WP-P3) — only where model knowledge is missing or post-cutoff
# --------------------------------------------------------------------------- #

@dataclass
class WebTarget:
    kind: str                               # person | role
    rec: Dict[str, Any]                     # the state record the result is cached on
    payload: Dict[str, Any]


def _needs_web_person(rec: Optional[Dict[str, Any]]) -> bool:
    """Unknown to the model, or a model description flagged post-cutoff.
    Never: private, pending, already looked up."""
    if not rec or rec.get("private") or rec.get("pending") or "web" in rec:
        return False
    return (not rec.get("known")) or bool(rec.get("verify"))


def _needs_web_role(rec: Optional[Dict[str, Any]]) -> bool:
    return bool(rec) and bool(rec.get("verify")) and "web" not in rec


def select_web_targets(entries: Sequence[PersonEntry], refs: Sequence[RoleRef],
                       state: Dict[str, Any], show: str) -> List[WebTarget]:
    """Roles flagged "verify — after cutoff" first, then people with no
    reliable model knowledge (most appearances first; multi-word names before
    bare handles). Private / unnamed / withheld rows never get here."""
    out: List[WebTarget] = []
    for r in refs:
        rec = state["roles"].get(r.key)
        if _needs_web_role(rec):
            out.append(WebTarget("role", rec, {
                "kind": "role", "reference": r.phrase, "episode_date": r.date.isoformat(),
                "sentence": r.context, "show": show,
                "model_answer": {"name": rec.get("name", ""),
                                 "confidence": rec.get("confidence", "")}}))
    people: List[Tuple[int, int, WebTarget]] = []
    seen: set = set()
    for i, p in enumerate(entries):
        rec = _state_person(state, p)
        if not _needs_web_person(rec) or id(rec) in seen:
            continue
        seen.add(id(rec))
        payload = _person_payload(p, show, "")
        payload.pop("id")
        payload["kind"] = "person"
        if rec.get("known") and rec.get("description"):
            payload["model_description"] = rec["description"]
        people.append((0 if len(p.name.split()) > 1 else 1, i, WebTarget("person", rec, payload)))
    out += [t for _, _, t in sorted(people, key=lambda x: (x[0], x[1]))]
    return out


def _sentences(text: str, n: int = 3) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split()))
    return " ".join(parts[:n]).strip()


def normalize_web(x: Dict[str, Any], kind: str, as_of: dt.date, model: str) -> Dict[str, Any]:
    """Enforce the contract: ≤3 sources (≤6 only on fallback), real URLs,
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


@dataclass
class WebLog:
    calls: int = 0
    models: List[str] = field(default_factory=list)
    targets: int = 0
    sent: int = 0
    found: int = 0
    not_found: int = 0
    stopped: str = ""


def run_web_lookups(targets: List[WebTarget], complete: CompleteFn, max_calls: Optional[int],
                    as_of: dt.date, log: WebLog) -> None:
    """ceil(targets / WEB_BATCH) calls, capped by max_calls. Each result is
    cached on its state record as rec["web"]; a target the reply omits is not
    cached (retried next run)."""
    log.targets = len(targets)
    system = WEB_PROMPT.read_text(encoding="utf-8")
    for i in range(math.ceil(len(targets) / WEB_BATCH)):
        if max_calls is not None and log.calls >= max_calls:
            log.stopped = f"--max-web-calls {max_calls} reached"
            break
        batch = targets[i * WEB_BATCH:(i + 1) * WEB_BATCH]
        ids = {f"W{j + 1}": t for j, t in enumerate(batch)}
        user = ("TARGETS:\n" + json.dumps([{"id": k, **t.payload} for k, t in ids.items()],
                                          ensure_ascii=False, indent=1) + "\n")
        try:
            text, model = complete(system, user)
        except Exception as exc:  # noqa: BLE001 - keep finished batches
            log.stopped = f"web lookup call {i + 1} failed: {str(exc)[:200]}"
            if type(exc).__name__ in ("SubscriptionExhausted", "BudgetExhausted"):
                raise
            break
        log.calls += 1
        log.sent += len(batch)
        if model and model not in log.models:
            log.models.append(model)
        try:
            data = dg.parse_reply(text)
        except dg.DigestError as exc:
            log.stopped = f"web lookup call {i + 1}: {exc}"
            break
        for x in data.get("targets") or []:
            t = ids.get(str(x.get("id"))) if isinstance(x, dict) else None
            if t is None or "web" in t.rec:
                continue
            t.rec["web"] = normalize_web(x, t.kind, as_of, model)
            if t.rec["web"]["status"] == "found":
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


def apply_web_role(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The effective resolution: a found web answer replaces the model's
    label for that fact; "verify" is cleared only when the lookup confirms
    (same holder, or model had none; high/medium confidence). A different
    holder is a contradiction: both are shown, verify stays."""
    res = dict(rec)
    w = rec.get("web")
    if not w or w.get("status") != "found":
        return res
    model_name = (rec.get("name") or "").strip()
    same = (not model_name) or people_mod.name_key(model_name) == people_mod.name_key(w["name"])
    res.update(name=w["name"], confidence=w.get("confidence") or "low", source="web",
               description=w.get("description") or rec.get("description", ""))
    if not same:
        res["model_name"] = model_name
        res["contradiction"] = True
    res["verify"] = not (same and res["confidence"] in ("high", "medium"))
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
    w = _web_found(rec)
    if w:
        return w["description"], "web"
    if rec.get("known") and (rec.get("description") or "").strip():
        return rec["description"].strip(), "model"
    return NO_KNOWLEDGE, "none"


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
    label = {"model": f"Description — {MODEL_KNOWLEDGE}",
             "web": f"Description — {web_label(w) if w else ''}"}.get(kind, "Description")
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
    elif kind == "none" and (rec or {}).get("web"):
        wn = rec["web"]
        if wn.get("status") == "found":
            md += [f"_Possible match, {web_label(wn)} — low confidence, not adopted: "
                   f"{wn.get('description')}_", ""]
        else:
            md += [f"_Web lookup {wn.get('looked_up_at')}: not found"
                   f"{' (' + wn['note'] + ')' if wn.get('note') else ''}._", ""]
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
    w = res.get("web") or {}
    return (f"per web lookup {w.get('looked_up_at')}; sources: {web_sources_md(w)}")


def _resolution_text(r: RoleRef, res: Dict[str, Any], link: Optional[str]) -> str:
    name = (res.get("name") or "").strip()
    who = f"[{name}]({link})" if (name and link) else (name or "unresolved")
    d = r.date.isoformat()
    if res.get("source") == "web":
        if res.get("contradiction"):
            return (f"→ {who}?, as of {d}, {_web_tail(res)}; contradicts model knowledge "
                    f"({res.get('model_name')}) — verify")
        if res.get("verify"):
            return f"→ {who}?, as of {d}, {_web_tail(res)} ({res.get('confidence')} confidence) — verify"
        return f"→ {who}, as of {d}, {_web_tail(res)} ({res.get('confidence')} confidence)"
    nf = ""
    if (res.get("web") or {}).get("status") == "not_found":
        nf = f"; web lookup {res['web'].get('looked_up_at')}: not found"
    if res.get("verify"):
        return f"→ {who}?, as of {d}, per model knowledge — {VERIFY}{nf}"
    return f"→ {who}, as of {d}, per model knowledge ({res.get('confidence', 'low')} confidence){nf}"


def annotation(r: RoleRef, res: Dict[str, Any], link: Optional[str]) -> str:
    """The minimal in-notes annotation (ANNOT matches it)."""
    name = (res.get("name") or "").strip()
    who = f"[{name}]({link})" if (name and link) else (name or "unresolved")
    q = "?" if res.get("verify") and name else ""
    if res.get("source") == "web":
        tail = ""
        if res.get("contradiction"):
            tail = f"; model knowledge: {res.get('model_name')} — verify"
        elif res.get("verify"):
            tail = " — verify"
        return f" (→ {who}{q}, as of {r.date.isoformat()}, {_web_tail(res)}{tail})"
    tail = f" — {VERIFY}" if res.get("verify") else ""
    return f" (→ {who}{q}, as of {r.date.isoformat()}, per model knowledge{tail})"


_KIND_TAG = {"model": " _(model knowledge)_", "web": " _(web lookup)_"}


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
    if res.get("source") == "web":
        s = f"{_web_tail(res)}"
        if res.get("contradiction"):
            return f"{s}; contradicts model knowledge ({res.get('model_name')}) — verify"
        return s + (" — verify" if res.get("verify") else "; confirmed")
    nf = ""
    if (res.get("web") or {}).get("status") == "not_found":
        nf = f"; web lookup {res['web'].get('looked_up_at')}: not found"
    return (VERIFY if res.get("verify") else "as of episode date, per model knowledge") + nf


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


def sources_index(scope: Scope, sdir: Path, as_of: dt.date) -> Tuple[str, List[Dict[str, Any]]]:
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
    md += ["", "## All sources", "",
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
        if "web" in prev:                        # a lookup is cached, not repeated by --refresh
            state["people"][p.slug]["web"] = prev["web"]
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
        if "web" in prev:
            state["roles"][r.key]["web"] = prev["web"]

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
                    if res.get("source") == "web":
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
        stext, sitems = sources_index(scope, sdir, as_of)
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
    """This run's calls + the state's totals over every eligible item."""
    people = [r for r in state["people"].values()
              if not r.get("private") and not r.get("from_role") and ((not r.get("known")) or r.get("verify") or "web" in r)]
    roles = [r for r in state["roles"].values() if r.get("verify") or "web" in r]
    def tally(recs):
        st = [(r.get("web") or {}).get("status") for r in recs]
        low = sum(1 for r in recs if (r.get("web") or {}).get("status") == "found"
                  and r["web"].get("confidence") == "low")
        return {"eligible": len(recs), "found": st.count("found") - low, "low_confidence": low,
                "not_found": st.count("not_found"), "pending": st.count(None)}
    return {"calls": wlog.calls, "models": wlog.models, "targets": wlog.targets,
            "sent": wlog.sent, "found": wlog.found, "not_found": wlog.not_found,
            "stopped": wlog.stopped, "people": tally(people), "roles": tally(roles)}


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
