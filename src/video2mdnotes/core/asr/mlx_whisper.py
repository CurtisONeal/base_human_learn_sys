"""OpenAI Whisper via MLX (Apple-Silicon GPU).

Same model family as faster-whisper, so it honours `initial_prompt` and
`language`; differs in runtime (Metal vs CPU) and in having no VAD filter.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from video2mdnotes.core.asr.base import (
    BackendUnavailable, Segment, clip_to_duration, is_apple_silicon,
    module_available, wav_duration,
)

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


class MlxWhisperBackend:
    name = "mlx-whisper"
    supports_prompt = True

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self.last_language: Optional[str] = None
        self._loaded = False

    def available(self) -> Tuple[bool, str]:
        if not is_apple_silicon():
            return False, "MLX requires macOS on Apple Silicon"
        if not module_available("mlx_whisper"):
            return False, "package 'mlx-whisper' not installed (extra: asr-mlx)"
        return True, "ok (MLX/Metal)"

    def load(self) -> None:
        if self._loaded:
            return
        ok, why = self.available()
        if not ok:
            raise BackendUnavailable(f"{self.name}: {why}")
        # mlx_whisper.transcribe() loads weights through ModelHolder, which
        # caches by repo id. Warming it here keeps download + load time out of
        # the first window's measurement.
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.get_model(self.model, mx.float16)
        self._loaded = True

    def transcribe(
        self,
        wav_path: Path,
        initial_prompt: Optional[str] = None,
        language: Optional[str] = None,
    ) -> List[Segment]:
        self.load()
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=self.model,
            initial_prompt=initial_prompt or None,
            language=language,
            verbose=None,
        )
        self.last_language = result.get("language") or language
        segs = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=s["text"])
            for s in result.get("segments", [])
        ]
        return clip_to_duration(segs, wav_duration(wav_path))
