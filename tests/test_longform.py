"""WP-3 (+WP-7): the long-form path. Assertions are on rendered prompts and on
the artifacts the evaluator reads — never on exit codes alone (AGENTS.md)."""

import datetime as dt
import json
import re
import shutil
from pathlib import Path

import pytest

from video2mdnotes.core import longform as lf
from video2mdnotes.core import quality, summarizer
from video2mdnotes.core.costs import BudgetExhausted
from video2mdnotes.core.metadata import SourceMeta
from video2mdnotes.core.sources import Chapter
from video2mdnotes.eval import longform as ev

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CCA = Path(__file__).parent / "fixtures" / "eval_longform" / "cca_f_expected_chapters.txt"
CCA_RUN_NAME = (
    "20260906_claude_certified_architect_foundations_prepare_for_and_pass_the_exam_captions"
)


def _find_cca_run():
    for base in [ROOT, *ROOT.parents]:
        d = base / "previous_run_results" / CCA_RUN_NAME
        if (d / "transcripts").is_dir() and (d / "yt_info.json").exists():
            return d
    return None


CCA_RUN = _find_cca_run()


def flat(text: str) -> str:
    return " ".join(text.split())


# --- mock LLM ------------------------------------------------------------------

_LINE = re.compile(r"^\[(\d+:\d{2}:\d{2})\] (.*)$")


def passage_lines(user: str):
    """(timestamp, text) of the lines the passage asks notes for — not the
    CONTEXT ONLY overlap."""
    body = user.split("PASSAGE TRANSCRIPT", 1)[1].split("END OF PASSAGE", 1)[0]
    return [m.groups() for m in map(_LINE.match, body.splitlines()) if m]


class MockLLM:
    """Deterministic notes citing real timestamps from the passage it was given."""

    def __init__(self, fail_on_call=None):
        self.calls = []          # (kind, user)
        self.fail_on_call = fail_on_call

    def __call__(self, system: str, user: str):
        kind = "map" if "Course Map" in system else "passage"
        if self.fail_on_call is not None and len(self.calls) + 1 == self.fail_on_call:
            self.calls.append(("fail", user))
            raise BudgetExhausted("cap reached (test)", provider="openai")
        self.calls.append((kind, user))
        if kind == "map":
            return self._map(user), "mock/map"
        return self._passage(user), "mock/passage"

    @staticmethod
    def _map(user: str) -> str:
        rows = ["| # | chapter | time range | gist |", "|---|---|---|---|"]
        for line in user.splitlines():
            m = re.match(r"^## (\S+) \| (.+?) \| (\[.+\])$", line)
            if m:
                rows.append(f"| {m.group(1)} | {m.group(2)} | {m.group(3)} | Gist of {m.group(2)}. |")
        return "\n".join(rows) + "\n\n### Cross-chapter themes\n- Agents recur (ch. 1, 2)\n"

    def _passage(self, user: str) -> str:
        lines = passage_lines(user)
        first, mid, last = lines[0], lines[len(lines) // 2], lines[-1]
        n = len(self.calls)
        where = re.search(r"THIS PASSAGE: (.*?)\.\n", user).group(1)
        return f"""### Summary
- Passage {n} ({where}) opens with: {first[1]} [{first[0]}]
- Middle point: {mid[1]} [{mid[0]}]
- Ends with: {last[1]} [{last[0]}]

### Key Concepts & Vocabulary
- `term{n}` - defined here [{first[0]}]

### Commands / Code / Config
```bash
# a comment that must not become a heading
claude --version
```

### Exam-relevant points
- Exam point {n} [{last[0]}]

### Special Attention
- None

### Transcription Uncertainty
| timestamp | heard | probable | confidence |
|---|---|---|---|
| {mid[0]} | claw | Claude | medium |

### Time-Sensitive Claims
| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
| Claim {n} about the exam | [{mid[0]}] |  | exam | exam guide PDF |

### Fact-Checked Notes
- The speaker configures cloud code hooks; correct as of the published date [{first[0]}]

```ledger
{{"concepts": [{{"term": "term{n}", "definition": "defined in passage {n}"}}],
  "terms_and_corrections": [], "open_threads": ["thread from passage {n}"],
  "last_point": "passage {n} ended at {last[0]}"}}
```
"""


# --- a small synthetic run dir ---------------------------------------------------

def _transcript_md(lines):
    out = ["---", 'title: "Claude Certified Test Course"',
           "source: https://www.youtube.com/watch?v=test", "model: youtube-auto-captions",
           "language: en-orig", "generated_at: 2026-09-06", "---", "", "## Transcript", ""]
    for t, text in lines:
        s = int(t)
        out.append(f"- [{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}.000] {text}")
    return "\n".join(out) + "\n"


@pytest.fixture
def small_run(tmp_path):
    """3 chapters; with max_words=800 the second splits into 2 parts.
    Passage order: 1 = ch1, 2 = ch2 part 1, 3 = ch2 part 2, 4 = ch3."""
    run = tmp_path / "run"
    (run / "transcripts").mkdir(parents=True)
    texts = [f"intro line {i} about agents and tools here" for i in range(60)]   # ch1 0-300 s
    texts += [f"setup line {i} covering sdk install steps" for i in range(150)]  # ch2 300-1050 s
    texts[60 + 10] = "now we open cloud code and log in"
    texts += [f"wrap line {i} with review of exam topics" for i in range(40)]    # ch3 1050-1250 s
    lines = [(5 * k, text) for k, text in enumerate(texts)]
    (run / "transcripts" / "test_course.captions.md").write_text(_transcript_md(lines))
    meta = SourceMeta(
        source_id="test", title="Claude Certified Test Course - exam prep",
        url="https://www.youtube.com/watch?v=test",
        published_at=dt.date(2026, 7, 20), published_by="Test Channel",
        duration_s=1250.0, chapter_source="description",
        chapters=[Chapter(start_s=0, end_s=300, title="Introduction"),
                  Chapter(start_s=300, end_s=1050, title="SDK Setup"),
                  Chapter(start_s=1050, end_s=1250, title="Wrap Up")],
        description="Claude certification exam course",
        captured_at=dt.datetime(2026, 9, 6, 10, 0),
    )
    meta.write(run)
    return run


# --- prompts -------------------------------------------------------------------------


def test_part2_prompt_carries_skeleton_header_ledger_overlap_hint(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    ch2 = plan.chapters[1]
    assert [p.part for p in ch2] == [1, 2]
    p1, p2 = ch2
    ledger = {"concepts": [{"term": "sentinel-term", "definition": "from part one"}],
              "terms_and_corrections": [], "open_threads": ["sentinel-thread"],
              "last_point": "sentinel-last-point"}

    part2 = lf.render_passage_prompt(plan, p2, ledger_in=ledger, prev=p1).text
    rendered = flat(part2)
    # (a) dated header
    assert "Published: 2026-07-20 by Test Channel" in rendered
    assert "Transcript source: auto-captions" in rendered
    # (b) skeleton: every chapter with its range
    for ch in plan.chapters:
        assert f"{ch[0].title} [{lf._chapter_range(ch)}]" in rendered
    # (c) ledger from part 1 + overlap tail, labelled
    assert "sentinel-term" in rendered and "sentinel-last-point" in rendered
    assert lf.CONTEXT_ONLY_LABEL in part2
    tail_start = plan.segments[p1.span[1] - 1]
    assert f"[{lf.hms(tail_start.start)}] {flat(tail_start.text)}" in part2
    ctx = part2.split(lf.CONTEXT_ONLY_LABEL + " —", 1)[1].split("END OF CONTEXT ONLY")[0]
    ctx_ts = re.findall(r"^\[(\d+:\d{2}:\d{2})\]", ctx, re.M)
    assert ctx_ts and all(p1.end_s - 150 <= quality.parse_ts(t) < p1.end_s for t in ctx_ts)
    # (d) every passage line carries its timestamp
    a, b = p2.span
    for seg in plan.segments[a:b]:
        assert f"[{lf.hms(seg.start)}] {flat(seg.text)}" in part2
    # (e) mishearing hint, (f) uncertainty block, (g) time-sensitive fragment
    assert 'bare "cloud" or "claw"' in rendered and "auto-captions" in rendered
    assert "## Transcription Uncertainty" in part2
    assert "## Time-Sensitive Claims" in part2
    assert "write as-of as `2026-07-20`" in rendered
    # exam-prep profile from metadata
    assert "### Exam-relevant points" in part2

    part1 = lf.render_passage_prompt(plan, p1).text
    assert "RUNNING LEDGER" not in part1 and lf.CONTEXT_ONLY_LABEL not in part1.split("SOURCE:", 1)[1]
    assert "COURSE SKELETON" in part1 and "Published: 2026-07-20" in part1


def test_passage_text_is_corrected_transcript(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    p1 = plan.chapters[1][0]
    user = lf.render_passage_prompt(plan, p1).user
    assert "Claude Code" in user and "cloud code" not in user.lower()


def test_manual_captions_get_no_mishearing_hint():
    prov = quality.TranscriptProvenance("manual-captions")
    assert lf.mishearing_hint(prov) == ""
    assert "auto-captions" in lf.mishearing_hint(quality.TranscriptProvenance("auto-captions"))


# --- ledger --------------------------------------------------------------------------


def test_extract_ledger_parses_and_strips():
    out = "### Summary\n- a [0:00:01]\n\n```ledger\n{\"concepts\": [{\"term\": \"t\", \"definition\": \"d\"}], \"last_point\": \"x\"}\n```\n"
    notes, led, err = lf.extract_ledger(out)
    assert err is None and "```ledger" not in notes and "### Summary" in notes
    assert led == {"concepts": [{"term": "t", "definition": "d"}], "claims": [],
                   "terms_and_corrections": [], "open_threads": [], "last_point": "x"}


def test_extract_ledger_missing_or_invalid():
    notes, led, err = lf.extract_ledger("### Summary\n- a\n")
    assert led is None and "no ```ledger" in err
    notes, led, err = lf.extract_ledger("x\n```ledger\n{not json}\n```\n")
    assert led is None and "JSON" in err and "ledger" not in notes


def test_cap_ledger_drops_oldest_deterministically():
    led = {"concepts": [{"term": f"t{i}", "definition": "word " * 20} for i in range(60)],
           "terms_and_corrections": ["a b c"], "open_threads": ["keep me"], "last_point": "end"}
    capped, dropped = lf.cap_ledger(led, max_words=800)
    assert lf._ledger_words(capped) <= 800
    assert dropped > 0 and capped["concepts"][-1]["term"] == "t59"
    assert capped["concepts"][0]["term"] == f"t{dropped}"   # oldest went first
    assert capped["open_threads"] == ["keep me"] and capped["last_point"] == "end"
    assert lf.cap_ledger(led, 800) == (capped, dropped)       # deterministic


# --- resume ---------------------------------------------------------------------------


def test_budget_exhausted_on_passage_3_then_resume(small_run, tmp_path):
    out = tmp_path / "out"
    mock = MockLLM(fail_on_call=3)
    with pytest.raises(BudgetExhausted):
        lf.run_longform(small_run, out, complete=mock, max_words=800, progress=lambda m: None)
    done = sorted(p.name for p in (out / lf.CHUNK_DIR).glob("*.md"))
    assert done == ["01_introduction_p1.md", "02_sdk_setup_p1.md"]
    assert (out / lf.LEDGER_DIR / "02_p1.json").exists()
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["passages"]["02_sdk_setup_p2"]["status"] == "pending"
    assert not (out / "summaries").exists()

    mock2 = MockLLM()
    result = lf.run_longform(small_run, out, complete=mock2, max_words=800,
                             progress=lambda m: None)
    passages = [u for k, u in mock2.calls if k == "passage"]
    # passages 1-2 not re-called: the rerun starts at ch2 part 2
    assert len(passages) == 2
    assert "part 2/2" in passages[0] and "Wrap Up" in passages[1]
    # ledger for part 2 restored from disk (written by the FIRST run's passage 2)
    saved = json.loads((out / lf.LEDGER_DIR / "02_p1.json").read_text())
    assert saved["last_point"] in passages[0]
    assert "thread from passage 2" in passages[0]
    assert result.passages_skipped == 2 and result.passages_run == 2
    assert result.summary_path.exists()


# --- assembly + evaluator ---------------------------------------------------------------


def _eval(run_dir, expected=None):
    report, _ = ev.evaluate(run_dir, expected_chapters=expected)
    return report["criteria"]


def test_small_run_end_to_end_artifacts(small_run, tmp_path):
    out = tmp_path / "out"
    mock = MockLLM()
    result = lf.run_longform(small_run, out, complete=mock, max_words=800,
                             progress=lambda m: None)
    notes = result.summary_path.read_text()
    assert result.summary_path == out / "summaries" / "test_course.summary.md"
    fm, _ = ev.split_frontmatter(notes)
    assert str(fm["published"]) == "2026-07-20" and str(fm["as_of"]) == "2026-07-20"
    assert fm["transcript_source"] == "auto-captions" and fm["structure"] == "description"
    # one Course Map; one section per chapter with its range; parts as ###
    assert notes.count("## Course Map") == 1
    assert "## SDK Setup [0:05:00–0:17:30]" in notes
    assert "### Part 1/2 [" in notes and "### Part 2/2 [" in notes
    assert "| 2 | SDK Setup | [0:05:00–0:17:30] | Gist of SDK Setup. |" in notes
    # code-fence comment was not treated as a heading
    assert "# a comment that must not become a heading" in notes
    assert "## Introduction [" in notes and "### Summary" in notes
    # cross sections merged once, chapter bodies do not repeat them
    for heading in ("## Transcription Uncertainty", "## Time-Sensitive Claims",
                    "## Fact-Checked Notes"):
        assert notes.count(heading) == 1
    assert notes.count("Time-Sensitive Claims") == 1
    # P5: model left as-of blank -> filled from the published date, labelled
    assert "2026-07-20 (filled: published)" in notes
    # ledger stripped, transcript not appended
    assert "```ledger" not in notes and not re.search(r"^## Transcript\s*$", notes, re.M)
    # notes lint: "cloud code" from the mock was fixed and logged with notes:
    assert "cloud code" not in notes.lower()
    corr = json.loads((out / "corrections.json").read_text())
    notes_fixes = [c for c in corr if c["rule"].startswith("notes:")]
    assert notes_fixes and all(re.fullmatch(r"\d+:\d{2}:\d{2}", c["timestamp"]) for c in notes_fixes)
    assert any(c["rule"] == "claude-code" for c in corr)   # transcript fix logged too
    assert quality.lint_notes(notes, quality.build_glossary(SourceMeta.read(out))) == []
    # raw transcript copied unmodified, corrected copy alongside
    raw = small_run / "transcripts" / "test_course.captions.md"
    assert (out / "transcripts" / raw.name).read_bytes() == raw.read_bytes()
    assert (out / "transcripts" / "test_course.captions.corrected.md").exists()
    # evaluator
    crit = _eval(out)
    for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert crit[k]["pass"], (k, crit[k]["summary"], crit[k]["details"])


def test_course_map_falls_back_when_reply_collapses(small_run, tmp_path):
    out = tmp_path / "out"
    base = MockLLM()

    def chatty(system, user):
        if "Course Map" in system:
            return "This is excellent work! Would you like me to build the map?", "mock/map"
        return base(system, user)

    result = lf.run_longform(small_run, out, complete=chatty, max_words=800,
                             progress=lambda m: None)
    notes = result.summary_path.read_text()
    assert result.course_map_fallback
    assert "excellent work" not in notes
    # deterministic table: every chapter row present with the skeleton's range
    assert "| 2 | SDK Setup | [0:05:00–0:17:30] |" in notes
    assert "Passage 1" in notes.split("## Course Map")[1].split("## Introduction")[0]


# --- length gate ---------------------------------------------------------------------------


def test_length_gate_short_transcript_uses_generate_summary(monkeypatch, tmp_path):
    from video2mdnotes import main
    from video2mdnotes.core.transcriber import Segment, TranscriptResult

    seen = {}

    def fake_generate_summary(transcript):
        seen["called"] = transcript
        return "single-call result"

    def no_longform(*a, **k):
        raise AssertionError("long-form path must not run for a short transcript")

    monkeypatch.setattr(main, "generate_summary", fake_generate_summary)
    monkeypatch.setattr(lf, "run_longform", no_longform)
    tr = TranscriptResult(
        source_file=tmp_path / "a.wav", language="en",
        segments=[Segment(start=0, end=1, text="short words only")],
        full_text="short words only", markdown_content="x", model_name="m",
        generated_at=dt.datetime.now(),
    )
    assert main._summarize_transcript(tr, tmp_path) == "single-call result"
    assert seen["called"] is tr
    assert not lf.needs_longform(tr.segments)
    assert lf.needs_longform([Segment(start=0, end=1, text="w " * 12_001)])


def test_length_gate_long_transcript_routes_to_longform(monkeypatch, tmp_path):
    from video2mdnotes import main
    from video2mdnotes.core.transcriber import Segment, TranscriptResult

    monkeypatch.setattr(main, "generate_summary",
                        lambda t: (_ for _ in ()).throw(AssertionError("single call used")))
    monkeypatch.setattr(lf, "run_longform", lambda run_dir, **k: lf.LongformResult(
        summary_path=run_dir / "s.md", notes="LONGFORM NOTES", models=["mock"]))
    tr = TranscriptResult(
        source_file=tmp_path / "a.wav", language="en",
        segments=[Segment(start=0, end=1, text="w " * 12_001)],
        full_text="", markdown_content="x", model_name="m", generated_at=dt.datetime.now(),
    )
    result = main._summarize_transcript(tr, tmp_path)
    assert result.summary_text == "LONGFORM NOTES" and result.model_name == "mock"


def test_longform_is_a_registered_command():
    from video2mdnotes import main
    assert main._with_default_command(["longform", "x"]) == ["longform", "x"]


# --- real data: CCA-F --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cca_copy(tmp_path_factory):
    if CCA_RUN is None:
        pytest.skip("CCA-F run artifacts absent")
    dst = tmp_path_factory.mktemp("cca") / CCA_RUN_NAME
    (dst / "transcripts").mkdir(parents=True)
    raw = sorted(p for p in (CCA_RUN / "transcripts").glob("*.md") if "corrected" not in p.name)[0]
    shutil.copy2(raw, dst / "transcripts" / raw.name)
    for name in ("yt_info.json", "caption_source.txt", "original_url.txt"):
        if (CCA_RUN / name).exists():
            shutil.copy2(CCA_RUN / name, dst / name)
    return dst


def test_cca_plan_only_17_passages_zero_calls(cca_copy, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    def forbidden(*a, **k):
        raise AssertionError("--plan-only made a model call")

    monkeypatch.setattr(summarizer, "complete_with_failover", forbidden)
    before = sorted(p.name for p in cca_copy.rglob("*"))
    result = CliRunner().invoke(app, ["longform", str(cca_copy), "--plan-only"])
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "Passages: 17 in all, 3 chapter(s) split" in out
    assert "Chapters: 14 (13 + preamble)" in out
    assert "Estimated model calls: 17 passage + 4 course map = 21" in out
    assert sorted(p.name for p in cca_copy.rglob("*")) == before   # nothing written


def _subset_expected(tmp_path, titles):
    path = tmp_path / "expected_subset.txt"
    path.write_text("\n".join(f"- {t}" for t in titles) + "\n")
    return path


def test_cca_only_three_chapters_end_to_end(cca_copy, tmp_path):
    out = tmp_path / "pilot"
    mock = MockLLM()
    only = "1,Sessions management,7"
    result = lf.run_longform(cca_copy, out, only=only, complete=mock, progress=lambda m: None)
    passages = [u for k, u in mock.calls if k == "passage"]
    assert len(passages) == 4      # ch 1, ch 6, ch 7 (2 parts)
    part2 = next(u for u in passages if "part 2/2" in u)
    assert "RUNNING LEDGER" in part2 and lf.CONTEXT_ONLY_LABEL in part2
    notes = result.summary_path.read_text()
    assert "Partial run" in notes
    expected = _subset_expected(tmp_path, [
        "Introduction", "Sessions management context state",
        "Claude code configuration permissions safety"])
    crit = _eval(out, expected)
    for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert crit[k]["pass"], (k, crit[k]["summary"], crit[k]["details"])


def test_cca_full_13_chapters_end_to_end(cca_copy, tmp_path):
    out = tmp_path / "full"
    mock = MockLLM()
    result = lf.run_longform(cca_copy, out, complete=mock, concurrency=3,
                             progress=lambda m: None)
    kinds = [k for k, _ in mock.calls]
    # 3 course-map batches + ONE themes consolidation call (WP-F7)
    assert kinds.count("passage") == 17 and kinds.count("map") == 4
    themes_user = [u for k, u in mock.calls if k == "map"][-1]
    assert themes_user.startswith("Chapters (for reference only):")
    assert "### Summary" not in themes_user and "Candidate themes from the batches" in themes_user
    notes = result.summary_path.read_text()
    assert "Partial run" not in notes
    themes = notes.split("### Cross-chapter themes", 1)[1].split("\n## ", 1)[0]
    assert 1 <= themes.count("\n- ") <= lf.MAX_THEMES
    # Recording lag: "April 23rd" at 10:17:30 vs published 2026-07-20 (inference only)
    fm = notes.split("---", 2)[1]
    assert 'recorded_estimate: "2026-04 (inferred from spoken dates: \\"April 23rd\\" [10:17:30]' in fm
    assert "as_of: 2026-07-20" in fm
    title_block = notes.split("---", 2)[2].lstrip().split("\n")
    assert title_block[0].startswith("# ") and title_block[2].startswith(
        "> Material appears recorded ~April 2026; time-sensitive claims may predate the publish date.")
    crit = _eval(out, EXPECTED_CCA)
    for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert crit[k]["pass"], (k, crit[k]["summary"], crit[k]["details"])
    assert crit["P1"]["summary"].startswith("13/13")
    corr = json.loads((out / "corrections.json").read_text())
    assert sum(1 for c in corr if not c["rule"].startswith("notes:")) == 104
    # Under concurrency, each split chapter's part 2 still got ITS part 1 ledger.
    part2s = [u for k, u in mock.calls if k == "passage" and "part 2/2" in u]
    assert len(part2s) == 3
    for n in (4, 5, 7):
        saved = json.loads((out / lf.LEDGER_DIR / f"{n:02d}_p1.json").read_text())
        prompt = next(u for u in part2s if f"THIS PASSAGE: chapter {n} " in u)
        assert saved["last_point"] in prompt


def test_source_meta_json_carries_evaluator_aliases(small_run):
    raw = json.loads((small_run / "source_meta.json").read_text())
    assert raw["duration"] == 1250.0 and raw["duration_s"] == 1250.0
    assert raw["chapters"][1]["start_time"] == 300.0
    assert SourceMeta.read(small_run).duration_s == 1250.0


def test_correct_notes_fixes_known_forms_only():
    g = quality.glossary_for_domain("claude")
    md = "  - nested: open cloud code now [0:01:00]\n- deploy to the cloud provider [0:02:00]\n"
    fixed, findings = quality.correct_notes(md, g)
    assert fixed.startswith("  - nested: open Claude Code now")
    assert "cloud provider" in fixed and len(findings) == 1
    assert quality.lint_notes(fixed, g) == []
