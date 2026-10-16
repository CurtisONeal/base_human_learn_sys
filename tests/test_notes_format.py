"""WP-5: dated notes. Assertions run the real long-form evaluator (P4/P5) on
the rendered artifact, not on intermediate data."""

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from video2mdnotes.core.metadata import SourceMeta, from_ytdlp_info, prompt_header
from video2mdnotes.core.notes_format import (
    CLAIM_TYPES,
    ClaimRow,
    frontmatter,
    merge_time_sensitive,
    parse_time_sensitive,
    render_time_sensitive,
    stamp_notes,
    time_sensitive_fragment,
    validate_time_sensitive,
)
from video2mdnotes.eval.longform import check_p4, check_p5, load_run

FIXTURE = Path(__file__).parent / "fixtures" / "metadata" / "cca_f_info.json"
CAPTURED = dt.datetime(2026, 10, 15, 21, 30)


@pytest.fixture
def cca_meta():
    meta = from_ytdlp_info(json.loads(FIXTURE.read_text(encoding="utf-8")))
    meta.captured_at = CAPTURED
    return meta


def _evaluate(tmp_path, notes: str):
    (tmp_path / "summaries").mkdir(parents=True, exist_ok=True)
    (tmp_path / "summaries" / "x.summary.md").write_text(notes, encoding="utf-8")
    inp = load_run(tmp_path)
    return check_p4(inp), check_p5(inp)


def _fm_dict(text: str) -> dict:
    assert text.startswith("---\n")
    block = text.split("---\n")[1]
    return yaml.safe_load(block)


CHAPTER_A = """# Chapter A

## Summary
- The exam has five domains [0:01:00].

## Time-Sensitive Claims

| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
| The exam has five domains | [0:01:00] | 2026-07-20 | exam | CCA-F exam guide |
| Claude Code supports hooks | [0:02:00] | | feature | Claude Code docs — hooks |

## Fact-Checked Notes
- Domain count correct as of 2026-07-20.
"""

CHAPTER_B = """# Chapter B

## Time-Sensitive Claims

| Claim | Timestamp | As of | Type | Verify-by |
|---|---|---|---|---|
| The exam has five domains. | [0:00:30] | 2026-07-20 | exam | CCA-F exam guide |
| The exam costs $99 | [1:10:00] | unknown | price | ExamPro CCA-F page |
| None | — | — | — | — |

## Fact-Checked Notes
- None
"""


# --- frontmatter ---------------------------------------------------------------


def test_cca_f_frontmatter_values(cca_meta):
    fm = frontmatter(cca_meta, provenance="auto-caption (en)", structure_rung="description",
                     model="claude-cli/sonnet", run_id="run-1")
    d = _fm_dict(fm)
    assert d["published"] == dt.date(2026, 7, 20)
    assert d["as_of"] == dt.date(2026, 7, 20)
    assert d["as_of_basis"] == "published"
    assert d["captured"] == dt.date(2026, 10, 15)
    assert d["material_age_days"] == 87  # 2026-07-20 -> 2026-10-15
    assert d["published_by"] == "freeCodeCamp.org"
    assert d["source"] == "https://www.youtube.com/watch?v=reDRM0tqhNs"
    assert d["duration"] == "12:38:36"  # quoted: not YAML base-60
    assert d["transcript_source"] == "auto-caption (en)"
    assert d["structure"] == "description"
    assert d["generated_by"] == "claude-cli/sonnet"
    assert d["run_id"] == "run-1"
    assert d["title"].startswith("Claude Certified Architect - Foundations")


def test_cca_f_frontmatter_passes_p4(tmp_path, cca_meta):
    fm = frontmatter(cca_meta, provenance="auto-caption (en)")
    p4, _ = _evaluate(tmp_path, fm + "\n# Notes\n")
    assert p4["pass"], p4
    assert p4["details"]["values"]["x.summary.md"] == {"published": "2026-07-20", "as_of": "2026-07-20"}


def test_captured_at_override(cca_meta):
    d = _fm_dict(frontmatter(cca_meta, captured_at=dt.date(2026, 8, 1)))
    assert d["material_age_days"] == 12
    assert d["captured"] == dt.date(2026, 8, 1)


def test_unknown_published_uses_captured_and_passes_p4(tmp_path):
    meta = SourceMeta(source_id="meetup", title="CLE-AI meetup", captured_at=CAPTURED)
    fm = frontmatter(meta, provenance="asr:faster-whisper")
    d = _fm_dict(fm)
    assert d["published"] == "unknown"
    assert d["as_of"] == dt.date(2026, 10, 15)
    assert d["as_of_basis"] == "captured"
    assert "material_age_days" not in d
    p4, _ = _evaluate(tmp_path, fm + "\n# Notes\n")
    assert p4["pass"], p4
    assert p4["details"]["values"]["x.summary.md"]["published"] == "unknown"


def test_recorded_date_used_when_plausible_and_reset_clock_ignored():
    good = SourceMeta(source_id="r", captured_at=CAPTURED,
                      recorded_at=dt.datetime(2026, 9, 24, 18, 0))
    d = _fm_dict(frontmatter(good))
    assert d["as_of"] == dt.date(2026, 9, 24) and d["as_of_basis"] == "recorded"
    assert d["published"] == "unknown"
    reset = SourceMeta(source_id="r", captured_at=CAPTURED,
                       recorded_at=dt.datetime(2000, 1, 1))
    assert _fm_dict(frontmatter(reset))["as_of_basis"] == "captured"


def test_extra_keys_and_collision(cca_meta):
    d = _fm_dict(frontmatter(cca_meta, extra={"domain": "exam-prep"}))
    assert d["domain"] == "exam-prep"
    with pytest.raises(ValueError):
        frontmatter(cca_meta, extra={"as_of": "2020-01-01"})


# --- parse / validate / merge -----------------------------------------------------


def test_parse_by_column_name_and_skips_none_rows():
    rows = parse_time_sensitive(CHAPTER_B)
    assert [r.claim for r in rows] == ["The exam has five domains.", "The exam costs $99"]
    assert rows[1].as_of == "unknown" and rows[1].type == "price"
    assert rows[1].verify_by == "ExamPro CCA-F page"


def test_parse_none_marker_and_no_section():
    md = "## Time-Sensitive Claims\n\n| claim | timestamp | as-of | type | verify-by |\n|---|---|---|---|---|\n\n- None\n"
    assert parse_time_sensitive(md) == []
    assert parse_time_sensitive("# x\n\n## Summary\n- a\n") == []


def test_validate_fills_missing_as_of_with_flag():
    rows = parse_time_sensitive(CHAPTER_A) + parse_time_sensitive(CHAPTER_B)
    check = validate_time_sensitive(rows, dt.date(2026, 7, 20))
    by_claim = {r.claim: r for r in check.rows}
    hooks = by_claim["Claude Code supports hooks"]
    assert hooks.as_of == "2026-07-20 (filled: published)"
    assert any("filled" in f for f in hooks.flags)
    cost = by_claim["The exam costs $99"]
    assert cost.as_of == "2026-07-20 (filled: published)" and cost.type == "pricing"
    filled = [f for f in check.findings if f.level == "filled"]
    assert {f.claim for f in filled} == {"Claude Code supports hooks", "The exam costs $99"}
    assert check.ok
    assert all(r.type in CLAIM_TYPES for r in check.rows)
    # input not mutated
    assert rows[1].as_of == ""


def test_validate_reports_when_no_fill():
    rows = [ClaimRow("x", "[0:01:00]", "", "news", "blog")]
    check = validate_time_sensitive(rows, None)
    assert not check.ok and check.rows[0].as_of == ""
    check = validate_time_sensitive(rows, dt.date(2026, 7, 20), fill=False)
    assert not check.ok


def test_validate_warns_on_model_knowledge_date():
    rows = [ClaimRow("x", "[0:01:00]", "2027-01", "version", "release notes")]
    check = validate_time_sensitive(rows, dt.date(2026, 7, 20))
    assert any(f.level == "warning" and "after the source date" in f.message
               for f in check.findings)


def test_unknown_published_rows_get_captured_basis(tmp_path):
    meta = SourceMeta(source_id="meetup", captured_at=CAPTURED)
    fm = frontmatter(meta)
    d = _fm_dict(fm)
    check = validate_time_sensitive(
        [ClaimRow("GPT-6 just launched", "[0:30:00]", "", "news", "OpenAI blog")],
        d["as_of"], basis=d["as_of_basis"],
    )
    assert check.rows[0].as_of == "2026-10-15 (filled: captured)"
    p4, p5 = _evaluate(tmp_path, stamp_notes("# Meetup\n", fm, check.rows))
    assert p4["pass"] and p5["pass"], (p4, p5)


def test_merge_dedupes_keeps_earliest_and_distinct():
    a, b = parse_time_sensitive(CHAPTER_A), parse_time_sensitive(CHAPTER_B)
    merged = merge_time_sensitive([a, b])
    claims = [r.claim for r in merged]
    assert len(claims) == 3
    domains = [r for r in merged if "five domains" in r.claim]
    assert len(domains) == 1 and domains[0].timestamp == "[0:00:30]"
    # ordered by timestamp
    assert claims[0].startswith("The exam has five domains")
    assert claims[-1] == "The exam costs $99"


# --- stamping -------------------------------------------------------------------


def test_stamped_note_passes_p4_and_p5(tmp_path, cca_meta):
    notes = "---\ntitle: old\n---\n" + CHAPTER_A + "\n" + CHAPTER_B
    rows = merge_time_sensitive([parse_time_sensitive(CHAPTER_A), parse_time_sensitive(CHAPTER_B)])
    check = validate_time_sensitive(rows, cca_meta.published_at)
    fm = frontmatter(cca_meta, provenance="auto-caption (en)", structure_rung="description")
    out = stamp_notes(notes, fm, check.rows)

    assert out.startswith(fm)
    assert "title: old" not in out
    assert out.count("Time-Sensitive Claims") == 1
    p4, p5 = _evaluate(tmp_path, out)
    assert p4["pass"], p4
    assert p5["pass"], p5
    assert p5["details"]["rows"] == 3


def test_unvalidated_merged_rows_fail_p5(tmp_path, cca_meta):
    """Guard: the fill step is what makes P5 pass, not the stamping."""
    rows = merge_time_sensitive([parse_time_sensitive(CHAPTER_A)])
    _, p5 = _evaluate(tmp_path, stamp_notes(CHAPTER_A, frontmatter(cca_meta), rows))
    assert not p5["pass"]


def test_placement_before_single_fact_checked():
    out = stamp_notes(CHAPTER_A, "---\na: 1\n---\n", [ClaimRow("c", "[0:01:00]", "2026", "news", "x")])
    assert out.index("## Time-Sensitive Claims") < out.index("## Fact-Checked Notes")
    assert out.index("## Summary") < out.index("## Time-Sensitive Claims")


def test_placement_before_transcript_or_end():
    md = "# T\n\n## Summary\n- a\n\n## Transcript\n- [0:00:01] hi\n"
    out = stamp_notes(md, "---\na: 1\n---\n", [])
    assert out.index("## Time-Sensitive Claims") < out.index("## Transcript")
    out = stamp_notes("# T\n\n## Summary\n- a\n", "---\na: 1\n---\n", [])
    assert out.rstrip().endswith("- None")


def test_empty_table_still_passes_p5(tmp_path, cca_meta):
    out = stamp_notes("# T\n", frontmatter(cca_meta), [])
    _, p5 = _evaluate(tmp_path, out)
    assert p5["pass"], p5


def test_stamp_parses_rows_when_none_given():
    out = stamp_notes(CHAPTER_A, "---\na: 1\n---\n")
    assert "Claude Code supports hooks" in out and out.count("## Time-Sensitive Claims") == 1


def test_render_escapes_pipes():
    text = render_time_sensitive([ClaimRow("a | b", "[0:00:01]", "2026", "other", "x")])
    assert "a \\| b" in text


# --- prompt fragment (what the model receives) ----------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split()).lower()


def test_fragment_content():
    flat = _flat(time_sensitive_fragment())
    assert "## time-sensitive claims" in flat
    assert "| claim | timestamp | as-of | type | verify-by |" in flat
    for kind in CLAIM_TYPES:
        assert f"`{kind}`" in flat
    for trigger in ("versions", "pricing", "exam details", "news", '"currently"', '"now"',
                    '"recently"', '"new"'):
        assert trigger in flat
    assert "never use it for as-of" in flat
    assert "- none" in flat
    assert "relative to the source's `published:` date" in flat
    assert "knowledge has a cutoff" in flat


def test_fragment_carries_concrete_as_of(cca_meta):
    rendered = time_sensitive_fragment(cca_meta)
    assert "`2026-07-20` (the published date)" in rendered
    # header the model sees uses the same date
    assert "Published: 2026-07-20" in prompt_header(cca_meta)
    unknown = time_sensitive_fragment(SourceMeta(source_id="m", captured_at=CAPTURED))
    assert "`2026-10-15 (captured)`" in unknown


def test_model_output_following_fragment_parses_and_passes(tmp_path, cca_meta):
    """A table written exactly as the fragment instructs survives the round trip."""
    model_out = (
        "# CCA-F\n\n## Summary\n- x [0:10:17]\n\n## Time-Sensitive Claims\n\n"
        "| claim | timestamp | as-of | type | verify-by |\n|---|---|---|---|---|\n"
        "| Agent SDK is at v1 | [0:12:00] | 2026-07-20 | version | Agent SDK release notes |\n"
        "| <claim as stated> | [H:MM:SS] | <published date, YYYY-MM-DD> | <type> | <official page or document to check> |\n"
        "\n## Fact-Checked Notes\n- None\n"
    )
    rows = parse_time_sensitive(model_out)
    assert [r.claim for r in rows] == ["Agent SDK is at v1"]  # placeholder row dropped
    out = stamp_notes(model_out, frontmatter(cca_meta), validate_time_sensitive(rows, "2026-07-20").rows)
    p4, p5 = _evaluate(tmp_path, out)
    assert p4["pass"] and p5["pass"]
