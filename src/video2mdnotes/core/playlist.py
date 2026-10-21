"""Pick a few entries out of a large playlist without probing all of it.

`process <playlist> --limit N / --items "1-3,7" / --since / --until /
--dry-run-list`. The plain `probe_source` path extracts FULL metadata for
every entry — for The AI Daily Brief that is 1,202 watch-page fetches before
the first download. Here:

1. **Flat, lazy listing.** ``extract_info(url, process=False)`` with
   ``extract_flat`` returns the playlist with its entries as a generator; the
   YouTube tab pages are fetched only as far as the loop reads (100 entries
   per page). A flat entry has id, title, URL and usually duration — but no
   upload date.
2. **Index filters first.** ``--items`` (yt-dlp ``playlist_items`` syntax:
   ``1-3,7``, ``10-`` open-ended, ``a:b`` as a range) and ``--limit`` are
   decided from the playlist position alone: no per-entry fetch, and the
   listing stops at the last index that can still match.
3. **Dates lazily, for candidates only.** ``--since`` / ``--until`` need the
   upload date, so the full metadata of an entry that passes the index
   filters is fetched (one watch-page request each) — and kept, so the run
   does not fetch it again.
4. **Early stop.** Once the dates seen so far are newest-first and
   ``STOP_AFTER_OLDER`` consecutive entries are older than ``--since``, the
   rest of the playlist cannot match and the listing stops. A playlist that
   is not newest-first is read to the end (bounded by ``MAX_DATE_PROBES``
   metadata fetches, reported when hit).

Selection order is playlist order. ``--limit`` applies after the other
filters ("the first N that match").
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

STOP_AFTER_OLDER = 2
MAX_DATE_PROBES = 300

InfoFn = Callable[[str], Dict[str, Any]]


# --- --items ---------------------------------------------------------------------------

_ITEM = re.compile(r"^(\d+)(?:\s*[-:]\s*(\d*))?$")


def parse_items(spec: str) -> List[Tuple[int, Optional[int]]]:
    """yt-dlp ``playlist_items`` subset: ``"1-3,7,10-"`` -> [(1,3),(7,7),(10,None)].

    1-based, inclusive. Negative (from-the-end) indices and steps need the
    whole listing and are refused."""
    out: List[Tuple[int, Optional[int]]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _ITEM.match(part)
        if not m:
            raise ValueError(f"--items: cannot read {part!r} (use e.g. \"1-3,7,10-\"; "
                             "negative indices and steps are not supported)")
        a = int(m.group(1))
        if a < 1:
            raise ValueError("--items: playlist positions start at 1")
        if m.group(2) is None:
            b: Optional[int] = a
        elif m.group(2) == "":
            b = None
        else:
            b = int(m.group(2))
            if b < a:
                raise ValueError(f"--items: range {part!r} ends before it starts")
        out.append((a, b))
    if not out:
        raise ValueError("--items: empty selection")
    return out


def items_max(ranges: Iterable[Tuple[int, Optional[int]]]) -> Optional[int]:
    """Last position that can match; None when a range is open-ended."""
    ends = [b for _, b in ranges]
    return None if any(b is None for b in ends) else max(ends)


def in_items(index: int, ranges: Iterable[Tuple[int, Optional[int]]]) -> bool:
    return any(a <= index and (b is None or index <= b) for a, b in ranges)


def parse_date(value: Optional[str], flag: str) -> Optional[dt.date]:
    if value is None or not str(value).strip():
        return None
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"{flag}: expected YYYY-MM-DD, got {value!r}")


# --- selection -----------------------------------------------------------------------


@dataclass
class Selection:
    limit: Optional[int] = None
    items: Optional[str] = None
    since: Optional[dt.date] = None
    until: Optional[dt.date] = None

    def __post_init__(self):
        if self.limit is not None and self.limit < 1:
            raise ValueError("--limit must be >= 1")
        if self.since and self.until and self.since > self.until:
            raise ValueError("--since is after --until")
        self.ranges = parse_items(self.items) if self.items else None

    @property
    def active(self) -> bool:
        return bool(self.limit or self.items or self.since or self.until)

    @property
    def by_date(self) -> bool:
        return bool(self.since or self.until)

    def describe(self) -> str:
        parts = []
        if self.items:
            parts.append(f"items {self.items}")
        if self.since:
            parts.append(f"since {self.since}")
        if self.until:
            parts.append(f"until {self.until}")
        if self.limit:
            parts.append(f"first {self.limit}")
        return ", ".join(parts) or "all"


@dataclass
class Entry:
    index: int                          # 1-based playlist position
    id: str
    title: str
    url: str
    duration_s: Optional[float] = None
    upload_date: Optional[dt.date] = None
    info: Optional[Dict[str, Any]] = None   # full metadata, when fetched

    @property
    def date_text(self) -> str:
        return self.upload_date.isoformat() if self.upload_date else "unknown"

    @property
    def duration_text(self) -> str:
        if not self.duration_s:
            return "?"
        s = int(self.duration_s)
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


@dataclass
class Listing:
    url: str
    is_playlist: bool
    playlist_title: Optional[str] = None
    playlist_id: Optional[str] = None
    playlist_count: Optional[int] = None
    entries: List[Entry] = field(default_factory=list)
    scanned: int = 0                    # flat entries read
    metadata_fetches: int = 0           # full per-entry fetches
    stopped: str = ""                   # why the listing stopped early
    order: str = "unknown"              # newest-first | oldest-first | unknown
    warnings: List[str] = field(default_factory=list)


def _upload_date(info: Dict[str, Any]) -> Optional[dt.date]:
    raw = info.get("upload_date") or info.get("release_date")
    if raw and re.fullmatch(r"\d{8}", str(raw)):
        return dt.datetime.strptime(str(raw), "%Y%m%d").date()
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        try:
            return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc).date()
        except (TypeError, ValueError, OSError):
            return None
    return None


def _entry_url(e: Dict[str, Any]) -> str:
    url = e.get("webpage_url") or e.get("url") or ""
    if url and not url.startswith("http") and e.get("ie_key") == "Youtube":
        url = f"https://www.youtube.com/watch?v={url}"
    if not url and e.get("id"):
        url = f"https://www.youtube.com/watch?v={e['id']}"
    return url


def default_list(url: str) -> Dict[str, Any]:
    """Flat, lazy playlist listing (no per-entry fetch)."""
    import yt_dlp
    ydl = yt_dlp.YoutubeDL({"quiet": True, "extract_flat": "in_playlist",
                            "skip_download": True})
    return ydl.extract_info(url, download=False, process=False)


def default_info(url: str) -> Dict[str, Any]:
    """Full metadata of ONE video (the same request probe_source makes)."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        return ydl.extract_info(url, download=False)


def select(url: str, sel: Selection, *, need_dates: bool = False,
           list_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
           info_fn: Optional[InfoFn] = None,
           progress: Callable[[str], None] = lambda m: None) -> Listing:
    """The selected entries of `url`, in playlist order (see module doc).

    `need_dates` also fetches the date of every SELECTED entry that has none
    (for --dry-run-list); never of an entry that was not selected."""
    list_fn = list_fn or default_list
    info_fn = info_fn or default_info
    top = list_fn(url)
    if top.get("_type") not in ("playlist", "multi_video") and "entries" not in top:
        # A single video: one entry, with the info yt-dlp already returned.
        info = top if top.get("title") else None
        e = Entry(index=1, id=str(top.get("id") or ""), title=top.get("title") or url,
                  url=top.get("webpage_url") or url, duration_s=top.get("duration"),
                  upload_date=_upload_date(top) if info else None)
        listing = Listing(url=url, is_playlist=False, scanned=1)
        if (sel.by_date or need_dates) and e.upload_date is None:
            e.info = info_fn(e.url)
            listing.metadata_fetches += 1
            e.upload_date = _upload_date(e.info)
            e.duration_s = e.duration_s or e.info.get("duration")
        if _date_ok(e.upload_date, sel) and (not sel.ranges or in_items(1, sel.ranges)):
            listing.entries.append(e)
        return listing

    listing = Listing(url=url, is_playlist=True, playlist_title=top.get("title"),
                      playlist_id=top.get("id"),
                      playlist_count=top.get("playlist_count"))
    last = items_max(sel.ranges) if sel.ranges else None
    dated: List[dt.date] = []
    older_streak = 0
    for index, raw in enumerate(top.get("entries") or [], 1):
        listing.scanned = index
        if last is not None and index > last:
            listing.stopped = f"past the last --items position ({last})"
            break
        if raw is None:
            listing.warnings.append(f"entry {index} is unavailable (private/deleted)")
            continue
        if sel.ranges and not in_items(index, sel.ranges):
            continue
        e = Entry(index=index, id=str(raw.get("id") or ""), title=raw.get("title") or "untitled",
                  url=_entry_url(raw), duration_s=raw.get("duration"),
                  upload_date=_upload_date(raw))
        if sel.by_date:
            if e.upload_date is None:
                if listing.metadata_fetches >= MAX_DATE_PROBES:
                    listing.stopped = (f"metadata-fetch cap reached ({MAX_DATE_PROBES}); "
                                       "narrow the range with --items")
                    listing.warnings.append(listing.stopped)
                    break
                progress(f"  date of #{index} {e.id} …")
                try:
                    e.info = info_fn(e.url)
                except Exception as exc:  # noqa: BLE001 - one bad entry is not fatal
                    listing.warnings.append(f"entry {index} ({e.id}): metadata failed: {exc}")
                    continue
                listing.metadata_fetches += 1
                e.upload_date = _upload_date(e.info)
                e.duration_s = e.duration_s or e.info.get("duration")
                e.title = e.info.get("title") or e.title
            if e.upload_date is None:
                listing.warnings.append(f"entry {index} ({e.id}): no upload date; skipped")
                continue
            dated.append(e.upload_date)
            listing.order = _order(dated)
            if sel.until and e.upload_date > sel.until:
                older_streak = 0
                continue
            if sel.since and e.upload_date < sel.since:
                older_streak += 1
                if listing.order == "newest-first" and older_streak >= STOP_AFTER_OLDER:
                    listing.stopped = (f"newest-first playlist; {older_streak} consecutive "
                                       f"entries older than --since {sel.since}")
                    break
                continue
            older_streak = 0
        listing.entries.append(e)
        if sel.limit and len(listing.entries) >= sel.limit:
            listing.stopped = f"--limit {sel.limit} reached"
            break

    if need_dates:
        for e in listing.entries:
            if e.upload_date is None:
                try:
                    e.info = info_fn(e.url)
                except Exception as exc:  # noqa: BLE001
                    listing.warnings.append(f"entry {e.index} ({e.id}): metadata failed: {exc}")
                    continue
                listing.metadata_fetches += 1
                e.upload_date = _upload_date(e.info)
                e.duration_s = e.duration_s or e.info.get("duration")
    return listing


def _date_ok(d: Optional[dt.date], sel: Selection) -> bool:
    if not sel.by_date:
        return True
    if d is None:
        return False
    return not ((sel.since and d < sel.since) or (sel.until and d > sel.until))


def _order(dated: List[dt.date]) -> str:
    """newest-first when the dates seen never increase and fall at least once."""
    if len(dated) < 2:
        return "unknown"
    ups = sum(1 for a, b in zip(dated, dated[1:]) if b > a)
    downs = sum(1 for a, b in zip(dated, dated[1:]) if b < a)
    if downs and not ups:
        return "newest-first"
    if ups and not downs:
        return "oldest-first"
    return "unknown" if ups == downs else ("newest-first" if downs > ups else "oldest-first")


def render_listing(listing: Listing, sel: Selection) -> str:
    head = (f"Playlist: {listing.playlist_title or listing.url}"
            + (f" ({listing.playlist_count} entries)" if listing.playlist_count else "")
            if listing.is_playlist else f"Video: {listing.url}")
    lines = [head,
             f"Selection: {sel.describe()} -> {len(listing.entries)} entr"
             f"{'y' if len(listing.entries) == 1 else 'ies'} "
             f"(scanned {listing.scanned} listing entr{'y' if listing.scanned == 1 else 'ies'}, "
             f"{listing.metadata_fetches} metadata fetch(es)"
             + (f"; stopped: {listing.stopped}" if listing.stopped else "") + ")",
             "",
             f"{'index':>5}  {'id':<11}  {'date':<10}  {'duration':>8}  title"]
    for e in listing.entries:
        lines.append(f"{e.index:>5}  {e.id:<11}  {e.date_text:<10}  {e.duration_text:>8}  {e.title}")
    for w in listing.warnings:
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def to_sources(listing: Listing, info_fn: Optional[InfoFn] = None):
    """SourceInfo for every selected entry, fetching full metadata only for
    the ones not already fetched while selecting."""
    from video2mdnotes.core.downloader import source_from_info
    info_fn = info_fn or default_info
    out = []
    for e in listing.entries:
        info = e.info if e.info is not None else info_fn(e.url)
        if not listing.is_playlist:
            out.append(source_from_info(info, listing.url))
        else:
            out.append(source_from_info(info, e.url, listing.playlist_title, listing.playlist_id))
    return out


__all__ = ["Entry", "Listing", "MAX_DATE_PROBES", "STOP_AFTER_OLDER", "Selection",
           "in_items", "items_max", "parse_date", "parse_items", "render_listing", "select",
           "to_sources"]
