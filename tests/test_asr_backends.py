"""ASR backend registry, unavailable handling, and benchmark metrics.

No model runs here except in the `integration`-marked tests at the bottom.
"""

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from video2mdnotes.core import asr
from video2mdnotes.core.asr import (
    BACKENDS, BackendUnavailable, get_backend, transcribe_to_result,
)
from video2mdnotes.core.asr import base as asr_base
from video2mdnotes.core.asr import metrics as M
from video2mdnotes.core.asr.faster_whisper import FasterWhisperBackend
from video2mdnotes.core.asr.mlx_whisper import MlxWhisperBackend
from video2mdnotes.core.asr.parakeet_mlx import ParakeetMlxBackend
from video2mdnotes.core.transcriber import Segment


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_registry_names():
    assert set(BACKENDS) == {"faster-whisper", "parakeet-mlx", "mlx-whisper"}


def test_get_backend_default_and_spec_model():
    b = get_backend("faster-whisper")
    assert isinstance(b, FasterWhisperBackend) and b.model == "large-v3"
    b = get_backend("faster-whisper:distil-large-v3")
    assert b.model == "distil-large-v3"
    b = get_backend("mlx-whisper:mlx-community/whisper-large-v3-mlx")
    assert isinstance(b, MlxWhisperBackend)
    assert b.model == "mlx-community/whisper-large-v3-mlx"
    # explicit model= wins over the spec suffix
    assert get_backend("parakeet-mlx:x", model="y").model == "y"


def test_get_backend_unknown_name():
    with pytest.raises(ValueError, match="unknown ASR backend"):
        get_backend("whisper.cpp")


def test_backends_satisfy_protocol():
    for factory in BACKENDS.values():
        assert isinstance(factory(), asr_base.AsrBackend)


def test_prompt_support_flags():
    # Parakeet has no prompt input; the flag must say so, so callers know the
    # vocabulary hint was dropped rather than silently assume it was used.
    assert ParakeetMlxBackend.supports_prompt is False
    assert FasterWhisperBackend.supports_prompt is True
    assert MlxWhisperBackend.supports_prompt is True


# --------------------------------------------------------------------------
# Unavailable handling: report, never crash
# --------------------------------------------------------------------------

@pytest.fixture
def no_mlx_packages(monkeypatch):
    real = asr_base.module_available

    def fake(mod):
        return False if mod in ("parakeet_mlx", "mlx_whisper") else real(mod)

    # Backends import module_available by name, so patch where it is used.
    for mod in ("parakeet_mlx", "mlx_whisper"):
        monkeypatch.setattr(f"video2mdnotes.core.asr.{mod}.module_available", fake)
    monkeypatch.setattr("video2mdnotes.core.asr.parakeet_mlx.is_apple_silicon", lambda: True)
    monkeypatch.setattr("video2mdnotes.core.asr.mlx_whisper.is_apple_silicon", lambda: True)


def test_missing_package_reports_unavailable(no_mlx_packages, tmp_path):
    for name in ("parakeet-mlx", "mlx-whisper"):
        b = get_backend(name)
        ok, why = b.available()
        assert ok is False
        assert "not installed" in why and "asr-mlx" in why
        with pytest.raises(BackendUnavailable, match=name):
            b.load()
        with pytest.raises(BackendUnavailable):
            b.transcribe(tmp_path / "x.wav")


def test_available_backends_excludes_missing(no_mlx_packages):
    status = asr.backend_status()
    assert status["parakeet-mlx"][0] is False
    assert "parakeet-mlx" not in asr.available_backends()
    assert "mlx-whisper" not in asr.available_backends()


def test_wrong_platform_reports_unavailable(monkeypatch):
    monkeypatch.setattr("video2mdnotes.core.asr.parakeet_mlx.is_apple_silicon", lambda: False)
    ok, why = ParakeetMlxBackend().available()
    assert not ok and "Apple Silicon" in why


def test_availability_check_does_not_import_engine(monkeypatch):
    import sys
    for mod in ("parakeet_mlx", "mlx_whisper"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    ParakeetMlxBackend().available()
    MlxWhisperBackend().available()
    assert "parakeet_mlx" not in sys.modules
    assert "mlx_whisper" not in sys.modules


def test_module_available_handles_bad_names():
    assert asr_base.module_available("definitely_not_a_module_xyz") is False
    assert asr_base.module_available("json") is True


# --------------------------------------------------------------------------
# transcribe_to_result: same TranscriptResult shape from any backend
# --------------------------------------------------------------------------

class _FakeBackend:
    name = "fake"
    model = "m1"
    supports_prompt = False
    last_language = None

    def __init__(self):
        self.prompt_seen = "unset"

    def available(self):
        return True, "ok"

    def load(self):
        pass

    def transcribe(self, wav_path, initial_prompt=None, language=None):
        self.prompt_seen = initial_prompt
        return [Segment(start=0.0, end=1.5, text=" Hello Claude."),
                Segment(start=1.5, end=3.0, text=" Second line.")]


def test_transcribe_to_result_shape_and_provenance(tmp_path):
    wav = tmp_path / "w.wav"
    wav.write_bytes(b"RIFF")
    fb = _FakeBackend()
    r = transcribe_to_result(fb, wav, title="T", initial_prompt="Claude, MCP", language="en")
    assert r.model_name == "fake:m1"
    assert r.transcript_source == "asr (fake:m1)"
    assert r.full_text == " Hello Claude. Second line."
    assert r.language == "en"
    # provenance reaches the rendered markdown, not just the object
    assert "transcript_source: asr (fake:m1)" in r.markdown_content
    assert "- [0:00:01.500] Second line." in r.markdown_content
    # a backend without prompt support is not handed the prompt
    assert fb.prompt_seen is None


def test_clip_to_duration_drops_padding_hallucinations():
    segs = [Segment(start=0.0, end=2.0, text="a"),
            Segment(start=2.5, end=3.4, text="b"),     # straddles the end
            Segment(start=3.0, end=3.0, text="C"),     # starts at the end
            Segment(start=3.2, end=4.0, text="C")]     # entirely past it
    out = asr_base.clip_to_duration(segs, 3.0)
    assert [s.text for s in out] == ["a", "b"]
    assert out[1].end == 3.0
    assert asr_base.clip_to_duration(segs, None) == segs
    backwards = [Segment(start=443.84, end=418.30, text="x")]
    fixed = asr_base.clip_to_duration(backwards, 600.0)
    assert fixed[0].end == fixed[0].start == 443.84


def test_wav_duration(tmp_path):
    import wave
    p = tmp_path / "s.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 2)
    assert asr_base.wav_duration(p) == pytest.approx(2.0)
    assert asr_base.wav_duration(tmp_path / "missing.wav") is None


def test_transcribe_to_result_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        transcribe_to_result(_FakeBackend(), tmp_path / "nope.wav")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def test_normalize_words():
    assert M.normalize_words("Claude's SDK, it's — CCA-F! code.claude.com") == [
        "claudes", "sdk", "its", "cca", "f", "code", "claude", "com"]
    assert M.normalize_words("") == []


@pytest.mark.parametrize("ref,hyp,expected", [
    ("a b c d", "a b c d", (0, 0, 0)),
    ("a b c d", "a x c d", (1, 0, 0)),
    ("a b c d", "a c d", (0, 1, 0)),
    ("a b c d", "a b q c d", (0, 0, 1)),
    ("a b c", "", (0, 3, 0)),
    ("", "a b", (0, 0, 2)),
])
def test_levenshtein_counts(ref, hyp, expected):
    assert M._levenshtein_counts(ref.split(), hyp.split()) == expected


def test_wer_builtin_normalises_first():
    r = M.word_error_rate("Hello, World! Claude Code.", "hello world cloud code", use_jiwer=False)
    assert r.engine == "builtin"
    assert (r.substitutions, r.deletions, r.insertions) == (1, 0, 0)
    assert r.ref_words == 4
    assert math.isclose(r.wer, 0.25)


def test_wer_empty_reference():
    assert M.word_error_rate("", "", use_jiwer=False).wer == 0.0
    assert math.isinf(M.word_error_rate("", "x", use_jiwer=False).wer)


def test_glossary_counts_hits_and_mishearings():
    text = ("Claude's Claude Code docs. Use cloud code or Cloud Desktop with "
            "cloud sonnet; MCP servers, the Agent SDK, Anthropic. Clawed and clawd.")
    g = M.glossary_counts(text)
    assert g["Claude"] == 2
    assert g["Claude Code"] == 1
    assert g["MCP"] == 1
    assert g["SDK"] == 1
    assert g["Anthropic"] == 1
    assert g["cloud code"] == 1
    assert g["cloud+product"] == 3
    assert g["clawed/clawd"] == 2
    assert M.glossary_counts("the clod agent, Claud said")["clod/claud"] == 2
    assert M.glossary_counts("agent STK, the SDK")["STK (for SDK)"] == 1


def test_glossary_does_not_flag_real_cloud():
    g = M.glossary_counts("Deploy to a cloud provider. The cloud is big. Cloud computing. Iron clad.")
    assert g["clod/claud"] == 0
    assert g["cloud code"] == 0 and g["cloud+product"] == 0
    assert g["Claude"] == 0


def test_repeated_ngrams_flags_loops():
    assert M.repeated_ngrams("one two three four five six") == 0
    looped = " ".join(["so in the end im going"] * 5)
    assert M.repeated_ngrams(looped) >= 20
    assert M.repeated_ngrams("") == 0


def test_caption_window_selection(tmp_path):
    md = tmp_path / "c.md"
    md.write_text(
        "---\ntitle: x\n---\n"
        "- [0:47:35.900] before window\n"
        "- [0:47:36.000] first in\n"
        "- [0:57:35.999] last in\n"
        "- [0:57:36.000] after window\n"
        "not a caption line\n",
        encoding="utf-8",
    )
    start = M.parse_hms("0:47:36")
    lines = M.caption_lines_in_window(md, start, 600)
    assert [t for _, t in lines] == ["first in", "last in"]
    assert lines[0][0] == pytest.approx(0.0)
    assert lines[1][0] == pytest.approx(599.999)
    assert M.caption_text_in_window(md, start, 600) == "first in last in"


def test_parse_hms():
    assert M.parse_hms("0:47:36") == 2856
    assert M.parse_hms("4:15:46") == 15346
    assert M.parse_hms("47:36") == 2856
    assert M.parse_hms("12.5") == 12.5


def test_timed_words_and_offsets():
    ref = M.timed_words([(0.0, 4.0, "one two three four"), (4.0, 8.0, "five six seven eight")])
    assert ref[0] == (0.0, "one") and ref[4] == (4.0, "five")
    hyp = [(t + 0.5, w) for t, w in ref]
    out = M.timestamp_offsets(ref, hyp)
    assert out["matched_words"] == 8
    assert out["median_s"] == pytest.approx(0.5)
    # short chance matches (< min_run) are ignored
    out = M.timestamp_offsets(ref, [(100.0, "one"), (101.0, "zzz")])
    assert out["matched_words"] == 0 and math.isnan(out["median_s"])


def test_caption_spans_end_at_next_start():
    spans = M.caption_spans([(0.0, "a"), (2.0, "b")], window_s=600)
    assert spans == [(0.0, 2.0, "a"), (2.0, 5.0, "b")]


# --------------------------------------------------------------------------
# Real engines (slow; need models cached)
# --------------------------------------------------------------------------

def _tone_wav(path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "3", "-c:a", "pcm_s16le",
                    str(path)], check=True)
    return True


@pytest.mark.integration
@pytest.mark.parametrize("spec", ["faster-whisper:tiny", "parakeet-mlx",
                                  "mlx-whisper:mlx-community/whisper-large-v3-turbo"])
def test_real_backend_runs_on_silence(spec, tmp_path):
    b = get_backend(spec)
    ok, why = b.available()
    if not ok:
        pytest.skip(why)
    wav = tmp_path / "s.wav"
    if not _tone_wav(wav):
        pytest.skip("ffmpeg not on PATH")
    r = transcribe_to_result(b, wav, title="silence", language="en")
    assert r.model_name.startswith(b.name + ":")
    assert all(0.0 <= s.start <= s.end <= 3.5 for s in r.segments)
