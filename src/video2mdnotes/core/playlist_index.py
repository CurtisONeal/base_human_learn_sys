"""A free index of a whole playlist as one Obsidian note (`playlist-index`). No model calls.

    playlist-index <playlist-url> --out <vault>/…/_index.md

Cost model (measured on The AI Daily Brief, 1,202 entries, 2026-10-20):

* **Flat listing** — ``extract_flat`` reads the playlist tab pages (100
  entries each): ~6 s for all 1,202. It gives id, title and duration, but
  NO upload date and NO description.
* **Per-entry metadata** — one watch-page GET each. yt-dlp's full
  extraction is ~1.9 s per video (player JS, formats); the watch page alone
  is ~0.8 s and already holds ``uploadDate`` / ``publishDate`` and
  ``shortDescription`` (microformat + player response JSON). So the page is
  fetched directly and parsed; yt-dlp is the fallback when the page does
  not parse (consent wall, layout change).

Politeness and resume:

* One request at a time, ``DELAY_S`` + jitter between requests.
* HTTP 429 / 5xx / network errors back off exponentially (``BACKOFF_S`` ×
  2^n, ``MAX_RETRIES``); ``MAX_CONSECUTIVE_FAILURES`` failed entries in a
  row stop the run (YouTube is throttling — come back later).
* Every result is kept in ``<cache>`` (``previous_run_results/_playlist_cache/
  <playlist_id>.json``), written atomically every ``SAVE_EVERY`` fetches and
  on exit (Ctrl-C included). A rerun fetches only entries not yet in the
  cache — new episodes at the top of a newest-first list, or the rest of an
  interrupted run. ``--max-fetches N`` bounds one invocation (batches).
* Entries are fetched in playlist order, so a partial index is complete
  from the newest end.

Status column: an episode is ``packaged`` when a learning package under
``<vault>/<learning_root>/<subject>/*/source_meta.json`` has its YouTube id
(linked relative to the index note), ``notes`` when a run dir under
``--runs-root`` has its source id and a ``summaries/*.summary.md``,
otherwise blank.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CACHE_VERSION = 1
DELAY_S = 1.0
JITTER_S = 0.5
BACKOFF_S = 5.0
MAX_RETRIES = 4
MAX_CONSECUTIVE_FAILURES = 8
SAVE_EVERY = 20
DESCRIPTION_KEEP = 600          # chars of the description kept in the cache
FIRST_SENTENCE_MAX = 220

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_DATE_RE = re.compile(r'"(?:uploadDate|publishDate)":"(\d{4}-\d{2}-\d{2})')
_DESC_RE = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')
_URL_RE = re.compile(r"https?://\S+|www\.\S+")


_GONE_RE = re.compile(r"video is unavailable|private video|has been removed|"
                      r"account associated with this video has been terminated", re.I)


def is_gone(cached: Optional[Dict[str, Any]]) -> bool:
    """A cached failure that will not change on retry (private / removed video)."""
    return bool(cached) and (cached.get("unavailable") is True
                             or bool(_GONE_RE.search(cached.get("error") or "")))


class Throttled(RuntimeError):
    """Too many consecutive failures: stop and keep the cache."""


# --- fetching -------------------------------------------------------------------------


def parse_watch_page(html: str) -> Dict[str, Any]:
    """{'upload_date': 'YYYY-MM-DD' | None, 'description': str | None} from a watch page."""
    m = _DATE_RE.search(html or "")
    d = _DESC_RE.search(html or "")
    desc = None
    if d:
        try:
            desc = json.loads('"' + d.group(1) + '"')
        except ValueError:
            desc = None
    return {"upload_date": m.group(1) if m else None, "description": desc}


def fetch_watch_page(video_id: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed host
        return r.read().decode("utf-8", "replace")


def ytdlp_meta(video_id: str) -> Dict[str, Any]:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    raw = info.get("upload_date") or ""
    date = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if re.fullmatch(r"\d{8}", raw) else None
    return {"upload_date": date, "description": info.get("description")}


def default_meta(video_id: str) -> Dict[str, Any]:
    """Date + description of one video: watch page first, yt-dlp if it does not parse."""
    page = parse_watch_page(fetch_watch_page(video_id))
    if page["upload_date"]:
        page["via"] = "watch-page"
        return page
    meta = ytdlp_meta(video_id)
    meta["via"] = "yt-dlp"
    return meta


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def first_sentence(text: Optional[str], limit: int = FIRST_SENTENCE_MAX) -> str:
    """The description's first sentence, links removed, capped."""
    t = " ".join(_URL_RE.sub("", text or "").split())
    if not t:
        return ""
    m = re.search(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'(])", t)
    s = t[:m.start()] if m else t
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + " …"
    return s


# --- cache ----------------------------------------------------------------------------


def load_cache(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                return data
        except (OSError, ValueError):
            pass
    return {"version": CACHE_VERSION, "entries": {}}


def save_cache(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- build ----------------------------------------------------------------------------


@dataclass
class IndexEntry:
    index: int
    id: str
    title: str
    duration_s: Optional[float]
    upload_date: Optional[str] = None
    description: str = ""
    status: str = ""
    link: Optional[str] = None


@dataclass
class IndexResult:
    url: str
    playlist_id: Optional[str]
    playlist_title: Optional[str]
    entries: List[IndexEntry] = field(default_factory=list)
    listed: int = 0
    unavailable: int = 0
    fetched: int = 0                # this invocation
    fetch_failed: int = 0
    gone: int = 0                   # private / removed (cached, never retried)
    from_cache: int = 0
    stopped: str = ""
    list_seconds: float = 0.0
    fetch_seconds: float = 0.0
    cache_path: Optional[Path] = None

    @property
    def dated(self) -> int:
        return sum(1 for e in self.entries if e.upload_date)


def build_index(url: str, cache_path: Optional[Path] = None, *,
                cache_root: Optional[Path] = None,
                max_fetches: Optional[int] = None,
                list_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
                meta_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
                sleep: Callable[[float], None] = time.sleep,
                delay: float = DELAY_S, jitter: float = JITTER_S,
                progress: Callable[[str], None] = lambda m: None) -> IndexResult:
    """Flat listing + cached per-entry date/description (see module doc)."""
    from video2mdnotes.core.playlist import default_list
    list_fn = list_fn or default_list
    meta_fn = meta_fn or default_meta

    t0 = time.monotonic()
    top = list_fn(url)
    raw_entries = list(top.get("entries") or [])
    res = IndexResult(url=url, playlist_id=top.get("id"), playlist_title=top.get("title"),
                      listed=len(raw_entries), list_seconds=time.monotonic() - t0)
    if cache_path is None:
        root = cache_root or Path("previous_run_results") / "_playlist_cache"
        cache_path = Path(root) / f"{res.playlist_id or 'playlist'}.json"
    cache = load_cache(cache_path)
    cache.update(playlist_id=res.playlist_id, url=url, title=res.playlist_title)
    known: Dict[str, Any] = cache.setdefault("entries", {})

    for i, raw in enumerate(raw_entries, 1):
        if not raw or not raw.get("id"):
            res.unavailable += 1
            continue
        res.entries.append(IndexEntry(index=i, id=str(raw["id"]),
                                      title=raw.get("title") or "untitled",
                                      duration_s=raw.get("duration")))

    t1 = time.monotonic()
    since_save = 0
    consecutive = 0
    try:
        for e in res.entries:
            c = known.get(e.id)
            if c and (c.get("upload_date") or is_gone(c)):
                res.from_cache += 1
                continue
            if max_fetches is not None and res.fetched + res.fetch_failed >= max_fetches:
                res.stopped = f"--max-fetches {max_fetches} reached"
                break
            if res.fetched or res.fetch_failed:
                sleep(delay + random.uniform(0, jitter))
            meta, err = None, None
            for attempt in range(MAX_RETRIES + 1):
                try:
                    meta = meta_fn(e.id)
                    break
                except Exception as exc:  # noqa: BLE001 - retried / recorded
                    err = exc
                    if attempt >= MAX_RETRIES or not _retryable(exc):
                        break
                    wait = BACKOFF_S * (2 ** attempt)
                    progress(f"  {e.id}: {type(exc).__name__} — backing off {wait:.0f}s")
                    sleep(wait)
            if meta and meta.get("upload_date"):
                known[e.id] = {"upload_date": meta["upload_date"],
                               "description": (meta.get("description") or "")[:DESCRIPTION_KEEP],
                               "via": meta.get("via", ""),
                               "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
                res.fetched += 1
                consecutive = 0
            else:
                res.fetch_failed += 1
                known[e.id] = {"upload_date": None, "error": f"{type(err).__name__}: {err}"[:200]
                               if err else "no date in metadata",
                               "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
                if is_gone(known[e.id]):
                    known[e.id]["unavailable"] = True   # never retried
                    consecutive = 0
                else:
                    consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    raise Throttled(f"{consecutive} consecutive failures; stopping (cache kept)")
            since_save += 1
            if since_save >= SAVE_EVERY:
                save_cache(cache_path, cache)
                since_save = 0
                progress(f"  {res.fetched + res.from_cache}/{len(res.entries)} dated "
                         f"({res.fetched} fetched now, {time.monotonic() - t1:.0f}s)")
    except Throttled as exc:
        res.stopped = str(exc)
    except KeyboardInterrupt:
        res.stopped = "interrupted (cache kept)"
    finally:
        save_cache(cache_path, cache)
        res.fetch_seconds = time.monotonic() - t1

    for e in res.entries:
        c = known.get(e.id) or {}
        if is_gone(c):
            e.status = "unavailable"
            res.gone += 1
        e.upload_date = c.get("upload_date")
        e.description = first_sentence(c.get("description"))
    res.cache_path = cache_path
    return res


# --- status ---------------------------------------------------------------------------


def find_packages(subject_dir: Optional[Path]) -> Dict[str, Path]:
    """youtube id -> notes.md of every package directly under `subject_dir`."""
    out: Dict[str, Path] = {}
    if not subject_dir or not Path(subject_dir).is_dir():
        return out
    for meta in sorted(Path(subject_dir).glob("*/source_meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        yid = ((data.get("source") or {}).get("youtube_id"))
        notes = meta.parent / "notes.md"
        if yid and notes.exists():
            out[yid] = notes
    return out


def find_runs(runs_root: Optional[Path], ids: set) -> Dict[str, Path]:
    """source id -> run dir with finished notes, for ids in `ids`."""
    out: Dict[str, Path] = {}
    if not runs_root or not Path(runs_root).is_dir():
        return out
    for meta in sorted(Path(runs_root).rglob("source_meta.json")):
        try:
            sid = json.loads(meta.read_text(encoding="utf-8")).get("source_id")
        except (OSError, ValueError):
            continue
        if sid in ids and sid not in out and any((meta.parent / "summaries").glob("*.summary.md")):
            out[sid] = meta.parent
    return out


def apply_status(res: IndexResult, index_path: Path, subject_dir: Optional[Path],
                 runs_root: Optional[Path]) -> None:
    from video2mdnotes.core.package import link_path
    pkgs = find_packages(subject_dir)
    runs = find_runs(runs_root, {e.id for e in res.entries} - set(pkgs))
    for e in res.entries:
        if e.status == "unavailable":
            continue
        if e.id in pkgs:
            e.status = "packaged"
            e.link = link_path(os.path.relpath(pkgs[e.id], index_path.parent))
        elif e.id in runs:
            e.status = "notes"


# --- render ---------------------------------------------------------------------------


def _hms(seconds: Optional[float]) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def render_index(res: IndexResult, built_at: Optional[str] = None) -> str:
    from video2mdnotes.core.notes_format import sanitize_cell as cell

    def q(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)
    built_at = built_at or dt.datetime.now().isoformat(timespec="seconds")
    total_s = sum(e.duration_s or 0 for e in res.entries)
    done = sum(1 for e in res.entries if e.status in ("notes", "packaged"))
    packaged = sum(1 for e in res.entries if e.status == "packaged")
    dates = sorted(e.upload_date for e in res.entries if e.upload_date)
    lines = ["---",
             f"playlist: {q(res.playlist_title or '')}",
             f"playlist_id: {q(res.playlist_id or '')}",
             f"url: {q(res.url)}",
             f"count: {len(res.entries)}",
             f"dated: {res.dated}",
             f"unavailable: {res.gone}",
             f"notes_done: {done}",
             f"packaged: {packaged}",
             f"total_duration: {q(_hms(total_s))}",
             f"first_date: {dates[0] if dates else 'null'}",
             f"last_date: {dates[-1] if dates else 'null'}",
             f"built_at: {built_at}",
             "generated_by: video2mdnotes.playlist-index/1",
             "tags: [playlist_index]",
             "---",
             f"# {res.playlist_title or 'Playlist'} — episode index",
             "",
             f"> {len(res.entries)} episodes · {_hms(total_s)} total · {res.dated} dated · "
             f"{done} with notes ({packaged} packaged) · built {built_at} by `playlist-index` "
             "(flat listing + one watch-page read per episode, cached; no model calls). "
             f"Source: [{res.playlist_title or 'playlist'}]({res.url})",
             ""]
    if res.gone:
        lines += [f"> {res.gone} episode(s) are private or removed on YouTube (status "
                  "`unavailable`; not retried).", ""]
    if res.dated + res.gone < len(res.entries):
        lines += [f"> [!note] {len(res.entries) - res.dated - res.gone} episode(s) have no date yet — "
                  "re-run `playlist-index` to fetch the rest (cached, resumable).", ""]
    lines += ["| # | date | duration | title | status | description |",
              "|---:|---|---:|---|---|---|"]
    for e in res.entries:
        title = cell(e.title).replace("[", "(").replace("]", ")")
        status = f"[packaged]({e.link})" if e.status == "packaged" and e.link else e.status
        lines.append(f"| {e.index} | {e.upload_date or ''} | {_hms(e.duration_s)} | "
                     f"[{title}](https://www.youtube.com/watch?v={e.id}) | {status} | "
                     f"{cell(e.description)} |")
    return "\n".join(lines) + "\n"


__all__ = ["IndexEntry", "IndexResult", "apply_status", "build_index", "default_meta",
           "find_packages", "find_runs", "first_sentence", "load_cache", "parse_watch_page",
           "render_index", "save_cache"]
