"""WP-4: transcript quality — provenance, glossary correction, notes lint.

The grader for residual mishearings is the evaluator (eval/longform.py and its
glossary_default.json). quality.py must not import that glossary; these tests
use it only to grade the corrected artifact.
"""

import hashlib
import json
from pathlib import Path

import pytest

from video2mdnotes.core import quality as q
from video2mdnotes.core.metadata import from_ytdlp_info
from video2mdnotes.core.transcriber import Segment
from video2mdnotes.eval import longform as lf

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_META = Path(__file__).parent / "fixtures" / "metadata" / "cca_f_info.json"
CCA_RUN_NAME = (
    "20260906_claude_certified_architect_foundations_prepare_for_and_pass_the_exam"
    "_captions"
)


def _find_cca_transcript():
    # Worktrees live under <repo>/.claude/worktrees/<name>; previous_run_results
    # is untracked, so look in this checkout and in the main checkout above it.
    for base in [ROOT, *ROOT.parents]:
        d = base / "previous_run_results" / CCA_RUN_NAME / "transcripts"
        hits = sorted(p for p in d.glob("*.md") if "corrected" not in p.name)
        if hits:
            return hits[0]
    return None


CCA_TRANSCRIPT = _find_cca_transcript()


@pytest.fixture(scope="module")
def claude_glossary():
    return q.glossary_for_domain("claude")


def segs(*texts, step=2.0):
    return [Segment(start=i * step, end=i * step + step, text=t) for i, t in enumerate(texts)]


def fix(text, glossary):
    out, corr = q.correct_segments(segs(text), glossary)
    return out[0].text, corr


# --- independence -------------------------------------------------------------


def test_quality_does_not_use_the_evaluators_glossary():
    import ast

    tree = ast.parse(Path(q.__file__).read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree, clean=False)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "eval" not in (node.module or "")
        if isinstance(node, ast.Import):
            assert all("eval" not in a.name for a in node.names)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value != docstring:
                assert "glossary_default" not in node.value
    domain = (q.GLOSSARY_DIR / "claude.json").read_text(encoding="utf-8")
    assert "must_not_flag" not in domain  # not a copy of the grader's file


# --- glossary selection -------------------------------------------------------


def test_cca_metadata_selects_claude_domain():
    meta = from_ytdlp_info(json.loads(FIXTURE_META.read_text(encoding="utf-8")))
    g = q.build_glossary(meta)
    assert "domain:claude" in g.sources
    assert "Claude Code" in g.terms
    # Metadata brand casing: acronyms survive, title-case words do not.
    assert "CCA-F" in g.terms
    assert "Prepare" not in g.terms and "Foundations" not in g.terms


def test_unrelated_metadata_selects_no_domain():
    meta = {"title": "Cloud computing on AWS", "description": "cloud provider basics",
            "tags": ["aws", "cloud"]}
    g = q.build_glossary(meta)
    assert g.sources == []
    out, corr = q.correct_segments(segs("we deploy cloud code to the cloud"), g)
    assert corr == [] and out[0].text == "we deploy cloud code to the cloud"


def test_metadata_casing_only_rewrites_all_lowercase():
    meta = {"title": "ExamPro course", "description": "", "tags": []}
    g = q.build_glossary(meta)
    out, corr = q.correct_segments(segs("welcome to exampro and ExamPro"), g)
    assert out[0].text == "welcome to ExamPro and ExamPro"
    assert [c["rule"] for c in corr] == ["metadata-casing:ExamPro"]


# --- positives ----------------------------------------------------------------


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("install cloud code now", "install Claude Code now"),
        ("use the cloud agent sdk here", "use the Claude Agent SDK here"),
        ("edit your cloud md file", "edit your CLAUDE.md file"),
        ("edit your cloud.md file", "edit your CLAUDE.md file"),
        ("open cloud.mmd please", "open CLAUDE.md please"),
        ("run clawed code", "run Claude Code"),
        ("switch to cloud sonnet 4.6", "switch to Claude Sonnet 4.6"),
        ("cloud opus is slower", "Claude Opus is slower"),
        ("cloud haiku is cheap", "Claude Haiku is cheap"),
        ("open cloud desktop", "open Claude Desktop"),
        ("their clawed API key", "their Claude API key"),
        ("then say clawed. And", "then say Claude. And"),
        ("type clawed off status", "type claude auth status"),
        ("the claw agent SDK", "the Claude Agent SDK"),
        ("the cloud uh code engine", "the Claude uh Code engine"),
        ("use cloud codes with GitHub", "use Claude Code with GitHub"),
        ("cloud codes settings", "Claude Code's settings"),
        ("find it at code.cloud.com.", "find it at code.claude.com."),
    ],
)
def test_product_mishearings_fixed(claude_glossary, heard, expected):
    text, corr = fix(heard, claude_glossary)
    assert text == expected
    assert corr and all(c["timestamp"] == "0:00:00" for c in corr)


# --- negatives: genuine cloud / clawed usage is untouched ---------------------


@pytest.mark.parametrize(
    "text",
    [
        "pick a cloud provider",
        "cloud computing is mature",
        "we run it on the AWS cloud",
        "it lives in the cloud",
        "the cloud API is billed hourly",
        "Google Cloud Code is an IDE plugin",
        "the virtual cloud desktop",
        "the cat clawed the sofa",
        "the company clawed back the bonus",
        "he clawed his way to the top",
        "she clawed off the bandage",
        "they clawed at the door",
        "Claude Code and CLAUDE.md are already right",
        "a cloud-native codebase",
        "cloud codecs",
        "my home directory/cloud folder",
    ],
)
def test_genuine_usage_unchanged(claude_glossary, text):
    out, corr = q.correct_segments(segs(text), claude_glossary)
    assert corr == []
    assert out[0].text == text


# --- casing -------------------------------------------------------------------


@pytest.mark.parametrize(
    "heard, expected",
    [
        ("Cloud Code rocks", "Claude Code rocks"),
        ("CLOUD CODE rocks", "CLAUDE CODE rocks"),
        ("Cloud agent SDK", "Claude Agent SDK"),
        ("Clawed code", "Claude Code"),
        ("CLOUD.MD", "CLAUDE.MD"),
    ],
)
def test_casing(claude_glossary, heard, expected):
    assert fix(heard, claude_glossary)[0] == expected


# --- cross-line ---------------------------------------------------------------


def test_cross_line_phrase_edits_both_lines(claude_glossary):
    lines = ["you can find cloud", "code at the docs", "unrelated line"]
    out, corr = q.correct_segments(segs(*lines), claude_glossary)
    assert [s.text for s in out] == ["you can find Claude", "Code at the docs", "unrelated line"]
    assert len(corr) == 1
    c = corr[0]
    assert c["line_index"] == 0 and c["line_end_index"] == 1
    assert c["timestamp"] == "0:00:00"
    assert c["original"] == "cloud code" and c["corrected"] == "Claude Code"
    # timestamps preserved, untouched segment is the same object
    assert [s.start for s in out] == [0.0, 2.0, 4.0]


def test_cross_line_three_words_and_timestamp_of_start(claude_glossary):
    lines = ["filler", "intro", "the cloud", "agent", "SDK is great"]
    out, corr = q.correct_segments(segs(*lines, step=61.0), claude_glossary)
    assert [s.text for s in out][2:] == ["the Claude", "Agent", "SDK is great"]
    assert corr[0]["timestamp"] == "0:02:02"  # line 2 starts at 122 s
    assert corr[0]["line_index"] == 2 and corr[0]["line_end_index"] == 4


def test_cross_line_collapse_lands_on_start_line(claude_glossary):
    out, corr = q.correct_segments(segs("update the cloud", "md files now"), claude_glossary)
    assert [s.text for s in out] == ["update the CLAUDE.md", "files now"]
    assert corr[0]["rule"] == "claude-md"


def test_cross_line_negative_not_joined_into_false_match(claude_glossary):
    lines = ["it runs in the cloud.", "Code review happens later"]
    out, corr = q.correct_segments(segs(*lines), claude_glossary)
    assert corr == [] and [s.text for s in out] == lines


def test_input_segments_not_mutated(claude_glossary):
    original = segs("cloud code")
    q.correct_segments(original, claude_glossary)
    assert original[0].text == "cloud code"


# --- file outputs -------------------------------------------------------------

SAMPLE_TRANSCRIPT = """---
title: "t"
model: youtube-auto-captions
---

## Transcript

- [0:00:01.000] welcome to cloud
- [0:00:03.500] code and the cloud provider story
- [0:01:05.250] edit cloud.md then
- [0:01:07.000] nothing here
"""


def test_write_corrected_transcript_artifacts(tmp_path, claude_glossary):
    run = tmp_path / "run"
    (run / "transcripts").mkdir(parents=True)
    raw = run / "transcripts" / "demo.captions.md"
    raw.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
    before = raw.read_bytes()

    out, cpath, corr = q.write_corrected_transcript(raw, claude_glossary)

    assert raw.read_bytes() == before  # raw untouched
    assert out == run / "transcripts" / "demo.captions.corrected.md"
    assert cpath == run / "corrections.json"
    text = out.read_text(encoding="utf-8")
    assert "- [0:00:01.000] welcome to Claude" in text
    assert "- [0:00:03.500] Code and the cloud provider story" in text
    assert "- [0:01:05.250] edit CLAUDE.md then" in text
    assert "- [0:01:07.000] nothing here" in text
    assert "corrected: true" in text and "corrections: 2" in text
    # same cues, same timestamps
    assert [t.t for t in lf.parse_transcript(text)] == [
        t.t for t in lf.parse_transcript(SAMPLE_TRANSCRIPT)
    ]
    logged = json.loads(cpath.read_text(encoding="utf-8"))
    assert logged == corr
    assert [(c["timestamp"], c["original"], c["corrected"]) for c in logged] == [
        ("0:00:01", "cloud code", "Claude Code"),
        ("0:01:05", "cloud.md", "CLAUDE.md"),
    ]
    for c in logged:
        assert set(c) >= {"timestamp", "original", "corrected", "rule", "line_index"}
    # P7 (the evaluator's check) accepts the log
    assert lf.check_p7(lf.RunInputs(run_dir=run))["pass"] is True
    # The evaluator picks the RAW transcript by default, not the corrected one
    assert lf.load_run(run).transcript_path == raw


def test_refuses_to_overwrite_raw(tmp_path, claude_glossary):
    raw = tmp_path / "x.md"
    raw.write_text(SAMPLE_TRANSCRIPT, encoding="utf-8")
    with pytest.raises(ValueError):
        q.write_corrected_transcript(raw, claude_glossary, out_path=raw)


def test_empty_corrections_still_written(tmp_path, claude_glossary):
    raw = tmp_path / "transcripts" / "clean.md"
    raw.parent.mkdir()
    raw.write_text("- [0:00:01.000] nothing to fix\n", encoding="utf-8")
    _, cpath, corr = q.write_corrected_transcript(raw, claude_glossary)
    assert corr == [] and json.loads(cpath.read_text()) == []


# --- real data: CCA-F auto-captions, graded by the evaluator -------------------


@pytest.mark.skipif(CCA_TRANSCRIPT is None, reason="CCA-F run artifacts absent")
def test_cca_f_corrected_transcript_passes_evaluator_scan(tmp_path):
    meta = from_ytdlp_info(json.loads(FIXTURE_META.read_text(encoding="utf-8")))
    glossary = q.build_glossary(meta)
    digest = hashlib.sha256(CCA_TRANSCRIPT.read_bytes()).hexdigest()

    run = tmp_path / "run"
    (run / "transcripts").mkdir(parents=True)
    out_path = run / "transcripts" / q.corrected_path_for(CCA_TRANSCRIPT).name
    out, cpath, corr = q.write_corrected_transcript(
        CCA_TRANSCRIPT, glossary, out_path=out_path, corrections_path=run / "corrections.json"
    )
    assert hashlib.sha256(CCA_TRANSCRIPT.read_bytes()).hexdigest() == digest

    grader = lf.Glossary.load()
    raw_lines = lf.parse_transcript(CCA_TRANSCRIPT.read_text(encoding="utf-8"))
    new_lines = lf.parse_transcript(out.read_text(encoding="utf-8"))
    before = lf.scan_transcript_glossary(raw_lines, grader)
    after = lf.scan_transcript_glossary(new_lines, grader)
    assert before["total"] >= 80  # WP-0 measured 89
    assert after["total"] == 0, after
    # Same cues, same timestamps
    assert [t.t for t in new_lines] == [t.t for t in raw_lines]
    # every correction logged and timestamped, and P7 passes
    assert len(corr) >= before["total"]
    assert lf.check_p7(lf.RunInputs(run_dir=run))["pass"] is True
    # genuine cloud usage in this course survives
    text = out.read_text(encoding="utf-8")
    assert "cloud computing" in text
    assert "Google Cloud" in text


@pytest.mark.skipif(CCA_TRANSCRIPT is None, reason="CCA-F run artifacts absent")
def test_cca_f_provenance_is_auto_captions():
    run_dir = CCA_TRANSCRIPT.parent.parent
    prov = q.infer_provenance(run_dir)
    assert prov is not None and prov.label == "auto-captions"


# --- provenance ---------------------------------------------------------------


def test_provenance_labels_roundtrip():
    for label in ["manual-captions", "auto-captions", "asr:faster-whisper/large-v3"]:
        assert q.TranscriptProvenance.parse(label).label == label
    with pytest.raises(ValueError):
        q.TranscriptProvenance.parse("whisper")
    assert not q.TranscriptProvenance("manual-captions").is_machine
    assert q.TranscriptProvenance("auto-captions").is_machine


@pytest.mark.parametrize(
    "source, model, label",
    [
        (None, "youtube-auto-captions", "auto-captions"),
        ("captions (manual, en)", "none (human-authored captions)", "manual-captions"),
        ("whisper", "large-v3", "asr:faster-whisper/large-v3"),
        ("asr:parakeet-mlx/tdt-0.6b", None, "asr:parakeet-mlx/tdt-0.6b"),
    ],
)
def test_provenance_from_fields(source, model, label):
    assert q.provenance_from_fields(source, model).label == label


def test_infer_provenance_from_caption_source(tmp_path):
    (tmp_path / "caption_source.txt").write_text("temp_files/abc.en-orig.json3\n")
    assert q.infer_provenance(tmp_path).label == "auto-captions"
    (tmp_path / "caption_source.txt").write_text("temp_files/abc.en.json3\n")
    assert q.infer_provenance(tmp_path).label == "manual-captions"
    (tmp_path / "caption_source.txt").unlink()
    assert q.infer_provenance(tmp_path) is None


def test_infer_provenance_prefers_transcript_frontmatter(tmp_path):
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "a.md").write_text(
        "---\ntranscript_source: whisper\nmodel: distil-large-v3\n---\n- [0:00:00.000] hi\n"
    )
    (tmp_path / "caption_source.txt").write_text("x.en-orig.json3")
    assert q.infer_provenance(tmp_path).label == "asr:faster-whisper/distil-large-v3"


# --- notes lint ---------------------------------------------------------------

PLANTED_NOTES = """---
title: notes
---

# Chapter 1

Cloud Code runs an agentic loop [0:48:56]. Pick any cloud provider.

- Configure the cloud
  agent SDK options [0:15:32].
- The team clawed back time.

## Chapter 2

Edit cloud.md to change behaviour. Claude Code is correct here.
"""


def test_lint_finds_planted_errors(claude_glossary):
    findings = q.lint_notes(PLANTED_NOTES, claude_glossary)
    got = [(f["line"], f["found"], f["suggested"]) for f in findings]
    assert got == [
        (7, "Cloud Code", "Claude Code"),
        (9, "cloud agent SDK", "Claude Agent SDK"),
        (15, "cloud.md", "CLAUDE.md"),
    ]
    assert all(f["rule"] and f["context"] for f in findings)


def test_lint_does_not_join_across_headings(claude_glossary):
    md = "text about the cloud\n## Code review\nmore"
    assert q.lint_notes(md, claude_glossary) == []


def test_lint_clean_notes(claude_glossary):
    assert q.lint_notes("Claude Code and the Claude Agent SDK on a cloud provider.",
                        claude_glossary) == []


# --- uncertainty block --------------------------------------------------------


def test_uncertainty_prompt_block_names_section_and_columns():
    block = " ".join(q.uncertainty_prompt_block().split()).lower()
    assert "## transcription uncertainty" in block
    for col in ("timestamp", "heard", "probable", "confidence"):
        assert col in block


def test_parse_uncertainty_table():
    md = """# Notes
Some notes.

## Transcription Uncertainty

| timestamp | heard | probable | confidence |
|---|---|---|---|
| 0:12:34 | cloud code | Claude Code | high |
| [1:02:03.5] | `enthropic` | Anthropic | Medium |
| not a time | x | y | low |
| 0:00:09 | clot | Claude | sure |

## Next section
| 0:99:00 | ignored | ignored | high |
"""
    items = q.parse_uncertainty_section(md)
    assert items == [
        {"timestamp": "0:12:34", "heard": "cloud code", "probable": "Claude Code",
         "confidence": "high"},
        {"timestamp": "1:02:03", "heard": "enthropic", "probable": "Anthropic",
         "confidence": "medium"},
        {"timestamp": "0:00:09", "heard": "clot", "probable": "Claude",
         "confidence": "unknown"},
    ]


def test_parse_uncertainty_reordered_columns_and_bullets():
    md = """## Transcription Uncertainty
| heard | probable | timestamp | confidence |
|--|--|--|--|
| cloud md | CLAUDE.md | 7:22:45 | low |

- [0:05:00] "claw de" -> "Claude" (high)
- 0:06:00: clawed → Claude
"""
    items = q.parse_uncertainty_section(md)
    assert [(i["timestamp"], i["heard"], i["probable"], i["confidence"]) for i in items] == [
        ("7:22:45", "cloud md", "CLAUDE.md", "low"),
        ("0:05:00", "claw de", "Claude", "high"),
        ("0:06:00", "clawed", "Claude", "unknown"),
    ]


def test_parse_uncertainty_absent_vs_none():
    assert q.parse_uncertainty_section("# Notes\nnothing") is None
    assert q.parse_uncertainty_section("## Transcription Uncertainty\nNone.\n") == []
    assert len(q.iter_uncertainty([
        "## Transcription Uncertainty\n| 0:00:01 | a | b | high |",
        "no section",
    ])) == 1
