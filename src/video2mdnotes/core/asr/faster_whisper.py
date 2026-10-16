"""faster-whisper (CTranslate2). CPU-only on macOS — it cannot use the GPU/ANE.

Mirrors the decode settings of `transcriber.transcribe_audio` (beam 5, VAD
filter on) so benchmark numbers describe the production path, not a variant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Tuple

from video2mdnotes.core.asr.base import (
    BackendUnavailable, Segment, clip_to_duration, module_available, wav_duration,
)

DEFAULT_MODEL = "large-v3"


class FasterWhisperBackend:
    name = "faster-whisper"
    supports_prompt = True

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        compute_type: Optional[str] = None,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> None:
        self.model = model
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.last_language: Optional[str] = None
        self._model: Any = None

    def available(self) -> Tuple[bool, str]:
        if not module_available("faster_whisper"):
            return False, "package 'faster-whisper' not installed"
        return True, "ok (CPU)"

    def load(self) -> None:
        if self._model is not None:
            return
        ok, why = self.available()
        if not ok:
            raise BackendUnavailable(f"{self.name}: {why}")
        from faster_whisper import WhisperModel

        compute = self.compute_type
        if compute is None:
            from video2mdnotes.config import settings

            compute = settings.fw_compute
        self._model = WhisperModel(self.model, device="cpu", compute_type=compute)

    def transcribe(
        self,
        wav_path: Path,
        initial_prompt: Optional[str] = None,
        language: Optional[str] = None,
    ) -> List[Segment]:
        self.load()
        segments, info = self._model.transcribe(
            str(wav_path),
            language=language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            initial_prompt=initial_prompt or None,
        )
        out = [Segment(start=s.start, end=s.end, text=s.text) for s in segments]
        self.last_language = getattr(info, "language", None) or language
        return clip_to_duration(out, wav_duration(wav_path))
