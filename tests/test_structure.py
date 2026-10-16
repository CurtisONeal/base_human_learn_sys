"""WP-2: structure ladder for media transcripts. Asserts on the sections produced."""

import json
from pathlib import Path

import pytest

from video2mdnotes.core.metadata import SourceMeta, from_ytdlp_info
from video2mdnotes.core.sources import Chapter, Section, Segment
from video2mdnotes.core.structure import (
    PREAMBLE_TITLE,
    course_skeleton,
    cue_strength,
    media_sections,
    pause_before,
    split_points,
)
from video2mdnotes.eval.longform import parse_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "metadata" / "cca_f_info.json"
REPO = Path(__file__).resolve().parents[1]
# The worktree has no previous_run_results; fall back to the main checkout.
_CANDIDATE_DIRS = [
    REPO / "previous_run_results",
    Path("/Users/s_admin/Documents/agent_set_up/repos/base_human_learn_sys/previous_run_results"),
]
CCA_RUN = "20260906_claude_certified_architect_foundations_prepare_for_and_pass_the_exam_captions"


def _cca_transcript():
    for base in _CANDIDATE_DIRS:
        hits = sorted((base / CCA_RUN / "transcripts").glob("*.md"))
        if hits:
            return hits[0]
    return None


def _segments_from_caption_md(path: Path):
    lines = parse_transcript(path.read_text(encoding="utf-8"))
    segs = []
    for a, b in zip(lines, lines[1:] + [None]):
        end = b.t if b is not None else a.t + 3.0
        segs.append(Segment(start=a.t, end=end, text=a.text))
    return segs


def assert_full_coverage(result, segments):
    """Every segment in exactly one section, in order, and texts match."""
    spans = result.segment_spans
    assert len(spans) == len(result.sections)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(segments)
    for (a, b), (c, d) in zip(spans, spans[1:]):
        assert b == c, "gap or overlap between sections"
    for (a, b), sec in zip(spans, result.sections):
        # Never mid-line: the section text is exactly its whole caption lines.
        assert (sec.text.split("\n") if sec.text else []) == [s.text.strip() for s in segments[a:b]]
        assert sec.word_count == len(sec.text.split())
        assert sec.locator and "–" in sec.locator
        assert sec.start_s is not None and sec.end_s is not None
        assert sec.start_s <= sec.end_s
    total = sum(len(s.text.split()) for s in segments)
    assert sum(s.word_count for s in result.sections) == total
    # Time ranges are contiguous.
    for x, y in zip(result.sections, result.sections[1:]):
        assert x.end_s == y.start_s


def _speech(start, minutes, words_per_line=8, line_s=3.0, first=None, cue_every=None):
    """Synthetic caption lines, `line_s` apart, with optional cue lines."""
    segs = []
    t = start
    n = int(minutes * 60 / line_s)
    for i in range(n):
        if i == 0 and first:
            text = first
        elif cue_every and i and i % cue_every == 0:
            text = "Okay, so " + " ".join(["word"] * (words_per_line - 2))
        else:
            text = " ".join(["word"] * words_per_line)
        segs.append(Segment(start=t, end=t + line_s, text=text))
        t += line_s
    return segs


# --- real data -------------------------------------------------------------------


@pytest.fixture(scope="module")
def cca():
    path = _cca_transcript()
    if path is None:
        pytest.skip("CCA-F caption transcript not present")
    segs = _segments_from_caption_md(path)
    meta = from_ytdlp_info(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return segs, meta, media_sections(segs, meta)


def test_cca_f_uses_description_rung(cca):
    segs, meta, result = cca
    assert result.rung == "description"
    assert result.structure_is_inferred is False
    groups = result.groups()
    titles = [g[0].path[0] for g in groups]
    assert titles[0] == PREAMBLE_TITLE
    assert titles[1:] == [c.title for c in meta.chapters]
    assert len(titles) == 14  # 13 chapters + preamble
    pre = groups[0][0]
    assert pre.locator == "0:00:00–0:00:47" and pre.word_count > 0


def test_cca_f_long_chapters_split_within_budget(cca):
    segs, meta, result = cca
    by_title = {g[0].path[0]: g for g in result.groups()}
    for title, start in [("Orchestration multi agent architecture", "1:52:53"),
                         ("Advanced Agent Patterns", "4:15:46")]:
        parts = by_title[title]
        assert len(parts) >= 2
        assert parts[0].locator.startswith(start)
        n = len(parts)
        for k, p in enumerate(parts, 1):
            assert p.path == [title, f"part {k}/{n}"]
            assert (p.part, p.parts) == (k, n)
            assert p.group_id == parts[0].group_id
        # Own, contiguous time ranges.
        for a, b in zip(parts, parts[1:]):
            assert a.end_s == b.start_s
    for sec in result.sections:
        assert sec.word_count <= 12_000
    assert len({s.group_id for s in result.sections}) == 14


def test_cca_f_full_coverage(cca):
    segs, meta, result = cca
    assert_full_coverage(result, segs)


def test_cca_f_skeleton(cca):
    segs, meta, result = cca
    sk = course_skeleton(result, meta)
    assert len(sk.split()) < 400
    assert "2026-07-20" in sk and "freeCodeCamp.org" in sk
    for c in meta.chapters:
        assert c.title in sk
    assert "Orchestration multi agent architecture [1:52:53–4:15:46] (2 parts)" in sk
    assert "description" in sk


def test_cca_f_cue_rung_finds_real_chapter_starts(cca):
    """Without metadata the cue rung should rediscover many real chapter starts."""
    segs, meta, _ = cca
    result = media_sections(segs, None)
    assert result.rung == "cues" and result.structure_is_inferred
    assert_full_coverage(result, segs)
    starts = [s.start_s for s in result.sections if s.part in (None, 1)]
    hits = sum(1 for c in meta.chapters[1:]
               if any(abs(c.start_s - t) <= 30 for t in starts))
    assert hits >= 6, hits


# --- ladder rungs (synthetic) -------------------------------------------------------


def _meta(chapters, source, duration=None):
    return SourceMeta(source_id="x", title="T", chapters=chapters,
                      chapter_source=source, duration_s=duration)


def test_platform_rung_and_preamble():
    segs = _speech(0, 30)
    meta = _meta([Chapter(start_s=60, end_s=600, title="A"),
                  Chapter(start_s=600, end_s=1800, title="B")], "platform", 1800)
    r = media_sections(segs, meta)
    assert r.rung == "platform" and not r.structure_is_inferred
    assert [s.path for s in r.sections] == [[PREAMBLE_TITLE], ["A"], ["B"]]
    assert [s.locator for s in r.sections] == [
        "0:00:00–0:01:00", "0:01:00–0:10:00", "0:10:00–0:30:00"]
    assert_full_coverage(r, segs)


def test_description_rung_no_preamble_when_chapter_at_zero():
    segs = _speech(0, 20)
    meta = _meta([Chapter(start_s=0, title="A"), Chapter(start_s=300, title="B"),
                  Chapter(start_s=900, title="C")], "description", 1200)
    r = media_sections(segs, meta)
    assert r.rung == "description"
    assert [s.title for s in r.sections] == ["A", "B", "C"]
    assert_full_coverage(r, segs)


def test_chapter_source_none_falls_through():
    segs = _speech(0, 20)
    meta = _meta([], "none", 1200)
    r = media_sections(segs, meta)
    assert r.rung in ("cues", "windows") and r.structure_is_inferred


def test_cue_rung():
    # 90 min; a cue line with a 4 s pause every 15 min.
    segs = []
    t = 0.0
    for block in range(6):
        for i in range(300):
            text = "word " * 8
            if i == 0 and block:
                text = "All right. So now the next topic " + "word " * 2
                t += 4.0
            segs.append(Segment(start=t, end=t + 1.0, text=text.strip()))
            t += 3.0
    r = media_sections(segs, None)
    assert r.rung == "cues" and r.structure_is_inferred
    assert len(r.sections) == 6
    for s in r.sections[1:]:
        assert s.text.startswith("All right. So")
    assert r.sections[0].path == ["Section 1"]
    assert_full_coverage(r, segs)


def test_windows_rung_when_no_cues():
    segs = _speech(0, 120)  # 2 h, no cues, no pauses
    r = media_sections(segs, None)
    assert r.rung == "windows" and r.structure_is_inferred
    assert r.sections[0].path == ["Window 1"]
    durations = [s.end_s - s.start_s for s in r.sections]
    assert all(20 * 60 <= d <= 40 * 60 for d in durations), durations
    assert_full_coverage(r, segs)


def test_windows_are_word_bounded():
    segs = _speech(0, 60, words_per_line=60)  # very dense speech: 72k words
    r = media_sections(segs, None, max_words=5000)
    assert all(s.word_count <= 5000 for s in r.sections)
    assert_full_coverage(r, segs)


def test_short_media_single_section():
    segs = _speech(0, 5)
    r = media_sections(segs, None)
    assert len(r.sections) == 1
    assert_full_coverage(r, segs)


# --- edge cases ------------------------------------------------------------------------


def test_empty_segments():
    r = media_sections([], None)
    assert r.sections == [] and r.rung == "empty"
    r = media_sections([], _meta([Chapter(start_s=0, title="A")], "platform"))
    assert r.sections == []


def test_one_huge_chapter_split_at_cue_lines():
    # 3 h of speech, 8 words/line = 28,800 words, cue every 200 lines.
    segs = _speech(0, 180, cue_every=200)
    meta = _meta([Chapter(start_s=0, title="Everything"),
                  Chapter(start_s=10799, title="Outro")], "platform", 10800)
    r = media_sections(segs, meta, max_words=10_000)
    parts = r.groups()[0]
    assert len(parts) == 3
    assert [p.path[1] for p in parts] == ["part 1/3", "part 2/3", "part 3/3"]
    for p in parts:
        assert p.word_count <= 10_000
    for p in parts[1:]:
        assert p.text.startswith("Okay, so"), p.text[:30]
    assert_full_coverage(r, segs)


def test_split_prefers_largest_pause_without_cues():
    segs = _speech(0, 60)  # 9,600 words
    # Plant one 8 s pause a bit off the midpoint.
    k = 640
    for s in segs[k:]:
        s.start += 8.0
        s.end += 8.0
    assert split_points(segs, 0, len(segs), 6000) == [k]


def test_single_oversized_line_is_not_cut():
    segs = [Segment(start=0, end=10, text="w " * 50),
            Segment(start=10, end=20, text="w " * 500),
            Segment(start=20, end=30, text="w " * 50)]
    meta = _meta([Chapter(start_s=0, title="A")], "platform", 30)
    r = media_sections(segs, meta, max_words=100)
    assert_full_coverage(r, segs)
    assert any(s.word_count == 500 for s in r.sections)


def test_segments_after_last_chapter_end_are_kept():
    segs = _speech(0, 10)
    meta = _meta([Chapter(start_s=0, end_s=60, title="A"),
                  Chapter(start_s=60, end_s=120, title="B")], "platform", 120)
    r = media_sections(segs, meta)
    assert_full_coverage(r, segs)
    assert r.sections[-1].end_s >= segs[-1].end


def test_chapter_with_no_speech_keeps_a_section():
    segs = _speech(0, 2) + _speech(600, 2)
    meta = _meta([Chapter(start_s=0, title="A"), Chapter(start_s=300, title="Silent"),
                  Chapter(start_s=600, title="C")], "platform", 720)
    r = media_sections(segs, meta)
    assert [s.title for s in r.sections] == ["A", "Silent", "C"]
    assert r.sections[1].word_count == 0
    assert_full_coverage(r, segs)


def test_section_new_fields_backward_compatible():
    s = Section(path=["a"], locator="ch. 1", text="x")
    assert s.start_s is None and s.group_id is None and s.part is None
    assert Section.model_validate(s.model_dump()) == s


def test_cue_and_pause_helpers():
    assert cue_strength("All right. So let's go") == 2.0
    assert cue_strength("um, okay, so now we") == 2.0
    assert cue_strength("Let's take a look at hooks") == 2.0
    assert cue_strength("that is done. Okay, so next") == 1.0
    assert cue_strength("and the cloud provider") == 0.0
    segs = [Segment(start=0, end=2, text="a b"), Segment(start=5, end=6, text="c")]
    assert pause_before(segs, 0) == 0.0
    assert pause_before(segs, 1) == pytest.approx(5 - 0.8)


def test_skeleton_marks_inferred_structure():
    segs = _speech(0, 120)
    r = media_sections(segs, None)
    sk = course_skeleton(r, None)
    assert "INFERRED" in sk
    assert sk.count("Window ") == len(r.sections)


def test_cca_f_split_points_land_on_cue_or_sentence_start(cca):
    segs, meta, result = cca
    for sec, (a, b) in zip(result.sections, result.segment_spans):
        if sec.part and sec.part > 1:
            prev = segs[a - 1].text.rstrip()
            assert cue_strength(segs[a].text) >= 1.0 or prev.endswith((".", "!", "?")), (
                sec.path, prev, segs[a].text)
