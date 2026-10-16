"""Publication metadata for a source, persisted as `source_meta.json`.

yt-dlp's `extract_info` already returns when a video was published, by whom,
how long it is and — often — its chapter list. The pipeline used to keep only
title/tags/description (for the Whisper vocabulary hint) and throw the rest
away, so every output carried only the *capture* date. For time-sensitive
material ("as of the July 2026 exam guide") that is the wrong date to cite.

This module turns an info dict into a `SourceMeta`, writes it next to the
transcript, and renders a one-line header a prompt can carry. Nothing here
reaches the network or reads config.

Chapter honesty: `chapter_source` says where the list came from.

    platform     info['chapters'] was non-empty (YouTube, or yt-dlp's own
                 description parser — yt-dlp does not say which)
    description  we parsed timestamp lines out of the description ourselves
    none         neither produced a plausible list

A description is untrusted free text, so a parsed list is only accepted when it
looks like a real chapter list: at least 3 entries, strictly increasing, all
before the end of the video.
"""

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from video2mdnotes.core.sources import Chapter

META_FILENAME = "source_meta.json"
MIN_DESCRIPTION_CHAPTERS = 3

# A line that starts (after optional bullets, dashes, emoji, brackets) with
# HH:MM:SS or MM:SS, followed by a title. `[^\w]` covers emoji and bullet
# characters without enumerating them; digits are \w so the timestamp is not
# swallowed by the prefix.
_TIMESTAMP_LINE = re.compile(
    r"^[^\w]*?(?P<ts>(?:\d{1,2}:)?\d{1,2}:\d{2})[)\]]?\s*[-–—:|.]?\s*(?P<title>\S.*?)\s*$"
)


class SourceMeta(BaseModel):
    source_id: str
    title: str = ""
    url: str = ""
    # None means unknown. Never fill this with the capture date.
    published_at: Optional[dt.date] = None
    published_by: Optional[str] = None
    channel: Optional[str] = None
    duration_s: Optional[float] = None
    chapters: List[Chapter] = Field(default_factory=list)
    chapter_source: Literal["platform", "description", "none"] = "none"
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    live_status: Optional[str] = None
    captured_at: dt.datetime = Field(default_factory=dt.datetime.now)
    # Local media only: the container's creation_time tag. This is the
    # recording DEVICE's clock, which is frequently wrong (never set, reset to
    # 2000-01-01, wrong timezone). It is not a publish date and must be cited
    # as "recorded (device clock)", if at all.
    recorded_at: Optional[dt.datetime] = None
    recorded_at_note: Optional[str] = None

    def write(self, run_dir: Path) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / META_FILENAME
        data = json.loads(self.model_dump_json())
        # yt-dlp-style aliases read by the long-form evaluator
        # (eval/longform.py: `duration`, chapters[].start_time). Without
        # `duration` the evaluator found no duration and P3 failed on every
        # run the pipeline wrote. Ignored on read (pydantic drops extras).
        data["duration"] = self.duration_s
        for chapter in data.get("chapters", []):
            chapter["start_time"] = chapter.get("start_s")
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def read(cls, run_dir: Path) -> Optional["SourceMeta"]:
        """Load `source_meta.json` from a run dir; None if it was never written."""
        path = Path(run_dir) / META_FILENAME
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def source_text_fields(self) -> Dict[str, Any]:
        """The subset of fields `SourceText` carries, for `model_copy(update=...)`."""
        return {
            "published_at": self.published_at,
            "published_by": self.published_by,
            "duration_s": self.duration_s,
            "chapters": [c.model_copy() for c in self.chapters],
        }


def _parse_timestamp(ts: str) -> Optional[int]:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 3:
        hours, minutes, seconds = parts
        if minutes >= 60:
            return None
    else:
        hours, (minutes, seconds) = 0, parts
    if seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _close_chapters(starts: List[tuple], duration_s: Optional[float]) -> List[Chapter]:
    """Give each chapter an end: the next chapter's start, or the duration."""
    chapters = []
    for i, (start, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else duration_s
        chapters.append(Chapter(start_s=float(start), end_s=None if end is None else float(end), title=title))
    return chapters


def parse_description_chapters(description: str, duration_s: Optional[float]) -> List[Chapter]:
    """Chapters from timestamp lines in a description, or [] if implausible.

    Rejected (returns []) when fewer than 3 lines match, when starts are not
    strictly increasing, or when any start is at/after the known duration —
    those are the shapes of "timestamps mentioned in passing", not a TOC.
    """
    starts: List[tuple] = []
    for line in (description or "").splitlines():
        match = _TIMESTAMP_LINE.match(line)
        if not match:
            continue
        seconds = _parse_timestamp(match.group("ts"))
        if seconds is None:
            continue
        starts.append((seconds, match.group("title")))

    if len(starts) < MIN_DESCRIPTION_CHAPTERS:
        return []
    if any(b[0] <= a[0] for a, b in zip(starts, starts[1:])):
        return []
    if duration_s is not None and any(s >= duration_s for s, _ in starts):
        return []
    return _close_chapters(starts, duration_s)


def _platform_chapters(raw: Any, duration_s: Optional[float]) -> List[Chapter]:
    if not raw:
        return []
    starts = []
    ends = []
    for item in raw:
        if not isinstance(item, dict) or item.get("start_time") is None:
            continue
        starts.append((float(item["start_time"]), str(item.get("title") or "")))
        ends.append(item.get("end_time"))
    chapters = _close_chapters(starts, duration_s)
    # Prefer the platform's own end_time when it gave one.
    for chapter, end in zip(chapters, ends):
        if end is not None:
            chapter.end_s = float(end)
    return chapters


def _parse_yyyymmdd(value: Any) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return None


def from_ytdlp_info(info: Dict[str, Any]) -> SourceMeta:
    """Build a SourceMeta from one yt-dlp video info dict (not a playlist)."""
    duration = info.get("duration")
    duration_s = float(duration) if duration is not None else None
    description = info.get("description") or ""

    chapters = _platform_chapters(info.get("chapters"), duration_s)
    chapter_source = "platform" if chapters else "none"
    if not chapters:
        chapters = parse_description_chapters(description, duration_s)
        if chapters:
            chapter_source = "description"

    channel = info.get("channel")
    return SourceMeta(
        source_id=str(info.get("id") or info.get("display_id") or ""),
        title=info.get("title") or "",
        url=info.get("webpage_url") or info.get("original_url") or info.get("url") or "",
        published_at=_parse_yyyymmdd(info.get("upload_date")) or _parse_yyyymmdd(info.get("release_date")),
        published_by=info.get("uploader") or channel,
        channel=channel,
        duration_s=duration_s,
        chapters=chapters,
        chapter_source=chapter_source,
        description=description,
        tags=list(info.get("tags") or []),
        live_status=info.get("live_status"),
    )


DEVICE_CLOCK_NOTE = (
    "container creation_time tag: the recording device's clock, often wrong "
    "or unset; not a publish date"
)


def from_local_media(info, title: Optional[str] = None) -> SourceMeta:
    """SourceMeta for a local file, from a `core.media.MediaInfo`.

    published_at is always None: a file on disk has no publisher. The
    container's creation_time, when present, goes to `recorded_at` with a note
    saying it is the device clock.
    """
    path = Path(info.path)
    recorded_at = None
    raw = getattr(info, "creation_time", None)
    if raw:
        try:
            recorded_at = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            recorded_at = None
    return SourceMeta(
        source_id=path.stem,
        title=title or path.stem,
        url=str(path.resolve()),
        duration_s=info.duration_seconds or None,
        recorded_at=recorded_at,
        recorded_at_note=DEVICE_CLOCK_NOTE if recorded_at else None,
    )


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    return f"{total // 3600:d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def prompt_header(meta: SourceMeta, transcript_source: Optional[str] = None) -> str:
    """One line of dated provenance for a prompt. Missing values say "unknown"."""
    published = meta.published_at.isoformat() if meta.published_at else "unknown"
    by = meta.published_by or "unknown"
    parts = [
        f"Published: {published} by {by}",
        f"Captured: {meta.captured_at.date().isoformat()}",
        f"Duration: {format_duration(meta.duration_s)}",
        f"Transcript source: {transcript_source or 'unknown'}",
    ]
    if meta.recorded_at:
        parts.append(
            f"Recorded (device clock, unverified): {meta.recorded_at.date().isoformat()}"
        )
    return " · ".join(parts)
