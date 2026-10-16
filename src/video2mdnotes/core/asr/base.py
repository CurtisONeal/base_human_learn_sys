"""The ASR backend contract.

A backend turns one bounded mono 16 kHz wav window into `Segment`s whose times
are relative to the start of that window. Windowing, offsets and stitching are
the caller's job (WP-6b), so a backend never sees — and must never produce — a
whole long recording.

Availability is checked without importing the engine: `importlib.util.find_spec`
plus a platform test. A missing optional package therefore makes a backend
*report* unavailable; it never makes `import video2mdnotes.core.asr` fail, and
it never pulls torch/MLX into a process that will not use them.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
from pathlib import Path
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from video2mdnotes.core.transcriber import Segment, Word

__all__ = [
    "AsrBackend",
    "BackendUnavailable",
    "Segment",
    "Word",
    "finalize_segments",
    "words_from_tokens",
    "module_available",
    "is_apple_silicon",
]


class BackendUnavailable(RuntimeError):
    """The backend cannot run here (package missing, wrong platform)."""


@runtime_checkable
class AsrBackend(Protocol):
    #: Registry name, e.g. "faster-whisper".
    name: str
    #: Model identifier as the engine understands it.
    model: str
    #: Whether `initial_prompt` is honoured. Parakeet has no prompt input; a
    #: caller should know the vocabulary hint was dropped rather than assume it.
    supports_prompt: bool
    #: Language detected or used on the last call, if the engine reports one.
    last_language: Optional[str]

    def available(self) -> Tuple[bool, str]:
        """(ok, reason). Cheap: must not import the engine or load a model."""
        ...

    def load(self) -> None:
        """Load (and if needed download) the model. Idempotent."""
        ...

    def transcribe(
        self,
        wav_path: Path,
        initial_prompt: Optional[str] = None,
        language: Optional[str] = None,
    ) -> List[Segment]:
        """Transcribe one window. Times are seconds from the window start."""
        ...


def wav_duration(wav_path: Path) -> Optional[float]:
    """Seconds of audio in a PCM wav, from the header; None if unreadable."""
    import wave

    try:
        with wave.open(str(wav_path)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, OSError, EOFError, ZeroDivisionError):
        return None


def clip_to_duration(segments: List[Segment], duration: Optional[float]) -> List[Segment]:
    """Drop segments starting at/after the end of the audio; clamp the rest.

    Measured: mlx-whisper keeps decoding into the zero padding after the last
    real audio — 73 garbage segments ("C", "") between 600 s and 613 s on a
    600 s window, and segments past 3 s on 3 s of silence. Text that starts
    after the audio ends cannot be speech, and WP-6b's offset stitching would
    otherwise place it inside the NEXT window's time range.

    Also repairs an end time earlier than its start (measured once from
    mlx-whisper large-v3: a 443.84 -> 418.30 s segment) by setting end = start.
    """
    out = []
    for s in segments:
        if duration is not None and s.start >= duration:
            continue
        end = max(s.end, s.start)
        if duration is not None:
            end = min(end, duration)
        if end != s.end:
            s = s.model_copy(update={"end": end})
        out.append(s)
    return out


def finalize_segments(segments: List[Segment], duration: Optional[float]) -> List[Segment]:
    """What every backend returns: clipped to the audio, then length-capped.

    Clip first so the split works inside [start, min(end, duration)] — the
    parts of a split segment never extend past the audio either.
    """
    from video2mdnotes.core.asr.segmenting import split_long_segments

    return split_long_segments(clip_to_duration(segments, duration))


def words_from_tokens(tokens) -> List[Word]:
    """Sub-word tokens (text, start, end) -> whitespace words with timings.

    A token whose text starts with whitespace begins a new word; any other
    token continues the current one (parakeet's SentencePiece convention).
    The words' texts concatenate to exactly the tokens' texts.
    """
    words: List[Word] = []
    for t in tokens:
        text, start, end = t.text, float(t.start), float(t.end)
        if not text:
            continue
        if words and not text[:1].isspace():
            w = words[-1]
            words[-1] = Word(start=w.start, end=max(w.end, end), text=w.text + text)
        else:
            words.append(Word(start=start, end=max(end, start), text=text))
    return words


def module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"
