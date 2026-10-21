"""WP-0 eval harness: assert on report *content*, not just exit codes.

Fixtures (tests/fixtures/eval_longform/):
  pass/  — satisfies P1-P7.
  fail/  — fails every one of P1-P7, each for one planted, known reason.
"""

import json
from pathlib import Path

import pytest

from video2mdnotes.eval import longform as lf

FIX = Path(__file__).parent / "fixtures" / "eval_longform"
PASS, FAIL = FIX / "pass", FIX / "fail"
CCA_DIR = Path(
    "/Users/s_admin/Documents/agent_set_up/repos/base_human_learn_sys/"
    "previous_run_results/20260906_claude_certified_architect_foundations_"
    "prepare_for_and_pass_the_exam_captions"
)


@pytest.fixture(scope="module")
def passing():
    return lf.evaluate(PASS)


@pytest.fixture(scope="module")
def failing():
    return lf.evaluate(FAIL)


# --- passing fixture ---------------------------------------------------------


def test_pass_fixture_passes_every_deterministic_criterion(passing):
    report, _ = passing
    assert report["overall_pass"] is True
    assert report["partial"] is False
    for key in lf.CRITERIA:
        assert report["criteria"][key]["pass"] is True, (key, report["criteria"][key])


def test_pass_fixture_p8_is_pending_not_passed(passing):
    report, _ = passing
    p8 = report["criteria"]["P8"]
    assert p8["pass"] is None
    assert p8["status"] == "pending reviewer"


def test_pass_fixture_counts_every_citation_format(passing):
    report, _ = passing
    p2 = report["criteria"]["P2"]["details"]
    # 10 bullet cites (incl. ranges, [MM:SS], a link-text cite, a cite on a
    # bullet continuation line) + 1 paragraph + 2 headings + 2 table cells.
    assert p2["cited"] == 15
    assert p2["unresolved"] == []


def test_pass_fixture_legit_cloud_phrases_not_flagged(passing):
    report, _ = passing
    # Notes contain "cloud provider", "cloud computing", lowercase "claude code".
    text = (PASS / "summaries" / "fixture_course.summary.md").read_text()
    assert "cloud provider" in text and "cloud computing" in text
    assert "claude code" in text
    assert report["criteria"]["P6"]["details"]["hits"] == []


def test_pass_fixture_p3_and_p5_numbers(passing):
    report, _ = passing
    p3 = report["criteria"]["P3"]["details"]
    assert p3["coverage"] >= 0.98
    assert p3["gaps_over_limit"] == []
    assert report["criteria"]["P5"]["details"]["rows"] == 2


# --- failing fixture: one planted failure per criterion ----------------------


def test_fail_fixture_fails_overall_and_every_criterion(failing):
    report, _ = failing
    assert report["overall_pass"] is False
    for key in lf.CRITERIA:
        assert report["criteria"][key]["pass"] is False, key


def test_fail_p1_names_the_missing_chapter(failing):
    d = failing[0]["criteria"]["P1"]["details"]
    assert d["missing"] == ["SDK Environment Setup"]
    assert d["found"] == ["Introduction", "Agentic Loop Foundations"]


def test_fail_p2_reports_each_unresolved_timestamp_with_reason(failing):
    unresolved = failing[0]["criteria"]["P2"]["details"]["unresolved"]
    by_point = {u["point"]: u["reason"] for u in unresolved}
    assert set(by_point) == {"0:06:30", "0:59:00"}
    assert "outside duration" in by_point["0:59:00"]
    assert "nearest transcript line 90s away" == by_point["0:06:30"]


def test_fail_p3_reports_coverage_and_gap(failing):
    d = failing[0]["criteria"]["P3"]["details"]
    assert d["coverage"] < 0.98
    assert d["last_timestamp"] == "0:08:20"
    assert d["gaps_over_limit"] == [
        {"from": "0:05:00", "to": "0:08:00", "seconds": 180.0}
    ]


def test_fail_p3_gap_limit_is_configurable():
    report, _ = lf.evaluate(FAIL, max_gap=200, criteria=("P3",))
    assert report["criteria"]["P3"]["details"]["gaps_over_limit"] == []
    # Still fails: coverage is 83 %.
    assert report["criteria"]["P3"]["pass"] is False


def test_fail_p4_names_missing_field(failing):
    d = failing[0]["criteria"]["P4"]["details"]
    assert d["problems"] == [
        {"file": "fixture_course.summary.md", "missing": ["as_of"]}
    ]
    assert d["values"]["fixture_course.summary.md"]["as_of"] is None


def test_fail_p5_flags_blank_and_unknown_as_of(failing):
    d = failing[0]["criteria"]["P5"]["details"]
    assert d["rows"] == 3
    assert [(p["claim"], p["as_of"]) for p in d["problems"]] == [
        ("Rate limit is 50 requests per minute", ""),
        ("The exam costs $99", "unknown"),
    ]


def test_fail_p6_flags_mishearings_but_not_cloud_provider(failing):
    hits = failing[0]["criteria"]["P6"]["details"]["hits"]
    assert [(h["found"], h["should_be"]) for h in hits] == [
        ("cloud code", "Claude Code"),
        ("Clawed", "Claude"),
    ]
    assert not any("provider" in h["found"] for h in hits)


def test_fail_p7_names_entry_without_timestamp(failing):
    d = failing[0]["criteria"]["P7"]["details"]
    assert d["entries"] == 2
    assert [p["index"] for p in d["problems"]] == [1]


def test_missing_section_and_files_fail_rather_than_crash(tmp_path):
    (tmp_path / "summaries").mkdir()
    (tmp_path / "summaries" / "x.summary.md").write_text("# x\n\n- a claim [0:00:01]\n")
    report, sample = lf.evaluate(tmp_path)
    c = report["criteria"]
    assert c["P1"]["summary"].startswith("no expected chapters")
    assert c["P2"]["summary"] == "no transcript lines to resolve against"
    assert c["P3"]["pass"] is False
    assert c["P4"]["details"]["problems"] == [
        {"file": "x.summary.md", "missing": ["frontmatter"]}
    ]
    assert c["P5"]["summary"].startswith("no '## Time-Sensitive Claims'")
    assert c["P7"]["summary"] == "corrections.json missing"
    assert report["overall_pass"] is False


def test_notes_without_citations_fail_p2(tmp_path):
    for sub in ("summaries", "transcripts"):
        (tmp_path / sub).mkdir()
    (tmp_path / "summaries" / "x.summary.md").write_text("# x\n\n- uncited claim\n")
    (tmp_path / "transcripts" / "t.md").write_text("- [0:00:01.000] hi\n")
    report, _ = lf.evaluate(tmp_path, criteria=("P2",))
    assert report["criteria"]["P2"]["pass"] is False
    assert "cite no timestamps" in report["criteria"]["P2"]["summary"]


# --- P8 sampler --------------------------------------------------------------


def test_p8_sample_has_claims_with_transcript_excerpts(passing):
    _, sample = passing
    assert sample["n_available"] == 10  # 10 bullets cite a timestamp
    assert sample["n_sampled"] == 10  # fewer than N=20 -> all of them
    for claim in sample["claims"]:
        assert claim["verdict"] is None
        assert claim["excerpt"], claim["id"]
        lo = min(c["start_s"] for c in claim["cited"]) - 60
        hi = max(c["end_s"] for c in claim["cited"]) + 60
        assert all(lo <= e["t"] <= hi for e in claim["excerpt"])
    rate = next(c for c in sample["claims"] if "50 requests" in c["claim"])
    assert any("fifty requests per minute" in e["text"] for e in rate["excerpt"])
    # The paragraph citation is not a bullet and must not be sampled.
    assert not any("not a bullet" in c["claim"] for c in sample["claims"])


def test_p8_sample_is_seeded_and_capped():
    a = lf.evaluate(PASS, p8_n=3, seed=7)[1]
    b = lf.evaluate(PASS, p8_n=3, seed=7)[1]
    c = lf.evaluate(PASS, p8_n=3, seed=8)[1]
    assert a["n_sampled"] == 3
    assert [x["claim"] for x in a["claims"]] == [x["claim"] for x in b["claims"]]
    assert {x["claim"] for x in a["claims"]} != {x["claim"] for x in c["claims"]}


# --- glossary ----------------------------------------------------------------


def test_glossary_explicit_forms_only():
    gl = lf.Glossary.load()
    text = (
        "Use a cloud provider for cloud computing via the cloud API. "
        "claude code and Claude Code are fine. Cloud is fine too."
    )
    assert gl.scan(text) == []
    found = [m for _, m in gl.scan("Cloud Code, clawed code, CLAWED, cloud.md")]
    assert found == [
        "Cloud Code",
        "clawed code",
        "CLAWED",
        "cloud.md",
    ]  # no double count


def test_transcript_scan_finds_phrase_split_across_cues():
    lines = lf.parse_transcript(
        "- [0:00:01.000] then open cloud\n- [0:00:03.000] code and the cloud provider\n"
    )
    res = lf.scan_transcript_glossary(lines, lf.Glossary.load())
    assert res["total"] == 1
    assert res["by_form"] == {"cloud code": 1}
    assert res["examples"][0]["timestamp"] == "0:00:01"


# --- expected chapters, CLI, outputs -----------------------------------------


def test_cca_expected_chapters_file_has_13_titles():
    titles = lf.parse_expected_chapters(
        (FIX / "cca_f_expected_chapters.txt").read_text()
    )
    assert len(titles) == 13
    assert titles[0] == "Introduction"
    assert titles[-1] == "Scenario Based Architect Patterns"
    assert "Claude Code in CI CD Automation" in titles


def test_expected_chapters_file_overrides_source_meta(tmp_path):
    f = tmp_path / "chapters.txt"
    f.write_text("- 00:00:00 Introduction\n- 00:05:00 Something Not In Notes\n")
    report, _ = lf.evaluate(PASS, expected_chapters=f, criteria=("P1",))
    assert report["criteria"]["P1"]["details"]["missing"] == ["Something Not In Notes"]
    assert any("differ" in w for w in report["warnings"])


def test_cli_writes_reports_and_sets_exit_code(tmp_path):
    assert lf.main([str(PASS), "--out", str(tmp_path / "p")]) == 0
    assert lf.main([str(FAIL), "--out", str(tmp_path / "f")]) == 1
    md = (tmp_path / "f" / "eval_report.md").read_text()
    assert "**Overall (P1–P7): FAIL**" in md
    assert "| P6 | FAIL | 2 residual mishearing(s) in notes |" in md
    assert "| P8 | pending reviewer |" in md
    data = json.loads((tmp_path / "f" / "eval_report.json").read_text())
    assert data["criteria"]["P1"]["details"]["missing"] == ["SDK Environment Setup"]
    p8 = json.loads((tmp_path / "f" / "p8_sample.json").read_text())
    assert p8["criterion"] == "P8" and p8["claims"]


# --- real data smoke (main checkout only; gitignored data) -------------------


@pytest.mark.skipif(not CCA_DIR.exists(), reason="CCA-F captions run not present")
def test_real_cca_captions_p3_and_transcript_mishearings():
    report, _ = lf.evaluate(
        CCA_DIR,
        criteria=("P3",),
        expected_chapters=FIX / "cca_f_expected_chapters.txt",
        scan_transcript=True,
    )
    p3 = report["criteria"]["P3"]
    assert p3["pass"] is True, p3
    assert p3["details"]["duration_s"] == 45516.0
    assert report["inputs"]["expected_chapters"] == 13
    # Auto-captions are full of "cloud code" — the corpus WP-4 must clean.
    assert report["transcript_glossary"]["by_form"]["cloud code"] >= 50


def test_p3_short_clip_tail_slack():
    """WP-M1b: a 15 s clip whose last cue starts at 0:00:10 (start times only;
    the cue runs to the end) passed the ASR gate but failed P3 at 66 % — the
    98 % rule needs a cue starting in the last 0.3 s. A tail <= 30 s passes;
    a long recording still needs 98 %."""
    def inp(times, duration):
        return lf.RunInputs(run_dir=Path("."), duration=duration,
                            transcript=[lf.TranscriptLine(t=t, text="x") for t in times])

    short = lf.check_p3(inp([0, 5, 10], 15.0))
    assert short["pass"] and short["details"]["tail_s"] == 5.0
    assert "slack" in short["summary"]
    # a 1 h recording that stops 40 s early: 98.9 % — passes on coverage, not slack
    hour = lf.check_p3(inp(range(0, 3561, 60), 3600.0))
    assert hour["pass"] and "slack" not in hour["summary"]
    # a 25 min talk missing its last 2 minutes still fails
    cut = lf.check_p3(inp(range(0, 1381, 60), 1500.0))
    assert not cut["pass"] and cut["details"]["tail_s"] == 120.0
    # an internal gap still fails a short clip
    assert not lf.check_p3(inp([0, 130, 140], 150.0), max_gap=120)["pass"]


def test_p3_excuses_only_measured_silence(tmp_path):
    """Tape/MP3 sources can have long silent stretches (leader, run-out,
    pauses). P3 excuses a gap or tail only when windowed ASR measured it as
    silence; an unmeasured stretch of the same length still fails."""
    import json
    from video2mdnotes.eval.longform import RunInputs, TranscriptLine, check_p3, load_silences

    lines = [TranscriptLine(float(t), "x") for t in (0, 30, 60, 300, 330, 360)]
    unmeasured = RunInputs(run_dir=tmp_path, transcript=lines, duration=600.0)
    r = check_p3(unmeasured)
    assert not r["pass"] and r["details"]["gaps_over_limit"]

    asr = tmp_path / "asr"
    asr.mkdir()
    # Window 2 of a file that starts at 100 s on the timeline: offsets are relative.
    (asr / "00_001.json").write_text(json.dumps({
        "file_offset_s": 100.0, "ext_start_s": 0.0,
        "silences": [[-30.0, 195.0], [265.0, 500.0]]}))
    measured = RunInputs(run_dir=tmp_path, transcript=lines, duration=600.0,
                         silences=load_silences(tmp_path))
    r = check_p3(measured)
    assert r["pass"], r
    assert "measured silence" in r["summary"]


# --- P6 ignores the Transcription Uncertainty section (WP-P2) ------------------


def _nf(text: str) -> "lf.NotesFile":
    return lf.NotesFile(path=Path("notes.summary.md"), text=text, frontmatter=None, body_offset=1)


def test_p6_ignores_transcription_uncertainty_section():
    """SX9LQD3yINg (Stage 3): the notes' own uncertainty row "Cloud Code ->
    Claude Code" records the mishearing on purpose; it is not a residual."""
    gl = lf.Glossary.load()
    text = (
        "# Notes\n\n## Window 1 [0:00–0:10]\n\n- Claude Code ships agents [0:00:05]\n\n"
        "## Transcription Uncertainty\n\n"
        "| timestamp | heard | probable | confidence |\n|---|---|---|---|\n"
        "| [0:00:05] | Cloud Code | Claude Code | high |\n\n"
        "### Names not verified\n\n- Cloud Code was heard once\n\n"
        "## Fact-Checked Notes\n\n- None\n"
    )
    assert lf.scan_notes_glossary(_nf(text), gl) == []
    # the same form outside the section is still a hit
    hits = lf.scan_notes_glossary(_nf(text + "\n## Later\n\n- Cloud Code again [0:00:06]\n"), gl)
    assert [h["found"].lower() for h in hits] == ["cloud code"]
    # a heading with a linked range is still recognised (package notes)
    linked = text.replace("## Transcription Uncertainty",
                          "## Transcription Uncertainty [0:00–0:10](https://youtu.be/x)")
    assert lf.scan_notes_glossary(_nf(linked), gl) == []
