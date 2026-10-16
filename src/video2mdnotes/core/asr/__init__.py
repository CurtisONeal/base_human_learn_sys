"""Pluggable ASR backends.

    from video2mdnotes.core.asr import get_backend, transcribe_to_result
    backend = get_backend("parakeet-mlx")            # or "faster-whisper:distil-large-v3"
    ok, why = backend.available()                    # never raises, never imports the engine
    result = transcribe_to_result(backend, wav, title="...", initial_prompt="...")

Every backend yields the same `TranscriptResult` shape as
`transcriber.transcribe_audio`, so downstream code does not care which engine
ran. The engine is recorded in `model_name` as "<backend>:<model>" — the
provenance must say which engine produced the words.

See design/ASR_BENCHMARK.md for measured speed / accuracy of each.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable, Dict, List, Optional

from video2mdnotes.core.asr.base import AsrBackend, BackendUnavailable, Segment
from video2mdnotes.core.asr.faster_whisper import FasterWhisperBackend
from video2mdnotes.core.asr.mlx_whisper import MlxWhisperBackend
from video2mdnotes.core.asr.parakeet_mlx import ParakeetMlxBackend
from video2mdnotes.core.transcriber import TranscriptResult, generate_markdown

__all__ = [
    "AsrBackend",
    "BackendUnavailable",
    "BACKENDS",
    "Segment",
    "available_backends",
    "backend_status",
    "get_backend",
    "transcribe_to_result",
]

#: name -> factory(model=None) -> backend. Module imports are cheap: each
#: backend imports its engine only inside load().
BACKENDS: Dict[str, Callable[..., AsrBackend]] = {
    FasterWhisperBackend.name: FasterWhisperBackend,
    ParakeetMlxBackend.name: ParakeetMlxBackend,
    MlxWhisperBackend.name: MlxWhisperBackend,
}


def get_backend(spec: str, model: Optional[str] = None, **kwargs) -> AsrBackend:
    """Build a backend from "name" or "name:model". Unknown name -> ValueError.

    Returns the backend even when it is unavailable on this machine — call
    `.available()` to find out why; `.load()`/`.transcribe()` raise
    BackendUnavailable rather than ImportError.
    """
    name, _, spec_model = spec.partition(":")
    name = name.strip()
    if name not in BACKENDS:
        raise ValueError(
            f"unknown ASR backend {name!r}; known: {', '.join(sorted(BACKENDS))}"
        )
    chosen = model or spec_model.strip() or None
    if chosen:
        kwargs["model"] = chosen
    return BACKENDS[name](**kwargs)


def backend_status() -> Dict[str, tuple]:
    """{name: (ok, reason)} for every registered backend, default models."""
    return {name: factory().available() for name, factory in BACKENDS.items()}


def available_backends() -> List[str]:
    return [name for name, (ok, _) in backend_status().items() if ok]


def transcribe_to_result(
    backend: AsrBackend,
    wav_path: Path,
    title: str = "Unknown",
    initial_prompt: Optional[str] = None,
    language: Optional[str] = None,
) -> TranscriptResult:
    """Run one backend over one wav and package it like transcribe_audio()."""
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    segments = backend.transcribe(
        wav_path,
        initial_prompt=initial_prompt if backend.supports_prompt else None,
        language=language,
    )
    lang = backend.last_language or language or "und"
    model_name = f"{backend.name}:{backend.model}"
    # Machine ASR, named by engine: never confusable with a caption track.
    provenance = f"asr ({model_name})"
    return TranscriptResult(
        source_file=wav_path,
        language=lang,
        segments=segments,
        full_text="".join(s.text for s in segments),
        markdown_content=generate_markdown(
            title=title,
            source_file=wav_path.name,
            segments=segments,
            language=lang,
            model=model_name,
            transcript_source=provenance,
        ),
        model_name=model_name,
        generated_at=dt.datetime.now(),
        transcript_source=provenance,
    )
