"""WP-F1: deterministic citation audit. Assertions are on the audited notes and
the audit log — the artifacts a reader and the P8 reviewer see."""

import json
import shutil
from pathlib import Path

import pytest

from video2mdnotes.core import citations as cit
from video2mdnotes.core import longform as lf

ROOT = Path(__file__).resolve().parents[1]


def _find_e1():
    for base in [ROOT, *ROOT.parents]:
        d = base / "previous_run_results" / "E1_cca_f_pilot"
        if (d / "chunk_summaries").is_dir() and (d / "transcripts").is_dir():
            return d
    return None


E1 = _find_e1()


def _passage():
    """A 10-minute passage. jq is demoed once (~1:00), Glob once (~3:00); the
    word 'permissions' is everywhere."""
    lines = []
    for k in range(0, 600, 5):
        lines.append((float(k), f"filler talk about permissions and settings number {k}"))
    put = {
        55: "can you use jq to return the hp field",
        60: "I don't have bash but I can read the file directly",
        180: "so I ask it for ls in the project",
        185: "it says it will use the glob tool instead",
        400: "now we look at the permission modes",
        520: "back to permissions and settings for a recap",
    }
    out = [(t, put.get(int(t), x)) for t, x in lines]
    return out


def test_retargets_restated_item_to_the_line_that_states_it():
    notes = ("### Summary\n- Claude read the file directly when jq was denied [0:01:00]\n\n"
             "### Special Attention\n- Reading the file directly instead of jq [0:08:40]\n"
             "  - Glob instead of `ls` [0:06:00].\n")
    audited, log = cit.audit_notes(notes, _passage())
    assert "- Claude read the file directly when jq was denied [0:01:00]" in audited
    assert "instead of jq [0:01:00]" in audited or "instead of jq [0:00:55]" in audited
    assert "Glob instead of `ls` [0:03:05]." in audited
    rules = sorted((e["rule"], e["original_ts"]) for e in log)
    assert rules == [("citation:retarget", "[0:06:00]"), ("citation:retarget", "[0:08:40]")]
    for e in log:
        assert e["new_ts"] and e["claim"] and e["terms"]
    # Re-auditing audited notes changes nothing.
    again, log2 = cit.audit_notes(audited, _passage())
    assert again == audited and log2 == []


def test_no_false_retarget_when_terms_are_at_the_cited_time():
    notes = "- Glob instead of `ls` [0:03:00]\n- The model used jq [0:00:58]\n"
    audited, log = cit.audit_notes(notes, _passage())
    assert audited == notes and log == []


def test_generic_bullet_is_never_touched():
    # No strong term: "permissions" and "settings" alone never justify a move.
    notes = "- He talks about permissions and settings again [0:08:40]\n"
    audited, log = cit.audit_notes(notes, _passage())
    assert audited == notes and log == []


def test_term_in_two_places_is_ambiguous_and_left_alone():
    lines = _passage()
    lines = [(t, "jq again here, read the file directly" if t == 450.0 else x) for t, x in lines]
    notes = "- Reading the file directly instead of jq [0:05:50]\n"
    # Cited 350 s; jq + "read the file directly" at both 55-60 s and 450 s.
    audited, log = cit.audit_notes(notes, lines)
    assert audited == notes
    assert [e["rule"] for e in log] == ["citation:ambiguous"] and log[0]["changed"] is False


def test_absent_everywhere_is_marked_unverified_once():
    notes = "- `spinnerTipsEnabled` hides the tips [0:04:00]\n"
    audited, log = cit.audit_notes(notes, _passage())
    assert audited.rstrip("\n").endswith("[0:04:00] " + cit.UNVERIFIED_MARK)
    assert [e["rule"] for e in log] == ["citation:unverified"]
    again, log2 = cit.audit_notes(audited, _passage())
    assert again == audited and log2 == []


def test_partly_spoken_identifier_is_not_unverified():
    # A key read from a slide: its words are said ("spinner tips") at the cite.
    lines = [(t, "you can override the spinner tips here" if t == 240.0 else x)
             for t, x in _passage()]
    notes = "- `spinnerTipsEnabled` - show tips in the spinner or not [0:04:00]\n"
    audited, log = cit.audit_notes(notes, lines)
    assert audited == notes and log == []


def test_range_citation_and_table_row():
    notes = ("### Time-Sensitive Claims\n| claim | timestamp | as-of | type | verify-by |\n"
             "|---|---|---|---|---|\n"
             "| Claude falls back to the glob tool when `ls` is denied | [0:07:00] | 2026-07-20 | feature | docs |\n"
             "\n### Summary\n- Glob instead of `ls` [0:02:50–0:03:10]\n")
    audited, log = cit.audit_notes(notes, _passage())
    assert "| [0:03:05] |" in audited                 # row retargeted
    assert "[0:02:50–0:03:10]" in audited             # range covers it: untouched
    assert [e["section"] for e in log] == ["Time-Sensitive Claims"]


def test_skipped_sections_and_code_fences():
    notes = ("### Transcription Uncertainty\n| timestamp | heard | probable | confidence |\n"
             "|---|---|---|---|\n| 0:07:00 | jay queue | `jq` | high |\n\n"
             "### Fact-Checked Notes\n- `cleanupPeriodDays` default is 30 [0:07:00]\n\n"
             "```\n- `jq` in a code block [0:07:00]\n```\n")
    audited, log = cit.audit_notes(notes, _passage())
    assert audited == notes and log == []


def test_extract_terms_strong_and_content():
    terms = {t.text: t.strong for t in cit.extract_terms(
        "When Read and Bash were denied, `jq` and --fork-session and settings.local.json "
        "took 30 days; Reading is fine")}
    assert terms["Read"] and terms["Bash"] and terms["jq"]
    assert terms["fork-session"] and terms["settings.local.json"] and terms["30 days"]
    assert "Reading" not in terms or not terms["Reading"]    # sentence word, not the tool


@pytest.mark.skipif(E1 is None, reason="E1 pilot results not present")
def test_e1_replay_retargets_the_two_reviewer_findings(tmp_path):
    run = tmp_path / "E1"
    shutil.copytree(E1, run)
    (run / cit.AUDIT_FILENAME).unlink(missing_ok=True)
    before = {p.name: p.read_text() for p in (run / "chunk_summaries").glob("*.md")}
    entries = lf.reaudit_chunks(run)
    changed = [e for e in entries if e["changed"]]
    by_claim = {e["claim"]: e for e in changed}
    jq = by_claim["Reading the file directly instead of jq ."]
    glob = by_claim["Glob instead of `ls` ."]
    assert jq["original_ts"] == "[8:09:43]" and glob["original_ts"] == "[8:04:58]"

    def secs(ts):
        h, m, s = map(int, ts.split(":"))
        return h * 3600 + m * 60 + s

    assert abs(secs(jq["new_ts"]) - secs("7:59:31")) <= 10
    assert abs(secs(glob["new_ts"]) - secs("7:58:24")) <= 10
    assert len(changed) == 2, [e["claim"][:60] for e in changed]   # precision on E1
    p1 = (run / "chunk_summaries" / "07_claude_code_configuration_permissions_sa_p1.md").read_text()
    assert f"instead of jq [{jq['new_ts']}]" in p1 and "[8:09:43]" not in p1
    assert f"Glob instead of `ls` [{glob['new_ts']}]" in p1
    for name, text in before.items():
        if not name.startswith("07_") and not name.startswith("_"):
            assert (run / "chunk_summaries" / name).read_text() == text
    log = json.loads((run / cit.AUDIT_FILENAME).read_text())
    assert sum(1 for e in log if e["rule"] == "citation:retarget") == 2
