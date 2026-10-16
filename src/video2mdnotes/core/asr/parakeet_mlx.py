"""NVIDIA Parakeet-TDT via MLX (Apple-Silicon GPU).

No prompt input: the vocabulary hint is dropped, and `supports_prompt` says so.
No language input either — v3 auto-detects among 25 EUROPEAN languages and has
no Japanese, so it must never be chosen where `asr_select` would require the
multilingual Whisper model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

from video2mdnotes.core.asr.base import (
    BackendUnavailable, Segment, clip_to_duration, is_apple_silicon,
    module_available, wav_duration,
)

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"


class ParakeetMlxBackend:
    name = "parakeet-mlx"
    supports_prompt = False

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        chunk_duration: float = 120.0,
        overlap_duration: float = 15.0,
    ) -> None:
        self.model = model
        # Encoder attention is quadratic in input length; chunking keeps memory
        # bounded on a 10-30 min window. 120 s / 15 s is the parakeet-mlx CLI
        # default.
        self.chunk_duration = chunk_duration
        self.overlap_duration = overlap_duration
        self.last_language: Optional[str] = None
        self._model: Any = None

    def available(self) -> Tuple[bool, str]:
        if not is_apple_silicon():
            return False, "MLX requires macOS on Apple Silicon"
        if not module_available("parakeet_mlx"):
            return False, "package 'parakeet-mlx' not installed (extra: asr-mlx)"
        return True, "ok (MLX/Metal)"

    def load(self) -> None:
        if self._model is not None:
            return
        ok, why = self.available()
        if not ok:
            raise BackendUnavailable(f"{self.name}: {why}")
        from parakeet_mlx import from_pretrained

        self._model = from_pretrained(self.model)

    def transcribe(
        self,
        wav_path: Path,
        initial_prompt: Optional[str] = None,
        language: Optional[str] = None,
    ) -> List[Segment]:
        self.load()
        result = self._model.transcribe(
            str(wav_path),
            chunk_duration=self.chunk_duration,
            overlap_duration=self.overlap_duration,
        )
        self.last_language = None  # the engine does not report one
        segs = [
            Segment(start=float(s.start), end=float(s.end), text=" " + s.text.strip())
            for s in result.sentences
            if s.text.strip()
        ]
        return clip_to_duration(segs, wav_duration(wav_path))
