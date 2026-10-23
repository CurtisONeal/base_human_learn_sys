"""Collections: batch-process a large playlist (or URL list) with resume.

    collection init   <name> --playlist <url> --subject <s>
    collection select <name> --last-days 7            (cached index; no network)
    collection status <name>
    collection run    <name> --package --digest week [--max-items N] [--max-calls N]
                             [--until-time HH:MM]

Design: ``design/COLLECTIONS.md``. In short:

* A collection is a directory ``<root>/<name>/`` holding ``collection.json``
  (the manifest: sources, subject, per-item state), ``listing.json`` (a
  snapshot of the playlist listing joined with the ``playlist-index`` date /
  description cache), ``runs/<YYYYMMDD>_<id>/`` (one run dir per item, named
  by publish date + YouTube id, never guessed from a title) and ``logs/``.
* Item states: ``selected`` (matched, not queued) → ``queued`` →
  ``transcribed`` (transcript on disk, notes not yet) → ``noted`` →
  ``packaged``; ``failed`` after ``max_attempts`` (error, attempts,
  timestamps kept). Every transition is saved atomically at once, so a run
  can be killed at any point and re-run.
* ``run`` walks the queue in order. Finished work is never redone: an item
  whose YouTube id already has a learning package in the subject folder is
  adopted (``reused: true``, its run dir taken from the package's
  ``source_meta.json``); a run dir with a transcript but no notes resumes
  with ``longform`` (no download); notes without a package are only
  packaged.
* Each item is processed by a child ``process``/``longform`` command with
  ``--on-exhaustion stop``; the collection decides what to do on
  exhaustion (``ON_EXHAUSTION``: ``wait`` sleeps until the reset — recorded
  in ``events`` — and retries the same item; anything else stops cleanly).
  The child never spends money the parent did not allow (``--usd-cap``).
* Limits (``--max-items``, ``--max-calls``, ``--until-time``) are checked
  BEFORE an item starts, so a run stops between items, never inside one.
* Digests: only spans that gained an episode in this run (or that have no
  digest yet, or whose previous span's digest was just rewritten — its
  "what changed" section depends on it) are rebuilt. Spans not built
  because of a limit are kept in ``pending_digests`` for the next run.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MANIFEST = "collection.json"
LISTING = "listing.json"
LOCK = "collection.lock"
LAST_RUN = "_last_run.json"
VERSION = 1

STATES = ("selected", "queued", "transcribed", "noted", "packaged", "failed")
RUNNABLE = ("queued", "transcribed", "noted", "failed")
DONE_PREFIX = "COLLECTION_ITEM_DONE="
DIGEST_PREFIX = "COLLECTION_DIGEST="
STOP_PREFIX = "COLLECTION_STOPPED="

# Estimates (status / --max-calls). Speech rate of a news podcast and the
# long-form word budget per passage call; refined by observed items.
WORDS_PER_MIN = 160
WORDS_PER_CALL = 12_000
MIN_PER_CALL = 2.5
ASR_MIN_PER_MEDIA_MIN = 0.05
OVERHEAD_MIN = 1.0

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 60.0


class CollectionError(RuntimeError):
    """A user-facing problem with a collection (bad name, missing manifest, …)."""


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(t: Optional[dt.datetime] = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


_NAME = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")


def youtube_id(url: str) -> Optional[str]:
    m = _YT_ID.search(url or "")
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[\w-]{11}", url or "") else None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def parse_duration(value: Optional[str]) -> Optional[float]:
    """Seconds from "20m", "1h30m", "90s", "1:05:00", "25" (minutes)."""
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", v):
        return float(v) * 60
    if re.fullmatch(r"\d+(:\d{2}){1,2}", v):
        parts = [int(x) for x in v.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", v)
    if m and any(m.groups()):
        h, mi, s = (int(x or 0) for x in m.groups())
        return float(h * 3600 + mi * 60 + s)
    raise ValueError(f"cannot read duration {value!r} (use e.g. 20m, 1h30m, 1:05:00, 25)")


def until_deadline(hhmm: Optional[str], now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """The next occurrence of HH:MM (today, or tomorrow when already past)."""
    if not hhmm:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"--until-time: expected HH:MM, got {hhmm!r}")
    now = now or _now()
    t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def estimate_calls(duration_s: Optional[float]) -> int:
    """Model calls for one episode's notes: passage calls (+ course map and
    consistency when there is more than one passage call)."""
    words = (duration_s or 20 * 60) / 60 * WORDS_PER_MIN
    calls = max(1, math.ceil(words / WORDS_PER_CALL))
    return calls + 2 if calls > 1 else calls


def estimate_minutes(duration_s: Optional[float]) -> float:
    media_min = (duration_s or 20 * 60) / 60
    return OVERHEAD_MIN + media_min * ASR_MIN_PER_MEDIA_MIN + estimate_calls(duration_s) * MIN_PER_CALL


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

@dataclass
class Collection:
    dir: Path
    data: Dict[str, Any]

    # -- files ------------------------------------------------------------- #
    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def subject(self) -> Optional[str]:
        return self.data.get("subject")

    @property
    def path(self) -> Path:
        return self.dir / MANIFEST

    @property
    def runs_dir(self) -> Path:
        return self.dir / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def items(self) -> Dict[str, Dict[str, Any]]:
        return self.data.setdefault("items", {})

    def save(self) -> None:
        self.data["updated_at"] = _iso()
        _atomic_json(self.path, self.data)

    @classmethod
    def load(cls, root: Path, name: str) -> "Collection":
        d = Path(root) / name
        p = d / MANIFEST
        if not p.exists():
            raise CollectionError(f"no collection {name!r} under {root} (run `collection init` first)")
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("version") != VERSION:
            raise CollectionError(f"{p}: unsupported manifest version {data.get('version')}")
        return cls(d, data)

    @classmethod
    def create(cls, root: Path, name: str, *, subject: Optional[str],
               sources: List[Dict[str, Any]], index_note: Optional[Path] = None,
               vault_subject_dir: Optional[Path] = None) -> "Collection":
        if not _NAME.match(name or ""):
            raise CollectionError("collection name: lowercase letters, digits, _ and - only")
        d = Path(root) / name
        if (d / MANIFEST).exists():
            col = cls.load(root, name)
            col.data["subject"] = subject or col.subject
            col.data["sources"] = sources or col.data.get("sources", [])
            if index_note:
                col.data["index_note"] = str(index_note)
            if vault_subject_dir:
                col.data["vault_subject_dir"] = str(vault_subject_dir)
            return col
        data = {"version": VERSION, "name": name, "subject": subject, "sources": sources,
                "created_at": _iso(), "updated_at": _iso(),
                "index_note": str(index_note) if index_note else None,
                "vault_subject_dir": str(vault_subject_dir) if vault_subject_dir else None,
                "items": {}, "events": [], "selections": [], "digests": {},
                "pending_digests": {}, "runs": []}
        return cls(d, data)

    # -- items ------------------------------------------------------------- #
    def queue(self) -> List[Dict[str, Any]]:
        """Items in queue order (queue_pos), every state."""
        return sorted(self.items.values(), key=lambda i: (i.get("queue_pos") or 0, i["id"]))

    def event(self, kind: str, **kw) -> Dict[str, Any]:
        ev = {"at": _iso(), "kind": kind, **kw}
        self.data.setdefault("events", []).append(ev)
        del self.data["events"][:-200]
        return ev

    def set_state(self, item: Dict[str, Any], state: str, **kw) -> None:
        assert state in STATES, state
        item["state"] = state
        item.setdefault("timestamps", {})[f"{state}_at"] = _iso()
        item.update(kw)
        self.save()


# --------------------------------------------------------------------------- #
# Listing (playlist snapshot + playlist-index cache)
# --------------------------------------------------------------------------- #

def cache_path_for(output_root: Path, playlist_id: str) -> Path:
    return Path(output_root) / "_playlist_cache" / f"{playlist_id}.json"


def build_listing(col: Collection, *, output_root: Path, fetch: bool = True,
                  max_fetches: Optional[int] = None,
                  list_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
                  meta_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
                  info_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
                  progress: Callable[[str], None] = lambda m: None) -> List[Dict[str, Any]]:
    """(Re)build listing.json. Network: one flat listing per playlist plus a
    watch-page read per entry NOT yet in the playlist-index cache (none when
    `fetch` is False). URL-list sources: one metadata read per URL not yet in
    the listing."""
    from video2mdnotes.core import playlist_index as pi
    entries: List[Dict[str, Any]] = []
    old = {e["id"]: e for e in load_listing(col)}
    for src in col.data.get("sources", []):
        if src.get("kind") == "playlist":
            res = pi.build_index(src["url"], cache_root=Path(output_root) / "_playlist_cache",
                                 max_fetches=None if fetch and max_fetches is None
                                 else (max_fetches if fetch else 0),
                                 list_fn=list_fn, meta_fn=meta_fn, progress=progress)
            src["playlist_id"] = res.playlist_id
            src["title"] = res.playlist_title
            cache = pi.load_cache(res.cache_path).get("entries", {})
            for e in res.entries:
                c = cache.get(e.id) or {}
                entries.append({"index": e.index, "id": e.id, "title": e.title,
                                "duration_s": e.duration_s, "published": e.upload_date,
                                "description": c.get("description") or "",
                                "url": f"https://www.youtube.com/watch?v={e.id}",
                                "unavailable": e.status == "unavailable",
                                "source": res.playlist_id})
        elif src.get("kind") == "urls":
            from video2mdnotes.core.playlist import default_info
            info_fn = info_fn or default_info
            for n, url in enumerate(src.get("urls", []), 1):
                yid = youtube_id(url) or url
                if yid in old and old[yid].get("published"):
                    entries.append({**old[yid], "index": n})
                    continue
                if not fetch:
                    entries.append({"index": n, "id": yid, "title": url, "duration_s": None,
                                    "published": None, "description": "", "url": url,
                                    "unavailable": False, "source": "urls"})
                    continue
                info = info_fn(url)
                raw = str(info.get("upload_date") or "")
                pub = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if re.fullmatch(r"\d{8}", raw) else None
                entries.append({"index": n, "id": str(info.get("id") or yid),
                                "title": info.get("title") or url,
                                "duration_s": info.get("duration"), "published": pub,
                                "description": (info.get("description") or "")[:600],
                                "url": info.get("webpage_url") or url, "unavailable": False,
                                "source": "urls"})
    _atomic_json(col.dir / LISTING, {"built_at": _iso(), "entries": entries})
    return entries


def load_listing(col: Collection) -> List[Dict[str, Any]]:
    p = col.dir / LISTING
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("entries", [])


def listing_built_at(col: Collection) -> Optional[dt.datetime]:
    p = col.dir / LISTING
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8")).get("built_at")
    try:
        return dt.datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def listing_stale_reason(col: Collection, f: "Filters",
                         today: Optional[dt.date] = None) -> Optional[str]:
    """Why the cached listing cannot answer `f`, or None. A dated window that
    ends after the day the listing was built may be missing newer episodes
    (phase A selected 0 new because the listing stopped a day early)."""
    if not (col.dir / LISTING).exists():
        return "no listing yet"
    end = f.until or ((today or dt.date.today()) if f.since else None)
    if end is None:
        return None
    built = listing_built_at(col)
    if built is None:
        return "listing has no build time"
    if built.date() < end:
        return f"listing built {built.date()} is older than the window end {end}"
    return None


def ensure_listing(col: Collection, f: "Filters", *, output_root: Path,
                   refresh: bool = False, auto: bool = True,
                   today: Optional[dt.date] = None,
                   **build_kw: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The listing to select from, re-listed first when asked (`refresh`) or
    when the cache is older than the requested window (`auto`). Returns
    (entries, note); on a failed automatic refresh the cached listing is kept
    and the note says so."""
    if refresh:
        return build_listing(col, output_root=output_root, **build_kw), "re-listed (--refresh)"
    reason = listing_stale_reason(col, f, today=today) if auto else None
    if not reason:
        return load_listing(col), None
    try:
        return (build_listing(col, output_root=output_root, **build_kw),
                f"re-listed automatically: {reason}")
    except Exception as exc:  # network down / throttled: fall back to the cache
        cached = load_listing(col)
        if not cached:
            raise
        return cached, f"{reason}; automatic re-list failed ({exc}); using the cached listing"


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

@dataclass
class Filters:
    since: Optional[dt.date] = None
    until: Optional[dt.date] = None
    last_days: Optional[int] = None
    as_of: Optional[dt.date] = None
    limit: Optional[int] = None
    items: Optional[str] = None
    match: Optional[str] = None
    min_duration_s: Optional[float] = None
    max_duration_s: Optional[float] = None

    def __post_init__(self):
        if self.last_days is not None:
            if self.last_days < 1:
                raise ValueError("--last-days must be >= 1")
            ref = self.as_of or dt.date.today()
            since = ref - dt.timedelta(days=self.last_days)
            self.since = max(self.since, since) if self.since else since
            self.until = min(self.until, ref) if self.until else ref
        if self.since and self.until and self.since > self.until:
            raise ValueError("--since is after --until")
        if self.limit is not None and self.limit < 1:
            raise ValueError("--limit must be >= 1")
        from video2mdnotes.core.playlist import parse_items
        self.ranges = parse_items(self.items) if self.items else None
        self.regex = re.compile(self.match, re.I) if self.match else None

    def describe(self) -> str:
        bits = []
        if self.since:
            bits.append(f"since {self.since}")
        if self.until:
            bits.append(f"until {self.until}")
        if self.items:
            bits.append(f"items {self.items}")
        if self.match:
            bits.append(f"match /{self.match}/")
        if self.min_duration_s:
            bits.append(f">= {self.min_duration_s / 60:.0f} min")
        if self.max_duration_s:
            bits.append(f"<= {self.max_duration_s / 60:.0f} min")
        if self.limit:
            bits.append(f"first {self.limit}")
        return ", ".join(bits) or "all"

    def to_json(self) -> Dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, dt.date) else v)
                for k, v in self.__dict__.items() if k not in ("ranges", "regex") and v is not None}


def select_entries(entries: Sequence[Dict[str, Any]], f: Filters) -> List[Dict[str, Any]]:
    """Matching listing entries in LISTING order (newest first for a
    newest-first playlist); --limit is "the first N that match"."""
    from video2mdnotes.core.playlist import in_items
    out = []
    for e in entries:
        if e.get("unavailable"):
            continue
        if f.ranges and not in_items(int(e.get("index") or 0), f.ranges):
            continue
        if f.since or f.until:
            if not e.get("published"):
                continue
            d = dt.date.fromisoformat(e["published"])
            if (f.since and d < f.since) or (f.until and d > f.until):
                continue
        dur = e.get("duration_s")
        if f.min_duration_s and (dur is None or dur < f.min_duration_s):
            continue
        if f.max_duration_s and (dur is None or dur > f.max_duration_s):
            continue
        if f.regex and not (f.regex.search(e.get("title") or "")
                            or f.regex.search(e.get("description") or "")):
            continue
        out.append(e)
        if f.limit and len(out) >= f.limit:
            break
    return out


def apply_selection(col: Collection, matched: Sequence[Dict[str, Any]], f: Filters, *,
                    order: str = "oldest", queue: bool = True, replace: bool = False
                    ) -> Dict[str, int]:
    """Add matched entries to the manifest. Items already present keep their
    state (a done item stays done); new ones become queued (or selected).
    `replace` drops queued/selected items that no longer match and were never
    started. Queue order: oldest first (default) or newest first."""
    if order not in ("oldest", "newest"):
        raise ValueError("--order must be oldest or newest")
    ordered = sorted(matched, key=lambda e: (e.get("published") or "", -(e.get("index") or 0)),
                     reverse=(order == "newest"))
    ids = {e["id"] for e in ordered}
    removed = 0
    if replace:
        for iid in list(col.items):
            it = col.items[iid]
            if iid not in ids and it["state"] in ("selected", "queued") and not it.get("attempts"):
                del col.items[iid]
                removed += 1
    pos = max([i.get("queue_pos") or 0 for i in col.items.values()] or [0])
    added = requeued = 0
    for e in ordered:
        it = col.items.get(e["id"])
        if it is None:
            pos += 1
            it = {"id": e["id"], "url": e["url"], "title": e.get("title"),
                  "index": e.get("index"), "published": e.get("published"),
                  "duration_s": e.get("duration_s"), "queue_pos": pos,
                  "state": "queued" if queue else "selected", "attempts": 0, "calls": 0,
                  "error": None, "timestamps": {"selected_at": _iso()}}
            if queue:
                it["timestamps"]["queued_at"] = _iso()
            col.items[e["id"]] = it
            added += 1
        elif queue and it["state"] == "selected":
            it["state"] = "queued"
            it["timestamps"]["queued_at"] = _iso()
            requeued += 1
    col.data.setdefault("selections", []).append(
        {"at": _iso(), "filters": f.to_json(), "matched": len(ordered), "added": added,
         "removed": removed, "order": order})
    col.save()
    return {"matched": len(ordered), "added": added, "requeued": requeued, "removed": removed}


# --------------------------------------------------------------------------- #
# Run-dir inspection
# --------------------------------------------------------------------------- #

def summary_file(run_dir: Path) -> Optional[Path]:
    s = sorted((Path(run_dir) / "summaries").glob("*.summary.md")) if run_dir else []
    s = [p for p in s if p.stat().st_size > 0]
    return s[0] if s else None


def has_transcript(run_dir: Path) -> bool:
    t = Path(run_dir) / "transcripts"
    return t.is_dir() and any(p.stat().st_size > 0 for p in t.glob("*.md")) \
        and (Path(run_dir) / "source_meta.json").exists()


def count_calls(run_dir: Path) -> int:
    """Model calls that produced the run's notes, from its checkpoints."""
    p = Path(run_dir) / "longform_state.json"
    if not p.exists():
        return 0
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return 0
    units = {(v.get("pack") or k) for k, v in (st.get("passages") or {}).items()
             if v.get("status") == "done" and v.get("model")}
    asm = st.get("assembly") or {}
    cm = asm.get("course_map_calls") or 0
    cons = 1 if (st.get("consistency") or {}).get("called") else 0
    outline = 1 if (Path(run_dir) / "structure_outline.json").exists() else 0
    return len(units) + int(cm) + cons + outline


def run_metrics(run_dir: Path) -> Dict[str, Any]:
    """calls, models, ASR backend/time, media minutes, notes words."""
    run_dir = Path(run_dir)
    out: Dict[str, Any] = {"calls": count_calls(run_dir)}
    st = run_dir / "longform_state.json"
    if st.exists():
        try:
            data = json.loads(st.read_text(encoding="utf-8"))
            out["models"] = sorted({v.get("model") for v in (data.get("passages") or {}).values()
                                    if v.get("model")})
        except ValueError:
            pass
    man = run_dir / "asr_manifest.json"
    if man.exists():
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
            out["asr"] = m.get("label") or m.get("backend")
            out["asr_seconds"] = round(sum((w.get("timings") or {}).get("transcribe_s") or 0
                                           for w in m.get("windows") or []), 1)
            out["media_s"] = m.get("total_duration_s")
        except ValueError:
            pass
    elif (run_dir / "transcripts").is_dir():
        out["asr"] = "captions"
    s = summary_file(run_dir)
    if s:
        out["notes_words"] = len(s.read_text(encoding="utf-8").split())
    return out


def find_package(subject_dir: Optional[Path], yid: str) -> Optional[Path]:
    """The package dir in `subject_dir` whose source_meta has this YouTube id."""
    if not subject_dir or not Path(subject_dir).is_dir():
        return None
    for meta in sorted(Path(subject_dir).glob("*/source_meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (data.get("source") or {}).get("youtube_id") == yid and (meta.parent / "notes.md").exists():
            return meta.parent
    return None


def package_run_dir(pkg_dir: Path) -> Optional[Path]:
    try:
        data = json.loads((Path(pkg_dir) / "source_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rd = data.get("run_dir")
    return Path(rd) if rd and Path(rd).is_dir() else None


# --------------------------------------------------------------------------- #
# Item runner (child process)
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    status: str                      # noted | failed | empty | exhausted | budget | interrupted
    run_dir: Path
    error: str = ""
    reset_at: Optional[str] = None
    log: Optional[Path] = None


ItemRunner = Callable[[Dict[str, Any], Path], Outcome]


class SubprocessRunner:
    """Runs one item with the pipeline's own commands in a child process:

    * a run dir with a transcript but no notes → ``longform <run_dir>``
      (no download, no ASR; passage checkpoints resume);
    * otherwise → ``process <url> --out <run_dir>``.

    Always ``--on-exhaustion stop`` (the collection decides about waiting);
    ``--usd-cap`` / ``--prompt-variant`` are passed through. The child's
    ``_last_run.json`` goes to ``logs/<id>.last_run.json`` and is read for
    the outcome — never the run dir guessed from output."""

    def __init__(self, logs_dir: Path, *, usd_cap: Optional[float] = None,
                 prompt_variant: Optional[str] = None, python: str = sys.executable,
                 echo: Callable[[str], None] = lambda m: None):
        self.logs_dir = Path(logs_dir)
        self.usd_cap = usd_cap
        self.prompt_variant = prompt_variant
        self.python = python
        self.echo = echo

    def command(self, item: Dict[str, Any], run_dir: Path) -> List[str]:
        base = [self.python, "-m", "video2mdnotes.main"]
        if has_transcript(run_dir) and not summary_file(run_dir):
            cmd = base + ["longform", str(run_dir)]
        else:
            cmd = base + ["process", item["url"], "--out", str(run_dir)]
        cmd += ["--on-exhaustion", "stop"]
        if self.usd_cap is not None:
            cmd += ["--usd-cap", str(self.usd_cap)]
        if self.prompt_variant:
            cmd += ["--prompt-variant", self.prompt_variant]
        return cmd

    def __call__(self, item: Dict[str, Any], run_dir: Path) -> Outcome:
        import video2mdnotes
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"{item['id']}.log"
        last = self.logs_dir / f"{item['id']}.last_run.json"
        last.unlink(missing_ok=True)
        env = dict(os.environ)
        src = str(Path(video2mdnotes.__file__).resolve().parent.parent)
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["LAST_RUN_FILE"] = str(last)
        cmd = self.command(item, run_dir)
        self.echo("  $ " + " ".join(cmd[1:]))
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {_iso()} {' '.join(cmd)}\n")
                fh.flush()
                code = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env)
        except KeyboardInterrupt:
            return Outcome("interrupted", run_dir, "interrupted", log=log)
        rec: Dict[str, Any] = {}
        if last.exists():
            try:
                rec = json.loads(last.read_text(encoding="utf-8"))
            except ValueError:
                rec = {}
        reason = (rec.get("extra") or {}).get("stop_reason")
        reset_at = (rec.get("extra") or {}).get("reset_at")
        empty = Path(run_dir).with_name(f"no_summary_{Path(run_dir).name}")
        if reason == "subscription_exhausted":
            return Outcome("exhausted", run_dir, "subscription exhausted", reset_at, log)
        if reason == "budget":
            return Outcome("budget", run_dir, "spend cap reached", log=log)
        if code == 0 and summary_file(run_dir):
            return Outcome("noted", run_dir, log=log)
        if empty.exists():
            return Outcome("empty", empty, "no usable notes (silent/empty source)", log=log)
        tail = ""
        try:
            lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            tail = " | ".join(lines[-3:])[-300:]
        except OSError:
            pass
        return Outcome("failed", run_dir, f"exit {code}: {tail}", log=log)


# --------------------------------------------------------------------------- #
# Package / eval / digest hooks (in-process, deterministic except digests)
# --------------------------------------------------------------------------- #

def default_packager(run_dir: Path, subject: str) -> Dict[str, Any]:
    from video2mdnotes.core.package import package as build_package
    r = build_package(run_dir, subject)
    c = r.report["citations"]
    return {"package_dir": str(r.package_dir), "linked": c["linked"], "total": c["total"],
            "unresolved": c["unresolved_count"], "broken": len(r.report.get("broken_links", []))}


def default_evaluator(run_dir: Path, reuse: bool = True) -> Dict[str, Any]:
    """P1–P7 (deterministic). An existing eval/eval_report.json is reused."""
    from video2mdnotes.eval import longform as ev
    rep_path = Path(run_dir) / "eval" / "eval_report.json"
    if reuse and rep_path.exists():
        report = json.loads(rep_path.read_text(encoding="utf-8"))
    else:
        report, sample = ev.evaluate(Path(run_dir))
        ev.write_outputs(report, sample, Path(run_dir) / "eval")
    crit = report.get("criteria") or {}
    return {"overall_pass": report.get("overall_pass"),
            **{k: (v.get("pass") if isinstance(v, dict) else v)
               for k, v in crit.items() if k != "P8"}}


def span_keys(by: str, dates: Iterable[str]) -> List[str]:
    from video2mdnotes.core.digest import span_key
    return sorted({span_key(by, dt.date.fromisoformat(d)) for d in dates if d})


def default_digester(collection_dir: Path, by: str, keys: Sequence[str], *,
                     max_calls: Optional[int] = None, as_of: Optional[dt.date] = None,
                     complete=None, on_built: Callable[[Any], None] = lambda r: None,
                     progress: Callable[[str], None] = lambda m: None) -> Tuple[List[Any], List[str]]:
    """Build the digests of `keys` (oldest first). A span whose PREVIOUS span's
    digest was rewritten in this call is rebuilt too (its "what changed"
    depends on it). Returns (results, keys left unbuilt because of max_calls)."""
    from video2mdnotes.core import digest as dg
    if complete is None:
        from video2mdnotes.core.longform import _default_complete as complete
    episodes = dg.load_collection(collection_dir)
    spans = dg.group(episodes, by)
    order = [s.key for s in spans]
    todo = set(keys) & set(order)
    out_dir = Path(collection_dir) / "digests"
    show = dg._show_name(episodes, Path(collection_dir)) if episodes else "Digest"
    results, left, used = [], [], 0
    for i, s in enumerate(spans):
        if s.key not in todo:
            continue
        if max_calls is not None and used >= max_calls:
            left.append(s.key)
            continue
        progress(f"digest {s.key}: {len(s.episodes)} episode(s)")
        r = dg.build_span(s, Path(collection_dir), out_dir, complete, all_episodes=episodes,
                          as_of=as_of, show=show)
        used += r.calls
        results.append(r)
        on_built(r)
        if r.written and i + 1 < len(spans):
            todo.add(spans[i + 1].key)
    return results, left


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

@dataclass
class Limits:
    max_items: Optional[int] = None
    max_calls: Optional[int] = None
    deadline: Optional[dt.datetime] = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_s: float = DEFAULT_BACKOFF_S
    retry_failed: bool = False


@dataclass
class RunSummary:
    started: str
    finished: str = ""
    processed: List[str] = field(default_factory=list)      # attempted and finished now
    reused: List[str] = field(default_factory=list)
    packaged: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    retried: List[str] = field(default_factory=list)
    calls: int = 0
    digest_calls: int = 0
    digests: List[Dict[str, Any]] = field(default_factory=list)
    pending_digests: List[str] = field(default_factory=list)
    waits: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    run_dirs: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class Locked(CollectionError):
    pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_lock(col: Collection) -> Path:
    p = col.dir / LOCK
    if p.exists():
        try:
            pid = int(p.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            raise Locked(f"collection {col.name!r} is being run by pid {pid} ({p})")
    col.dir.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))
    return p


def item_run_dir(col: Collection, item: Dict[str, Any]) -> Path:
    if item.get("run_dir"):
        return Path(item["run_dir"])
    date = (item.get("published") or "").replace("-", "") or "undated"
    return col.runs_dir / f"{date}_{item['id']}"


def run_collection(col: Collection, limits: Limits, *,
                   runner: ItemRunner,
                   package_subject: Optional[str] = None,
                   digest_by: Optional[str] = None,
                   evaluate: bool = True,
                   packager: Callable[[Path, str], Dict[str, Any]] = default_packager,
                   evaluator: Callable[[Path], Dict[str, Any]] = default_evaluator,
                   digester: Callable[..., Tuple[List[Any], List[str]]] = default_digester,
                   vault_subject_dir: Optional[Path] = None,
                   on_exhaustion: str = "wait",
                   exhaustion_wait_s: float = 5 * 3600,
                   exhaustion_max_wait_s: float = 12 * 3600,
                   now: Callable[[], dt.datetime] = _now,
                   sleep: Callable[[float], None] = time.sleep,
                   emit: Callable[[str], None] = print,
                   say: Callable[[str], None] = lambda m: None,
                   as_of: Optional[dt.date] = None) -> RunSummary:
    """Process the queue (see module doc). Never raises for one item's
    failure; returns a RunSummary and leaves the manifest consistent."""
    summary = RunSummary(started=_iso(now()))
    lock = acquire_lock(col)
    newly_done: List[Dict[str, Any]] = []
    policy = (on_exhaustion or "wait").strip().lower()
    if policy == "ask":
        policy = "wait"
    try:
        eligible = []
        for it in col.queue():
            if it["state"] == "selected" or it["state"] == "packaged":
                continue
            if it["state"] == "noted" and not package_subject:
                continue
            if it["state"] == "failed":
                if limits.retry_failed:
                    it["attempts"] = 0
                elif (it.get("attempts") or 0) >= limits.max_attempts or it.get("permanent"):
                    continue
            eligible.append(it)
        pending = list(eligible)
        attempted = 0
        while pending:
            it = pending.pop(0)
            # 1. Reuse: a package for this id already exists in the subject folder.
            if package_subject and vault_subject_dir is not None and it["state"] != "packaged":
                pkg = find_package(vault_subject_dir, it["id"])
                if pkg is not None:
                    rd = package_run_dir(pkg) or (Path(it["run_dir"]) if it.get("run_dir") else None)
                    col.set_state(it, "packaged", package_dir=str(pkg),
                                  run_dir=str(rd) if rd else it.get("run_dir"), reused=True,
                                  error=None)
                    if rd:
                        it.update(run_metrics(rd))
                        if evaluate and summary_file(rd):
                            _safe_eval(col, it, rd, evaluator)
                    summary.reused.append(it["id"])
                    emit(f"{DONE_PREFIX}{it['id']} {pkg}")
                    say(f"reused {it['id']} (already packaged): {pkg}")
                    continue
            run_dir = item_run_dir(col, it)
            # 2. Notes needed?
            if not summary_file(run_dir):
                t = now()
                if limits.deadline and t >= limits.deadline:
                    summary.stop_reason = f"until-time {limits.deadline:%H:%M} reached"
                    break
                if limits.max_items is not None and attempted >= limits.max_items:
                    summary.stop_reason = f"--max-items {limits.max_items} reached"
                    break
                est = estimate_calls(it.get("duration_s"))
                if limits.max_calls is not None and summary.calls + est > limits.max_calls:
                    summary.stop_reason = (f"--max-calls {limits.max_calls}: next item needs "
                                           f"~{est}, {summary.calls} used")
                    break
                ra = it.get("retry_after")
                if ra:
                    wait = (dt.datetime.fromisoformat(ra) - t).total_seconds()
                    if wait > 0:
                        if limits.deadline and t + dt.timedelta(seconds=wait) > limits.deadline:
                            summary.stop_reason = "retry backoff ends after --until-time"
                            break
                        say(f"backoff {wait:.0f}s before retrying {it['id']}")
                        sleep(wait)
                attempted += 1
                before = count_calls(run_dir)
                it["attempts"] = (it.get("attempts") or 0) + 1
                it["run_dir"] = str(run_dir)
                it.setdefault("timestamps", {})["last_attempt_at"] = _iso(now())
                col.save()
                say(f"[{attempted}] {it['id']} {it.get('published')} {it.get('title')}")
                t0 = time.monotonic()
                try:
                    out = runner(it, run_dir)
                except KeyboardInterrupt:
                    out = Outcome("interrupted", run_dir, "interrupted")
                except Exception as exc:  # noqa: BLE001 - runner bug: one item, not the run
                    out = Outcome("failed", run_dir, f"{type(exc).__name__}: {exc}")
                wall = time.monotonic() - t0
                made = max(0, count_calls(out.run_dir) - before)
                summary.calls += made
                it["calls_spent"] = (it.get("calls_spent") or 0) + made
                it["wall_s"] = round((it.get("wall_s") or 0) + wall, 1)
                if out.log:
                    it["log"] = str(out.log)
                if out.status == "exhausted":
                    it["attempts"] -= 1          # not the item's fault
                    ev = col.event("exhausted", item=it["id"], reset_at=out.reset_at,
                                   policy=policy)
                    if has_transcript(run_dir):
                        col.set_state(it, "transcribed")
                    wait_s = _wait_seconds(out.reset_at, now(), exhaustion_wait_s,
                                           exhaustion_max_wait_s)
                    if policy != "wait":
                        summary.stop_reason = "subscription exhausted (ON_EXHAUSTION=" + policy + ")"
                        col.save()
                        break
                    if limits.deadline and now() + dt.timedelta(seconds=wait_s) > limits.deadline:
                        summary.stop_reason = "subscription exhausted; reset is after --until-time"
                        col.save()
                        break
                    ev["waited_s"] = wait_s
                    summary.waits.append({"item": it["id"], "seconds": wait_s,
                                          "reset_at": out.reset_at})
                    col.save()
                    say(f"subscription exhausted; waiting {wait_s / 60:.0f} min")
                    sleep(wait_s)
                    attempted -= 1
                    pending.insert(0, it)
                    continue
                if out.status in ("budget", "interrupted"):
                    it["attempts"] -= 1
                    if has_transcript(run_dir):
                        col.set_state(it, "transcribed")
                    col.event(out.status, item=it["id"])
                    summary.stop_reason = ("spend cap reached" if out.status == "budget"
                                           else "interrupted")
                    col.save()
                    break
                if out.status in ("failed", "empty"):
                    permanent = out.status == "empty"
                    it["error"] = out.error
                    if out.status == "empty":
                        it["run_dir"] = str(out.run_dir)
                    if not permanent and it["attempts"] < limits.max_attempts:
                        back = limits.backoff_s * (2 ** (it["attempts"] - 1))
                        it["retry_after"] = _iso(now() + dt.timedelta(seconds=back))
                        state = "transcribed" if has_transcript(run_dir) else "queued"
                        col.set_state(it, state)
                        summary.retried.append(it["id"])
                        say(f"failed (attempt {it['attempts']}/{limits.max_attempts}), "
                            f"retry in {back:.0f}s: {out.error[:120]}")
                        pending.append(it)
                    else:
                        col.set_state(it, "failed", permanent=permanent)
                        it.setdefault("timestamps", {})["failed_at"] = _iso(now())
                        summary.failed.append(it["id"])
                        col.event("failed", item=it["id"], error=out.error[:300])
                        col.save()
                        say(f"FAILED {it['id']}: {out.error[:200]}")
                    continue
                # noted
                it.pop("retry_after", None)
                col.set_state(it, "noted", error=None, **run_metrics(run_dir))
                summary.processed.append(it["id"])
                summary.run_dirs.append(str(run_dir))
                if evaluate:
                    _safe_eval(col, it, run_dir, evaluator)
            elif it["state"] not in ("noted",):
                col.set_state(it, "noted", error=None, **run_metrics(run_dir))
                summary.run_dirs.append(str(run_dir))
                if evaluate and "eval" not in it:
                    _safe_eval(col, it, run_dir, evaluator)
            # 3. Package.
            done_dir = run_dir
            if package_subject:
                try:
                    p = packager(run_dir, package_subject)
                    col.set_state(it, "packaged", package_dir=p["package_dir"],
                                  package={k: v for k, v in p.items() if k != "package_dir"})
                    summary.packaged.append(it["id"])
                    done_dir = Path(p["package_dir"])
                except Exception as exc:  # noqa: BLE001 - notes exist; packaging retried next run
                    it["error"] = f"package: {type(exc).__name__}: {exc}"
                    col.save()
                    say(f"package failed for {it['id']}: {exc}")
                    continue
            newly_done.append(it)
            emit(f"{DONE_PREFIX}{it['id']} {done_dir}")

        # 4. Digests over the affected spans only.
        if digest_by and summary.stop_reason not in ("interrupted", "spend cap reached") \
                and not summary.stop_reason.startswith("subscription exhausted"):
            _run_digests(col, digest_by, newly_done, summary, limits, package_subject,
                         vault_subject_dir, digester, emit, say, as_of, policy, now, sleep,
                         exhaustion_wait_s, exhaustion_max_wait_s)
        elif digest_by:
            keys = span_keys(digest_by, [i.get("published") for i in newly_done])
            pend = col.data.setdefault("pending_digests", {}).setdefault(digest_by, [])
            for k in keys:
                if k not in pend:
                    pend.append(k)
            summary.pending_digests = list(pend)
    except KeyboardInterrupt:
        summary.stop_reason = "interrupted"
    finally:
        summary.finished = _iso(now())
        col.data.setdefault("runs", []).append(
            {k: summary.to_json()[k] for k in ("started", "finished", "calls", "digest_calls",
                                               "stop_reason")}
            | {"processed": len(summary.processed), "reused": len(summary.reused),
               "failed": len(summary.failed)})
        del col.data["runs"][:-50]
        col.save()
        _atomic_json(col.dir / LAST_RUN, summary.to_json())
        try:
            lock.unlink()
        except OSError:
            pass
    if summary.stop_reason:
        emit(f"{STOP_PREFIX}{summary.stop_reason}")
    return summary


def _wait_seconds(reset_at: Optional[str], now: dt.datetime, default_s: float, max_s: float) -> float:
    if reset_at:
        try:
            r = dt.datetime.fromisoformat(reset_at)
            if r.tzinfo is not None:
                r = r.astimezone().replace(tzinfo=None)
            s = (r - now).total_seconds()
            if s > 0:
                return min(s + 60, max_s)
        except ValueError:
            pass
    return min(default_s, max_s)


def _safe_eval(col: Collection, it: Dict[str, Any], run_dir: Path, evaluator) -> None:
    try:
        it["eval"] = evaluator(run_dir)
    except Exception as exc:  # noqa: BLE001 - eval is a report, never a blocker
        it["eval"] = {"error": f"{type(exc).__name__}: {exc}"}
    col.save()


def _run_digests(col, by, newly_done, summary, limits, package_subject, vault_subject_dir,
                 digester, emit, say, as_of, policy, now, sleep, wait_default, wait_max) -> None:
    digest_dir = (Path(vault_subject_dir) if package_subject and vault_subject_dir is not None
                  else col.runs_dir)
    done = [i for i in col.items.values() if i["state"] in ("noted", "packaged")]
    keys = set(span_keys(by, [i.get("published") for i in newly_done]))
    keys |= set(col.data.get("pending_digests", {}).get(by, []))
    out_dir = digest_dir / "digests"
    for k in span_keys(by, [i.get("published") for i in done]):
        if not (out_dir / f"{k}.md").exists():
            keys.add(k)
    if not keys:
        say("digests: no affected span")
        col.data.setdefault("pending_digests", {})[by] = []
        return
    budget = None
    if limits.max_calls is not None:
        budget = max(0, limits.max_calls - summary.calls)

    def built(r):
        rec = {"span": r.span, "path": str(r.path), "episodes": r.episodes, "calls": r.calls,
               "cached": r.cached, "citations": r.citations, "dropped": r.dropped,
               "link_check": "pass" if not r.link_problems else "FAIL",
               "partial": r.partial, "written": r.written, "built_at": _iso()}
        col.data.setdefault("digests", {})[f"{by}:{r.span}"] = rec
        summary.digests.append(rec)
        summary.digest_calls += r.calls
        col.save()
        emit(f"{DIGEST_PREFIX}{r.span} {r.path}")

    from video2mdnotes.core.summarizer import SubscriptionExhausted
    while True:
        try:
            _, left = digester(digest_dir, by, sorted(keys), max_calls=budget, as_of=as_of,
                               on_built=built, progress=say)
            break
        except SubscriptionExhausted as exc:
            reset = exc.reset_at.isoformat() if getattr(exc, "reset_at", None) else None
            ev = col.event("exhausted", item=f"digest:{by}", reset_at=reset, policy=policy)
            wait_s = _wait_seconds(reset, now(), wait_default, wait_max)
            if policy != "wait" or (limits.deadline and now() + dt.timedelta(seconds=wait_s)
                                    > limits.deadline):
                left = sorted(keys)
                summary.stop_reason = "subscription exhausted during digests"
                break
            ev["waited_s"] = wait_s
            summary.waits.append({"item": f"digest:{by}", "seconds": wait_s, "reset_at": reset})
            col.save()
            sleep(wait_s)
    built_keys = {d["span"] for d in summary.digests}
    left = [k for k in left if k not in built_keys]
    col.data.setdefault("pending_digests", {})[by] = left
    summary.pending_digests = left
    if left and not summary.stop_reason:
        summary.stop_reason = f"--max-calls reached before digests {', '.join(left)}"
    col.save()


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def counts(col: Collection) -> Dict[str, int]:
    c = {s: 0 for s in STATES}
    for it in col.items.values():
        c[it["state"]] = c.get(it["state"], 0) + 1
    return c


def remaining(col: Collection, package: bool = True, max_attempts: int = DEFAULT_MAX_ATTEMPTS
              ) -> List[Dict[str, Any]]:
    out = []
    for it in col.queue():
        s = it["state"]
        if s in ("selected", "packaged") or (s == "noted" and not package):
            continue
        if s == "failed" and ((it.get("attempts") or 0) >= max_attempts or it.get("permanent")):
            continue
        out.append(it)
    return out


def estimate_remaining(col: Collection, items: Sequence[Dict[str, Any]]) -> Tuple[int, float]:
    """(calls, minutes) for `items`; minutes use the median observed wall time
    per media-minute when this collection has finished items. Items with notes
    or with an existing package in the subject folder cost nothing."""
    sd = col.data.get("vault_subject_dir")
    sd = Path(sd) if sd else None
    items = [i for i in items if not summary_file(item_run_dir(col, i))
             and (i["state"] == "packaged" or find_package(sd, i["id"]) is None)]
    calls = sum(estimate_calls(i.get("duration_s")) for i in items)
    observed = [i["wall_s"] / max(1.0, (i.get("duration_s") or 1200) / 60)
                for i in col.items.values()
                if i.get("wall_s") and i["state"] in ("noted", "packaged") and not i.get("reused")]
    mins = 0.0
    for i in items:
        if summary_file(item_run_dir(col, i)):
            continue
        if observed:
            mins += statistics.median(observed) * (i.get("duration_s") or 1200) / 60 / 60
        else:
            mins += estimate_minutes(i.get("duration_s"))
    return calls, mins


def render_status(col: Collection, *, next_n: int = 5) -> str:
    c = counts(col)
    rem = remaining(col)
    calls, mins = estimate_remaining(col, rem)
    srcs = ", ".join(s.get("title") or s.get("url") or s.get("kind") for s in col.data.get("sources", []))
    lines = [f"Collection {col.name} · subject {col.subject or '-'} · {srcs}",
             f"Manifest: {col.path}",
             "States: " + " · ".join(f"{k} {v}" for k, v in c.items()),
             f"Remaining: {len(rem)} item(s) · est. {calls} model call(s) · ~{mins:.0f} min",
             ""]
    if rem:
        lines.append("Next:")
        for it in rem[:next_n]:
            lines.append(f"  {it.get('published') or '?':<10}  {it['id']}  {it['state']:<11}  "
                         f"{_hms(it.get('duration_s'))}  {it.get('title') or ''}")
        lines.append("")
    fails = [i for i in col.items.values() if i["state"] == "failed"]
    for it in fails:
        lines.append(f"failed: {it['id']} (attempts {it.get('attempts')}): {(it.get('error') or '')[:150]}")
    pend = {k: v for k, v in (col.data.get("pending_digests") or {}).items() if v}
    if pend:
        lines.append(f"Pending digests: {pend}")
    for k, d in sorted((col.data.get("digests") or {}).items()):
        lines.append(f"digest {k}: {d['episodes']} ep · {d['citations']} cites ({d['dropped']} dropped) · "
                     f"links {d['link_check']}{' · partial' if d.get('partial') else ''} · {d['path']}")
    for ev in (col.data.get("events") or [])[-5:]:
        lines.append(f"event {ev['at']} {ev['kind']} {ev.get('item', '')}")
    if col.data.get("runs"):
        r = col.data["runs"][-1]
        lines.append(f"Last run {r['started']} → {r['finished']}: {r['processed']} processed, "
                     f"{r['reused']} reused, {r['failed']} failed, {r['calls']} + "
                     f"{r['digest_calls']} digest call(s){'; stopped: ' + r['stop_reason'] if r['stop_reason'] else ''}")
    return "\n".join(lines)


def render_selection(col: Collection, matched: Sequence[Dict[str, Any]], f: Filters,
                     subject_dir: Optional[Path] = None) -> str:
    """The matches with their state; `packaged*` = a package for the id
    already exists in `subject_dir` (it will be adopted, not re-made)."""
    states: Dict[str, str] = {}
    for e in matched:
        st = (col.items.get(e["id"]) or {}).get("state", "new")
        if st != "packaged" and find_package(subject_dir, e["id"]) is not None:
            st = "packaged*"
        states[e["id"]] = st
    todo = [e for e in matched if not states[e["id"]].startswith("packaged")]
    est = sum(estimate_calls(e.get("duration_s")) for e in todo
              if not summary_file(item_run_dir(col, col.items.get(e["id"]) or
                                                {"id": e["id"], "published": e.get("published")})))
    total_s = sum(e.get("duration_s") or 0 for e in matched)
    reuse = len(matched) - len(todo)
    lines = [f"Selection: {f.describe()} -> {len(matched)} episode(s), {_hms(total_s)} of media; "
             f"{reuse} already packaged (reused); est. {est} model call(s) for the "
             f"{len(todo)} to do",
             f"{'#':>5}  {'date':<10}  {'id':<11}  {'dur':>7}  {'state':<11}  title"]
    for e in matched:
        st = states[e["id"]]
        lines.append(f"{e.get('index') or '':>5}  {e.get('published') or '?':<10}  {e['id']:<11}  "
                     f"{_hms(e.get('duration_s')):>7}  {st:<11}  {e.get('title') or ''}")
    return "\n".join(lines)


def _hms(seconds: Optional[float]) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


__all__ = ["Collection", "CollectionError", "Filters", "Limits", "Locked", "Outcome",
           "RunSummary", "SubprocessRunner", "apply_selection", "build_listing", "count_calls",
           "default_digester", "ensure_listing", "estimate_calls", "find_package",
           "listing_built_at", "listing_stale_reason", "load_listing",
           "parse_duration", "render_selection", "render_status", "run_collection",
           "select_entries", "until_deadline"]
