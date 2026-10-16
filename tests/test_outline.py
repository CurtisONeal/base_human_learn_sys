"""WP-F6: the `outline` structure rung (one model call, validated, cached).

The model is always mocked here; assertions are on the sections produced and
on the prompt the model would actually receive.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from video2mdnotes.core import longform as lf
from video2mdnotes.core import outline as ol
from video2mdnotes.core.metadata import SourceMeta
from video2mdnotes.core.sources import Chapter, Segment
from video2mdnotes.core.structure import (
    OutlineEntry, PREAMBLE_TITLE, media_sections, snap_outline,
)

E3_DIRS = [
    Path(__file__).resolve().parents[1] / "previous_run_results" / "E3_cle_ai_meetup",
    Path("/Users/s_admin/Documents/agent_set_up/repos/base_human_learn_sys/"
         "previous_run_results/E3_cle_ai_meetup"),
]

# A 60-minute meetup: host intro, two talks, Q&A. One caption line every 5 s.
ALICE_AT = 300      # "my name is Alice" line
BOB_AT = 1800       # line after "thank you" + applause
QA_AT = 3300        # "any questions"
DURATION = 3600


def _meetup_lines():
    lines = []
    for t in range(0, DURATION, 5):
        if t == ALICE_AT:
            text = "Hi everyone, my name is Alice Example and I build agents."
        elif t == BOB_AT - 10:
            text = "That's all I have, thank you."
        elif t == BOB_AT - 5:
            text = "[applause]"
        elif t == BOB_AT:
            text = "Okay so our second talk is about networking for AI clusters."
        elif t == QA_AT:
            text = "We have time for a few questions now from the audience."
        elif t < ALICE_AT:
            text = f"welcome line {t} about sponsors and the group tonight"
        elif t < BOB_AT:
            text = f"agent talk line {t} covering tools memory and evaluation"
        elif t < QA_AT:
            text = f"network talk line {t} covering fabrics optics and rails"
        else:
            text = f"question line {t} from the audience about the talks"
        lines.append((t, text))
    return lines


def _segments(lines):
    return [Segment(start=float(t), end=float(t) + 5.0, text=x) for t, x in lines]


def _meta(**kw):
    base = dict(source_id="meetup", title="Test meetup", url="/tmp/meetup.mov",
                duration_s=float(DURATION), chapter_source="none", chapters=[],
                captured_at=dt.datetime(2026, 10, 15, 12, 0))
    base.update(kw)
    return SourceMeta(**base)


GOOD_REPLY = json.dumps([
    {"start": "0:00:00", "title": "Welcome and sponsors", "speaker": None},
    {"start": "0:05:20", "title": "Building agents that ship", "speaker": "Alice Example"},
    {"start": "0:29:40", "title": "Networking for AI clusters", "speaker": None},
    {"start": "0:54:45", "title": "Audience Q&A", "speaker": None},
])


def assert_full_coverage(result, segments):
    spans = result.segment_spans
    assert len(spans) == len(result.sections)
    assert spans[0][0] == 0 and spans[-1][1] == len(segments)
    for (_, b), (c, _) in zip(spans, spans[1:]):
        assert b == c, "gap or overlap between sections"
    for (a, b), sec in zip(spans, result.sections):
        assert sec.text.split("\n") == [s.text.strip() for s in segments[a:b]]
    for s1, s2 in zip(result.sections, result.sections[1:]):
        assert s1.end_s == s2.start_s


# --- the rung itself (structure.py, no model) ---------------------------------------


def test_good_outline_sections_follow_it_snapped_to_transition_lines():
    segs = _segments(_meetup_lines())
    entries, reason = ol.parse_outline(GOOD_REPLY)
    assert reason == "" and len(entries) == 4
    res = media_sections(segs, _meta(), outline=entries, outline_source="claude-cli/sonnet")
    assert res.rung == "outline" and res.structure_is_inferred
    assert "claude-cli/sonnet" in res.provenance and "titles are the model's" in res.provenance
    titles = [s.path[0] for s in res.sections]
    assert titles == ["Welcome and sponsors", "Building agents that ship (Alice Example)",
                      "Networking for AI clusters", "Audience Q&A"]
    # Starts were 20 s, 20 s and 15 s off; each snapped to the transition line.
    assert [s.start_s for s in res.sections] == [0.0, ALICE_AT, BOB_AT, QA_AT]
    assert res.sections[-1].end_s == DURATION
    assert_full_coverage(res, segs)


def test_bad_outlines_fall_back_to_cues():
    segs = _segments(_meetup_lines())
    bad = {
        "non-monotonic": [OutlineEntry(start_s=0, title="A"), OutlineEntry(start_s=1800, title="B"),
                          OutlineEntry(start_s=900, title="C")],
        "past the end": [OutlineEntry(start_s=0, title="A"), OutlineEntry(start_s=4000, title="B")],
        "too few": [OutlineEntry(start_s=0, title="A")],
        "too many": [OutlineEntry(start_s=200 * k, title=f"T{k}") for k in range(16)],
        "empty title": [OutlineEntry(start_s=0, title="A"), OutlineEntry(start_s=900, title=" ")],
        "only short ones": [OutlineEntry(start_s=0, title="A"),
                            OutlineEntry(start_s=DURATION - 30, title="B")],
    }
    for name, entries in bad.items():
        res = media_sections(segs, _meta(), outline=entries)
        assert res.rung in ("cues", "windows"), name
        assert res.fallback_reason.startswith("outline rejected:"), name
        assert "outline rejected" in res.provenance, name
        assert_full_coverage(res, segs)


def test_malformed_reply_is_rejected_by_the_parser():
    for reply in ["Sure! Here are the sections.", "[{\"start\": \"0:00:00\"}]",
                  "[{\"start\": \"soon\", \"title\": \"x\"}]", "{\"start\": 1}"]:
        entries, reason = ol.parse_outline(reply)
        assert entries is None and reason


def test_no_caption_line_within_snap_window_rejects():
    lines = [(t, f"line {t}") for t in range(0, 600, 5)] + \
            [(t, f"line {t}") for t in range(1500, 3600, 5)]     # 15 min of silence
    segs = _segments(lines)
    entries = [OutlineEntry(start_s=0, title="A"), OutlineEntry(start_s=1000, title="B")]
    snap = snap_outline(segs, entries, 3600)
    assert snap.indices is None and "no caption line" in snap.reason


def test_sub_minute_section_is_merged_into_the_previous_not_rejected():
    # E3's real outline had a 34-s "announcement" between the welcome and
    # the first talk; one tiny section must not throw the outline away.
    segs = _segments(_meetup_lines())
    entries = [OutlineEntry(start_s=0, title="Welcome"),
               OutlineEntry(start_s=ALICE_AT, title="Agents", speaker="Alice Example"),
               OutlineEntry(start_s=ALICE_AT + 30, title="Announcement"),
               OutlineEntry(start_s=BOB_AT, title="Networks"),
               OutlineEntry(start_s=DURATION - 20, title="Bye")]
    res = media_sections(segs, _meta(), outline=entries)
    assert res.rung == "outline"
    assert [s.path[0] for s in res.sections] == ["Welcome", "Announcement", "Networks"]
    # "Announcement" (0:05:30) snapped onto the same opener line as "Agents",
    # leaving "Agents" empty: the later entry takes the line.
    assert res.sections[1].start_s == ALICE_AT
    assert "merged into the previous: 'Agents', 'Bye'" in res.provenance
    assert_full_coverage(res, segs)
    # A short FIRST section absorbs the next entry instead.
    first = [OutlineEntry(start_s=0, title="Hello"), OutlineEntry(start_s=40, title="Intro"),
             OutlineEntry(start_s=BOB_AT, title="Talk")]
    res = media_sections(segs, _meta(), outline=first)
    assert [s.path[0] for s in res.sections] == ["Hello", "Talk"]


def test_late_first_entry_keeps_a_preamble():
    segs = _segments(_meetup_lines())
    entries = [OutlineEntry(start_s=ALICE_AT, title="Agents", speaker="Alice Example"),
               OutlineEntry(start_s=BOB_AT, title="Networks")]
    res = media_sections(segs, _meta(), outline=entries)
    assert res.rung == "outline"
    assert res.sections[0].path == [PREAMBLE_TITLE] and res.sections[0].group_id == "ch00"
    assert_full_coverage(res, segs)


def test_oversized_outline_section_is_still_split():
    segs = _segments(_meetup_lines())
    entries, _ = ol.parse_outline(GOOD_REPLY)
    res = media_sections(segs, _meta(), outline=entries, max_words=1500)
    agent_parts = [s for s in res.sections if s.path[0].startswith("Building agents")]
    assert len(agent_parts) > 1
    assert all(s.word_count <= 1500 for s in agent_parts)
    assert agent_parts[0].path[1] == f"part 1/{len(agent_parts)}"
    assert_full_coverage(res, segs)


def test_chapters_still_win_in_auto_and_explicit_mode_skips_them():
    segs = _segments(_meetup_lines())
    meta = _meta(chapter_source="description",
                 chapters=[Chapter(start_s=0, end_s=1800, title="One"),
                           Chapter(start_s=1800, end_s=3600, title="Two")])
    entries, _ = ol.parse_outline(GOOD_REPLY)
    assert media_sections(segs, meta, outline=entries).rung == "description"
    assert media_sections(segs, meta, outline=entries, mode="outline").rung == "outline"
    assert media_sections(segs, meta, mode="windows").rung == "windows"
    assert not ol.needs_outline(segs, meta, "auto")
    assert ol.needs_outline(segs, _meta(), "auto")
    short = [s for s in segs if s.start < 20 * 60]
    assert not ol.needs_outline(short, _meta(duration_s=1200.0), "auto")


# --- the call + cache (outline.py, via longform.build_plan) -------------------------------


def _write_run(tmp_path, lines):
    run = tmp_path / "run"
    (run / "transcripts").mkdir(parents=True)
    out = ["---", 'title: "Test meetup"', "source: meetup.mov", "model: asr:test",
           "language: en", "generated_at: 2026-10-15", "---", "", "# Notes", ""]
    for t, text in lines:
        s = int(t)
        out.append(f"- [{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.000] {text}")
    (run / "transcripts" / "meetup.md").write_text("\n".join(out) + "\n")
    _meta().write(run)
    return run


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.reply, "mock/outline"


def test_cache_prevents_a_second_call(tmp_path):
    run = _write_run(tmp_path, _meetup_lines())
    rec = Recorder(GOOD_REPLY)
    plan = lf.build_plan(run, write=False, outline_complete=rec)
    assert len(rec.calls) == 1
    assert plan.structure.rung == "outline"
    assert plan.chapters[1][0].title == "Building agents that ship (Alice Example)"
    cache = json.loads((run / ol.CACHE_FILENAME).read_text())
    assert cache["model"] == "mock/outline" and cache["reply"] == GOOD_REPLY
    # Rerun with a model available: served from cache. Rerun cache-only: same.
    again = lf.build_plan(run, write=False, outline_complete=rec)
    assert len(rec.calls) == 1 and again.outline.cached
    assert [c[0].title for c in again.chapters] == [c[0].title for c in plan.chapters]
    cache_only = lf.build_plan(run, write=False)
    assert cache_only.structure.rung == "outline" and len(rec.calls) == 1
    # Only the cache file was written (write=False).
    assert sorted(p.name for p in run.iterdir()) == sorted(
        ["transcripts", "source_meta.json", ol.CACHE_FILENAME])


def test_bad_reply_is_cached_and_falls_back_to_cues(tmp_path):
    run = _write_run(tmp_path, _meetup_lines())
    rec = Recorder('[{"start": "0:30:00", "title": "B"}, {"start": "0:10:00", "title": "A"}]')
    plan = lf.build_plan(run, write=False, outline_complete=rec)
    assert plan.structure.rung in ("cues", "windows")
    assert "outline rejected: start times not increasing" in plan.structure.provenance
    lf.build_plan(run, write=False, outline_complete=rec)
    assert len(rec.calls) == 1


def test_no_model_given_means_no_call_and_plan_says_so(tmp_path):
    run = _write_run(tmp_path, _meetup_lines())
    plan = lf.build_plan(run, write=False)
    assert plan.structure.rung in ("cues", "windows")
    assert not (run / ol.CACHE_FILENAME).exists()
    text = lf.describe_plan(plan)
    assert "(+1 outline)" in text and "not cached" in text
    # Explicit cues: the outline rung is not tried at all.
    assert lf.build_plan(run, write=False, structure="cues").outline is None


def test_call_failure_falls_back(tmp_path):
    run = _write_run(tmp_path, _meetup_lines())

    def boom(system, user):
        raise RuntimeError("every configured provider failed")

    plan = lf.build_plan(run, write=False, outline_complete=boom)
    assert plan.structure.rung in ("cues", "windows")
    assert "outline call failed" in plan.structure.provenance
    assert not (run / ol.CACHE_FILENAME).exists()


def test_prompt_carries_transition_lines_and_samples():
    segs = _segments(_meetup_lines())
    system, user = ol.render_prompt(segs, _meta())
    assert "JSON array" in system
    assert "[0:05:00] Hi everyone, my name is Alice Example" in user
    assert "[0:29:55] [applause]" in user
    assert "[0:55:00] We have time for a few questions" in user
    # ~one sampled line per 30 s, not every 5 s line.
    body = user.split("Compressed transcript:\n", 1)[1].splitlines()
    assert DURATION / 30 <= len(body) <= DURATION / 30 + 10


def test_prompt_size_bounded_on_e3_transcript():
    run = next((d for d in E3_DIRS if (d / "transcripts").is_dir()), None)
    if run is None:
        pytest.skip("E3 run dir not available")
    raw = lf.raw_transcript_path(run)
    meta, _ = lf.load_meta(run, lf.notes_name(raw), raw)
    segs = lf.load_segments(raw)
    system, user = ol.render_prompt(segs, meta)
    full_chars = sum(len(s.text) + 12 for s in segs)
    assert len(system) + len(user) <= ol.MAX_DIGEST_CHARS + 3_000
    assert len(user) < full_chars / 3
    # The talk transitions a learner cares about survive the compression.
    assert "My name is Chris Halasi" in user
    assert "On to the next speaker." in user


def test_digest_cap_holds_on_a_very_long_recording():
    lines = [(t, f"line {t} " + "word " * 15 + ("thank you." if t % 300 == 0 else ""))
             for t in range(0, 12 * 3600, 4)]
    text = ol.compress_transcript(_segments(lines))
    assert len(text) <= ol.MAX_DIGEST_CHARS


def test_cache_survives_heuristic_tweaks_but_not_a_new_transcript(tmp_path, monkeypatch):
    run = _write_run(tmp_path, _meetup_lines())
    rec = Recorder(GOOD_REPLY)
    lf.build_plan(run, write=False, outline_complete=rec)
    # A different digest (prompt) for the same transcript: still cached.
    monkeypatch.setattr(ol, "SAMPLE_WORDS", 5)
    assert lf.build_plan(run, write=False, outline_complete=rec).outline.cached
    assert len(rec.calls) == 1
    # A changed transcript: the old outline no longer applies.
    lines = _meetup_lines()
    lines[3] = (lines[3][0], "a corrected caption line")
    run2 = _write_run(tmp_path / "b", lines)
    (run2 / ol.CACHE_FILENAME).write_text((run / ol.CACHE_FILENAME).read_text())
    lf.build_plan(run2, write=False, outline_complete=rec)
    assert len(rec.calls) == 2
