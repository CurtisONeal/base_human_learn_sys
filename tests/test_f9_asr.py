"""WP-F9: one windowed ASR path for every local recording and caption-less
download; unknown durations never become a whole-file decode; /Volumes refused.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from video2mdnotes.core import longasr
from video2mdnotes.core import media as media_mod
from video2mdnotes.core.media import MediaError, RemovableMediaError, probe, refuse_removable
from video2mdnotes.core.transcriber import Segment

ffmpeg_present = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
W, OV = 20.0, 4.0


def _tone(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-t", str(seconds), "-i", "sine=frequency=300:sample_rate=16000",
         "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True, timeout=60)
    return path


class GridBackend:
    """One 1.5 s segment every 2 s of each window (window-relative)."""
    name, model, supports_prompt = "fake", "m1", False

    def __init__(self):
        self.calls, self.last_language = [], "en"

    def available(self):
        return True, "ok"

    def load(self):
        pass

    def transcribe(self, wav_path, initial_prompt=None, language=None):
        from video2mdnotes.core.asr.base import wav_duration
        d = wav_duration(wav_path)
        self.calls.append(d)
        return [Segment(start=t, end=min(t + 1.5, d), text=f" s{len(self.calls)}_{t}")
                for t in range(0, int(d), 2)]


@pytest.fixture
def windowed(monkeypatch):
    from video2mdnotes.config import settings
    backend = GridBackend()
    monkeypatch.setattr(longasr, "select_backend", lambda **k: (backend, "test"))
    monkeypatch.setattr(settings, "asr_window_s", W)
    monkeypatch.setattr(settings, "asr_overlap_s", OV)
    return backend


# --- (c) unknown duration ------------------------------------------------------


@ffmpeg_present
def test_zero_probe_duration_is_measured_not_decoded_whole(tmp_path):
    src = _tone(tmp_path / "broken.wav", 50)
    info = probe(src)
    info.duration_seconds = 0.0                   # probe found no "Duration:"
    res = longasr.transcribe_long([src], GridBackend(), run_dir=tmp_path / "run",
                                  window_s=W, overlap_s=OV, infos=[info],
                                  progress=lambda m: None)
    assert res.gate["passed"], res.gate
    assert res.gate["windows_planned"] == 3       # windowed, bounded
    assert res.parts[0].duration_s == pytest.approx(50, abs=0.2)


def test_unmeasurable_duration_is_a_clear_error(tmp_path, monkeypatch):
    info = media_mod.MediaInfo(path=tmp_path / "x.mov", duration_seconds=0.0, codec="aac",
                               sample_rate=48000, channels="mono", has_video=True)
    monkeypatch.setattr(media_mod, "measure_duration", lambda p, **k: 0.0)
    with pytest.raises(MediaError, match="duration unknown"):
        longasr.ensure_durations([info], progress=lambda m: None)


@ffmpeg_present
def test_media_cli_zero_duration_goes_windowed(tmp_path, monkeypatch, windowed):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    src = _tone(tmp_path / "clip.wav", 50)
    real_probe = media_mod.probe

    def zero_probe(path):
        info = real_probe(path)
        info.duration_seconds = 0.0
        return info

    monkeypatch.setattr(media_mod, "probe", zero_probe)
    out = tmp_path / "run"
    result = CliRunner().invoke(app, ["media", str(src), "--transcribe-only", "--out", str(out)])
    assert result.exit_code == 0, result.output
    man = json.loads((out / longasr.MANIFEST_FILENAME).read_text())
    assert man["complete"] and len(man["windows"]) == 3
    assert max(windowed.calls) <= W + 2 * OV + 0.05
    assert not list(out.rglob("*.wav"))


@ffmpeg_present
def test_media_cli_short_clip_is_one_window(tmp_path, monkeypatch, windowed):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    src = _tone(tmp_path / "short.wav", 6)
    out = tmp_path / "run"
    result = CliRunner().invoke(app, ["media", str(src), "--transcribe-only", "--out", str(out)])
    assert result.exit_code == 0, result.output
    man = json.loads((out / longasr.MANIFEST_FILENAME).read_text())
    assert len(man["windows"]) == 1 and man["complete"]
    assert not hasattr(media_mod, "to_wav")


# --- (f) process: caption-less download --------------------------------------


@ffmpeg_present
def test_process_without_captions_uses_windowed_asr(tmp_path, monkeypatch, windowed):
    from unittest.mock import patch
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.core.downloader import SourceInfo
    from video2mdnotes.main import app

    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")
    audio = _tone(tmp_path / "download.m4a.wav", 50)
    seen = {}

    def fake_fetch(source, convert=True):
        seen["convert"] = convert
        return audio

    src = SourceInfo(title="Long Talk", url="https://example/v")
    with patch("video2mdnotes.main.probe_source", return_value=[src]), \
         patch("video2mdnotes.main.transcript_from_captions", return_value=None), \
         patch("video2mdnotes.main.fetch_audio", side_effect=fake_fetch), \
         patch("video2mdnotes.main.generate_summary",
               side_effect=RuntimeError("no model in tests")):
        CliRunner().invoke(app, ["process", "https://example/v"])
    assert seen["convert"] is False               # native audio, no whole-file wav
    manifests = list((tmp_path / "out").glob("*/" + longasr.MANIFEST_FILENAME))
    assert len(manifests) == 1
    man = json.loads(manifests[0].read_text())
    assert man["complete"] and len(man["windows"]) == 3
    assert list(manifests[0].parent.glob("asr/*.json"))   # checkpoints
    assert list((tmp_path / "out").glob("*/transcripts/*.md"))


# --- (h) removable media ------------------------------------------------------------


def test_refuse_removable_paths():
    with pytest.raises(RemovableMediaError, match="safety copy"):
        refuse_removable(Path("/Volumes/AKASO/DCIM/clip.MOV"))
    refuse_removable(Path.home() / "Movies" / "copy.MOV")      # no raise


def test_media_cli_refuses_volumes(tmp_path):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    result = CliRunner().invoke(app, ["media", "/Volumes/AKASO/DCIM", "--probe"])
    assert result.exit_code == 2
    assert "safety copy" in result.output


def test_process_cli_refuses_volumes_local_path():
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    result = CliRunner().invoke(app, ["process", "/Volumes/AKASO/notes.pdf"])
    assert result.exit_code == 2
    assert "safety copy" in result.output
