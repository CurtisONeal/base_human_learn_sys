"""Long-recording ASR (WP-6b): windows, overlap dedupe, multi-part stitching,
resume, and the completeness gate.

Real ffmpeg on synthetic tone/silence files; the ASR engine is a fake that
emits segments on an ABSOLUTE 2-second grid, so a correctly stitched timeline
contains every grid point exactly once. Only the `integration` test at the
bottom runs a real model.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from video2mdnotes.core import longasr
from video2mdnotes.core import media as media_mod
from video2mdnotes.core.asr.base import wav_duration
from video2mdnotes.core.media import MediaInfo, probe
from video2mdnotes.core.transcriber import Segment

ffmpeg_present = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

W, OV = 20.0, 4.0


def _tone(path: Path, seconds: float, silent: bool = False) -> Path:
    src = "anullsrc=r=16000:cl=mono" if silent else "sine=frequency=300:sample_rate=16000"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-t", str(seconds), "-i", src, "-c:a", "pcm_s16le", str(path)],
        check=True, capture_output=True, timeout=60)
    return path


class FakeBackend:
    """Emits 1.5 s segments at every even ABSOLUTE second inside the window.

    `starts` = absolute start of each window, in call order. A segment that
    would run past the window's audio is cut at the edge (as a real engine's
    would be) so the overlap holds a truncated and a full copy.
    """

    name = "fake"
    model = "m1"
    supports_prompt = True

    def __init__(self, starts=None, empty=(), fail=(), lang="en"):
        self.starts = list(starts or [])
        self.empty, self.fail = set(empty), set(fail)
        self.calls = []
        self.last_language = None
        self.lang = lang
        self.loads = 0

    def available(self):
        return True, "ok"

    def load(self):
        self.loads += 1

    def transcribe(self, wav_path, initial_prompt=None, language=None):
        i = len(self.calls)
        d = wav_duration(wav_path)
        self.calls.append((Path(wav_path), d))
        self.last_language = self.lang
        if i in self.fail:
            raise RuntimeError("engine crashed")
        if i in self.empty or not self.starts:
            return []
        start = self.starts[i]
        out = []
        t = math.ceil(start / 2) * 2
        while t < start + d:
            rel = t - start
            out.append(Segment(start=rel, end=min(rel + 1.5, d), text=f" t{t:g}"))
            t += 2
        return out


def _abs_starts(durations):
    starts, offset = [], 0.0
    for i, d in enumerate(durations):
        starts += [offset + w.ext_start for w in longasr.plan_windows(d, W, OV, i)]
        offset += d
    return starts


@pytest.fixture
def two_files(tmp_path):
    return [_tone(tmp_path / "A_001.wav", 50), _tone(tmp_path / "A_002.wav", 30)]


def _run(sources, backend, run_dir, **kw):
    kw.setdefault("window_s", W)
    kw.setdefault("overlap_s", OV)
    kw.setdefault("progress", lambda m: None)
    return longasr.transcribe_long(sources, backend, run_dir=run_dir, **kw)


# --- window math -------------------------------------------------------------


def test_plan_windows_cover_file_and_bound_length():
    ws = longasr.plan_windows(50, W, OV)
    assert [(w.ext_start, w.ext_end, w.core_start, w.core_end) for w in ws] == [
        (0, 24, 0, 20), (16, 44, 20, 40), (36, 50, 40, 50)]
    # cores tile the file exactly
    assert ws[0].core_start == 0 and ws[-1].core_end == 50
    assert all(a.core_end == b.core_start for a, b in zip(ws, ws[1:]))
    assert all(w.expected_s <= W + 2 * OV for w in ws)
    # symmetric overlap: every inner seam is heard OV before and after
    assert all(b.ext_start == a.core_end - OV and a.ext_end == a.core_end + OV
               for a, b in zip(ws, ws[1:]))


def test_plan_windows_tail_shorter_than_overlap_is_absorbed():
    ws = longasr.plan_windows(23, W, OV)
    assert len(ws) == 1 and ws[0].ext_end == 23
    assert longasr.plan_windows(0, W, OV) == []
    assert len(longasr.plan_windows(7200, 1500, 8)) == 5


def test_plan_windows_rejects_bad_overlap():
    with pytest.raises(ValueError):
        longasr.plan_windows(100, 10, 10)


# --- overlap dedupe ------------------------------------------------------------


def test_dedupe_keeps_copy_further_from_edge():
    # prev window ends at 24, next starts at 20. Sentence A [19,23] is whole
    # in prev and cut at next's start edge; sentence B [22.5,26] is cut at
    # prev's end edge (22.5-24) and whole in next.
    prev = [Segment(start=10, end=15, text="early"),
            Segment(start=19, end=23, text="A full"),
            Segment(start=22.5, end=24, text="B cut")]
    nxt = [Segment(start=20, end=23, text="A cut"),
           Segment(start=22.5, end=26, text="B full"),
           Segment(start=30, end=31, text="later")]
    p, n = longasr.dedupe_overlap(prev, 24, nxt, 20)
    assert [s.text for s in p] == ["early", "A full"]
    assert [s.text for s in n] == ["B full", "later"]


def test_dedupe_both_clipped_prefers_next_window():
    # Real E3 seam at 1500 s (windows 0-1508 / 1500-...): both copies touch
    # an edge. Keeping prev's duplicated "you need to issue session limits".
    prev = [Segment(start=1497.84, end=1499.52, text="That's not what we want."),
            Segment(start=1499.52, end=1508.0,
                    text="So this scenario ... in QA, and you need to issue session limits.")]
    nxt = [Segment(start=1500.0, end=1505.6, text="So this scenario ... in QA."),
           Segment(start=1505.6, end=1512.24, text="And you need to issue session limits and ...")]
    p, n = longasr.dedupe_overlap(prev, 1508.0, nxt, 1500.0)
    joined = " ".join(s.text for s in p + n)
    assert joined.count("issue session limits") == 1
    assert joined.count("So this scenario") == 1
    assert [s.text for s in p] == ["That's not what we want."]


def test_dedupe_never_drops_unmatched_speech():
    prev = [Segment(start=21, end=22, text="only in prev")]
    nxt = [Segment(start=22.5, end=23.5, text="only in next")]
    p, n = longasr.dedupe_overlap(prev, 24, nxt, 20)
    assert len(p) == 1 and len(n) == 1


def _worded(start, end, words):
    """A segment with evenly spaced word timings."""
    from video2mdnotes.core.transcriber import Word
    step = (end - start) / len(words)
    ws = [Word(start=start + k * step, end=start + (k + 1) * step, text=f" {w}")
          for k, w in enumerate(words)]
    return Segment(start=start, end=end, text="".join(w.text for w in ws), words=ws)


def test_seam_sentence_cut_by_both_edges_loses_nothing():
    # F9 review (a): seam at 1500, symmetric overlap 8 s -> prev extracted to
    # 1508, next from 1492. One 30 s sentence 1485-1515 crosses BOTH edges:
    # prev holds 1485-1508, next 1492-1515. Old code dropped prev's copy and
    # with it 1485-1492, which only prev heard.
    words = [f"w{k}" for k in range(30)]               # one word per second
    full = _worded(1485, 1515, words)
    prev = [_worded(1485, 1508, words[:23])]
    nxt = [_worded(1492, 1515, words[7:])]
    p, n = longasr.dedupe_overlap(prev, 1508.0, nxt, 1492.0, seam=1500.0)
    got = " ".join(s.text for s in p + n).split()
    assert got == full.text.split()                    # every word, once, in order
    assert p and p[0].end <= 1500.0 + 1e-6 and n and n[0].start >= 1500.0 - 1e-6


def test_seam_both_clipped_without_word_timings_keeps_both():
    # No timings to trim with: duplicate rather than lose 1485-1492.
    prev = [Segment(start=1485, end=1508, text="early words shared words")]
    nxt = [Segment(start=1492, end=1515, text="shared words late words")]
    p, n = longasr.dedupe_overlap(prev, 1508.0, nxt, 1492.0, seam=1500.0)
    assert len(p) == 1 and len(n) == 1


def test_seam_unclipped_winner_never_hides_speech_only_loser_heard():
    # prev's copy is whole (19-22.9) but next's clipped copy runs on to 27:
    # dropping next's copy would lose 22.9-27, which prev never heard.
    prev = [Segment(start=19, end=22.9, text=" a b c d")]
    nxt = [_worded(20, 27, ["b", "c", "d", "e", "f", "g", "h"])]
    p, n = longasr.dedupe_overlap(prev, 24.0, nxt, 20.0, seam=22.0)
    assert [s.text for s in p] == [" a b c d"]
    assert [s.text.split() for s in n] == [["e", "f", "g", "h"]]


@ffmpeg_present
def test_window_extraction_starts_before_core(two_files, tmp_path, monkeypatch):
    starts = []
    real = media_mod.extract_window

    def spy(path, dest, start, dur):
        starts.append(start)
        return real(path, dest, start, dur)

    monkeypatch.setattr(media_mod, "extract_window", spy)
    _run(two_files, FakeBackend(_abs_starts([50, 30])), tmp_path / "run")
    # file A (50 s): cores at 0, 20, 40 -> extracted from 0, 16, 36
    assert starts[:3] == [0.0, 16.0, 36.0]


# --- end-to-end on real ffmpeg windows -----------------------------------------


@ffmpeg_present
def test_two_file_stitch_offsets_and_dedupe(two_files, tmp_path):
    backend = FakeBackend(_abs_starts([50, 30]))
    res = _run(two_files, backend, tmp_path / "run")
    texts = [s.text.strip() for s in res.segments]
    expected = [f"t{t}" for t in range(0, 80, 2)]
    assert texts == expected                      # every grid point once, in order
    for s in res.segments:                        # each at its absolute time
        assert s.start == pytest.approx(float(s.text.strip()[1:]), abs=1e-3)
    assert [p.offset_s for p in res.parts] == [0.0, pytest.approx(50, abs=0.1)]
    assert res.gate["passed"], res.gate
    man = json.loads((tmp_path / "run" / longasr.MANIFEST_FILENAME).read_text())
    assert man["complete"] is True
    assert [p["name"] for p in man["parts"]] == ["A_001.wav", "A_002.wav"]
    assert man["parts"][1]["offset_s"] == pytest.approx(50, abs=0.1)
    assert len(man["windows"]) == 5
    assert sorted(p.name for p in (tmp_path / "run" / "asr").glob("*.json")) == [
        "00_000.json", "00_001.json", "00_002.json", "01_000.json", "01_001.json"]
    md = res.result.markdown_content
    assert "model: asr:fake/m1" in md and "language: en" in md and "generated_at:" in md
    assert "- [0:00:50.000] t50" in md


@ffmpeg_present
def test_no_temp_wav_exceeds_window_and_none_remain(two_files, tmp_path, monkeypatch):
    seen = []
    real = media_mod.extract_window

    def spy(path, dest, start, dur):
        out = real(path, dest, start, dur)
        seen.append(wav_duration(out))
        return out

    monkeypatch.setattr(media_mod, "extract_window", spy)
    backend = FakeBackend(_abs_starts([50, 30]))
    _run(two_files, backend, tmp_path / "run")
    assert len(seen) == 5
    assert max(seen) <= W + 2 * OV + 0.05
    assert not list((tmp_path / "run").rglob("*.wav"))
    # the engine only ever saw bounded windows too
    assert max(d for _, d in backend.calls) <= W + 2 * OV + 0.05


@ffmpeg_present
def test_resume_skips_completed_windows(two_files, tmp_path):
    run = tmp_path / "run"
    first = _run(two_files, FakeBackend(_abs_starts([50, 30])), run)
    again = FakeBackend(fail=range(100))          # would fail if called at all
    second = _run(two_files, again, run)
    assert again.calls == [] and second.windows_skipped == 5
    assert [s.text for s in second.segments] == [s.text for s in first.segments]

    (run / "asr" / "00_001.json").unlink()
    starts = _abs_starts([50, 30])
    third_backend = FakeBackend([starts[1]])
    third = _run(two_files, third_backend, run)
    assert len(third_backend.calls) == 1 and third.windows_run == 1
    assert [s.text for s in third.segments] == [s.text for s in first.segments]


@ffmpeg_present
def test_checkpoint_from_other_engine_is_redone(two_files, tmp_path):
    run = tmp_path / "run"
    _run(two_files, FakeBackend(_abs_starts([50, 30])), run)
    other = FakeBackend(_abs_starts([50, 30]))
    other.model = "m2"
    res = _run(two_files, other, run)
    assert len(other.calls) == 5 and res.label == "asr:fake/m2"


@ffmpeg_present
def test_gate_fails_on_missing_window(two_files, tmp_path):
    backend = FakeBackend(_abs_starts([50, 30]), fail={1})
    with pytest.raises(longasr.AsrIncomplete) as err:
        _run(two_files, backend, tmp_path / "run")
    gate = err.value.gate
    assert gate["missing_windows"] == ["00_001"]
    assert any("without checkpoint" in f for f in gate["failures"])
    man = json.loads((tmp_path / "run" / longasr.MANIFEST_FILENAME).read_text())
    assert man["complete"] is False
    assert not (tmp_path / "run" / "asr" / "tmp").exists() or not list(
        (tmp_path / "run" / "asr" / "tmp").iterdir())


@ffmpeg_present
def test_gate_fails_on_truncated_audio(tmp_path):
    real = _tone(tmp_path / "short.wav", 30)
    info = probe(real)
    info.duration_seconds = 60.0                  # container claims 60 s; audio is 30 s
    backend = FakeBackend(_abs_starts([60]))
    with pytest.raises(longasr.AsrIncomplete) as err:
        _run([real], backend, tmp_path / "run", infos=[info])
    gate = err.value.gate
    assert gate["audio_coverage"] < 0.98
    assert gate["truncated_windows"] or gate["missing_windows"]
    assert not gate["passed"]


@ffmpeg_present
def test_gate_fails_on_unexplained_gap_but_excuses_silence(tmp_path):
    tone = _tone(tmp_path / "B_001.wav", 50)
    # A non-silent window that produced no text is a hole, not silence.
    with pytest.raises(longasr.AsrIncomplete) as err:
        _run([tone], FakeBackend(_abs_starts([50]), empty={1}), tmp_path / "r1", max_gap_s=10)
    assert any("non-silent" in f for f in err.value.gate["failures"])
    assert err.value.gate["silent_windows"] == []

    # A silent second part with no text is recorded SILENT and passes.
    quiet = _tone(tmp_path / "B_002.wav", 30, silent=True)
    starts = _abs_starts([50, 30])
    res = _run([tone, quiet], FakeBackend(starts, empty={3, 4}), tmp_path / "r2", max_gap_s=10)
    assert res.gate["passed"], res.gate
    assert res.gate["silent_windows"] == ["01_000", "01_001"]
    assert res.gate["silence_excused_gaps"]


# --- grouping & backend choice -------------------------------------------------


def _info(name, ct, dur, vcodec="h264", w=3840, h=2160, codec="aac", rate=48000):
    return MediaInfo(path=Path(name), duration_seconds=dur, codec=codec, sample_rate=rate,
                     channels="stereo", has_video=True, creation_time=ct,
                     video_codec=vcodec, width=w, height=h)


def test_group_parts_joins_camera_split_only():
    infos = [
        _info("20240101_200143.MOV", "2024-01-01T12:01:43Z", 4),
        _info("20240101_200251.MOV", "2024-01-01T12:02:51Z", 30),
        _info("20240101_205924.MOV", "2024-01-01T12:59:26Z", 1811.5),
        _info("20240101_212938.MOV", "2024-01-01T13:29:40Z", 1704.5),
        _info("20240101_215805.MOV", "2024-01-01T13:58:07Z", 1941.0),
        _info("20240101_223029.MOV", "2024-01-01T14:30:30Z", 1689.0),
    ]
    groups = longasr.group_parts(list(reversed(infos)))
    assert [len(g) for g in groups] == [1, 1, 4]
    assert [i.path.name for i in groups[2]] == [
        "20240101_205924.MOV", "20240101_212938.MOV", "20240101_215805.MOV", "20240101_223029.MOV"]


def test_group_parts_requires_same_shape_and_clock():
    a = _info("a.MOV", "2024-01-01T12:00:00Z", 100)
    assert not longasr.continues(a, _info("b.MOV", "2024-01-01T12:01:40Z", 5, w=1920, h=1080))
    assert not longasr.continues(a, _info("b.MOV", "2024-01-01T12:02:00Z", 5))   # 20 s late
    assert not longasr.continues(a, _info("b.MOV", None, 5))
    assert longasr.continues(a, _info("b.MOV", "2024-01-01T12:01:49Z", 5))


def test_select_backend_policy(monkeypatch):
    from video2mdnotes.core.asr.parakeet_mlx import ParakeetMlxBackend

    monkeypatch.setattr(ParakeetMlxBackend, "available", lambda self: (True, "ok"))
    b, why = longasr.select_backend("CLE AI meetup", "en", spec="auto")
    assert b.name == "parakeet-mlx"
    # non-English context always gets multilingual large-v3, even over a pin
    b, why = longasr.select_backend("Aikido shomenuchi ikkyo", "en", spec="parakeet-mlx")
    assert (b.name, b.model) == ("faster-whisper", "large-v3")
    b, _ = longasr.select_backend("talk", "ja", spec="auto")
    assert b.model == "large-v3"
    b, _ = longasr.select_backend("talk", "en", spec="faster-whisper:distil-large-v3")
    assert b.model == "distil-large-v3"
    monkeypatch.setattr(ParakeetMlxBackend, "available", lambda self: (False, "no mlx"))
    b, why = longasr.select_backend("talk", None, spec="auto")
    assert (b.name, b.model) == ("faster-whisper", "distil-large-v3") and "unavailable" in why


# --- CLI wiring ------------------------------------------------------------------


@ffmpeg_present
def test_media_group_transcribe_only(two_files, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    backend = FakeBackend(_abs_starts([50, 30]))
    monkeypatch.setattr(longasr, "select_backend", lambda **k: (backend, "test"))
    monkeypatch.setattr(settings, "asr_window_s", W)
    monkeypatch.setattr(settings, "asr_overlap_s", OV)

    def boom(*a, **k):
        raise AssertionError("must not be called")

    assert not hasattr(media_mod, "to_wav")                          # never whole-file
    monkeypatch.setattr("video2mdnotes.main.generate_summary", boom)  # no LLM
    out = tmp_path / "E3"
    result = CliRunner().invoke(app, ["media", *map(str, two_files), "--group",
                                      "--transcribe-only", "--out", str(out)])
    assert result.exit_code == 0, result.output
    transcripts = list((out / "transcripts").glob("*.md"))
    assert len(transcripts) == 1
    text = transcripts[0].read_text()
    assert "model: asr:fake/m1" in text and "- [0:01:18.000] t78" in text
    meta = json.loads((out / "source_meta.json").read_text())
    assert meta["duration_s"] == pytest.approx(80, abs=0.2)
    assert meta["published_at"] is None
    assert [p["offset_s"] for p in meta["parts"]] == [0.0, pytest.approx(50, abs=0.1)]
    assert (out / longasr.MANIFEST_FILENAME).exists()
    assert not (out / "summaries").exists()
    assert not list(out.rglob("*.wav"))


@ffmpeg_present
def test_media_incomplete_writes_no_transcript(two_files, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    backend = FakeBackend(_abs_starts([50, 30]), fail={2})
    monkeypatch.setattr(longasr, "select_backend", lambda **k: (backend, "test"))
    monkeypatch.setattr(settings, "asr_window_s", W)
    monkeypatch.setattr(settings, "asr_overlap_s", OV)
    out = tmp_path / "E3"
    result = CliRunner().invoke(app, ["media", *map(str, two_files), "--group",
                                      "--transcribe-only", "--out", str(out)])
    assert "Incomplete" in result.output
    assert not (out / "transcripts").exists()
    assert json.loads((out / longasr.MANIFEST_FILENAME).read_text())["complete"] is False


# --- real engine -------------------------------------------------------------------

CLIP = Path.home() / "Movies" / "CLE-AI-26_09_safety_copies" / "20240101_200403.MOV"


@pytest.mark.integration
@pytest.mark.skipif(not CLIP.exists(), reason="safety-copy clip not present")
def test_parakeet_on_12s_clip(tmp_path):
    from video2mdnotes.core.asr import get_backend

    backend = get_backend("parakeet-mlx")
    ok, why = backend.available()
    if not ok:
        pytest.skip(why)
    res = longasr.transcribe_long([CLIP], backend, run_dir=tmp_path / "run",
                                  window_s=6, overlap_s=2, progress=lambda m: None)
    assert res.gate["passed"], res.gate
    assert res.gate["windows_planned"] == 2
    assert all(0 <= s.start <= s.end <= 12.5 for s in res.segments)
    assert not list(tmp_path.rglob("*.wav"))
    assert res.label.startswith("asr:parakeet-mlx/")
