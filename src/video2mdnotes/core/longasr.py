"""Long-recording ASR: bounded windows, per-window checkpoints, stitching, and a
completeness gate.

Why this exists. `media.to_wav` converts a WHOLE file to one wav. For a two-hour
camera recording that is a multi-gigabyte intermediate, one ASR call that either
finishes or loses everything, and no way to tell a transcript that stopped at
minute 70 from one that covered the recording. This module instead:

1. probes each source file (camera-split parts of one recording, in filename
   order) and lays them on one timeline with cumulative offsets;
2. cuts each file into windows of `window_s` seconds plus `overlap_s` of
   look-ahead, extracts ONE window at a time to a temp 16 kHz mono wav with
   ffmpeg (`-ss start -t dur -i src -vn -ac 1 -ar 16000`), transcribes it,
   writes `asr/<file>_<window>.json` atomically, and deletes the wav — so no
   temp audio file is ever longer than `window_s + overlap_s`, and a rerun
   skips every window already checkpointed;
3. shifts segments onto the timeline, de-duplicates the overlap between
   neighbouring windows (keeping the copy further from its window's edge,
   where the engine had context on both sides), and
4. runs a completeness gate — audio coverage, transcribed span, longest
   unexplained gap, every window checkpointed — that fails loudly. A window
   with no speech is recorded as SILENT (ffmpeg silencedetect), never as
   missing; a gap is only excused by measured silence.

The output is never a silently partial transcript: if the gate fails,
`asr_manifest.json` says `complete: false` with the reasons and
`AsrIncomplete` is raised.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from video2mdnotes.core.asr import get_backend
from video2mdnotes.core.asr.base import (
    AsrBackend, BackendUnavailable, clip_to_duration, wav_duration,
)
from video2mdnotes.core.transcriber import Segment, TranscriptResult, generate_markdown

MANIFEST_FILENAME = "asr_manifest.json"
CHECKPOINT_VERSION = 1
# A window whose decoded audio is shorter than expected by more than this is
# recorded as truncated (and loses coverage).
TRUNCATION_TOLERANCE_S = 1.0
# Fraction of a window that must be detected silence for it to count as silent.
SILENT_WINDOW_FRACTION = 0.95
# Minimum silence run ffmpeg silencedetect reports.
SILENCE_MIN_S = 2.0
# Two segments from neighbouring windows are the same speech when they overlap
# in time by at least this fraction of the shorter one.
DUPLICATE_OVERLAP_FRACTION = 0.5
# Parts of one camera recording: next part's creation_time within this many
# seconds of previous creation_time + duration.
GROUP_TOLERANCE_S = 10.0
# Un-transcribed, non-silent tail always tolerated by the span check.
TAIL_ALLOWANCE_S = 10.0

Progress = Callable[[str], None]


class AsrIncomplete(RuntimeError):
    """The completeness gate failed. `gate` carries the measured reasons."""

    def __init__(self, message: str, gate: dict):
        super().__init__(message)
        self.gate = gate


# --- plan --------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One extraction window. Times are seconds from the start of its FILE.

    [ext_start, ext_end) is what gets extracted; [core_start, core_end) is the
    window's own share of the file. ext_end - core_end is the look-ahead
    overlap into the next window (zero for a file's last window).
    """

    file_idx: int
    window_idx: int
    ext_start: float
    ext_end: float
    core_start: float
    core_end: float

    @property
    def key(self) -> str:
        return f"{self.file_idx:02d}_{self.window_idx:03d}"

    @property
    def expected_s(self) -> float:
        return self.ext_end - self.ext_start


def plan_windows(duration_s: float, window_s: float, overlap_s: float,
                 file_idx: int = 0) -> List[Window]:
    """Windows covering [0, duration_s). Each is at most window_s + overlap_s.

    A tail shorter than the overlap is absorbed by the previous window (its
    look-ahead already reaches the end), so no redundant sliver window exists.
    """
    if window_s <= 0:
        raise ValueError("window_s must be > 0")
    if overlap_s < 0 or overlap_s >= window_s:
        raise ValueError("overlap_s must be >= 0 and < window_s")
    out: List[Window] = []
    start, k = 0.0, 0
    duration_s = max(0.0, float(duration_s))
    while start < duration_s:
        core_end = min(start + window_s, duration_s)
        ext_end = min(core_end + overlap_s, duration_s)
        if ext_end >= duration_s:
            out.append(Window(file_idx, k, start, duration_s, start, duration_s))
            break
        out.append(Window(file_idx, k, start, ext_end, start, core_end))
        start, k = core_end, k + 1
    return out


@dataclass
class Part:
    index: int
    path: Path
    duration_s: float
    offset_s: float
    creation_time: Optional[str] = None
    codec: Optional[str] = None
    video_codec: Optional[str] = None
    resolution: Optional[str] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        d["name"] = self.path.name
        return d


def parts_from_infos(infos: Sequence) -> List[Part]:
    """Timeline parts from MediaInfo objects, in the order given."""
    parts, offset = [], 0.0
    for i, info in enumerate(infos):
        dur = float(info.duration_seconds or 0.0)
        res = (f"{info.width}x{info.height}"
               if getattr(info, "width", None) and getattr(info, "height", None) else None)
        parts.append(Part(i, Path(info.path), dur, offset,
                          getattr(info, "creation_time", None), getattr(info, "codec", None),
                          getattr(info, "video_codec", None), res))
        offset += dur
    return parts


# --- multi-part grouping -----------------------------------------------------


def _parse_ct(raw: Optional[str]) -> Optional[dt.datetime]:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def continues(prev, nxt, tolerance_s: float = GROUP_TOLERANCE_S) -> bool:
    """Is `nxt` the next camera-split part of the recording `prev` belongs to?

    Same audio codec/rate, same video codec and frame size, and nxt's
    creation_time within tolerance of prev's creation_time + duration.
    """
    a, b = _parse_ct(prev.creation_time), _parse_ct(nxt.creation_time)
    if a is None or b is None:
        return False
    same_shape = (
        prev.codec == nxt.codec
        and prev.sample_rate == nxt.sample_rate
        and getattr(prev, "video_codec", None) == getattr(nxt, "video_codec", None)
        and getattr(prev, "width", None) == getattr(nxt, "width", None)
        and getattr(prev, "height", None) == getattr(nxt, "height", None)
    )
    if not same_shape:
        return False
    expected = a + dt.timedelta(seconds=float(prev.duration_seconds or 0.0))
    return abs((b - expected).total_seconds()) <= tolerance_s


def group_parts(infos: Sequence, tolerance_s: float = GROUP_TOLERANCE_S) -> List[List]:
    """Split MediaInfos (sorted by file name) into recordings.

    Consecutive files that `continues()` join one group; everything else is a
    group of one. Order inside a group is filename order (camera numbering).
    """
    ordered = sorted(infos, key=lambda i: Path(i.path).name)
    groups: List[List] = []
    for info in ordered:
        if groups and continues(groups[-1][-1], info, tolerance_s):
            groups[-1].append(info)
        else:
            groups.append([info])
    return groups


# --- backend selection -------------------------------------------------------


ENGLISH_CODES = {"", "en", "english", "auto", "und", "unknown"}


def select_backend(metadata_text: str = "", language: Optional[str] = None,
                   spec: Optional[str] = None) -> Tuple[AsrBackend, str]:
    """Which engine transcribes a long recording, and why.

    1. Non-English context (asr_select vocabulary) or a non-English language
       -> faster-whisper:large-v3. Parakeet v3 has no Japanese and distil is
       English-only; an explicit ASR_BACKEND pin of either is overridden.
    2. ASR_BACKEND pinned ("name[:model]") -> that.
    3. auto: parakeet-mlx when available (measured 40x real time, WER ~ large-v3
       on content words; glossary pass fixes its domain mishearings), else
       faster-whisper:distil-large-v3 (CPU fallback).
    """
    from video2mdnotes.config import settings
    from video2mdnotes.core.asr_select import MULTILINGUAL, detect_non_english_context

    spec = (spec if spec is not None else getattr(settings, "asr_backend", "auto")) or "auto"
    spec = spec.strip()
    hit = detect_non_english_context(metadata_text)
    lang = (language or "").strip().lower()
    non_english = hit or (lang not in ENGLISH_CODES)
    if non_english:
        why = f"non-English context ({hit!r})" if hit else f"language={language!r}"
        pinned = get_backend(spec) if spec != "auto" else None
        english_only = (pinned is None or pinned.name == "parakeet-mlx"
                        or "distil" in (pinned.model or ""))
        if english_only:
            return (get_backend(f"faster-whisper:{MULTILINGUAL}"),
                    f"{why}: multilingual large-v3 required (asr_select policy)")
        return pinned, f"pinned by ASR_BACKEND={spec} ({why})"
    if spec != "auto":
        return get_backend(spec), f"pinned by ASR_BACKEND={spec}"
    parakeet = get_backend("parakeet-mlx")
    ok, why = parakeet.available()
    if ok:
        return parakeet, "auto: parakeet-mlx (English long-form primary)"
    fallback = get_backend("faster-whisper:distil-large-v3")
    ok2, why2 = fallback.available()
    if ok2:
        return fallback, f"auto: parakeet-mlx unavailable ({why}); CPU fallback distil-large-v3"
    raise BackendUnavailable(f"no ASR backend available: parakeet ({why}); faster-whisper ({why2})")


# --- silence -----------------------------------------------------------------


_SIL_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def detect_silences(wav: Path, noise_db: float = -40.0,
                    min_s: float = SILENCE_MIN_S) -> List[Tuple[float, float]]:
    """Silent stretches of a wav, [(start, end)], via ffmpeg silencedetect.

    Returns [] if ffmpeg is missing (silence is then never assumed — a gap
    stays unexplained, which is the safe direction).
    """
    binary = shutil.which("ffmpeg")
    if not binary:
        return []
    proc = subprocess.run(
        [binary, "-hide_banner", "-nostats", "-i", str(wav), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    out, start = [], None
    total = wav_duration(wav)
    for line in (proc.stderr or "").splitlines():
        m = _SIL_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SIL_END.search(line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    if start is not None and total is not None:
        out.append((start, total))
    return out


# --- overlap dedupe ----------------------------------------------------------


def _dur(s: Segment) -> float:
    return max(0.0, s.end - s.start)


def dedupe_overlap(prev: List[Segment], prev_ext_end: float,
                   nxt: List[Segment], nxt_ext_start: float) -> Tuple[List[Segment], List[Segment]]:
    """Remove duplicate speech between two neighbouring windows.

    All times absolute. Only segments inside the shared region
    [nxt_ext_start, prev_ext_end) can conflict. Two segments are the same
    speech when they overlap in time by >= half of the shorter one; the copy
    kept is the one further from its own window's edge (prev's end edge /
    nxt's start edge), because a segment cut by an edge is the truncated one.
    Non-conflicting segments are always kept — nothing is dropped unless a
    better copy of it exists.
    """
    if not prev or not nxt or prev_ext_end <= nxt_ext_start:
        return prev, nxt
    drop_p, drop_n = set(), set()
    for i, a in enumerate(prev):
        if a.end <= nxt_ext_start:
            continue
        for j, b in enumerate(nxt):
            if i in drop_p:
                break
            if j in drop_n or b.start >= prev_ext_end:
                continue
            inter = min(a.end, b.end) - max(a.start, b.start)
            shorter = min(_dur(a), _dur(b))
            same = (inter > 0 and inter >= DUPLICATE_OVERLAP_FRACTION * shorter) or (
                shorter == 0 and abs(a.start - b.start) < 0.5)
            if not same:
                continue
            dist_a = prev_ext_end - a.end
            dist_b = b.start - nxt_ext_start
            if dist_a >= dist_b:
                drop_n.add(j)
            else:
                drop_p.add(i)
    return ([s for i, s in enumerate(prev) if i not in drop_p],
            [s for j, s in enumerate(nxt) if j not in drop_n])


# --- checkpoints -------------------------------------------------------------


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def checkpoint_path(run_dir: Path, window: Window) -> Path:
    return Path(run_dir) / "asr" / f"{window.key}.json"


def _load_checkpoint(path: Path, part: Part, window: Window, backend: AsrBackend) -> Optional[dict]:
    """A checkpoint that matches this window and engine, else None (redo)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (data.get("version") == CHECKPOINT_VERSION
            and data.get("source") == part.path.name
            and abs(data.get("ext_start_s", -1) - window.ext_start) < 1e-3
            and abs(data.get("ext_end_s", -1) - window.ext_end) < 1e-3
            and data.get("backend") == backend.name
            and data.get("model") == backend.model):
        return data
    return None


# --- the run -----------------------------------------------------------------


@dataclass
class LongTranscript:
    segments: List[Segment]
    parts: List[Part]
    gate: dict
    manifest: dict
    label: str  # "asr:<backend>/<model>"
    language: str
    result: Optional[TranscriptResult] = None
    windows_run: int = 0
    windows_skipped: int = 0


def provenance_label(backend: AsrBackend) -> str:
    return f"asr:{backend.name}/{backend.model}"


def _run_window(part: Part, w: Window, backend: AsrBackend, tmp_dir: Path,
                initial_prompt: Optional[str], language: Optional[str],
                silence_db: float):
    """Extract one window to a temp wav, transcribe, measure silence, delete.

    The wav exists only inside this function; it is removed even on error.
    """
    from video2mdnotes.core.media import extract_window

    wav = tmp_dir / f"{w.key}.wav"
    try:
        te = time.monotonic()
        extract_window(part.path, wav, w.ext_start, w.expected_s)
        extract_s = time.monotonic() - te
        actual = wav_duration(wav) or 0.0
        tt = time.monotonic()
        segs = backend.transcribe(
            wav,
            initial_prompt=initial_prompt if backend.supports_prompt else None,
            language=language,
        )
        transcribe_s = time.monotonic() - tt
        segs = clip_to_duration(list(segs), actual)
        silences = detect_silences(wav, silence_db) if actual > 0 else []
    finally:
        wav.unlink(missing_ok=True)
    timings = {"extract_s": round(extract_s, 2), "transcribe_s": round(transcribe_s, 2),
               "rtf": round(transcribe_s / actual, 4) if actual else None}
    return actual, segs, silences, timings


def transcribe_long(
    sources: Sequence[Path],
    backend: AsrBackend,
    *,
    run_dir: Path,
    window_s: float = 1500.0,
    overlap_s: float = 8.0,
    initial_prompt: Optional[str] = None,
    language: Optional[str] = None,
    title: Optional[str] = None,
    max_gap_s: float = 120.0,
    min_coverage: float = 0.98,
    silence_db: float = -40.0,
    tmp_dir: Optional[Path] = None,
    infos: Optional[Sequence] = None,
    backend_reason: str = "",
    progress: Progress = print,
) -> LongTranscript:
    """Transcribe one recording (one file or ordered camera-split parts).

    Raises AsrIncomplete when the completeness gate fails (manifest written
    with complete=false first). Backend errors propagate; checkpoints of the
    windows already done survive, so a rerun resumes.
    """
    from video2mdnotes.core.media import probe

    run_dir = Path(run_dir)
    asr_dir = run_dir / "asr"
    asr_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tmp_dir) if tmp_dir else asr_dir / "tmp"
    if infos is None:
        infos = [probe(Path(s)) for s in sources]
    parts = parts_from_infos(infos)
    total = sum(p.duration_s for p in parts)
    label = provenance_label(backend)

    t0 = time.monotonic()
    load_s = None
    records: List[Tuple[Part, Window, dict]] = []
    errors: List[str] = []
    ran = skipped = 0
    plan = [(part, w) for part in parts
            for w in plan_windows(part.duration_s, window_s, overlap_s, part.index)]
    for n, (part, w) in enumerate(plan, 1):
        ck_path = checkpoint_path(run_dir, w)
        data = _load_checkpoint(ck_path, part, w, backend)
        if data is not None:
            skipped += 1
            records.append((part, w, data))
            progress(f"ASR window {n}/{len(plan)} {w.key}: checkpointed, skipped")
            continue
        if load_s is None:
            lt = time.monotonic()
            backend.load()
            load_s = round(time.monotonic() - lt, 2)
        try:
            actual, segs, silences, timings = _run_window(
                part, w, backend, tmp_dir, initial_prompt, language, silence_db)
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded; the gate fails on it
            errors.append(f"{w.key}: {type(exc).__name__}: {exc}")
            progress(f"ASR window {n}/{len(plan)} {w.key}: FAILED ({exc}); continuing")
            continue
        transcribe_s = timings["transcribe_s"]
        silent_total = sum(e - s for s, e in silences)
        silent = actual > 0 and silent_total >= SILENT_WINDOW_FRACTION * actual
        data = {
            "version": CHECKPOINT_VERSION,
            "key": w.key,
            "file_idx": w.file_idx,
            "window_idx": w.window_idx,
            "source": part.path.name,
            "file_offset_s": part.offset_s,
            "ext_start_s": w.ext_start,
            "ext_end_s": w.ext_end,
            "core_start_s": w.core_start,
            "core_end_s": w.core_end,
            "expected_s": round(w.expected_s, 3),
            "actual_s": round(actual, 3),
            "truncated": actual < w.expected_s - TRUNCATION_TOLERANCE_S,
            "silent": bool(silent),
            "silences": [[round(s, 3), round(e, 3)] for s, e in silences],
            "backend": backend.name,
            "model": backend.model,
            "language": backend.last_language,
            "prompt_used": bool(initial_prompt and backend.supports_prompt),
            "timings": timings,
            "completed_at": dt.datetime.now().isoformat(timespec="seconds"),
            "segments": [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text}
                         for s in segs],
        }
        _atomic_write_json(ck_path, data)
        records.append((part, w, data))
        ran += 1
        status = "SILENT" if silent else f"{len(segs)} segments"
        progress(f"ASR window {n}/{len(plan)} {w.key} [{part.path.name} "
                 f"{w.ext_start:.0f}-{w.ext_end:.0f}s]: {status}, "
                 f"{transcribe_s:.1f}s ({actual / max(transcribe_s, 1e-6):.0f}x)")
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    segments = stitch(records)
    gate = completeness_gate(parts, plan, records, segments,
                             max_gap_s=max_gap_s, min_coverage=min_coverage)
    if errors:
        gate["window_errors"] = errors
        gate["failures"].append(f"{len(errors)} window error(s): {errors[0]}")
    langs = [r[2].get("language") for r in records if r[2].get("language")]
    lang = (langs[0] if langs else None) or language or "und"
    wall = round(time.monotonic() - t0, 1)
    manifest = {
        "complete": gate["passed"],
        "title": title,
        "label": label,
        "backend": backend.name,
        "model": backend.model,
        "backend_reason": backend_reason,
        "language": lang,
        "prompt_supported": backend.supports_prompt,
        "window_s": window_s,
        "overlap_s": overlap_s,
        "total_duration_s": round(total, 3),
        "parts": [p.as_dict() for p in parts],
        "windows": [{k: r[2][k] for k in ("key", "source", "ext_start_s", "ext_end_s",
                                           "actual_s", "truncated", "silent", "timings")}
                    | {"segments": len(r[2]["segments"])} for r in records],
        "windows_run": ran,
        "windows_skipped": skipped,
        "model_load_s": load_s,
        "wall_s": wall,
        "segments": len(segments),
        "words": sum(len(s.text.split()) for s in segments),
        "gate": gate,
        "written_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(run_dir / MANIFEST_FILENAME, manifest)
    out = LongTranscript(segments, parts, gate, manifest, label, lang,
                         windows_run=ran, windows_skipped=skipped)
    if not gate["passed"]:
        raise AsrIncomplete("ASR completeness gate failed: " + "; ".join(gate["failures"]), gate)

    title = title or parts[0].path.stem
    out.result = TranscriptResult(
        source_file=parts[0].path,
        language=lang,
        segments=segments,
        full_text="".join(s.text for s in segments),
        markdown_content=generate_markdown(
            title=title,
            source_file=", ".join(p.path.name for p in parts),
            segments=segments,
            language=lang,
            model=label,
            transcript_source=label,
        ),
        model_name=label,
        generated_at=dt.datetime.now(),
        transcript_source=label,
    )
    return out


def stitch(records: Sequence[Tuple[Part, Window, dict]]) -> List[Segment]:
    """Checkpointed window segments -> one absolute timeline, overlaps deduped."""
    out: List[Segment] = []
    prev: Optional[Tuple[Window, Part, List[Segment]]] = None
    for part, w, data in records:
        base = part.offset_s + w.ext_start
        segs = [Segment(start=base + s["start"], end=base + s["end"], text=s["text"])
                for s in data.get("segments", [])]
        if prev is not None and prev[1].index == part.index:
            pw, pp, psegs = prev
            kept_prev, segs = dedupe_overlap(psegs, pp.offset_s + pw.ext_end,
                                             segs, base)
            out.extend(kept_prev)
        elif prev is not None:
            out.extend(prev[2])
        prev = (w, part, segs)
    if prev is not None:
        out.extend(prev[2])
    out.sort(key=lambda s: (s.start, s.end))
    return out


# --- completeness gate -------------------------------------------------------


def _overlap_len(a: float, b: float, intervals: Sequence[Tuple[float, float]]) -> float:
    return sum(max(0.0, min(b, e) - max(a, s)) for s, e in intervals)


def completeness_gate(parts: Sequence[Part], plan: Sequence[Tuple[Part, Window]],
                      records: Sequence[Tuple[Part, Window, dict]],
                      segments: Sequence[Segment], *, max_gap_s: float = 120.0,
                      min_coverage: float = 0.98) -> dict:
    """Is this transcript the whole recording? Deterministic; never raises.

    Checks (all must hold):
      * every planned window has a checkpoint;
      * audio coverage: decoded audio over each window's own share, summed,
        >= min_coverage of the summed media duration (catches truncation);
      * transcribed span: last segment end >= min_coverage of the duration,
        unless the tail after it is measured silence;
      * no gap between segments (incl. start and end) whose NON-silent part
        exceeds max_gap_s. Silence comes from ffmpeg silencedetect per window,
        so a quiet break is excused and a missed stretch of speech is not.
    """
    total = sum(p.duration_s for p in parts)
    failures: List[str] = []
    have = {(r[1].file_idx, r[1].window_idx) for r in records}
    missing = [w.key for _, w in plan if (w.file_idx, w.window_idx) not in have]
    if missing:
        failures.append(f"{len(missing)} window(s) without checkpoint: {', '.join(missing)}")

    covered = 0.0
    silences: List[Tuple[float, float]] = []
    truncated, silent = [], []
    for part, w, data in records:
        actual_end = w.ext_start + float(data.get("actual_s", 0.0))
        covered += max(0.0, min(w.core_end, actual_end) - w.core_start)
        base = part.offset_s + w.ext_start
        silences += [(base + s, base + e) for s, e in data.get("silences", [])]
        if data.get("truncated"):
            truncated.append(f"{w.key} ({data.get('actual_s')}s of {data.get('expected_s')}s)")
        if data.get("silent"):
            silent.append(w.key)
    coverage = covered / total if total else 0.0
    if coverage < min_coverage:
        failures.append(f"audio coverage {coverage:.2%} < {min_coverage:.0%}"
                        + (f" (truncated: {', '.join(truncated)})" if truncated else ""))

    last_end = max((s.end for s in segments), default=0.0)
    span_ratio = last_end / total if total else 0.0
    tail_unexplained = (total - last_end) - _overlap_len(last_end, total, silences)
    # A speaker stops before the camera does; on a 12 s clip 2 % is 0.24 s,
    # so the tail allowance never drops below TAIL_ALLOWANCE_S.
    if span_ratio < min_coverage and tail_unexplained > max(
            (1 - min_coverage) * total, TAIL_ALLOWANCE_S):
        failures.append(f"transcribed span {span_ratio:.2%} < {min_coverage:.0%} "
                        f"(last segment ends {last_end:.1f}s of {total:.1f}s)")

    gaps = []
    cursor = 0.0
    for s in list(segments) + [Segment(start=total, end=total, text="")]:
        if s.start - cursor > 0:
            gap = s.start - cursor
            unexplained = gap - _overlap_len(cursor, s.start, silences)
            gaps.append((cursor, s.start, gap, unexplained))
        cursor = max(cursor, s.end)
    max_gap = max((g[2] for g in gaps), default=0.0)
    max_unexplained = max((g[3] for g in gaps), default=0.0)
    bad = [g for g in gaps if g[3] > max_gap_s]
    if bad:
        shown = ", ".join(f"{g[0]:.0f}-{g[1]:.0f}s ({g[3]:.0f}s non-silent)" for g in bad[:5])
        failures.append(f"{len(bad)} gap(s) with > {max_gap_s:.0f}s of non-silent audio "
                        f"untranscribed: {shown}")
    excused = [g for g in gaps if g[2] > max_gap_s and g[3] <= max_gap_s]
    return {
        "passed": not failures,
        "failures": failures,
        "total_duration_s": round(total, 3),
        "audio_coverage": round(coverage, 5),
        "span_ratio": round(span_ratio, 5),
        "last_segment_end_s": round(last_end, 3),
        "max_gap_s": round(max_gap, 3),
        "max_unexplained_gap_s": round(max_unexplained, 3),
        "max_gap_limit_s": max_gap_s,
        "min_coverage": min_coverage,
        "windows_planned": len(plan),
        "windows_checkpointed": len(records),
        "missing_windows": missing,
        "truncated_windows": truncated,
        "silent_windows": silent,
        "silence_excused_gaps": [[round(g[0], 1), round(g[1], 1)] for g in excused],
    }
