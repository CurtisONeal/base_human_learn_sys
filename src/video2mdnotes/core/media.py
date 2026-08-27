"""Local audio and video files as a source. No download, no network.

The video path gets its audio from yt-dlp, which hands over a predictable wav.
A file off a voice recorder does not: it may be AMR at 8 kHz mono, WMA from a
Windows-era device, IMA-ADPCM inside a `.wav` that nothing but ffmpeg will
open, or an `.mp4` where the audio is one stream among several.

So this module makes exactly one assumption — **ffmpeg can decode it** — and
verifies that assumption per file rather than trusting the extension. Measured
on this machine, ffmpeg carries decoders for every format a consumer recorder
is likely to emit:

    mp3  aac  amr_nb  amr_wb  wmav2  opus  vorbis  flac
    adpcm_ima_wav  gsm  speex  sbc  pcm_*

**Extension is not format.** Cheap recorders routinely write `.wav` files
containing ADPCM or GSM rather than PCM, and `.mp3` files that are actually
MPEG-2.5 at 8 kHz. Probing tells the truth; the extension is only a hint about
which files to try.

**ffprobe is not installed on this machine**, so probing runs `ffmpeg -i` and
reads its stderr. That is uglier than `ffprobe -show_streams -of json` but it
works with the binary that is actually here, which matters more.

Nothing here reaches the network. WP-03's acceptance criterion says so
explicitly and a test asserts it: a local file must never trigger a download.

**Getting files off a recorder is a separate problem from decoding them**, and
this module cannot solve it. Three cases, in descending order of luck:

* **USB mass storage** — the device appears under `/Volumes`. Point straight at
  it: `media /Volumes/RECORDER --probe`. No copying step needed.
* **MTP** — macOS will not mount it at all. Needs Android File Transfer or
  similar; nothing in this repo helps.
* **Proprietary container** — the files copy over but ffmpeg cannot read them.
  Export to WAV or MP3 from the vendor software first. `probe()` says so by
  name rather than reporting a generic decode failure, because that is the
  point at which people usually give up.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


# Containers worth trying. Deliberately broad: the cost of attempting a file
# ffmpeg cannot read is one fast failure with a clear message, whereas omitting
# a format the user's recorder emits means the tool silently ignores their data.
AUDIO_SUFFIXES = {
    ".mp3", ".m4a", ".m4b", ".wav", ".aac", ".opus", ".ogg", ".oga", ".flac",
    ".wma", ".amr", ".awb", ".3gp", ".3gpp", ".aiff", ".aif", ".aifc", ".caf",
    ".mp2", ".mpga", ".spx", ".ac3", ".dts", ".ape", ".wv", ".mka", ".voc",
    ".au", ".snd", ".gsm", ".dss", ".dvf", ".msv", ".vox",
}
VIDEO_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".3g2",
}
SUPPORTED = AUDIO_SUFFIXES | VIDEO_SUFFIXES

# `.mts` is MPEG transport stream AND the TypeScript module-declaration
# extension. A real `data-U8FS-wNn.d.mts` in Downloads was picked up as video
# and reported as an unreadable recording, which is noise that trains you to
# ignore the failure list. Compound suffixes settle it without sniffing.
EXCLUDED_COMPOUND_SUFFIXES = (".d.mts", ".d.ts", ".d.cts")

# Whisper wants 16 kHz mono. Converting once here means the model never sees a
# 48 kHz stereo file it would resample internally anyway.
TARGET_RATE = 16_000

_DURATION = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_AUDIO_STREAM = re.compile(
    r"Stream #\d+:\d+.*?:\s*Audio:\s*([A-Za-z0-9_]+).*?(\d+)\s*Hz,\s*([^,]+)", re.S
)
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?:\s*Video:")
# Recognised so the user is told the real problem rather than "decode failed".
_DRM = re.compile(r"drm|encrypted|protected content|sample_aes", re.I)


class MediaError(RuntimeError):
    """Base for everything this module refuses."""


class NoAudioStreamError(MediaError):
    """The file opened, but carries no audio — a silent screen recording, say."""


class EncryptedMediaError(MediaError):
    """DRM-protected (e.g. an iTunes .m4p). Not something to work around."""


class UnreadableMediaError(MediaError):
    """ffmpeg could not decode it at all."""


@dataclass
class MediaInfo:
    path: Path
    duration_seconds: float
    codec: str
    sample_rate: int
    channels: str
    has_video: bool

    @property
    def is_probably_speech(self) -> bool:
        """A weak but useful hint that this is a voice recording.

        Consumer recorders write mono at 8-24 kHz. Music is near-universally
        stereo at 44.1 kHz or above. This does not gate anything — it is
        reported so a surprising transcription result has an explanation.
        """
        return "mono" in self.channels.lower() and self.sample_rate <= 24_000

    @property
    def human_duration(self) -> str:
        total = int(self.duration_seconds)
        return f"{total // 3600:d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise MediaError(
            "ffmpeg not found on PATH. It decodes every input format this "
            "supports; without it no local media can be read."
        )
    return binary


def probe(path: Path) -> MediaInfo:
    """Read real stream information out of the file.

    Uses `ffmpeg -i` rather than ffprobe because ffprobe is not installed here.
    ffmpeg exits non-zero when given no output target, so the return code is
    ignored and only stderr is parsed — that is expected, not a failure.

    Raises:
        EncryptedMediaError, NoAudioStreamError, UnreadableMediaError
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    output = proc.stderr or ""

    if _DRM.search(output):
        raise EncryptedMediaError(
            f"{path.name} is DRM-protected. This tool does not strip DRM; "
            "use a copy you can play without it."
        )

    audio = _AUDIO_STREAM.search(output)
    if not audio:
        if "Invalid data found" in output or "No such file" in output:
            raise UnreadableMediaError(
                f"{path.name}: ffmpeg cannot decode this file. If it came off a "
                "recorder, it may be a proprietary container that needs the "
                "vendor's export step first."
            )
        raise NoAudioStreamError(
            f"{path.name} has no audio stream — nothing to transcribe."
        )

    duration = 0.0
    match = _DURATION.search(output)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    return MediaInfo(
        path=path,
        duration_seconds=duration,
        codec=audio.group(1),
        sample_rate=int(audio.group(2)),
        channels=audio.group(3).strip(),
        has_video=bool(_VIDEO_STREAM.search(output)),
    )


def to_wav(path: Path, destination: Path, info: Optional[MediaInfo] = None) -> Path:
    """Decode anything ffmpeg understands into 16 kHz mono PCM wav.

    `-vn` matters: handed an `.mp4`, ffmpeg would otherwise try to carry the
    video stream into a wav container and fail with a message about muxers that
    says nothing about the real situation.
    """
    path, destination = Path(path), Path(destination)
    info = info or probe(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path),
            "-vn",                       # drop video; we only want speech
            "-ac", "1",
            "-ar", str(TARGET_RATE),
            "-c:a", "pcm_s16le",
            str(destination),
        ],
        capture_output=True, text=True, timeout=3600,
    )
    if proc.returncode != 0 or not destination.exists():
        raise UnreadableMediaError(
            f"{path.name}: conversion failed — {(proc.stderr or '').strip()[:200]}"
        )
    if destination.stat().st_size < 1024:
        raise NoAudioStreamError(
            f"{path.name}: produced an empty audio track ({info.human_duration} "
            "reported). The file may be silent or truncated."
        )
    return destination


def find_media(root: Path) -> List[Path]:
    """Every supported media file under root, deterministically ordered."""
    root = Path(root)
    if root.is_file():
        usable = (root.suffix.lower() in SUPPORTED
                  and not root.name.lower().endswith(EXCLUDED_COMPOUND_SUFFIXES))
        return [root] if usable else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED
        and not p.name.lower().endswith(EXCLUDED_COMPOUND_SUFFIXES)
    )


def describe(info: MediaInfo) -> str:
    """One line a human can sanity-check against what they think they recorded."""
    shape = "video+audio" if info.has_video else "audio"
    hint = " · looks like a voice recording" if info.is_probably_speech else ""
    return (
        f"{info.human_duration} · {info.codec} · {info.sample_rate} Hz · "
        f"{info.channels} · {shape}{hint}"
    )
