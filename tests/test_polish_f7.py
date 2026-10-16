"""WP-F7 polish: citation commentary, course-map themes, time-sensitive
ephemera, recording-lag inference, attribution prompt rules. Assertions are on
rendered prompts and assembled artifacts (AGENTS.md), never exit codes alone."""

import datetime as dt
import json
import re

import pytest

from video2mdnotes.core import citations as cit
from video2mdnotes.core import longform as lf
from video2mdnotes.core import notes_format as nf
from video2mdnotes.core import spoken_dates as sd
from video2mdnotes.core import summarizer
from video2mdnotes.core.transcriber import Segment

from tests.test_longform import MockLLM, flat, small_run  # noqa: F401  (fixture)


def hms_s(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# --- 1. leaked repair text around citations ---------------------------------------

E2_LEAKS = [
    ("Follows every per-subagent log file of a run at once [5:55:13 is not valid — see [4:55:13]]",
     "Follows every per-subagent log file of a run at once [4:55:13]"),
    ("- Goal plus quality criteria beats rigid procedural steps, because procedural prompts "
     "break when a step fails [4:04:50 is not valid — see [5:04:50]] [5:05:13].",
     "- Goal plus quality criteria beats rigid procedural steps, because procedural prompts "
     "break when a step fails [5:04:50] [5:05:13]."),
    ("- `/init` creates `CLAUDE.md` but not a `.claude/` directory or settings file. Create "
     "those by hand [8:46:29]–not cited; see [7:46:29] [7:47:50].",
     "- `/init` creates `CLAUDE.md` but not a `.claude/` directory or settings file. Create "
     "those by hand [7:46:29] [7:47:50]."),
]
# Caption starts of the E2 transcript around those citations.
E2_CAPTIONS = [hms_s(t) for t in ("4:55:13.599", "5:04:50.637", "5:05:13.360",
                                  "5:55:14.240", "7:46:29.520", "7:47:50.477", "8:46:29.599")]


@pytest.mark.parametrize("leak,fixed", E2_LEAKS)
def test_e2_repair_commentary_collapses_to_the_valid_citation(leak, fixed):
    out, entries = cit.normalize_citation_commentary(leak, cit.caption_validator(E2_CAPTIONS))
    assert out == fixed
    assert len(entries) == 1 and entries[0]["rule"] == "citation:normalized"
    assert entries[0]["original"] in leak and entries[0]["new"] in out
    assert "not valid" not in out and "not cited" not in out and "see [" not in out


def test_commentary_keeps_original_when_the_suggested_target_is_not_a_caption():
    text = "- x [1:00:00 is not valid — see [2:00:00]]"
    out, entries = cit.normalize_citation_commentary(text, cit.caption_validator([3600.4]))
    assert out == "- x [1:00:00]" and entries[0]["new_ts"] == "1:00:00"


def test_ordinary_see_reference_and_ranges_are_untouched():
    text = "- ok [1:00:00] and [1:00:05–1:00:09] fine (see [1:00:10])"
    assert cit.normalize_citation_commentary(text) == (text, [])


def test_audit_logs_each_normalization_and_the_notes_are_clean():
    lines = [(t, "tail f logs run asterisk log per subagent") for t in E2_CAPTIONS]
    notes = "### Summary\n- " + E2_LEAKS[0][0] + "\n"
    audited, entries = cit.audit_notes(notes, lines)
    assert "[4:55:13]" in audited and "not valid" not in audited
    norm = [e for e in entries if e["rule"] == "citation:normalized"]
    assert norm and norm[0]["original"] == "[5:55:13 is not valid — see [4:55:13]]"


def test_passage_prompt_forbids_commentary_around_citations(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    system = flat(lf.render_passage_prompt(plan, plan.chapters[0][0]).system)
    assert "Never write commentary inside or around a citation" in system
    assert "[5:55:13 is not valid — see [4:55:13]]" in system   # shown as the thing never to write


def test_assembly_normalizes_old_chunks_and_records_it(small_run, tmp_path):
    out = tmp_path / "out"
    base = MockLLM()

    def leaky(system, user):
        reply, model = base(system, user)
        if "Course Map" not in system:
            m = re.search(r"^- Middle point: .*\[(\d+:\d{2}:\d{2})\]$", reply, re.M)
            reply = reply.replace(f"[{m.group(1)}]", f"[9:59:59 is not valid — see [{m.group(1)}]]", 1)
        return reply, model

    result = lf.run_longform(small_run, out, complete=leaky, max_words=800, progress=lambda m: None)
    notes = result.summary_path.read_text()
    assert "not valid" not in notes
    audit = json.loads((out / cit.AUDIT_FILENAME).read_text())
    assert sum(1 for e in audit if e["rule"] == "citation:normalized") == 4    # one per passage
    assert "model commentary collapsed to the valid citation" in notes


# --- 2. course map themes: one consolidation call, <= 8, no re-summarizing -----------

E2_CANDIDATES = [
    "Using `stop_reason` to drive and end the tool-use loop (ch. 2, 3)",
    "Anthropic API client vs the Claude Agent SDK, and when each fits (ch. 2, 3, 4)",
    "Defining custom tools as decorated functions exposed through an in-process SDK MCP server (ch. 2, 4)",
    "Checking AI-generated code and docs against hands-on runs (ch. 1, 2, 4)",
    "Using Claude Code features (plan mode, `/debug`, CLAUDE.md, model and effort choice) to build the demos (ch. 2, 3, 4)",
    "Logging and observability for agent runs (ch. 2, 4)",
    "Capping iterations and gating exits in agent loops (ch. 3, 4)",
    "Mapping the course to the exam guide's domains (ch. 1, 3, 4)",
    "Checking Claude-generated code and documentation claims with hands-on labs (ch. 5, 6, 7, 8)",
    "What the Agent SDK can do compared with the lower-level Anthropic SDK (ch. 5, 6, 8)",
    "Differences between the Python and TypeScript SDKs (ch. 5, 6)",
    "Prompt guidance compared with enforcement in code: gates, hooks, deny rules and the sandbox (ch. 5, 7)",
    "Isolated and parallel execution through subagents and forked sessions (ch. 5, 6)",
    "Built-in tools and controlling which tools an agent can use (ch. 5, 7, 8)",
    "Structured data with schemas, Pydantic models and structured findings (ch. 5, 8)",
    "Context management, isolation and token use (ch. 5, 6)",
    "Multi-agent explorer/synthesizer designs with scratchpad findings and manifest files (ch. 9, 10)",
    "Debugging and correcting code generated by Claude Code, and choosing between Sonnet and Opus (ch. 9, 10, 11, 13)",
    "Session usage limits and rate limits during demo runs (ch. 9, 10)",
    "Structured output via forced tool calls, and validating the output (ch. 9, 10)",
    "Human-in-the-loop workflows: review queues, confidence thresholds and escalation to humans (ch. 9, 13)",
    "Using the Agent SDK versus the low-level Anthropic SDK, with MCP servers and mocked tools (ch. 11, 13)",
    "Spelling out explicit, specific criteria in prompts and specs (ch. 9, 10, 13)",
    "Checking demo builds against exam-guide sections (ch. 9, 10, 11, 12, 13)",
]


def test_deterministic_merge_dedupes_e2_themes():
    themes = lf.merge_themes(E2_CANDIDATES)
    assert 1 <= len(themes) <= lf.MAX_THEMES
    sdk = [t for t in themes if re.search(r"Anthropic (API client|SDK)|low(er)?-level", t)]
    assert len(sdk) == 1, themes                      # was 3 separate themes in E2
    text, chs = lf.split_theme(sdk[0])
    assert set(chs) >= {"2", "3", "4", "5", "6", "8", "11", "13"}


def test_split_and_render_theme():
    assert lf.split_theme("Hooks vs prompts (ch. 5, 7)") == ("Hooks vs prompts", ["5", "7"])
    assert lf.render_theme("Hooks", ["7", "5", "10"]) == "Hooks (ch. 5, 7, 10)"


class ThemesMock(MockLLM):
    def __init__(self, themes_reply=None, fail=False):
        super().__init__()
        self.themes_reply, self.fail = themes_reply, fail
        self.systems = []

    def __call__(self, system, user):
        self.systems.append(system)
        if "Course Map themes" in system:
            self.calls.append(("themes", user))
            if self.fail:
                raise RuntimeError("themes down (test)")
            return self.themes_reply, "mock/themes"
        if "Course Map" in system:
            self.calls.append(("map", user))
            return self._map(user).replace(
                "- Agents recur (ch. 1, 2)",
                "- Agent SDK vs the Anthropic SDK (ch. 1, 2)\n- Testing hooks live (ch. 2, 3)"), "mock/map"
        return super().__call__(system, user)


def test_batched_map_makes_one_themes_call_over_candidates_only(small_run, tmp_path, monkeypatch):
    monkeypatch.setattr(summarizer, "_REDUCE_BATCH", 1)          # one chapter per batch
    mock = ThemesMock("### Cross-chapter themes\n\n- Agent SDK compared with the Anthropic SDK (ch. 1, 2, 3)\n"
                      "- Testing hooks live (ch. 2, 3)\n")
    result = lf.run_longform(small_run, tmp_path / "out", complete=mock, max_words=800,
                             progress=lambda m: None)
    kinds = [k for k, _ in mock.calls]
    assert kinds.count("map") == 3 and kinds.count("themes") == 1
    user = next(u for k, u in mock.calls if k == "themes")
    # Input is the candidate bullets and titles — never the chapter notes.
    assert "Agent SDK vs the Anthropic SDK (ch. 1, 2)" in user
    assert "### Summary" not in user and "Passage 1" not in user and "Key Concepts" not in user
    # The per-batch prompt asks for candidates; the themes prompt consolidates.
    batch_system = next(s for s in mock.systems if "Course Map" in s and "themes" not in s.split("\n")[0])
    assert "### Candidate themes" in batch_system
    notes = result.summary_path.read_text()
    block = notes.split("### Cross-chapter themes", 1)[1].split("\n## ", 1)[0]
    # Tagged on 3 of 3 chapters -> shown as "(all chapters)" (WP-F9 (m)).
    assert "- Agent SDK compared with the Anthropic SDK (all chapters)" in block
    assert block.count("Anthropic SDK") == 1
    assert result.course_map_calls == 4


def test_failed_themes_call_falls_back_to_deterministic_merge(small_run, tmp_path, monkeypatch):
    monkeypatch.setattr(summarizer, "_REDUCE_BATCH", 1)
    mock = ThemesMock(fail=True)
    out = tmp_path / "out"
    result = lf.run_longform(small_run, out, complete=mock, max_words=800, progress=lambda m: None)
    notes = result.summary_path.read_text()
    block = notes.split("### Cross-chapter themes", 1)[1].split("\n## ", 1)[0]
    bullets = [l for l in block.splitlines() if l.startswith("- ")]
    # 3 batches x 2 identical candidates -> deduplicated, chapters unioned
    assert bullets == ["- Agent SDK vs the Anthropic SDK (ch. 1, 2)", "- Testing hooks live (ch. 2, 3)"]
    assert any(f.startswith("themes: call failed") for f in result.course_map_fallback)
    assert "themes: merged candidates" in notes


def test_single_batch_makes_no_themes_call(small_run, tmp_path):
    mock = ThemesMock("unused")
    lf.run_longform(small_run, tmp_path / "out", complete=mock, max_words=800, progress=lambda m: None)
    assert [k for k, _ in mock.calls].count("themes") == 0


# --- 3. time-sensitive ephemera ------------------------------------------------------

E2_EPHEMERA = [
    "His subscription usage window had about 15 minutes left before it rolled over",
    'Usage limits reset on a short window (resets "in 9 minutes", 33% used) and peak usage may overlap European hours',
    'Sonnet "is not today working as expected" for searching',
    "Opus uses up subscription usage quickly; his usage limit reset was days away (April 23, 5 pm)",
    "The session hit 100% usage, though he has extra credits",
]
E2_KEEP = [
    "Subscription usage uses a five-hour window",
    'The settings shown are "all the settings as of today"',
    "Autocompact buffer reserves 22% of the context window",
    "Exam Pro support is 100% human at the time of the video",
    "Batch processing can take up to 24 hours with no guaranteed SLA",
    "After going over the usage limit, Claude Code showed \"now using extra credits\"",
]


def test_ephemera_filter_moves_session_state_and_keeps_policy():
    rows = [nf.ClaimRow(c, "[1:00:00]", "", "pricing", "x") for c in E2_EPHEMERA + E2_KEEP]
    kept, dropped = nf.filter_ephemeral(rows)
    assert [d["claim"] for d in dropped] == E2_EPHEMERA
    assert all(d["pattern"] for d in dropped)
    assert [r.claim for r in kept] == E2_KEEP


def test_limit_and_enum_types_map_to_feature():
    rows = [nf.ClaimRow("Autocompact reserves 22%", "[1:00:00]", "2026-07-20", "limit", "docs"),
            nf.ClaimRow("stop_reason values", "[1:00:00]", "2026-07-20", "enum", "docs"),
            nf.ClaimRow("50k ITPM", "[1:00:00]", "2026-07-20", "rate limit", "docs")]
    check = nf.validate_time_sensitive(rows, dt.date(2026, 7, 20))
    assert [r.type for r in check.rows] == ["feature", "feature", "pricing"]


def test_time_sensitive_prompt_excludes_ephemera_and_guides_types(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    system = flat(lf.render_passage_prompt(plan, plan.chapters[0][0]).system)
    assert "Do NOT include personal or ephemeral session state" in system
    assert '"resets in 9 minutes"' in system
    assert "Include it only when it states a product policy" in system
    assert "`feature` — what a product does or allows, including its limits, buffers" in system


def test_ephemeral_rows_are_dropped_from_notes_and_logged_in_state(small_run, tmp_path):
    out = tmp_path / "out"
    base = MockLLM()

    def ephemeral(system, user):
        reply, model = base(system, user)
        if "Course Map" not in system:
            m = re.search(r"\| Claim (\d+) about the exam \| (\[[^]]+\]) \|", reply)
            extra = f"| His usage resets in 9 minutes ({m.group(1)}) | {m.group(2)} |  | pricing | plans page |\n"
            reply = reply.replace(m.group(0), extra + m.group(0), 1)
        return reply, model

    result = lf.run_longform(small_run, out, complete=ephemeral, max_words=800, progress=lambda m: None)
    notes = result.summary_path.read_text()
    table = notes.split("## Time-Sensitive Claims", 1)[1].split("\n## ", 1)[0]
    assert "resets in 9 minutes" not in table and "Claim 1 about the exam" in table
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    dropped = state["assembly"]["time_sensitive_dropped"]
    assert len(dropped) == 4 and all(d["pattern"] == "resets-in" for d in dropped)
    assert dropped[0]["claim"].startswith("His usage resets in 9 minutes")


# --- 4. recording lag ----------------------------------------------------------------


def segs(*lines):
    return [Segment(start=float(t), end=float(t) + 2, text=x) for t, x in lines]


def test_e2_spoken_date_infers_april_recording():
    s = segs((37048, "that. So 70% and the next time will be"),
             (37050, "April 23rd 5:00 pm. Um so what's today"),
             (37055, "date? Today"),
             (4432, "Mars in"), (4433, "2026. And so the idea there is that it's"),
             (34667, "exam guide is still dated from 2025."))
    est = sd.estimate_recording(s, dt.date(2026, 7, 20), "published")
    assert est is not None and est.month == "2026-04"
    assert est.frontmatter_value().startswith('2026-04 (inferred from spoken dates: "April 23rd" [10:17:30]')
    assert est.note_line().startswith(
        "> Material appears recorded ~April 2026; time-sensitive claims may predate the publish date.")
    assert "Inference" in est.note_line()


def test_date_split_across_caption_lines_is_found():
    s = segs((10, "as of today the reset is on April"), (12, "the 2nd at noon"))
    assert [m.text for m in sd.scan(s, dt.date(2026, 7, 20))] == ["April the 2nd"]


def test_e3_historical_years_give_no_estimate():
    s = segs((145, "this group actually started in 2010."),
             (2119, "surprising in 2026, but maybe in 2028"),
             (4478, "I joined OpenAI in 2019"),
             (6071, "In October of 2024, so top left corner"),
             (6081, "in February of 25, they changed their update to be 2027."),
             (332, "Just for being here tonight"))
    assert sd.estimate_recording(s, dt.date(2026, 10, 15), "captured") is None


def test_unanchored_mentions_are_never_enough():
    # WP-F9 (k): the old ">= 2 clustered unanchored" path is gone; history
    # told twice is not a recording date.
    one = segs((10, "the conference on March 3rd was fun"))
    assert sd.estimate_recording(one, dt.date(2026, 7, 20)) is None
    two = one + segs((900, "and on March 10th they shipped it"))
    assert sd.estimate_recording(two, dt.date(2026, 7, 20)) is None
    anchored = two + segs((950, "and right now, March 12th, we are recording this"))
    est = sd.estimate_recording(anchored, dt.date(2026, 7, 20))
    assert est is not None and est.month == "2026-03"


def test_recent_dates_give_no_estimate():
    s = segs((10, "today is July 15th"))
    assert sd.estimate_recording(s, dt.date(2026, 7, 20)) is None


def test_modal_may_is_not_a_date():
    s = segs((10, "you may 2 times retry today"))
    assert sd.scan(s, dt.date(2026, 7, 20)) == []


def test_assembled_notes_carry_estimate_and_keep_as_of(small_run, tmp_path):
    t = small_run / "transcripts" / "test_course.captions.md"
    t.write_text(t.read_text().replace("intro line 3 about agents and tools here",
                                       "as of today, April 2nd, the beta is open"))
    result = lf.run_longform(small_run, tmp_path / "out", complete=MockLLM(), max_words=800,
                             progress=lambda m: None)
    notes = result.summary_path.read_text()
    fm = notes.split("---", 2)[1]
    assert 'recorded_estimate: "2026-04 (inferred from spoken dates: \\"April 2nd\\" [0:00:15]' in fm
    assert "as_of: 2026-07-20" in fm and 'as_of_basis: "published"' in fm
    body = notes.split("---", 2)[2].lstrip().split("\n")
    assert body[0].startswith("# ") and body[2].startswith("> Material appears recorded ~April 2026;")
    table = notes.split("## Time-Sensitive Claims", 1)[1]
    assert "| 2026-07-20 (filled: published) |" in table          # P5 semantics unchanged
    state = json.loads((tmp_path / "out" / lf.STATE_FILENAME).read_text())
    assert state["assembly"]["recorded_estimate"]["month"] == "2026-04"


def test_no_dates_no_estimate_in_artifact(small_run, tmp_path):
    result = lf.run_longform(small_run, tmp_path / "out", complete=MockLLM(), max_words=800,
                             progress=lambda m: None)
    notes = result.summary_path.read_text()
    assert "recorded_estimate" not in notes and "Material appears recorded" not in notes


# --- 5. attribution rules in the passage prompt ------------------------------------------


def test_passage_prompt_carries_attribution_rules(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    system = flat(lf.render_passage_prompt(plan, plan.chapters[1][1]).system)
    assert "label it `Check:`" in system and 'Never attribute it to the speaker ("his math check"' in system
    assert "Definitions do not promise more than was said or shown" in system
    assert '"proposes without executing"' in system
    assert '"reported by Claude" / "reported by the speaker"' in system
