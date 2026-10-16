"""WP-F1 wiring, WP-F2 outcome discipline, WP-F3 cosmetics (E1 P8 failure loop).

Prompt rules are asserted on the RENDERED prompt the model receives; the rest
on the artifacts (chunk notes, audit log, assembled notes, frontmatter)."""

import json
import re

from tests.test_longform import MockLLM, flat, passage_lines, small_run  # noqa: F401
from video2mdnotes.core import citations as cit
from video2mdnotes.core import longform as lf
from video2mdnotes.core import notes_format, quality
from video2mdnotes.eval import longform as ev


# --- WP-F1: audit runs before the checkpoint ------------------------------------------


class MisCitingLLM(MockLLM):
    """Restates the passage's first line in Special Attention with the LAST
    line's timestamp — the E1 defect. The first line names `jq`; nothing else
    in the passage does."""

    def _passage(self, user):
        out = super()._passage(user)
        lines = passage_lines(user)
        first, last = lines[0], lines[-1]
        if "intro line 0" in first[1]:
            out = out.replace(
                "### Special Attention\n- None",
                f"### Special Attention\n- Claude falls back to `jq` here [{last[0]}]")
        return out


def _jq_run(small_run):
    tpath = small_run / "transcripts" / "test_course.captions.md"
    text = tpath.read_text().replace("intro line 0 about agents", "intro line 0 about jq agents")
    tpath.write_text(text)
    return small_run


def test_checkpointed_chunk_notes_are_audited(small_run, tmp_path):
    run = _jq_run(small_run)
    out = tmp_path / "out"
    result = lf.run_longform(run, out, complete=MisCitingLLM(), max_words=800,
                             progress=lambda m: None)
    chunk = (out / lf.CHUNK_DIR / "01_introduction_p1.md").read_text()
    assert "Claude falls back to `jq` here [0:00:00]" in chunk      # retargeted on disk
    log = json.loads((out / cit.AUDIT_FILENAME).read_text())
    hits = [e for e in log if e["rule"] == "citation:retarget"]
    assert len(hits) == 1 and hits[0]["passage"] == "01_introduction_p1"
    assert hits[0]["original_ts"] == "[0:04:55]" and hits[0]["new_ts"] == "0:00:00"
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["passages"]["01_introduction_p1"]["citation_audit"]["retarget"] == 1
    notes = result.summary_path.read_text()
    assert "Claude falls back to `jq` here [0:00:00]" in notes
    assert "> Citation audit: 1 citation(s) retargeted" in notes
    # the mock's `termN` is never said in the transcript -> marked, listed
    assert cit.UNVERIFIED_MARK in notes
    assert state["assembly"]["citations_unverified"]
    crit, _ = ev.evaluate(out)
    for k in ("P2", "P6", "P7"):
        assert crit["criteria"][k]["pass"], (k, crit["criteria"][k]["summary"])


def test_reaudit_is_idempotent_on_checkpoints(small_run, tmp_path):
    run = _jq_run(small_run)
    out = tmp_path / "out"
    lf.run_longform(run, out, complete=MisCitingLLM(), max_words=800, progress=lambda m: None)
    before = {p.name: p.read_text() for p in (out / lf.CHUNK_DIR).glob("*.md")}
    entries = lf.reaudit_chunks(out, max_words=800)
    after = {p.name: p.read_text() for p in (out / lf.CHUNK_DIR).glob("*.md")}
    assert before == after
    assert not [e for e in entries if e["rule"] == "citation:retarget"]


def test_passage_prompt_has_citation_reuse_rule(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    text = flat(lf.render_passage_prompt(plan, plan.chapters[0][0]).text)
    assert "Restated items reuse the original citation" in text
    assert "Cite the caption line that STATES the claim" in text


# --- WP-F2: outcome discipline -----------------------------------------------------------


def test_passage_prompt_has_outcome_discipline(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    p1, p2 = plan.chapters[1]
    led = {"concepts": [{"term": "npx rule", "definition": "Bash(npx *) blocks npx",
                         "status": "expected"}],
           "claims": [{"claim": "restart needed to reload settings", "status": "superseded",
                       "ts": "[0:06:00]"}],
           "terms_and_corrections": [], "open_threads": [], "last_point": "x"}
    text = flat(lf.render_passage_prompt(plan, p2, ledger_in=led, prev=p1).text)
    assert "Outcome discipline" in text
    assert "Write \"fixed\", \"worked\", \"blocked\", \"took effect\" ONLY for an observed result" in text
    assert "expected to fix" in text
    assert ("the Summary, Special Attention, Exam-relevant points and Time-Sensitive Claims "
            "must state the FINAL observed outcome and mark the earlier statement as superseded") in text
    assert "Do not leave the correction only in Fact-Checked Notes" in text
    assert "starts its claim with `(superseded)`" in text
    # ledger schema in the prompt, and the carried ledger keeps its statuses
    assert '"status": "observed"' in text and '"claims"' in text
    assert '"status": "superseded"' in text and "never restate either as an observed fact" in text


def test_ledger_status_schema_and_backward_compat():
    new = lf.normalize_ledger({
        "concepts": [{"term": "a", "definition": "d", "status": "Observed"},
                     {"term": "b", "definition": "d", "status": "bogus"}],
        "claims": [{"claim": "Bash(npx *) fixed it", "status": "attempted", "ts": "[8:54:41]"},
                   {"claim": "restart needed", "status": "corrected", "superseded_by": "x"},
                   {"claim": "no status given"}, "bare string claim", {"claim": ""}],
    })
    assert new["concepts"] == [{"term": "a", "definition": "d", "status": "observed"},
                               {"term": "b", "definition": "d"}]
    assert [c["status"] for c in new["claims"]] == ["expected", "superseded", "expected", "expected"]
    assert new["claims"][0]["ts"] == "[8:54:41]" and new["claims"][1]["superseded_by"] == "x"
    for c in new["claims"]:
        assert c["status"] in lf.LEDGER_STATUSES
    # An E1-era ledger (no claims, no status) parses unchanged — no invented status.
    old = {"concepts": [{"term": "t", "definition": "d"}], "terms_and_corrections": ["x"],
           "open_threads": [], "last_point": "p"}
    norm = lf.normalize_ledger(old)
    assert norm["concepts"] == [{"term": "t", "definition": "d"}] and norm["claims"] == []
    assert lf.extract_ledger("```ledger\n" + json.dumps(old) + "\n```")[1] == norm


def test_cap_ledger_drops_superseded_claims_first():
    led = {"concepts": [{"term": f"t{i}", "definition": "word " * 10} for i in range(5)],
           "claims": [{"claim": "old wrong " * 30, "status": "superseded"},
                      {"claim": "kept outcome", "status": "observed"}],
           "last_point": "end"}
    capped, dropped = lf.cap_ledger(led, max_words=80)
    assert dropped >= 1 and all(c["status"] != "superseded" for c in capped["claims"])
    assert len(capped["concepts"]) == 5


def test_course_map_prompt_limits_themes_to_topics():
    system = lf.COURSE_MAP_PROMPT.read_text(encoding="utf-8")
    flat_system = flat(system)
    assert "A theme names a TOPIC, never a verdict or conclusion about it" in flat_system


# --- WP-F3 -----------------------------------------------------------------------------


def test_sanitize_cell():
    s = notes_format.sanitize_cell
    assert s("a | b") == "a \\| b"
    assert s("already \\| escaped") == "already \\| escaped"
    assert s("init__`, formatter") == "init__, formatter"
    assert s("`__init__`, formatter") == "`__init__`, formatter"
    assert s('Japanese for "version is …') == 'Japanese for "version is …"'
    assert s("“open quote") == "“open quote”"
    assert s("two\nlines") == "two lines"


def test_uncertainty_cells_keep_code_spans_and_quotes():
    md = ("## Transcription Uncertainty\n| timestamp | heard | probable | confidence |\n"
          "|---|---|---|---|\n"
          "| 0:37:16 | net formatter | `__init__`, formatter | medium |\n"
          "| 7:54:01 | bajing bajin wades | Japanese for \"version is …\" | low |\n"
          "| 0:01:00 | \"claw\" | `Claude` | high |\n")
    items = quality.parse_uncertainty_section(md)
    assert items[0]["probable"] == "`__init__`, formatter"
    assert items[1]["probable"] == 'Japanese for "version is …"'
    assert items[2]["heard"] == "claw" and items[2]["probable"] == "Claude"


def test_assembled_tables_are_sanitized(small_run, tmp_path):
    class Pipey(MockLLM):
        def _passage(self, user):
            out = super()._passage(user)
            ts = passage_lines(user)[0][0]
            return out.replace(
                "| claw | Claude | medium |",
                "| net formatter | `__init__`, formatter | medium |\n"
                f"\n- [{ts}] grep a|b -> `grep a|b` (high)")

    out = tmp_path / "out"
    result = lf.run_longform(small_run, out, complete=Pipey(), max_words=800,
                             progress=lambda m: None)
    notes = result.summary_path.read_text()
    section = notes.split("## Transcription Uncertainty", 1)[1].split("\n## ", 1)[0]
    assert "| `__init__`, formatter |" in section
    rows = [l for l in section.splitlines() if l.startswith("|")]
    assert any("grep a\\|b" in r for r in rows)
    for line in rows:
        assert line.count("`") % 2 == 0, line
        assert len(re.findall(r"(?<!\\)\|", line)) == 5, line


def test_chapters_total_excludes_preamble(small_run, tmp_path):
    out = tmp_path / "out"
    result = lf.run_longform(small_run, out, complete=MockLLM(), max_words=800,
                             progress=lambda m: None)
    fm, _ = ev.split_frontmatter(result.summary_path.read_text())
    assert fm["chapters_total"] == 3 and fm["chapters_included"] == 3


def test_chapters_total_with_a_preamble(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    pre = [lf.Passage(key="00_preamble_p1", chapter_no=0, title="Preamble", group_id="ch00",
                      part=1, parts=1, start_s=0, end_s=1, span=(0, 1), words=3)]
    plan.chapters = [pre] + plan.chapters
    plan.selected = list(plan.chapters)
    notes_by_key = {p.key: "### Summary\n- x [0:00:00]\n" for p in plan.passages}
    final, _ = lf.assemble(plan, notes_by_key, "## Course Map\n", [], "run")
    fm, _ = ev.split_frontmatter(final)
    assert fm["chapters_total"] == 3 and fm["chapters_included"] == 3
    assert fm["preamble_included"] is True


def test_fact_checked_wording_rule_in_prompt_and_output(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    text = flat(lf.render_passage_prompt(plan, plan.chapters[0][0]).text)
    assert 'write "per model knowledge (cutoff uncertain)"' in text
    assert 'Never write "correct as of <published date>"' in text
    assert "write \"correct as of <published date>; may have changed since\"" not in text

    fix = lf.fix_fact_checked_wording
    m = plan.meta
    assert fix("- The rename is correct as of 2026-07-20. It happened in 2025 [0:14:40]", m) == \
        "- The rename is correct per model knowledge (cutoff uncertain). It happened in 2025 [0:14:40]"
    assert fix("- `--fork-session`: correct as of 2026-07-20 according to my knowledge; "
               "may have changed since [7:14:25].", m) == \
        "- `--fork-session`: correct per model knowledge (cutoff uncertain) [7:14:25]."
    # a date that is not the published date is the speaker's, left alone
    assert fix("- correct as of 2025-01-01 per the slide", m) == "- correct as of 2025-01-01 per the slide"
