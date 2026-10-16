"""WP-1: metadata capture. Fixture is a trimmed copy of the real CCA-F yt_info.json."""

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from video2mdnotes.core.media import MediaInfo
from video2mdnotes.core.metadata import (
    META_FILENAME,
    SourceMeta,
    from_local_media,
    from_ytdlp_info,
    parse_description_chapters,
    prompt_header,
)
from video2mdnotes.core.sources import SourceText

FIXTURE = Path(__file__).parent / "fixtures" / "metadata" / "cca_f_info.json"


@pytest.fixture
def cca_info():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_cca_f_publication_facts(cca_info):
    meta = from_ytdlp_info(cca_info)
    assert meta.source_id == "reDRM0tqhNs"
    assert meta.published_at == dt.date(2026, 7, 20)
    assert meta.published_by == "freeCodeCamp.org"
    assert meta.channel == "freeCodeCamp.org"
    assert meta.duration_s == 45516
    assert meta.live_status == "not_live"
    assert meta.url == "https://www.youtube.com/watch?v=reDRM0tqhNs"


def test_cca_f_chapters_from_description(cca_info):
    assert not cca_info["chapters"]  # the real info dict had none from the platform
    meta = from_ytdlp_info(cca_info)
    assert meta.chapter_source == "description"
    assert len(meta.chapters) == 13
    first, last = meta.chapters[0], meta.chapters[-1]
    assert first.start_s == 47 and first.title == "Introduction"
    assert first.end_s == 10 * 60 + 17
    assert last.title == "Scenario Based Architect Patterns"
    assert last.start_s == 12 * 3600 + 14 * 60 + 57
    assert last.end_s == 45516
    # Contiguous: every end is the next start.
    for a, b in zip(meta.chapters, meta.chapters[1:]):
        assert a.end_s == b.start_s
    # Sponsor lines ("👾 @handle") must not leak in as chapters.
    assert not any("@" in c.title for c in meta.chapters)


def test_platform_chapters_preferred(cca_info):
    cca_info["chapters"] = [
        {"start_time": 0.0, "end_time": 100.0, "title": "A"},
        {"start_time": 100.0, "end_time": 200.0, "title": "B"},
    ]
    meta = from_ytdlp_info(cca_info)
    assert meta.chapter_source == "platform"
    assert [c.title for c in meta.chapters] == ["A", "B"]
    assert meta.chapters[1].end_s == 200.0


def test_non_monotonic_rejected():
    desc = "0:00 Intro\n5:00 Middle\n3:00 Back again\n9:00 End"
    assert parse_description_chapters(desc, 1000) == []


def test_fewer_than_three_rejected():
    assert parse_description_chapters("0:00 Intro\n5:00 End", 1000) == []


def test_beyond_duration_rejected():
    desc = "0:00 A\n5:00 B\n20:00 C"
    assert parse_description_chapters(desc, 600) == []


def test_bullet_and_emoji_prefixes_accepted():
    desc = "• 0:00 Alpha\n* 01:30 Beta\n🎯 1:02:03 Gamma\n- 1:10:00 – Delta"
    chapters = parse_description_chapters(desc, 5000)
    assert [c.title for c in chapters] == ["Alpha", "Beta", "Gamma", "Delta"]
    assert [c.start_s for c in chapters] == [0, 90, 3723, 4200]
    assert chapters[-1].end_s == 5000


def test_no_chapters_gives_none(cca_info):
    cca_info["description"] = "no timestamps here"
    meta = from_ytdlp_info(cca_info)
    assert meta.chapter_source == "none" and meta.chapters == []


def test_write_read_roundtrip(tmp_path, cca_info):
    meta = from_ytdlp_info(cca_info)
    path = meta.write(tmp_path)
    assert path == tmp_path / META_FILENAME
    raw = json.loads(path.read_text())
    assert raw["published_at"] == "2026-07-20"
    assert len(raw["chapters"]) == 13
    assert SourceMeta.read(tmp_path) == meta
    assert SourceMeta.read(tmp_path / "missing") is None


def test_prompt_header_known(cca_info):
    meta = from_ytdlp_info(cca_info)
    meta.captured_at = dt.datetime(2026, 9, 6, 10, 0)
    header = prompt_header(meta, transcript_source="captions (manual, en)")
    assert header == (
        "Published: 2026-07-20 by freeCodeCamp.org · Captured: 2026-09-06 · "
        "Duration: 12:38:36 · Transcript source: captions (manual, en)"
    )


def test_prompt_header_unknowns():
    meta = SourceMeta(source_id="x", captured_at=dt.datetime(2026, 10, 1))
    header = prompt_header(meta)
    assert "Published: unknown by unknown" in header
    assert "Duration: unknown" in header
    assert "Transcript source: unknown" in header


def test_local_media_meta(tmp_path):
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"")
    info = MediaInfo(path=f, duration_seconds=125.4, codec="aac", sample_rate=16000,
                     channels="mono", has_video=False,
                     creation_time="2024-03-05T14:00:00.000000Z")
    meta = from_local_media(info)
    assert meta.published_at is None
    assert meta.duration_s == 125.4
    assert meta.recorded_at.date() == dt.date(2024, 3, 5)
    assert "device" in meta.recorded_at_note
    header = prompt_header(meta)
    assert "Published: unknown" in header
    assert "Recorded (device clock, unverified): 2024-03-05" in header


def test_media_probe_reads_creation_time(tmp_path):
    from video2mdnotes.core import media
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"")
    stderr = (
        "Input #0, mov,mp4, from 'clip.mp4':\n"
        "  Metadata:\n"
        "    creation_time   : 2023-11-02T08:15:00.000000Z\n"
        "  Duration: 00:01:05.00, start: 0.0, bitrate: 128 kb/s\n"
        "  Stream #0:0[0x1](und): Audio: aac (LC), 48000 Hz, stereo, fltp\n"
    )
    with patch.object(media, "_ffmpeg", return_value="ffmpeg"), \
         patch.object(media.subprocess, "run", return_value=MagicMock(stderr=stderr)):
        info = media.probe(f)
    assert info.creation_time == "2023-11-02T08:15:00.000000Z"
    assert info.duration_seconds == 65


def test_source_text_backward_compatible_and_accepts_meta(cca_info):
    st = SourceText(source_id="a", title="t", kind="media", origin="u")
    assert st.published_at is None and st.chapters == [] and st.duration_s is None
    meta = from_ytdlp_info(cca_info)
    st2 = st.model_copy(update=meta.source_text_fields())
    assert st2.published_at == dt.date(2026, 7, 20)
    assert len(st2.chapters) == 13
    SourceText.model_validate(st2.model_dump())


def test_probe_source_attaches_meta(cca_info):
    from video2mdnotes.core import downloader
    ydl = MagicMock()
    ydl.__enter__.return_value.extract_info.return_value = cca_info
    with patch.object(downloader.yt_dlp, "YoutubeDL", return_value=ydl):
        [source] = downloader.probe_source(cca_info["webpage_url"])
    assert source.meta is not None
    assert source.meta.published_at == dt.date(2026, 7, 20)
    assert source.meta.chapter_source == "description"
