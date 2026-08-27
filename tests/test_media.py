"""Local media input tests.

Fixtures are REAL encoded files built with ffmpeg, not mocks. The whole risk in
this module is "the format is not what the extension says", and a mock cannot
express that — a mocked `.wav` is whatever the test author imagined, while a
real ADPCM `.wav` is the thing that actually comes off a voice recorder.

Building them takes a second and they are torn down with tmp_path.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from video2mdnotes.core.media import (
    SUPPORTED, EncryptedMediaError, MediaInfo, NoAudioStreamError,
    UnreadableMediaError, describe, find_media, probe, to_wav,
)

ffmpeg_present = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _encode(destination: Path, *, codec: str, rate: int, channels: int,
            seconds: int = 2, video: bool = False, audio: bool = True) -> Path:
    """Build a real media file with ffmpeg."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        command += ["-f", "lavfi", "-i",
                    f"testsrc=duration={seconds}:size=160x120:rate=10"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}"]
        command += ["-ar", str(rate), "-ac", str(channels), "-c:a", codec]
    if video:
        command += ["-c:v", "libx264", "-shortest"]
    command.append(str(destination))
    subprocess.run(command, capture_output=True, check=True, timeout=120)
    return destination


# --- Format coverage -------------------------------------------------------

@ffmpeg_present
@pytest.mark.parametrize("name,codec,rate,channels", [
    # The recorder gotcha: a .wav that is not PCM. Cheap devices write these
    # and most naive readers choke on them.
    ("adpcm.wav", "adpcm_ima_wav", 8000, 1),
    ("windows_era.wma", "wmav2", 8000, 1),
    ("voice.mp3", "libmp3lame", 16000, 1),
    ("phone.m4a", "aac", 44100, 1),
    ("stereo.opus", "libopus", 48000, 2),
    ("music.flac", "flac", 44100, 2),
])
def test_real_files_in_awkward_formats_convert(tmp_path, name, codec, rate, channels):
    source = _encode(tmp_path / name, codec=codec, rate=rate, channels=channels)
    info = probe(source)
    assert info.duration_seconds > 1
    output = to_wav(source, tmp_path / "out" / "converted.wav", info)
    assert output.exists() and output.stat().st_size > 1024


@ffmpeg_present
def test_extension_is_not_trusted_over_actual_codec(tmp_path):
    """A .wav holding ADPCM must be reported as ADPCM, not assumed PCM."""
    source = _encode(tmp_path / "lies.wav", codec="adpcm_ima_wav", rate=8000, channels=1)
    assert probe(source).codec == "adpcm_ima_wav"


@ffmpeg_present
def test_output_is_always_16k_mono_whatever_went_in(tmp_path):
    """Whisper wants 16 kHz mono; converting once beats resampling per call."""
    source = _encode(tmp_path / "big.flac", codec="flac", rate=44100, channels=2)
    output = to_wav(source, tmp_path / "out.wav")
    info = probe(output)
    assert info.sample_rate == 16_000 and "mono" in info.channels.lower()


# --- Video -----------------------------------------------------------------

@ffmpeg_present
def test_audio_is_extracted_from_a_video_file(tmp_path):
    source = _encode(tmp_path / "screen.mp4", codec="aac", rate=44100,
                     channels=1, video=True)
    info = probe(source)
    assert info.has_video
    assert to_wav(source, tmp_path / "out.wav", info).stat().st_size > 1024


@ffmpeg_present
def test_video_with_no_audio_is_refused_with_a_reason(tmp_path):
    """A silent screen recording must say so, not transcribe silence."""
    source = _encode(tmp_path / "silent.mp4", codec="", rate=0, channels=0,
                     video=True, audio=False)
    with pytest.raises(NoAudioStreamError, match="no audio stream"):
        probe(source)


# --- Failure modes ---------------------------------------------------------

@ffmpeg_present
def test_a_file_that_is_not_media_fails_with_advice(tmp_path):
    """Recorder owners hit this: a proprietary container named .mp3."""
    fake = tmp_path / "notmedia.mp3"
    fake.write_text("this is not media at all")
    with pytest.raises(UnreadableMediaError, match="vendor"):
        probe(fake)


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        probe(tmp_path / "nope.mp3")


def test_drm_is_reported_as_drm_not_as_a_decode_failure(monkeypatch, tmp_path):
    """An iTunes .m4p should not read as "corrupt file".

    Mocks subprocess.run AND shutil.which — probe() checks for the ffmpeg
    binary via shutil.which() before ever reaching subprocess.run(), so mocking
    only the latter left this test silently dependent on ffmpeg actually being
    installed. It happened to pass because ffmpeg is on this machine; it should
    not need to be.
    """
    import video2mdnotes.core.media as media

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "Error: this file has DRM protection applied"

    target = tmp_path / "bought.m4p"
    target.write_bytes(b"x")
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(EncryptedMediaError, match="DRM"):
        probe(target)


# --- No network ------------------------------------------------------------

@ffmpeg_present
def test_local_media_never_touches_the_network(tmp_path, monkeypatch):
    """WP-03 acceptance: a file on disk must not trigger a download."""
    import socket

    def _no_socket(*args, **kwargs):
        raise AssertionError("local media path opened a network connection")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)
    source = _encode(tmp_path / "a.mp3", codec="libmp3lame", rate=16000, channels=1)
    to_wav(source, tmp_path / "out.wav")


# --- Discovery and reporting ----------------------------------------------

@ffmpeg_present
def test_find_media_walks_nested_directories(tmp_path):
    _encode(tmp_path / "a.mp3", codec="libmp3lame", rate=16000, channels=1)
    _encode(tmp_path / "sub" / "b.m4a", codec="aac", rate=44100, channels=1)
    (tmp_path / "notes.txt").write_text("ignore me")
    found = find_media(tmp_path)
    assert [p.name for p in found] == ["a.mp3", "b.m4a"]


def test_find_media_order_is_deterministic(tmp_path):
    for name in ("c.mp3", "a.mp3", "b.mp3"):
        (tmp_path / name).write_bytes(b"x")
    assert find_media(tmp_path) == find_media(tmp_path)


def test_voice_recording_hint_uses_mono_and_low_sample_rate():
    """Consumer recorders are mono at 8-24 kHz; music is stereo at 44.1+."""
    recorder = MediaInfo(Path("a"), 60, "amr_nb", 8000, "mono", False)
    music = MediaInfo(Path("b"), 60, "flac", 44100, "stereo", False)
    assert recorder.is_probably_speech
    assert not music.is_probably_speech


def test_describe_is_checkable_by_a_human():
    info = MediaInfo(Path("a"), 3661, "amr_nb", 8000, "mono", False)
    text = describe(info)
    assert "1:01:01" in text and "amr_nb" in text and "voice recording" in text


def test_supported_covers_the_formats_consumer_recorders_emit():
    """Breadth is deliberate: omitting a format means silently ignoring data."""
    for suffix in (".amr", ".wma", ".3gp", ".dss", ".msv", ".vox", ".opus", ".m4a"):
        assert suffix in SUPPORTED


# --- CLI integration -------------------------------------------------------

@ffmpeg_present
def test_media_command_produces_the_same_artifacts_as_a_video(tmp_path, monkeypatch):
    """WP-03 acceptance: same artifact shape, origin is the file path."""
    import datetime as dt
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app
    from video2mdnotes.core.transcriber import Segment, TranscriptResult
    from video2mdnotes.core.summarizer import SummaryResult

    source = _encode(tmp_path / "meeting.m4a", codec="aac", rate=44100, channels=1)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")

    transcript = TranscriptResult(
        source_file=tmp_path / "x.wav", language="en",
        segments=[Segment(start=0, end=2, text="hello there")],
        full_text="hello there", markdown_content="# T\n\n- [0:00:00.000] hello there",
        model_name="medium", generated_at=dt.datetime.now(),
    )
    monkeypatch.setattr("video2mdnotes.main.transcribe_audio",
                        lambda *a, **k: transcript)
    monkeypatch.setattr("video2mdnotes.main.generate_summary",
                        lambda t: SummaryResult(
                            source_file=tmp_path / "x.wav", model_name="test",
                            summary_text="## Summary\n\nnotes",
                            generated_at=dt.datetime.now()))

    result = CliRunner().invoke(app, ["media", str(source)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "meeting" / "transcripts" / "meeting.md").exists()
    assert (tmp_path / "out" / "meeting" / "summaries" / "meeting.summary.md").exists()


@ffmpeg_present
def test_transcript_survives_a_summarizer_failure(tmp_path, monkeypatch):
    """Transcription is the expensive step; a summary failure must not lose it."""
    import datetime as dt
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app
    from video2mdnotes.core.transcriber import Segment, TranscriptResult

    source = _encode(tmp_path / "talk.mp3", codec="libmp3lame", rate=16000, channels=1)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")
    monkeypatch.setattr("video2mdnotes.main.transcribe_audio", lambda *a, **k:
        TranscriptResult(
            source_file=tmp_path / "x.wav", language="en",
            segments=[Segment(start=0, end=1, text="real words")],
            full_text="real words", markdown_content="# T\n\n- [0:00:00.000] real words",
            model_name="medium", generated_at=dt.datetime.now()))
    monkeypatch.setattr("video2mdnotes.main.generate_summary",
                        lambda t: (_ for _ in ()).throw(RuntimeError("provider down")))

    CliRunner().invoke(app, ["media", str(source)])
    written = list((tmp_path / "out").glob("*/transcripts/*.md"))
    assert written and "real words" in written[0].read_text()
    assert not list((tmp_path / "out").glob("*/summaries/*.md"))


@ffmpeg_present
def test_probe_mode_transcribes_nothing(tmp_path, monkeypatch):
    """--probe must be free: no Whisper, no model."""
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    source = _encode(tmp_path / "a.mp3", codec="libmp3lame", rate=16000, channels=1)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")

    def _boom(*a, **k):
        raise AssertionError("--probe transcribed something")

    monkeypatch.setattr("video2mdnotes.main.transcribe_audio", _boom)
    monkeypatch.setattr("video2mdnotes.main.generate_summary", _boom)
    result = CliRunner().invoke(app, ["media", str(source), "--probe"])
    assert result.exit_code == 0
    assert not list((tmp_path / "out").rglob("*.md"))


@ffmpeg_present
def test_one_unreadable_file_does_not_stop_the_batch(tmp_path, monkeypatch):
    """The same lesson as the None playlist entry."""
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    _encode(tmp_path / "good.mp3", codec="libmp3lame", rate=16000, channels=1)
    (tmp_path / "bad.mp3").write_text("not media")
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")
    result = CliRunner().invoke(app, ["media", str(tmp_path), "--probe"])
    assert result.exit_code == 0
    assert "1 unreadable" in result.output


def test_typescript_declaration_files_are_not_treated_as_video(tmp_path):
    """`.mts` is both MPEG transport stream and a TypeScript extension.

    A real `data-U8FS-wNn.d.mts` in Downloads was picked up as video and
    reported as an unreadable recording — noise that trains you to ignore the
    failure list.
    """
    (tmp_path / "types.d.mts").write_text("export type X = string;")
    (tmp_path / "footage.mts").write_bytes(b"\x47" * 200)
    assert [p.name for p in find_media(tmp_path)] == ["footage.mts"]
