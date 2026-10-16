"""The `outline` structure rung: one cheap model call that names the talks.

A two-hour meetup with five speakers and no chapter list falls to the `cues`
rung, which cuts at "okay, so" + a pause and calls the result "Section 2" —
even when Section 2 is the end of one talk and the start of the next. A model
reading a skim of the transcript can tell where the talks are; a regex cannot.

So, when a recording has no declared chapters and is long enough to need them:

    compressed transcript   one line per ~30 s (first ~12 words) plus every
                            talk-transition line (thanks, applause, "my name
                            is", "please welcome", long pauses) — a few
                            thousand words for two hours, not twenty thousand
    ONE model call          `complete_with_failover` (the subscription chain),
                            asked for [{start, title, speaker}]
    validation              core/structure.py `snap_outline`: deterministic;
                            any failure falls back to the `cues` rung
    cache                   <out dir>/structure_outline.json, keyed by a hash
                            of the TRANSCRIPT (times + text), so reruns never
                            re-call — not even after a tweak to the digest
                            heuristics or the snapping. The raw reply is
                            cached even when the outline is rejected (it is
                            re-validated on every run); delete the file, or
                            bump CACHE_VERSION for a prompt change that must
                            invalidate old outlines, to re-ask.

This module makes the call and owns the cache; structure.py stays pure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from video2mdnotes.config import BASE_DIR
from video2mdnotes.core.metadata import SourceMeta
from video2mdnotes.core.sources import Segment
from video2mdnotes.core.structure import (
    OutlineEntry, fmt_hms, is_closer, is_opener, pause_before,
)

OUTLINE_PROMPT = BASE_DIR / "prompts" / "longform" / "outline.md"
CACHE_FILENAME = "structure_outline.json"
CACHE_VERSION = 1

# auto mode tries the outline only for recordings at least this long.
OUTLINE_MIN_MEDIA_S = 30 * 60
SAMPLE_EVERY_S = 30.0
SAMPLE_WORDS = 12
CUE_WORDS = 20
LONG_PAUSE_S = 4.0
# Bound on the compressed transcript: past this many characters the sampling
# interval doubles (and transition lines are capped) until it fits.
MAX_DIGEST_CHARS = 24_000

CompleteFn = Callable[[str, str], Tuple[str, str]]


@dataclass
class OutlineResult:
    entries: Optional[List[OutlineEntry]]   # None: no usable outline
    model: str = ""
    cached: bool = False
    called: bool = False
    note: str = ""                          # why there is no outline


# --- compressed transcript ---------------------------------------------------------


def _clip(text: str, words: int) -> str:
    parts = text.split()
    return " ".join(parts[:words]) + (" …" if len(parts) > words else "")


def _is_transition(segments: Sequence[Segment], k: int) -> bool:
    text = segments[k].text
    return is_opener(text) or is_closer(text) or pause_before(segments, k) >= LONG_PAUSE_S


def compress_transcript(segments: Sequence[Segment], *,
                        every_s: float = SAMPLE_EVERY_S,
                        max_chars: int = MAX_DIGEST_CHARS) -> str:
    """One line per ~`every_s` seconds plus all talk-transition lines.

    Lines are `[H:MM:SS] text`, clipped to SAMPLE_WORDS (transition lines to
    CUE_WORDS, so "my name is X and I'm from Y" survives). The result is kept
    under `max_chars` by widening the interval and then capping transitions.
    """
    segments = list(segments)
    transitions = [k for k in range(len(segments)) if _is_transition(segments, k)]
    every = every_s
    cue_cap: Optional[int] = None
    while True:
        picked = set()
        mark = None
        for k, seg in enumerate(segments):
            if mark is None or seg.start >= mark:
                picked.add(k)
                mark = seg.start + every
        cues = transitions
        if cue_cap is not None:
            # Openers first: they name speakers; then the rest, evenly.
            openers = [k for k in transitions if is_opener(segments[k].text)]
            rest = [k for k in transitions if k not in set(openers)]
            step = max(1, len(rest) // max(1, cue_cap - len(openers)))
            cues = sorted(set(openers[:cue_cap]) | set(rest[::step]))
        lines = []
        for k in sorted(picked | set(cues)):
            words = CUE_WORDS if k in set(cues) else SAMPLE_WORDS
            lines.append(f"[{fmt_hms(segments[k].start)}] {_clip(segments[k].text, words)}")
        text = "\n".join(lines)
        if len(text) <= max_chars or (every > 600 and cue_cap == 0):
            return text
        if every <= 120:
            every *= 2
        else:
            cue_cap = 50 if cue_cap is None else cue_cap // 2
            every *= 1.5


def render_prompt(segments: Sequence[Segment], meta: Optional[SourceMeta]) -> Tuple[str, str]:
    """(system, user) for the outline call. The user message is small by design."""
    system = OUTLINE_PROMPT.read_text(encoding="utf-8")
    duration = max((s.end for s in segments), default=0.0)
    if meta is not None and meta.duration_s:
        duration = max(duration, float(meta.duration_s))
    head = [f"Recording: {(meta.title if meta else None) or 'untitled'}",
            f"Duration: {fmt_hms(duration)}"]
    if meta is not None and meta.description:
        head.append(f"Description: {_clip(meta.description, 60)}")
    if meta is not None and len(meta.parts or []) > 1:
        head.append("Recorded as separate files starting at: "
                    + ", ".join(fmt_hms(float(p.get("offset_s") or 0.0)) for p in meta.parts))
    head += ["", "Compressed transcript:", compress_transcript(segments)]
    return system, "\n".join(head)


# --- parsing --------------------------------------------------------------------------

_TS_RE = re.compile(r"^\s*\[?\s*(\d{1,2}(?::\d{1,2}){1,2}(?:\.\d+)?)\s*\]?\s*$")


def parse_ts(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = _TS_RE.match(str(value or ""))
    if not m:
        return None
    parts = [float(p) for p in m.group(1).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, mnt, s = parts
    if mnt >= 60 or s >= 60:
        return None
    return h * 3600 + mnt * 60 + s


def parse_outline(text: str) -> Tuple[Optional[List[OutlineEntry]], str]:
    """Entries from the model's reply, or (None, reason). Order is NOT fixed
    here: a non-monotonic outline must reach validation and be rejected."""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return None, "reply has no JSON array"
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        return None, f"reply is not valid JSON ({exc.msg})"
    if not isinstance(data, list):
        return None, "reply is not a JSON array"
    out: List[OutlineEntry] = []
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict):
            return None, f"item {n} is not an object"
        t = parse_ts(item.get("start"))
        if t is None:
            return None, f"item {n} has no valid start time ({item.get('start')!r})"
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, f"item {n} has no title"
        speaker = item.get("speaker", item.get("speaker_or_null"))
        speaker = speaker.strip() if isinstance(speaker, str) and speaker.strip() else None
        if speaker and speaker.lower() in ("null", "none", "unknown", "n/a"):
            speaker = None
        role = item.get("role")
        role = role.strip().lower() if isinstance(role, str) and role.strip() else None
        if role in ("null", "none", "unknown", "n/a"):
            role = None
        raw_var = item.get("speaker_variants", item.get("variants")) or []
        if isinstance(raw_var, str):
            raw_var = [raw_var]
        variants = [" ".join(str(v).split()) for v in raw_var
                    if isinstance(v, str) and v.strip() and v.strip() != speaker]
        out.append(OutlineEntry(start_s=t, title=title.strip(), speaker=speaker,
                                role=role, speaker_variants=variants))
    return out, ""


# --- cache + call -----------------------------------------------------------------------


def transcript_hash(segments: Sequence[Segment]) -> str:
    """Cache key: what the outline is a function of — the transcript."""
    h = hashlib.sha256(f"v{CACHE_VERSION}\n".encode("utf-8"))
    for s in segments:
        h.update(f"{s.start:.3f}\t{s.text.strip()}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def _read_cache(paths: Sequence[Path], key: str) -> Optional[dict]:
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("transcript_sha256") == key and "reply" in data:
            return data
    return None


def needs_outline(segments: Sequence[Segment], meta: Optional[SourceMeta], mode: str) -> bool:
    """Whether the outline rung applies: explicitly asked, or auto with no
    declared chapters and a recording over OUTLINE_MIN_MEDIA_S."""
    if mode == "outline":
        return True
    if mode != "auto" or not segments:
        return False
    if meta is not None and meta.chapters and meta.chapter_source in ("platform", "description"):
        return False
    duration = max(max(s.end for s in segments),
                   float(meta.duration_s) if meta is not None and meta.duration_s else 0.0)
    return duration > OUTLINE_MIN_MEDIA_S


def obtain_outline(
    segments: Sequence[Segment],
    meta: Optional[SourceMeta],
    cache_dir: Path,
    *,
    complete: Optional[CompleteFn],
    extra_cache_dirs: Sequence[Path] = (),
) -> OutlineResult:
    """The outline from cache, or from ONE call to `complete` (cached).

    `complete=None` means "cache only": no call is made, and the result says
    an outline call is pending. SubscriptionExhausted and BudgetExhausted
    propagate (the caller's wait/stop policy applies); any other failure of
    the call is a note, and the ladder falls back to cues.
    """
    from video2mdnotes.core.costs import BudgetExhausted
    from video2mdnotes.core.summarizer import SubscriptionExhausted

    system, user = render_prompt(segments, meta)
    key = transcript_hash(segments)
    cache_path = Path(cache_dir) / CACHE_FILENAME
    hit = _read_cache([cache_path, *(Path(d) / CACHE_FILENAME for d in extra_cache_dirs)], key)
    if hit is not None:
        entries, reason = parse_outline(hit["reply"])
        return OutlineResult(entries=entries, model=hit.get("model", ""), cached=True,
                             note=f"cached reply unusable: {reason}" if entries is None else "")
    if complete is None:
        return OutlineResult(entries=None, note="not cached; a run makes 1 outline call")
    try:
        reply, model = complete(system, user)
    except (SubscriptionExhausted, BudgetExhausted):
        raise
    except Exception as exc:  # noqa: BLE001 — a failed outline is a fallback, not a crash
        return OutlineResult(entries=None, called=True,
                             note=f"outline call failed: {type(exc).__name__}: {exc}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "version": CACHE_VERSION,
        "transcript_sha256": key,
        "prompt_sha256": hashlib.sha256(f"{system}\n{user}".encode("utf-8")).hexdigest()[:16],
        "model": model,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "prompt_chars": len(system) + len(user),
        "reply": reply,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(cache_path)
    entries, reason = parse_outline(reply)
    return OutlineResult(entries=entries, model=model, called=True,
                         note=f"reply unusable: {reason}" if entries is None else "")
