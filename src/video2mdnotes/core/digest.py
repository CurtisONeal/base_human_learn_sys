"""Span digests: a week or a month of episodes, from EXISTING per-episode notes.

    digest --collection ai_daily_brief --by week [--since --until] [--out DIR]

No transcript is read and nothing is re-transcribed: the input is each
episode's finished notes (a learning package's ``notes.md`` or a run dir's
``summaries/*.summary.md``) plus its ``source_meta.json``.

Design (see also design/DIGESTS.md):

**Grouping.** By the episode's ``published`` date (frontmatter, else
source_meta ``published_at``). Weeks are ISO weeks, Monday–Sunday, named
``YYYY-Www``; months are calendar months, ``YYYY-MM``. The pipeline stores
the publish date as a date without a time (YouTube's upload date), so it is
used as-is as the America/New_York calendar day; a full timestamp, when one
is present, is converted to America/New_York first.

**What the model does — and what it does not.** ONE call per span writes the
parts that need judgement: title, overview, top stories (with the time each
episode spent on them), threads across days, the span's relevance of each
person, superseded time-sensitive claims (as id pairs), what changed since
the previous digest, open questions. Everything else is deterministic, from
the notes: the episode table, the People table (merged with
``core.people.merge_people`` across episodes), key terms new this span, the
time-sensitive claims table, the ranking of top stories (episodes covering
the story, then minutes spent), and every link.

**Citations.** The model cites ``E<n> H:MM:SS``. Each citation is checked
deterministically, like eval P2: the label must be an episode of the span,
the time must be within ``CITE_TOLERANCE_S`` of a timestamp that episode's
notes actually cite, and inside the recording. An invalid citation is
dropped; an item left with no valid citation is dropped (no uncited
claims); both are counted in the frontmatter and listed in the
``Citation check`` section. Valid citations become ``youtu.be/<id>?t=``
links; every episode is linked to its notes file. After writing, every
relative link in the digest is resolved (file exists, ``#heading`` matches)
and every YouTube link is checked against the span's episodes and their
durations — ``link_check`` in the frontmatter.

**Budget / map-reduce.** Each episode is packed compactly (summaries,
special attention, people, key-term names, time-sensitive claims; capped
per episode). If a span's packed input exceeds ``MAX_INPUT_CHARS`` it is
split into consecutive sub-spans (map: one partial digest each, same
schema, global ``E`` labels) and one reduce call merges the partials.

**Months are hierarchical.** A month is mapped over its ISO weeks (each
clipped to the month). A week digest already built whose episode set equals
the clipped week is reused as that week's partial at no cost; any other
week is computed as a partial and cached. One reduce call then writes the
month. Why: a month of a daily show (20–30 episodes × ~25k chars of notes)
never fits one call, the week digests usually exist already, and building
the month from them keeps the week and month views consistent. The
deterministic parts (People, key terms, claims) are still built from the
episode notes, never from the week digests.

**Idempotent.** Each call's reply is cached under
``<out>/.digest_cache/<key>.json``, keyed by a hash of the exact prompt
(system + user), which contains the notes of every episode in the span and
the previous digest. A rerun with the same episode set makes no call and
rewrites nothing; a new or changed episode changes the hash and re-calls.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core import cited_sources, notes_format, people as people_mod
from video2mdnotes.core.package import (_LINK_RE, anchor_matches, link_path, obsidian_anchor,
                                        youtube_id)

PROMPT_DIR = BASE_DIR / "prompts" / "digest"
SPAN_PROMPT = PROMPT_DIR / "span.md"
REDUCE_FRAGMENT = PROMPT_DIR / "reduce.md"
GENERATOR = "video2mdnotes.digest/1"
CACHE_DIRNAME = ".digest_cache"
CACHE_VERSION = 1
MAX_INPUT_CHARS = 120_000
EPISODE_MAX_CHARS = 16_000
CITE_TOLERANCE_S = 30
TOP_STORIES_MAX = 8
PEOPLE_MAX = 25
SOURCES_MAX = 30
TERMS_MAX = 40

CompleteFn = Callable[[str, str], Tuple[str, str]]

_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
_LINKED_CITE = re.compile(r"\[(?P<body>[^\[\]\n]{1,40})\]\((?:https?://youtu\.be|https?://www\.youtube\.com)[^)\s]*\)")
_TIME_IN = re.compile(_TS)
_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_EP_CITE = re.compile(rf"^\s*E(?P<n>\d+)\s*[,:@]?\s*\[?(?P<t>{_TS})\]?\s*$")
_TERM = re.compile(r"^\s*[-*]\s+(?:`(?P<a>[^`]+)`|\*\*(?P<b>[^*]+)\*\*)\s*[-—–:]\s*(?P<def>.*)$")
_CITE_ANY = re.compile(r"\[[^\[\]\n]+\]")
# annotate-people's in-notes role annotation: "per model knowledge" or, after a
# web lookup (WP-P3), "per web lookup <date>; sources: …".
_ANNOTATION = re.compile(r"\s\(→ (?:[^()\n]|\([^()\n]*\))*?per (?:model knowledge|web lookup)(?:[^()\n]|\([^()\n]*\))*?\)")


class DigestError(RuntimeError):
    """The digest cannot be built (no episodes, unusable model reply...)."""


# --------------------------------------------------------------------------- #
# Episodes
# --------------------------------------------------------------------------- #

def parse_ts(text: str) -> Optional[float]:
    m = _TIME_IN.search(text or "")
    if not m:
        return None
    parts = [int(p) for p in m.group(0).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, mi, s = parts
    return float(h * 3600 + mi * 60 + s)


def hms(seconds: Optional[float]) -> str:
    s = int(max(0.0, seconds or 0.0))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm: Dict[str, str] = {}
    for line in text[3:end].split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"')
    return fm, text[end + 4:].lstrip("\n")


def unlink_citations(text: str) -> str:
    """`[0:13:29](https://youtu.be/…?t=809)` → `[0:13:29]` (package notes back to plain)."""
    return _LINKED_CITE.sub(lambda m: f"[{m.group('body')}]", text or "")


def _sections(body: str) -> List[Tuple[int, str, str, int]]:
    """[(level, title, text, line_no)] — text runs to the next heading of <= level."""
    lines = body.split("\n")
    heads = []
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (m := _HEAD.match(line)):
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for k, (i, lv, t) in enumerate(heads):
        end = next((j for j, l2, _ in heads[k + 1:] if l2 <= lv), len(lines))
        out.append((lv, t, "\n".join(lines[i + 1:end]).strip("\n"), i))
    return out


def _to_date(value: Any) -> Optional[dt.date]:
    """A publish date as the America/New_York calendar day."""
    if not value:
        return None
    s = str(value).strip().strip('"')
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return dt.date.fromisoformat(s)
    try:
        ts = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.date()
    from zoneinfo import ZoneInfo
    return ts.astimezone(ZoneInfo("America/New_York")).date()


@dataclass
class Term:
    name: str
    definition: str
    cites: List[str]


@dataclass
class Episode:
    notes_path: Path
    title: str
    published: dt.date
    youtube_id: Optional[str]
    url: Optional[str]
    duration_s: Optional[float]
    body: str                         # notes body, citations unlinked
    source_id: str = ""
    label: str = ""                   # E1, E2 … within one span
    timestamps: List[float] = field(default_factory=list)
    people_rows: List[people_mod.PersonRow] = field(default_factory=list)
    source_rows: List[cited_sources.SourceRow] = field(default_factory=list)
    terms: List[Term] = field(default_factory=list)
    claims: List[notes_format.ClaimRow] = field(default_factory=list)
    sha: str = ""

    @property
    def key(self) -> str:
        return self.youtube_id or self.source_id or self.notes_path.parent.name

    def section_texts(self, title: str) -> List[str]:
        return [t for lv, h, t, _ in _sections(self.body)
                if _bare(h).lower() == title.lower() and t.strip()]

    def cite_ok(self, t: float) -> bool:
        if self.duration_s and t > self.duration_s + 1:
            return False
        return any(abs(t - x) <= CITE_TOLERANCE_S for x in self.timestamps)

    def youtube_link(self, t: float) -> Optional[str]:
        return f"https://youtu.be/{self.youtube_id}?t={int(t)}" if self.youtube_id else None


def _bare(heading: str) -> str:
    """Heading text without a trailing [time range](link) or [time range]."""
    h = re.sub(r"\s*\[[^\]]*\](\([^)]*\))?\s*$", "", heading or "")
    return h.strip()


def load_episode(notes_path: Path) -> Optional[Episode]:
    notes_path = Path(notes_path)
    raw = notes_path.read_text(encoding="utf-8")
    fm, body = _frontmatter(raw)
    meta: Dict[str, Any] = {}
    for cand in (notes_path.parent / "source_meta.json",
                 notes_path.parent.parent / "source_meta.json"):
        if cand.exists():
            try:
                meta = json.loads(cand.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
            break
    src = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    url = fm.get("source") or meta.get("url") or src.get("url")
    yid = youtube_id(url) or src.get("youtube_id")
    published = _to_date(fm.get("published") or meta.get("published_at") or src.get("published_at")
                         or fm.get("as_of"))
    if published is None:
        return None
    try:
        duration = float(fm.get("duration_s") or meta.get("duration_s") or src.get("duration_s") or 0) or None
    except ValueError:
        duration = None
    # annotate-people's "(→ name, as of …, per model knowledge)" notes are not
    # the source's words: stripped, so the digest input (and cache key) stays
    # what the episode said.
    body = _ANNOTATION.sub("", unlink_citations(body))
    ep = Episode(notes_path=notes_path, title=fm.get("title") or meta.get("title") or notes_path.stem,
                 published=published, youtube_id=yid, url=url, duration_s=duration, body=body,
                 source_id=str(meta.get("source_id") or yid or ""),
                 sha=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])
    ep.timestamps = sorted({t for m in _CITE_ANY.finditer(body)
                            for t in [parse_ts(m.group(0))] if t is not None})
    for lv, h, text, _ in _sections(body):
        bare = _bare(h).lower()
        if bare == "people" and lv == 2:
            ep.people_rows.extend(people_mod.parse_rows(text))
        elif bare == cited_sources.HEADING.lower() and lv == 2:
            ep.source_rows.extend(cited_sources.parse_rows(text))
        elif bare.startswith("key concepts"):
            for line in text.split("\n"):
                m = _TERM.match(line)
                if m:
                    name = (m.group("a") or m.group("b") or "").strip()
                    d = m.group("def")
                    ep.terms.append(Term(name, _CITE_ANY.sub("", d).strip(" ;,"),
                                         _CITE_ANY.findall(d)))
    ep.claims = notes_format.parse_time_sensitive(body)
    return ep


def find_notes(collection: Path) -> List[Path]:
    """Episode notes under `collection`: package dirs (`*/notes.md`) and run
    dirs (`*/summaries/*.summary.md`), one level or two deep. `digests/` and
    hidden dirs are skipped."""
    collection = Path(collection)
    out: List[Path] = []
    for d in sorted(p for p in collection.rglob("*") if p.is_dir()):
        rel = d.relative_to(collection).parts
        if any(x.startswith((".", "_")) or x == "digests" for x in rel) or len(rel) > 3:
            continue
        if (d / "notes.md").exists() and (d / "source_meta.json").exists():
            out.append(d / "notes.md")
        elif d.name == "summaries" and (d.parent / "source_meta.json").exists():
            out.extend(c for c in sorted(d.glob("*.summary.md")) if not c.name.startswith("no_summary_"))
    return out


def load_collection(collection: Path) -> List[Episode]:
    eps: Dict[str, Episode] = {}
    for p in find_notes(collection):
        ep = load_episode(p)
        if ep is None:
            continue
        # One notes file per episode: a package wins over a run dir.
        if ep.key not in eps or p.name == "notes.md":
            eps[ep.key] = ep
    return sorted(eps.values(), key=lambda e: (e.published, e.title))


# --------------------------------------------------------------------------- #
# Spans
# --------------------------------------------------------------------------- #

@dataclass
class Span:
    kind: str                         # week | month
    key: str                          # 2026-W43 | 2026-10
    start: dt.date
    end: dt.date
    episodes: List[Episode] = field(default_factory=list)


def week_key(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(key: str) -> Tuple[dt.date, dt.date]:
    y, w = key.split("-W")
    start = dt.date.fromisocalendar(int(y), int(w), 1)
    return start, start + dt.timedelta(days=6)


def month_bounds(key: str) -> Tuple[dt.date, dt.date]:
    y, m = (int(x) for x in key.split("-"))
    start = dt.date(y, m, 1)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def span_key(kind: str, d: dt.date) -> str:
    return week_key(d) if kind == "week" else f"{d.year}-{d.month:02d}"


def group(episodes: Sequence[Episode], kind: str, since: Optional[dt.date] = None,
          until: Optional[dt.date] = None) -> List[Span]:
    if kind not in ("week", "month"):
        raise ValueError("--by must be week or month")
    spans: Dict[str, Span] = {}
    for ep in episodes:
        if (since and ep.published < since) or (until and ep.published > until):
            continue
        k = span_key(kind, ep.published)
        if k not in spans:
            a, b = week_bounds(k) if kind == "week" else month_bounds(k)
            spans[k] = Span(kind, k, a, b)
        spans[k].episodes.append(ep)
    out = sorted(spans.values(), key=lambda s: s.start)
    for s in out:
        s.episodes.sort(key=lambda e: (e.published, e.title))
    return out


def previous_key(kind: str, key: str) -> str:
    a, _ = week_bounds(key) if kind == "week" else month_bounds(key)
    return span_key(kind, a - dt.timedelta(days=1))


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rsplit("\n", 1)[0] + "\n[…]"


def pack_episode(ep: Episode, max_chars: int = EPISODE_MAX_CHARS) -> str:
    """One episode for the prompt: its label, date, title, then the notes'
    summaries, special attention, people and key-term names (claims are listed
    separately with ids)."""
    head = (f"=== {ep.label} · {ep.published.isoformat()} · \"{ep.title}\" · "
            f"duration {hms(ep.duration_s)} ===")
    parts = []
    summ = ep.section_texts("Summary")
    if summ:
        parts.append("SUMMARY:\n" + "\n".join(summ))
    sa = ep.section_texts("Special Attention")
    if sa:
        parts.append("SPECIAL ATTENTION:\n" + "\n".join(sa))
    if ep.people_rows:
        parts.append("PEOPLE:\n" + "\n".join(
            f"- {r.name} — {r.role}; {r.background}; {r.relevance} {' '.join(r.cites[:3])}".strip()
            for r in ep.people_rows))
    if ep.terms:
        parts.append("KEY TERMS: " + "; ".join(t.name for t in ep.terms))
    body = "\n\n".join(parts)
    return head + "\n" + _cap(body, max_chars - len(head) - 1)


@dataclass
class ClaimRef:
    id: str
    episode: Episode
    row: notes_format.ClaimRow


def number_claims(span: Span) -> List[ClaimRef]:
    out = []
    for ep in span.episodes:
        for row in ep.claims:
            out.append(ClaimRef(f"C{len(out) + 1}", ep, row))
    return out


def claims_block(claims: Sequence[ClaimRef]) -> str:
    if not claims:
        return "TIME-SENSITIVE CLAIMS: none"
    return "TIME-SENSITIVE CLAIMS (id · episode · as-of · claim):\n" + "\n".join(
        f"{c.id} · {c.episode.label} {c.row.timestamp.strip('[]')} · {c.row.as_of} · "
        f"{' '.join(c.row.claim.split())[:240]}" for c in claims)


def span_header(span: Span, episodes: Sequence[Episode]) -> str:
    return (f"SPAN: {span.kind} {span.key} ({span.start.isoformat()} – {span.end.isoformat()}), "
            f"{len(episodes)} episode(s): "
            + ", ".join(f"{e.label} = {e.published.isoformat()} \"{e.title}\"" for e in episodes))


def previous_block(prev: Optional[Dict[str, Any]]) -> str:
    if not prev:
        return ""
    d = prev.get("digest") or {}
    lines = [f"PREVIOUS DIGEST ({prev.get('span')}) — context for `what_changed` only; "
             "never cite it:",
             f"title: {d.get('title', '')}"]
    for s in (d.get("top_stories") or [])[:TOP_STORIES_MAX]:
        lines.append(f"- story: {s.get('headline', '')} — {s.get('summary', '')}")
    for q in (d.get("open_questions") or [])[:6]:
        lines.append(f"- open question: {q.get('text', '')}")
    return "\n".join(lines)


def render_user(span: Span, episodes: Sequence[Episode], claims: Sequence[ClaimRef],
                prev: Optional[Dict[str, Any]], partials: Optional[Sequence[Dict]] = None) -> str:
    blocks = [span_header(span, episodes)]
    pb = previous_block(prev)
    if pb:
        blocks.append(pb)
    if partials is None:
        blocks += [pack_episode(e) for e in episodes]
    else:
        blocks.append("PARTIAL DIGESTS (one per sub-span; their citations already use the "
                      "span's episode labels):")
        blocks += [json.dumps(p, ensure_ascii=False) for p in partials]
    blocks.append(claims_block(claims))
    return "\n\n".join(blocks) + "\n"


def system_prompt(reduce: bool = False) -> str:
    s = SPAN_PROMPT.read_text(encoding="utf-8").rstrip() + "\n"
    if reduce:
        s += "\n" + REDUCE_FRAGMENT.read_text(encoding="utf-8").rstrip() + "\n"
    return s


# --------------------------------------------------------------------------- #
# Model call + cache
# --------------------------------------------------------------------------- #

def parse_reply(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        raise DigestError("reply has no JSON object")
    try:
        data = json.loads(t[a:b + 1])
    except ValueError as exc:
        raise DigestError(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise DigestError("reply is not a JSON object")
    return data


@dataclass
class CallStats:
    calls: int = 0
    cached: int = 0
    models: List[str] = field(default_factory=list)


def cached_call(cache_dir: Path, name: str, system: str, user: str,
                complete: Optional[CompleteFn], stats: CallStats) -> Dict[str, Any]:
    key = hashlib.sha256(f"v{CACHE_VERSION}\n{system}\n{user}".encode("utf-8")).hexdigest()[:16]
    path = cache_dir / f"{name}.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("input_sha256") == key and isinstance(data.get("digest"), dict):
                stats.cached += 1
                if data.get("model") and data["model"] not in stats.models:
                    stats.models.append(data["model"])
                return data
        except (OSError, ValueError):
            pass
    if complete is None:
        raise DigestError(f"{name}: not cached and no model given")
    reply, model = complete(system, user)
    stats.calls += 1
    if model and model not in stats.models:
        stats.models.append(model)
    digest = parse_reply(reply)
    data = {"version": CACHE_VERSION, "name": name, "input_sha256": key, "model": model,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "user_chars": len(user), "digest": digest, "raw_reply": reply}
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return data


def _chunks(episodes: Sequence[Episode], budget: int) -> List[List[Episode]]:
    out: List[List[Episode]] = [[]]
    size = 0
    for e in episodes:
        n = len(pack_episode(e)) + 2
        if out[-1] and size + n > budget:
            out.append([])
            size = 0
        out[-1].append(e)
        size += n
    return out


# --------------------------------------------------------------------------- #
# Validation (deterministic citation check)
# --------------------------------------------------------------------------- #

@dataclass
class Check:
    total: int = 0
    valid: int = 0
    dropped_cites: List[str] = field(default_factory=list)
    dropped_items: List[str] = field(default_factory=list)


def resolve_cite(raw: Any, by_label: Dict[str, Episode]) -> Optional[Tuple[Episode, float]]:
    m = _EP_CITE.match(str(raw or ""))
    if not m:
        return None
    ep = by_label.get(f"E{int(m.group('n'))}")
    t = parse_ts(m.group("t"))
    if ep is None or t is None or not ep.cite_ok(t):
        return None
    return ep, t


def _valid_cites(cites: Any, by_label: Dict[str, Episode], chk: Check, where: str
                 ) -> List[Tuple[Episode, float]]:
    out: List[Tuple[Episode, float]] = []
    for c in cites if isinstance(cites, list) else [cites] if cites else []:
        chk.total += 1
        r = resolve_cite(c, by_label)
        if r is None:
            chk.dropped_cites.append(f"{where}: {c}")
        elif r not in out:
            chk.valid += 1
            out.append(r)
    return out


def _text(v: Any) -> str:
    return " ".join(str(v or "").split())


@dataclass
class Story:
    headline: str
    summary: str
    cites: List[Tuple[Episode, float]]
    seconds: Dict[str, float]          # episode label -> seconds spent

    @property
    def episodes(self) -> List[str]:
        return sorted({e.label for e, _ in self.cites} | set(self.seconds),
                      key=lambda x: int(x[1:]))


def validate(digest: Dict[str, Any], span: Span, claims: Sequence[ClaimRef]
             ) -> Tuple[Dict[str, Any], Check]:
    """Keep only items with at least one valid citation; resolve citations."""
    by_label = {e.label: e for e in span.episodes}
    chk = Check()
    out: Dict[str, Any] = {"title": _text(digest.get("title")) or f"{span.kind} {span.key}"}

    def items(key: str, text_key: str = "text") -> List[Tuple[str, List]]:
        res = []
        for i, it in enumerate(digest.get(key) or []):
            if not isinstance(it, dict):
                continue
            cs = _valid_cites(it.get("cites"), by_label, chk, f"{key}[{i}]")
            txt = _text(it.get(text_key))
            if txt and cs:
                res.append((txt, cs))
            elif txt:
                chk.dropped_items.append(f"{key}: {txt[:80]}")
        return res
    out["overview"] = items("overview")
    out["what_changed"] = items("what_changed")
    out["open_questions"] = items("open_questions")

    stories: List[Story] = []
    for i, s in enumerate(digest.get("top_stories") or []):
        if not isinstance(s, dict):
            continue
        cs = _valid_cites(s.get("cites"), by_label, chk, f"top_stories[{i}]")
        secs: Dict[str, float] = {}
        for seg in s.get("segments") or []:
            if not isinstance(seg, dict):
                continue
            ep = by_label.get(str(seg.get("episode", "")).strip())
            a, b = parse_ts(str(seg.get("from", ""))), parse_ts(str(seg.get("to", "")))
            if ep is None or a is None or b is None or b < a:
                continue
            if ep.duration_s:
                b = min(b, ep.duration_s)
            if ep.cite_ok(a):
                secs[ep.label] = secs.get(ep.label, 0.0) + max(0.0, b - a)
        if _text(s.get("headline")) and cs:
            stories.append(Story(_text(s.get("headline")), _text(s.get("summary")), cs, secs))
        elif _text(s.get("headline")):
            chk.dropped_items.append(f"top_stories: {_text(s.get('headline'))[:80]}")
    stories.sort(key=lambda st: (-len(st.episodes), -sum(st.seconds.values()), -len(st.cites)))
    out["top_stories"] = stories[:TOP_STORIES_MAX]

    threads = []
    for i, th in enumerate(digest.get("threads") or []):
        if not isinstance(th, dict):
            continue
        devs = []
        for j, d in enumerate(th.get("developments") or []):
            if not isinstance(d, dict):
                continue
            cs = _valid_cites(d.get("cites"), by_label, chk, f"threads[{i}].developments[{j}]")
            if _text(d.get("text")) and cs:
                devs.append((_text(d.get("text")), cs))
            elif _text(d.get("text")):
                chk.dropped_items.append(f"thread development: {_text(d.get('text'))[:80]}")
        devs.sort(key=lambda x: (min(e.published for e, _ in x[1]), min(int(e.label[1:]) for e, _ in x[1])))
        latest = th.get("latest") if isinstance(th.get("latest"), dict) else {}
        lcs = _valid_cites(latest.get("cites"), by_label, chk, f"threads[{i}].latest")
        lat = (_text(latest.get("text")), lcs) if _text(latest.get("text")) and lcs else None
        if len(devs) + (1 if lat else 0) >= 2 and _text(th.get("thread")):
            threads.append((_text(th.get("thread")), devs, lat))
        elif _text(th.get("thread")):
            chk.dropped_items.append(f"thread (fewer than 2 cited points): {_text(th.get('thread'))[:80]}")
    out["threads"] = threads

    rel: Dict[str, Tuple[str, List]] = {}
    for i, p in enumerate(digest.get("people") or []):
        if not isinstance(p, dict):
            continue
        cs = _valid_cites(p.get("cites"), by_label, chk, f"people[{i}]")
        if _text(p.get("name")) and _text(p.get("relevance")) and cs:
            rel[people_mod.name_key(_text(p.get("name")))] = (_text(p.get("relevance")), cs)
    out["people_relevance"] = rel

    ids = {c.id: c for c in claims}
    sup = []
    for p in digest.get("superseded") or []:
        if not isinstance(p, dict):
            continue
        e, lt = ids.get(str(p.get("earlier", "")).strip()), ids.get(str(p.get("later", "")).strip())
        kind = str(p.get("kind") or "").strip().lower().replace("_", " ")
        if kind not in ("superseded", "partly contradicted", "discrepancy"):
            kind = "discrepancy"
        if e is None or lt is None or e.episode is lt.episode:
            chk.dropped_items.append(f"superseded pair {p.get('earlier')}→{p.get('later')} (invalid)")
            continue
        if (e.episode.published, int(e.episode.label[1:])) > (lt.episode.published, int(lt.episode.label[1:])):
            e, lt = lt, e
        sup.append((e, lt, kind, _text(p.get("resolution"))))
    out["superseded"] = sup
    return out, chk


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _rel(target: Path, from_dir: Path) -> str:
    return link_path(os.path.relpath(target, from_dir))


def section_anchor(ep: Episode, t: float) -> str:
    """`#Heading` of the notes section (level 2, plain-text heading with a
    time range) that contains time t — '' when headings carry links (packages)
    or no range matches."""
    # The headings of the FILE as written (ep.body has its citations unlinked,
    # so a package heading "Window 1 [0:00–0:25](https://…)" would look plain
    # there and yield an anchor that matches nothing in the real file).
    raw = ep.notes_path.read_text(encoding="utf-8")
    for lv, h, _, _ in _sections(_frontmatter(raw)[1]):
        if lv != 2 or "](" in h:
            continue
        m = re.search(rf"\[({_TS})\s*[–-]\s*({_TS})\]", h)
        if m:
            a, b = parse_ts(m.group(1)), parse_ts(m.group(2))
            if a is not None and b is not None and a <= t <= b:
                return "#" + obsidian_anchor(h)
    return ""


def fmt_cites(cites: Sequence[Tuple[Episode, float]]) -> str:
    out = []
    for ep, t in cites:
        link = ep.youtube_link(t)
        label = f"{ep.label} {hms(t)}"
        out.append(f"[{label}]({link})" if link else f"[{label}]")
    return " ".join(out)


def _q(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _clean_cell(s: str) -> str:
    return notes_format.sanitize_cell(s)


def render(span: Span, v: Dict[str, Any], chk: Check, claims: Sequence[ClaimRef],
           out_path: Path, *, stats: CallStats, as_of: dt.date, partial: bool,
           coverage: Optional[int], prev_path: Optional[Path], show: str, built_at: str,
           input_sha: str, prior_terms: set) -> str:
    out_dir = out_path.parent
    eps = span.episodes
    title = v["title"]
    span_label = ("week " if span.kind == "week" else "") + span.key
    fm = ["---",
          f"title: {_q(f'{show} — {span_label}: {title}')}",
          f"span: {_q(span.key)}",
          f"span_kind: {span.kind}",
          f"span_start: {span.start.isoformat()}",
          f"span_end: {span.end.isoformat()}",
          f"partial: {'true' if partial else 'false'}",
          f"as_of: {as_of.isoformat()}",
          f"episodes: {len(eps)}",
          "episode_ids: [" + ", ".join(_q(e.key) for e in eps) + "]",
          f"episodes_listed_in_index: {coverage if coverage is not None else 'null'}",
          f"model_calls: {stats.calls + stats.cached}",
          f"model_calls_this_run: {stats.calls}",
          "models: [" + ", ".join(_q(m) for m in stats.models) + "]",
          f"input_sha256: {_q(input_sha)}",
          f"citations: {chk.valid}",
          f"citations_dropped: {len(chk.dropped_cites)}",
          f"items_dropped: {len(chk.dropped_items)}",
          "link_check: LINKCHECK",
          f"built_at: {built_at}",
          f"generated_by: {GENERATOR}",
          "tags: [digest]",
          "---"]
    md = [f"# {show} — {span_label} ({span.start:%a %Y-%m-%d} – {span.end:%a %Y-%m-%d}): {title}", ""]
    if partial:
        md += [f"> [!warning] Partial {span.kind}: as of {as_of.isoformat()} the span is not over"
               + (f", and only {len(eps)} of {coverage} episodes listed in the index have notes"
                  if coverage and coverage > len(eps) else "")
               + ". Rerun `digest` when the span is complete.", ""]
    md += [f"> Built from {len(eps)} episode note(s), not transcripts · {stats.calls + stats.cached} "
           f"model call(s) · every statement cites an episode (`E<n> H:MM:SS` → YouTube) · "
           f"generated by {GENERATOR}", ""]

    md += ["## Episodes", "", "| # | date | title | duration | notes | YouTube |", "|---|---|---|---:|---|---|"]
    for e in eps:
        notes_link = f"[notes]({_rel(e.notes_path, out_dir)})"
        yt = f"[watch](https://youtu.be/{e.youtube_id})" if e.youtube_id else "-"
        md.append(f"| {e.label} | {e.published.isoformat()} | {_clean_cell(e.title)} | "
                  f"{hms(e.duration_s)} | {notes_link} | {yt} |")
    md.append("")

    md += ["## Overview", ""]
    md += [f"- {t} {fmt_cites(cs)}" for t, cs in v["overview"]] or ["- (no cited overview)"]
    md.append("")

    md += ["## Top stories", "",
           "_Ranked by how many episodes covered the story, then by the time spent on it._", ""]
    for i, st in enumerate(v["top_stories"], 1):
        cover = ", ".join(f"{lab} ({hms(st.seconds[lab])[2:] if lab in st.seconds else 'cited'})"
                          for lab in st.episodes)
        by_label = {e.label: e for e in eps}
        first_t: Dict[str, float] = {}
        for e, t in st.cites:
            first_t.setdefault(e.label, t)
        notes = " · ".join(
            f"[{lab} notes]({_rel(by_label[lab].notes_path, out_dir)}"
            f"{section_anchor(by_label[lab], first_t.get(lab, 0.0))})" for lab in st.episodes)
        md += [f"{i}. **{st.headline}** — {st.summary} {fmt_cites(st.cites)}",
               f"   - Covered in: {cover} · {notes}"]
    if not v["top_stories"]:
        md.append("- (no cited stories)")
    md.append("")

    md += ["## Threads & trends", ""]
    if not v["threads"]:
        md += ["- No story developed across more than one episode in this span.", ""]
    for name, devs, lat in v["threads"]:
        md += [f"### {name}", ""]
        for text, cs in devs:
            d = min(e.published for e, _ in cs)
            md.append(f"- {d.isoformat()} ({', '.join(dict.fromkeys(e.label for e, _ in cs))}): {text} {fmt_cites(cs)}")
        if lat:
            md.append(f"- **Latest:** {lat[0]} {fmt_cites(lat[1])}")
        md.append("")

    # People: deterministic merge across episodes, relevance-this-span from the model.
    rows: List[people_mod.PersonRow] = []
    order = 0
    for e in eps:
        for r in e.people_rows:
            cites = [f"{e.label} {hms(parse_ts(c))}" for c in r.cites if parse_ts(c) is not None]
            rows.append(people_mod.PersonRow(r.name, r.role, r.background, r.relevance, cites, order))
            order += 1
    merged = people_mod.merge_people(rows, asr=True, timed=False)
    rel = v["people_relevance"]
    md += [f"## People of the {span.kind}", "",
           "_Merged across episodes (one row per person). Background is only what the episodes "
           "state; relevance is this span's, cited._", ""]
    if not merged:
        md += ["- None identified in the episodes.", ""]
    else:
        md += ["| person | role | affiliation & background (as stated) | relevance this span | episodes | citations |",
               "|---|---|---|---|---|---|"]
        by_label = {e.label: e for e in eps}
        scored = []
        for p in merged:
            keys = {people_mod.name_key(n) for n in p.names} | {people_mod.name_key(p.shown)}
            hit = next((rel[k] for k in keys if k in rel), None)
            labs = sorted({c.split()[0] for c in p.cites}, key=lambda x: int(x[1:]))
            scored.append((0 if people_mod.role_rank(p.role) == 3 else 1, -len(labs),
                           0 if hit else 1, p, hit, labs))
        scored.sort(key=lambda x: x[:3])
        for _, _, _, p, hit, labs in scored[:PEOPLE_MAX]:
            hedged = any("asr" in n.lower() for r in p.rows for n in [r.name])
            name = p.shown + (" (ASR spelling)" if hedged and "(" not in p.shown else "")
            if hit:
                relevance = f"{hit[0]} {fmt_cites(hit[1])}"
            else:
                relevance = p.relevance or "-"
            cites = []
            for c in p.cites[:6]:
                r = resolve_cite(c, by_label)
                if r:
                    cites.append(r)
            md.append("| " + " | ".join(_clean_cell(x) for x in (
                name, p.role, p.background, relevance, ", ".join(labs), fmt_cites(cites) or "-")) + " |")
        if len(scored) > PEOPLE_MAX:
            md.append(f"\n_{len(scored) - PEOPLE_MAX} more people appear in the episode notes._")
        md.append("")

    md += render_sources(span, eps, out_dir)

    # Key terms new this span.
    seen: Dict[str, Tuple[Term, Episode]] = {}
    counts: Dict[str, int] = {}
    for e in eps:
        for t in e.terms:
            k = re.sub(r"[^a-z0-9]+", " ", t.name.lower()).strip()
            if not k or k in prior_terms:
                continue
            counts[k] = counts.get(k, 0) + 1
            seen.setdefault(k, (t, e))
    md += ["## Key terms new this span", "",
           "_Terms defined in this span's episode notes and in no earlier episode of the collection._", ""]
    if not seen:
        md += ["- None.", ""]
    else:
        md += ["| term | definition (as in the first episode) | first in | episodes | citation |",
               "|---|---|---|---:|---|"]
        ranked = sorted(seen.items(), key=lambda kv: (-counts[kv[0]], kv[1][1].published, kv[1][0].name.lower()))
        for k, (t, e) in ranked[:TERMS_MAX]:
            c = [(e, parse_ts(x)) for x in t.cites[:1] if parse_ts(x) is not None]
            md.append("| " + " | ".join(_clean_cell(x) for x in (
                f"`{t.name}`", t.definition[:240], e.label, str(counts[k]), fmt_cites(c) or "-")) + " |")
        if len(ranked) > TERMS_MAX:
            md.append(f"\n_{len(ranked) - TERMS_MAX} more new terms in the episode notes._")
        md.append("")

    # Time-sensitive claims, with cross-episode superseded marking.
    notes_for: Dict[str, str] = {}
    for e_ref, l_ref, kind, res in v["superseded"]:
        t = parse_ts(l_ref.row.timestamp)
        ptr = fmt_cites([(l_ref.episode, t)]) if t is not None else l_ref.episode.label
        tgt = l_ref if kind == "discrepancy" else e_ref
        other = e_ref if kind == "discrepancy" else l_ref
        if kind == "discrepancy":
            t2 = parse_ts(other.row.timestamp)
            ptr = fmt_cites([(other.episode, t2)]) if t2 is not None else other.episode.label
        verb = "later in the span" if kind != "discrepancy" else "elsewhere in the span"
        notes_for[tgt.id] = (f" _({kind} {verb}: see {ptr} — {res})_" if res
                             else f" _({kind} {verb}: see {ptr})_")
    md += ["## Time-sensitive claims", "",
           "_Every dated claim from the episode notes. A claim a later episode updates is marked "
           "`(superseded)` and points to the update._", ""]
    if not claims:
        md += ["- None.", ""]
    else:
        md += ["| claim | episode | as-of | type | verify-by |", "|---|---|---|---|---|"]
        for c in claims:
            t = parse_ts(c.row.timestamp)
            claim = " ".join(c.row.claim.split())
            note = notes_for.get(c.id, "")
            if note and "superseded" in note.split(" ", 2)[1] and not claim.lower().startswith("(superseded)"):
                claim = "(superseded) " + claim
            md.append("| " + " | ".join(_clean_cell(x) for x in (
                claim + note, fmt_cites([(c.episode, t)]) if t is not None else c.episode.label,
                c.row.as_of, c.row.type, c.row.verify_by)) + " |")
        md.append("")

    md += ["## What changed", ""]
    if prev_path is None:
        md += [f"- No previous {span.kind} digest in this collection to compare with.", ""]
    else:
        md += [f"_Compared with [{prev_path.stem}]({_rel(prev_path, out_dir)})._", ""]
        md += [f"- {t} {fmt_cites(cs)}" for t, cs in v["what_changed"]] or ["- Nothing the episodes mark as changed."]
        md.append("")

    md += ["## Open questions", ""]
    md += [f"- {t} {fmt_cites(cs)}" for t, cs in v["open_questions"]] or ["- None cited."]
    md.append("")

    md += ["## Citation check", "",
           f"- {chk.valid} model citation(s) kept; every one names an episode of this span and a "
           f"time within {CITE_TOLERANCE_S}s of a timestamp that episode's notes cite.",
           f"- {len(chk.dropped_cites)} citation(s) dropped as unresolvable; "
           f"{len(chk.dropped_items)} item(s) dropped for having no valid citation."]
    for d in (chk.dropped_cites + chk.dropped_items)[:20]:
        md.append(f"  - dropped: {d}")
    md += ["- Links: LINKSUMMARY", ""]
    return "\n".join(fm) + "\n" + "\n".join(md)


def merge_span_sources(eps: Sequence[Episode]) -> List[Tuple[cited_sources.Source, List[str], int]]:
    """Every episode's Sources Referenced rows merged across the span:
    [(source, episode labels, times cited)], ranked by episodes, then times
    cited, then first episode. Deterministic; no model call."""
    rows: List[cited_sources.SourceRow] = []
    order = 0
    for e in eps:
        for r in e.source_rows:
            cites = [f"{e.label} {hms(parse_ts(c))}" for c in r.cites if parse_ts(c) is not None]
            rows.append(cited_sources.SourceRow(r.name, r.type, r.cited_by, r.cited_for, r.date,
                                                cites or [f"{e.label} -"], order))
            order += 1
    merged, _ = cited_sources.merge_sources(rows, timed=False)
    out = []
    for s in merged:
        labs = sorted({c.split()[0] for r in s.rows for c in r.cites},
                      key=lambda x: int(x[1:]))
        times = len({c for r in s.rows for c in r.cites if not c.endswith(" -")}) or len(s.rows)
        out.append((s, labs, times))
    out.sort(key=lambda x: (-len(x[1]), -x[2], min(r.order for r in x[0].rows)))
    return out


def render_sources(span: Span, eps: Sequence[Episode], out_dir: Path) -> List[str]:
    """`## Sources of the week|month`: outside sources the episodes cite,
    merged with counts and episode links (WP-P2)."""
    md = [f"## Sources of the {span.kind}", "",
          "_Outside sources the episodes cite (outlets, podcasts, papers, reports, posts …), "
          "merged across episodes and ranked by how many episodes cite them, then how often. "
          "As stated in the episode notes; people are in the People table._", ""]
    merged = merge_span_sources(eps)
    if not merged:
        return md + ["- None identified in the episodes.", ""]
    md += ["| source | type | episodes | times cited | cited by | cited for | citations |",
           "|---|---|---|---:|---|---|---|"]
    by_label = {e.label: e for e in eps}
    for s, labs, times in merged[:SOURCES_MAX]:
        cites = [r for c in s.cites if (r := resolve_cite(c, by_label))]
        ep_links = ", ".join(f"[{lab}]({_rel(by_label[lab].notes_path, out_dir)})"
                             for lab in labs)
        md.append("| " + " | ".join(_clean_cell(x) for x in (
            s.shown, s.type or "-", ep_links, str(times), s.cited_by or "-",
            s.cited_for or "-", fmt_cites(cites) or "-")) + " |")
    if len(merged) > SOURCES_MAX:
        md.append(f"\n_{len(merged) - SOURCES_MAX} more sources appear in the episode notes._")
    md.append("")
    return md


def check_digest_links(text: str, out_dir: Path, episodes: Sequence[Episode]) -> List[str]:
    """Every relative link resolves (file + #heading); every YouTube link is one
    of the span's episodes, inside its duration."""
    problems: List[str] = []
    durs = {e.youtube_id: e.duration_s for e in episodes if e.youtube_id}
    for lineno, line in enumerate(text.split("\n"), 1):
        for m in _LINK_RE.finditer(line):
            target = m.group(1)
            ym = re.match(r"https://youtu\.be/([\w-]{11})(?:\?t=(\d+))?$", target)
            if ym:
                if ym.group(1) not in durs:
                    problems.append(f"line {lineno}: {target}: not an episode of this span")
                elif ym.group(2) and durs[ym.group(1)] and int(ym.group(2)) > durs[ym.group(1)] + 1:
                    problems.append(f"line {lineno}: {target}: past the end of the episode")
                continue
            if re.match(r"^[a-z]+:", target):
                continue
            path_part, _, frag = target.partition("#")
            dest = (out_dir / unquote(path_part)).resolve()
            if not dest.exists():
                problems.append(f"line {lineno}: {target}: missing file")
                continue
            if frag:
                heads = [h for _, h, _, _ in _sections(dest.read_text(encoding="utf-8"))]
                if not any(anchor_matches(frag, h) for h in heads):
                    problems.append(f"line {lineno}: {target}: heading not found")
    return problems


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class DigestResult:
    span: str
    path: Path
    episodes: int
    calls: int
    cached: int
    written: bool
    citations: int
    dropped: int
    link_problems: List[str]
    partial: bool


def _index_coverage(collection: Path, span: Span) -> Optional[int]:
    """Episodes the collection's _index.md (playlist-index) lists in the span."""
    idx = Path(collection) / "_index.md"
    if not idx.exists():
        return None
    n = 0
    for line in idx.read_text(encoding="utf-8").split("\n"):
        m = re.match(r"^\|\s*\d+\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|", line)
        if m and span.start <= dt.date.fromisoformat(m.group(1)) <= span.end:
            n += 1
    return n


def _label(episodes: Sequence[Episode]) -> None:
    for i, e in enumerate(episodes, 1):
        e.label = f"E{i}"


def build_span(span: Span, collection: Path, out_dir: Path, complete: Optional[CompleteFn], *,
               all_episodes: Sequence[Episode], as_of: Optional[dt.date] = None,
               show: str = "Digest", clipped: bool = False,
               max_chars: int = MAX_INPUT_CHARS, write: bool = True) -> DigestResult:
    as_of = as_of or dt.date.today()
    _label(span.episodes)
    cache_dir = out_dir / CACHE_DIRNAME
    stats = CallStats()
    claims = number_claims(span)
    prev_key = previous_key(span.kind, span.key)
    prev_path = out_dir / f"{prev_key}.md"
    prev = None
    if prev_path.exists() and (cache_dir / f"{prev_key}.json").exists():
        prev = json.loads((cache_dir / f"{prev_key}.json").read_text(encoding="utf-8"))
        prev = {"span": prev_key, "digest": prev.get("digest")}
    else:
        prev_path = None

    if span.kind == "month":
        data = _month(span, collection, out_dir, complete, cache_dir, stats, claims, prev,
                      all_episodes=all_episodes, as_of=as_of, show=show, max_chars=max_chars)
    else:
        data = _week(span, cache_dir, complete, stats, claims, prev, max_chars, name=span.key)
    v, chk = validate(data["digest"], span, claims)

    prior_terms = {re.sub(r"[^a-z0-9]+", " ", t.name.lower()).strip()
                   for e in all_episodes if e.published < span.start for t in e.terms}
    partial = clipped or span.end >= as_of
    coverage = _index_coverage(collection, span)
    if coverage is not None and coverage > len(span.episodes):
        partial = True
    out_path = out_dir / f"{span.key}.md"
    text = render(span, v, chk, claims, out_path, stats=stats, as_of=as_of, partial=partial,
                  coverage=coverage, prev_path=prev_path, show=show,
                  built_at=data.get("created_at", ""), input_sha=data.get("input_sha256", ""),
                  prior_terms=prior_terms)
    problems = check_digest_links(text, out_dir, span.episodes)
    text = text.replace("link_check: LINKCHECK", "link_check: " + ("pass" if not problems else "FAIL"))
    text = text.replace("- Links: LINKSUMMARY",
                        "- Links: every relative link resolves and every YouTube link is an episode "
                        "of this span inside its duration." if not problems else
                        "- Links: " + "; ".join(problems[:10]))
    # model_calls_this_run differs between a first run and a cached rerun; keep
    # the file byte-identical on reruns by not writing when only that changed.
    written = False
    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        stable = re.sub(r"(?m)^model_calls_this_run: \d+\n", "", text)
        old = out_path.read_text(encoding="utf-8") if out_path.exists() else None
        old_stable = re.sub(r"(?m)^model_calls_this_run: \d+\n", "", old) if old else None
        if old_stable != stable:
            tmp = out_path.with_name(f".{out_path.name}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, out_path)
            written = True
    return DigestResult(span.key, out_path, len(span.episodes), stats.calls, stats.cached, written,
                        chk.valid, len(chk.dropped_cites) + len(chk.dropped_items), problems, partial)


def _week(span: Span, cache_dir: Path, complete, stats: CallStats, claims, prev, max_chars: int,
          name: str) -> Dict[str, Any]:
    eps = span.episodes
    user = render_user(span, eps, claims, prev)
    if len(user) <= max_chars:
        return cached_call(cache_dir, name, system_prompt(), user, complete, stats)
    partials = []
    for i, chunk in enumerate(_chunks(eps, max_chars - 4000), 1):
        ids = {e.key for e in chunk}
        cl = [c for c in claims if c.episode.key in ids]
        u = render_user(span, chunk, cl, None)
        partials.append(cached_call(cache_dir, f"{name}.part{i}", system_prompt(), u, complete,
                                    stats)["digest"])
    user = render_user(span, eps, claims, prev, partials=partials)
    return cached_call(cache_dir, name, system_prompt(reduce=True), user, complete, stats)


def _month(span: Span, collection: Path, out_dir: Path, complete, cache_dir: Path,
           stats: CallStats, claims, prev, *, all_episodes, as_of, show, max_chars
           ) -> Dict[str, Any]:
    """Map over the month's ISO weeks (reusing week digests whose episode set
    equals the clipped week), then one reduce call. See the module doc."""
    weeks: Dict[str, List[Episode]] = {}
    for e in span.episodes:
        weeks.setdefault(week_key(e.published), []).append(e)
    if len(weeks) == 1:
        return _week(span, cache_dir, complete, stats, claims, prev, max_chars, name=span.key)
    partials = []
    for wk, eps in sorted(weeks.items()):
        reuse = cache_dir / f"{wk}.json"
        week_ids = {e.key for e in all_episodes if week_key(e.published) == wk}
        if reuse.exists() and week_ids == {e.key for e in eps}:
            data = json.loads(reuse.read_text(encoding="utf-8"))
            d = data.get("digest") or {}
            # Week labels are E1.. within the week: relabel to the month's labels.
            wk_eps = sorted([e for e in all_episodes if week_key(e.published) == wk],
                            key=lambda e: (e.published, e.title))
            remap = {f"E{i}": e.label for i, e in enumerate(wk_eps, 1)}
            partials.append(_relabel(d, remap))
            stats.cached += 1
            if data.get("model") and data["model"] not in stats.models:
                stats.models.append(data["model"])
            continue
        a, b = week_bounds(wk)
        sub = Span("week", wk, max(a, span.start), min(b, span.end), eps)
        ids = {e.key for e in eps}
        cl = [c for c in claims if c.episode.key in ids]
        partials.append(_week(sub, cache_dir, complete, stats, cl, None, max_chars,
                              name=f"{span.key}.{wk}")["digest"])
    user = render_user(span, span.episodes, claims, prev, partials=partials)
    return cached_call(cache_dir, span.key, system_prompt(reduce=True), user, complete, stats)


_LABEL_IN = re.compile(r"\bE(\d+)\b")


def _relabel(obj: Any, remap: Dict[str, str]) -> Any:
    if isinstance(obj, str):
        return _LABEL_IN.sub(lambda m: remap.get(f"E{m.group(1)}", m.group(0)), obj)
    if isinstance(obj, list):
        return [_relabel(x, remap) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("earlier", "later") else _relabel(v, remap)) for k, v in obj.items()
                if k != "superseded"}
    return obj


def resolve_collection(value: str, vault: Optional[Path] = None,
                       learning_root: Optional[str] = None) -> Path:
    """A directory, or a subject name under <vault>/<learning_root>/."""
    p = Path(value).expanduser()
    if p.is_dir():
        return p
    from video2mdnotes.config import settings
    d = Path(vault or settings.obsidian_vault) / (learning_root or settings.learning_root) / value
    if d.is_dir():
        return d
    raise DigestError(f"{value}: not a directory and not a subject under {d.parent}")


def run(collection: Path, by: str, *, since: Optional[dt.date] = None,
        until: Optional[dt.date] = None, out: Optional[Path] = None,
        complete: Optional[CompleteFn] = None, as_of: Optional[dt.date] = None,
        show: Optional[str] = None, max_chars: int = MAX_INPUT_CHARS,
        progress: Callable[[str], None] = lambda m: None) -> List[DigestResult]:
    collection = Path(collection)
    episodes = load_collection(collection)
    if not episodes:
        raise DigestError(f"{collection}: no episode notes found")
    out_dir = Path(out) if out else collection / "digests"
    spans = group(episodes, by, since, until)
    if not spans:
        raise DigestError("no episodes in the selected range")
    show = show or _show_name(episodes, collection)
    results = []
    for s in spans:
        full = [e for e in episodes if span_key(by, e.published) == s.key]
        clipped = len(full) != len(s.episodes)
        progress(f"{s.key}: {len(s.episodes)} episode(s)")
        results.append(build_span(s, collection, out_dir, complete, all_episodes=episodes,
                                  as_of=as_of, show=show, clipped=clipped, max_chars=max_chars))
    return results


def _show_name(episodes: Sequence[Episode], collection: Path) -> str:
    for e in episodes:
        fm, _ = _frontmatter(e.notes_path.read_text(encoding="utf-8"))
        pb = fm.get("published_by")
        if pb:
            return pb.split(":")[0].strip()
    return collection.name.replace("_", " ").title()


__all__ = ["DigestError", "DigestResult", "Episode", "Span", "build_span", "check_digest_links",
           "group", "load_collection", "load_episode", "parse_reply", "resolve_collection", "run",
           "validate", "week_bounds", "week_key"]
