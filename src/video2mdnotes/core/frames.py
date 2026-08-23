"""Keyframe extraction for videos whose information is on screen, not in audio.

Some instructional content carries its substance visually — slides, code
screencasts, tables, UI walkthroughs. The audio pipeline cannot see any of it,
and on a narration-free product video it correctly produces "No speech
detected" while the actual content sits in the pixels.

The enabling trick here is **scene-change detection rather than interval
sampling**. ffmpeg's `select='gt(scene,N)'` emits a frame only when the picture
materially changes, which on slide decks and screencasts maps almost one-to-one
onto "a new state appeared". That turns "look at 40 minutes of video" into
"look at ~30 distinct images", which is what makes the downstream OCR/VLM step
affordable at all.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

from video2mdnotes.config import settings
from video2mdnotes.logger import logger


class Keyframe(BaseModel):
    """One extracted frame, with its position in the video."""
    path: Path
    timestamp: float  # seconds into the video

    @property
    def label(self) -> str:
        """Human-friendly position, e.g. '3m07s' or '0m04.5s'.

        Sub-second precision appears only when the fractional part is
        significant, because scene detection can emit several genuinely
        different frames inside one second during a cut or flash — labelling
        them all '0m04s' makes distinct entries look like duplicates.
        """
        minutes, seconds = int(self.timestamp) // 60, self.timestamp % 60
        if abs(seconds - round(seconds)) < 0.05:
            return f"{minutes}m{int(round(seconds)):02d}s"
        return f"{minutes}m{seconds:04.1f}s"


# ffmpeg writes `pts_time:12.345` lines to stderr for each selected frame when
# the showinfo filter is attached. That is how we recover timestamps — the
# output filenames are just a sequence.
_PTS = re.compile(r"pts_time:([0-9.]+)")

# yt-dlp per-stream fragment naming, e.g. source_video.f399.mp4
FRAGMENT = re.compile(r"\.f\d+\.")


def _ffmpeg() -> str:
    return shutil.which(settings.ffmpeg_path) or settings.ffmpeg_path


def extract_keyframes(
    video_path: Path,
    output_dir: Path,
    scene_threshold: Optional[float] = None,
    max_frames: Optional[int] = None,
) -> List[Keyframe]:
    """Extract frames where the picture materially changes.

    Returns frames in chronological order, capped at `max_frames`. An empty
    list is a normal outcome (a talking-head video has no slide changes) and
    should be treated as "nothing visual to extract", not an error.
    """
    threshold = scene_threshold if scene_threshold is not None else settings.frame_scene_threshold
    cap = max_frames if max_frames is not None else settings.frame_max

    output_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            _ffmpeg(), "-i", str(video_path),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-vsync", "vfr",
            "-frames:v", str(cap),
            str(output_dir / "frame_%04d.png"),
        ],
        capture_output=True, text=True, timeout=settings.frame_extract_timeout,
    )

    # ffmpeg exits non-zero on some inputs even after writing frames, so trust
    # the files on disk over the return code.
    files = sorted(output_dir.glob("frame_*.png"))
    timestamps = [float(m) for m in _PTS.findall(proc.stderr or "")]

    if not files:
        if proc.returncode != 0:
            logger.warning(
                f"Frame extraction failed for {video_path.name}: "
                f"{(proc.stderr or '').strip()[-200:]}"
            )
        return []

    frames = [
        Keyframe(path=path, timestamp=timestamps[i] if i < len(timestamps) else 0.0)
        for i, path in enumerate(files)
    ]
    logger.success(f"Extracted {len(frames)} keyframe(s) from {video_path.name}")
    return frames


def deduplicate(frames: List[Keyframe], hamming_threshold: Optional[int] = None) -> List[Keyframe]:
    """Drop frames that are perceptually near-identical to one already kept.

    Scene detection over-fires on screencasts: a moving cursor, a blinking
    caret, or a line of typing all register as scene changes while the slide is
    unchanged. Comparing against every kept frame (not just the previous one)
    also collapses the common "flip back to the earlier diagram" pattern.
    """
    threshold = hamming_threshold if hamming_threshold is not None else settings.frame_dedupe_threshold
    if len(frames) < 2:
        return frames

    # Burst suppression and perceptual de-duplication are independent: turning
    # off hash matching (threshold 0) must not also disable the time gap, and a
    # missing imagehash must not disable it either.
    try:
        import imagehash
        from PIL import Image
        hashing = threshold > 0
    except ImportError:
        logger.warning("imagehash/Pillow not installed — perceptual de-duplication skipped.")
        hashing = False

    kept: List[Keyframe] = []
    hashes: List["imagehash.ImageHash"] = []
    min_gap = settings.frame_min_gap_seconds
    for frame in frames:
        # Burst suppression. A cut or flash can emit several frames inside a
        # fraction of a second; measured on an 81s video, six frames landed
        # between 4.500s and 4.700s and every one was escalated to the paid
        # vision model. Perceptual hashing does not collapse them because a
        # white flash genuinely differs from the content on either side, so the
        # gap has to be enforced in time rather than in image space.
        if min_gap > 0 and kept and (frame.timestamp - kept[-1].timestamp) < min_gap:
            frame.path.unlink(missing_ok=True)
            continue
        if not hashing:
            kept.append(frame)
            continue

        try:
            with Image.open(frame.path) as image:
                digest = imagehash.phash(image)
        except Exception as e:  # noqa: BLE001 - an unreadable frame is not fatal
            logger.warning(f"Could not hash {frame.path.name}: {e}")
            kept.append(frame)
            continue

        if any(digest - seen <= threshold for seen in hashes):
            frame.path.unlink(missing_ok=True)
            continue
        hashes.append(digest)
        kept.append(frame)

    if len(kept) < len(frames):
        logger.info(f"De-duplicated {len(frames)} keyframe(s) down to {len(kept)}.")
    return kept


def video_duration(video_path: Path) -> Optional[float]:
    """Duration in seconds, parsed from ffmpeg's own report."""
    proc = subprocess.run(
        [_ffmpeg(), "-i", str(video_path)],
        capture_output=True, text=True, timeout=60,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
    if not match:
        return None
    h, m, sec = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def extract_interval(
    video_path: Path, output_dir: Path, every_seconds: float
) -> List[Keyframe]:
    """Sample one frame every N seconds, ignoring scene changes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("frame_*.png"):
        stale.unlink()

    subprocess.run(
        [
            _ffmpeg(), "-i", str(video_path),
            "-vf", f"fps=1/{every_seconds}",
            "-vsync", "vfr",
            "-frames:v", str(settings.frame_max),
            str(output_dir / "frame_%04d.png"), "-y",
        ],
        capture_output=True, text=True, timeout=settings.frame_extract_timeout,
    )
    return [
        Keyframe(path=path, timestamp=i * every_seconds)
        for i, path in enumerate(sorted(output_dir.glob("frame_*.png")))
    ]


def extract_and_dedupe(video_path: Path, output_dir: Path) -> List[Keyframe]:
    """Scene-detect, falling back to interval sampling when that under-samples.

    Scene detection assumes cuts. Continuously-animated video has none, so it
    can return almost nothing while the screen is carrying the entire message.
    Measured on a 25-second product announcement that the audio pipeline
    reported as "No speech detected": scene detection found 1 frame, while
    sampling every 3 seconds found 8 — and those 8 held the whole content
    ("Monday - Send the weekly team standup summary", "Giving you back some
    time"), half of it readable by free OCR.

    Falling back on a frame *count* rather than trusting the threshold means the
    failure is self-correcting instead of silent.
    """
    frames = deduplicate(extract_keyframes(video_path, output_dir))
    if len(frames) >= settings.frame_min_before_interval:
        return frames

    duration = video_duration(video_path)
    if not duration:
        return frames

    # Aim for a target count rather than a fixed cadence, so a 25-second clip
    # and a 40-minute lecture both yield a sensible number of frames.
    every = max(1.0, duration / settings.frame_interval_target)
    logger.info(
        f"Scene detection found only {len(frames)} frame(s) in "
        f"{video_path.name} — falling back to interval sampling every "
        f"{every:.0f}s."
    )
    interval = deduplicate(extract_interval(video_path, output_dir, every))
    return interval if len(interval) > len(frames) else frames


def probe_has_video_stream(video_path: Path) -> bool:
    """True when the file actually carries a video stream worth sampling.

    The pipeline downloads audio-only .wav for the Whisper path; asking ffmpeg
    to scene-detect one wastes a subprocess and logs a confusing error.
    """
    proc = subprocess.run(
        [_ffmpeg(), "-i", str(video_path)],
        capture_output=True, text=True, timeout=60,
    )
    return "Video:" in (proc.stderr or "")


def download_video(url: str, dest_dir: Path) -> Optional[Path]:
    """Fetch a low-resolution video stream for frame extraction.

    Deliberately small: frames get downscaled before OCR/VLM anyway, so paying
    for a 1080p download to sample 30 stills is waste. Returns None on failure —
    visual extraction is an enhancement, never a reason to fail a run.
    """
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    output = dest_dir / "source_video.%(ext)s"
    opts = {
        "format": f"bestvideo[height<={settings.frame_video_height}]/best[height<={settings.frame_video_height}]/best",
        "outtmpl": str(output),
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:  # noqa: BLE001 - optional feature, never fatal
        logger.warning(f"Could not download video for frame extraction: {e}")
        return None

    # yt-dlp leaves per-stream fragments (source_video.f399.mp4) alongside the
    # merged output and deletes them once merging finishes. Sorted-first picks
    # the fragment ('f' < 'm'), which is then gone by the time ffmpeg opens it —
    # an intermittent "No such file or directory" that retries cleanly, which is
    # exactly the kind of failure that gets written off as flaky.
    candidates = [
        c for c in dest_dir.glob("source_video.*")
        if c.exists() and c.suffix not in (".part", ".ytdl") and not FRAGMENT.search(c.name)
    ]
    if not candidates:
        # Fall back to anything that survived, largest first — a fragment is
        # better than nothing if merging did not produce a clean output.
        candidates = [c for c in dest_dir.glob("source_video.*") if c.exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.stat().st_size)
