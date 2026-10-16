"""WP-F8: cross-section consistency, quantity attribution, ASR name hedging,
relative event dates, fuzzy citation audit, the Check: rule and theme tagging.

Every model is mocked. Assertions are on rendered prompts and on the
assembled artifacts (notes, state file, caches) — never on exit codes alone."""

import datetime as dt
import json
import re

import pytest

from video2mdnotes.core import citations as cit
from video2mdnotes.core import consistency as cons
from video2mdnotes.core import longform as lf
from video2mdnotes.core import names
from video2mdnotes.core import outline as ol
from video2mdnotes.core import quality
from video2mdnotes.core import spoken_dates as sd
from video2mdnotes.core.metadata import SourceMeta
from video2mdnotes.core.transcriber import Segment

from tests.test_longform import MockLLM, flat, small_run  # noqa: F401  (fixture)
from tests.test_outline import GOOD_REPLY, Recorder, _meetup_lines, _write_run


def segs(*lines):
    return [Segment(start=float(t), end=float(t) + 2, text=x) for t, x in lines]


# --- 1. cross-section consistency ---------------------------------------------------------

CH1 = """### Summary
- The giveaway draw is postponed; the winner will be announced Wednesday [0:01:50] [0:02:00].
- The group has 5,000 members [0:02:39].

### Key Concepts & Vocabulary
- `AI Ready Ohio` - a statewide program [0:05:24]

### Special Attention
- The giveaway was not drawn at the event [0:01:53].

### Transcription Uncertainty
| timestamp | heard | probable | confidence |
|---|---|---|---|
| 0:01:01 | pod pro | Pro | low |

### Time-Sensitive Claims
| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
| The giveaway winner will be announced Wednesday | [0:02:00] | 2026-09-06 | news | Meetup email |

### Fact-Checked Notes
- None
"""
CH2 = """### Summary
- Morgan said about 80% of AI pilots stall before production [0:07:50].
- The speaker explains guardrails in depth [0:09:00].
"""
CH3 = """### Summary
- Jason referenced the 95% failure figure Morgan mentioned earlier [0:51:24].
- He announced the giveaway winner, picked at random from 112 submissions [1:12:21] [1:12:26].
"""


def _sections():
    return [cons.Section("1", "Welcome (Zachary Hune)", "0:00:00–0:06:36", [("k1", CH1)]),
            cons.Section("2", "Production agents", "0:06:36–0:21:47", [("k2", CH2)]),
            cons.Section("3", "Intermission", "0:58:36–1:12:58", [("k3", CH3)])]


def _ref(statements, needle):
    return next(s.ref for s in statements if needle in s.text)


def test_extracted_statements_are_outcomes_numbers_people_with_citations():
    sts = cons.extract_statements(_sections())
    texts = [s.text for s in sts]
    assert any("announced Wednesday" in t for t in texts)
    assert any("not drawn at the event" in t for t in texts)
    assert any("112 submissions" in t for t in texts)
    assert any("80%" in t for t in texts) and any("95%" in t for t in texts)
    # skipped: Key Concepts, Transcription Uncertainty, generic bullets
    assert not any("statewide program" in t or "pod pro" in t for t in texts)
    assert not any("guardrails in depth" in t for t in texts)
    ts_row = next(s for s in sts if s.is_time_sensitive)
    assert ts_row.kind == "row" and ts_row.cites == ["[0:02:00]"]
    assert [s.ref for s in sts if s.section == 1][0] == "S1.1"


def test_consistency_prompt_carries_statements_sections_and_quote_rule():
    secs = _sections()
    sts = cons.extract_statements(secs)
    system, user = cons.render_prompt(secs, sts)
    assert "JSON array" in system and "later_ref" in system
    assert "QUOTING another speaker's figure" in system
    assert 'S3: ch. 3 "Intermission" [0:58:36–1:12:58]' in user
    assert f"[{_ref(sts, '112 submissions')}] S3 · Summary · He announced the giveaway winner" in user
    assert "[0:02:00]" in user
    # compact: never the full notes
    assert "Transcription Uncertainty" not in user and "statewide" not in user


def _contradiction_reply(sts):
    later = _ref(sts, "112 submissions")
    pairs = [{"earlier_ref": _ref(sts, n), "later_ref": later, "kind": "superseded",
              "resolution": "The winner was drawn at the event from 112 submissions."}
             for n in ("announced Wednesday", "not drawn at the event")]
    pairs.append({"earlier_ref": _ref(sts, "giveaway winner will be announced Wednesday"),
                  "later_ref": later, "kind": "superseded", "resolution": "Drawn at the event."})
    pairs.append({"earlier_ref": _ref(sts, "80% of AI pilots"),
                  "later_ref": _ref(sts, "95% failure figure"), "kind": "discrepancy",
                  "resolution": "Jason quotes Morgan as 95%; Morgan said about 80%."})
    pairs.append({"earlier_ref": "S9.9", "later_ref": later, "kind": "superseded",
                  "resolution": "invented"})
    return json.dumps(pairs)


def test_apply_annotates_superseded_and_discrepancy_only(tmp_path):
    secs = _sections()
    sts = cons.extract_statements(secs)
    calls = []

    def model(system, user):
        calls.append(user)
        return _contradiction_reply(sts), "mock/consistency"

    notes = {"k1": CH1, "k2": CH2, "k3": CH3}
    res = cons.check(secs, notes, tmp_path, model)
    out = res.notes_by_key
    assert res.status == "applied" and len(calls) == 1
    pointer = '_(superseded: see [1:12:21] in ch. 3 "Intermission")_'
    # WP-F9: the false clause leads these bullets, so "(superseded)" comes first.
    assert ("- (superseded) The giveaway draw is postponed; the winner will be announced "
            f"Wednesday [0:01:50] [0:02:00]. {pointer}") in out["k1"]
    assert f"- (superseded) The giveaway was not drawn at the event [0:01:53]. {pointer}" in out["k1"]
    assert (f"| (superseded) The giveaway winner will be announced Wednesday {pointer} | [0:02:00] |"
            in out["k1"])
    # discrepancy: the LATER (quoting) statement is annotated, not Morgan's 80%
    assert ('[0:51:24]. _(discrepancy: see [0:07:50] in ch. 2 "Production agents")_'
            in out["k3"])
    assert "_(" not in out["k2"]
    # annotation only: removing the annotations restores the input exactly
    for k in ("k1", "k3"):
        removed = re.sub(r" _\((?:superseded|discrepancy): see [^\n]*?\"\)_", "", out[k])
        removed = removed.replace("(superseded) ", "")
        assert [l.strip() for l in removed.split("\n")] == [l.strip() for l in notes[k].split("\n")]
    # every pair logged; the invented ref rejected
    log = res.log
    assert sum(e.get("applied") is True for e in log) == 4
    assert any(e.get("reason") == "unknown reference id" for e in log)
    # cached: a rerun does not call the model
    again = cons.check(secs, notes, tmp_path, model)
    assert again.cached and len(calls) == 1 and again.notes_by_key == out
    cache = json.loads((tmp_path / cons.CACHE_FILENAME).read_text())
    assert cache["model"] == "mock/consistency" and cache["statements"]


def test_consistency_skips_one_section_and_survives_a_failed_call(tmp_path):
    secs = _sections()
    one = cons.check(secs[:1], {"k1": CH1}, tmp_path, lambda s, u: 1 / 0)
    assert one.status == "skipped" and one.notes_by_key == {"k1": CH1}
    assert not (tmp_path / cons.CACHE_FILENAME).exists()

    def boom(system, user):
        raise RuntimeError("every configured provider failed")

    notes = {"k1": CH1, "k2": CH2, "k3": CH3}
    res = cons.check(secs, notes, tmp_path, boom)
    assert res.status == "failed" and "every configured provider failed" in res.reason
    assert res.notes_by_key == notes
    bad = cons.check(secs, notes, tmp_path / "b", lambda s, u: ("I found none!", "m"))
    assert bad.status == "failed" and "no JSON array" in bad.reason


class ConsistencyLLM(MockLLM):
    """Passage notes say "will be announced Wednesday" in chapter 1 and "was
    drawn at the event" in chapter 3; the consistency call links them."""

    def _passage(self, user):
        out = super()._passage(user)
        lines = __import__("tests.test_longform", fromlist=["passage_lines"]).passage_lines(user)
        ts = lines[1][0]
        if '"Introduction"' in user:
            extra = f"- The giveaway winner will be announced Wednesday [{ts}]\n"
        elif '"Wrap Up"' in user:
            extra = f"- The giveaway winner was drawn at the event [{ts}]\n"
        else:
            return out
        return out.replace("### Key Concepts", extra + "\n### Key Concepts", 1)

    def __call__(self, system, user):
        if "cross-section consistency" in system:
            self.calls.append(("consistency", user))
            e = re.search(r"^\[(S\d+\.\d+)\] S1 · Summary · The giveaway winner will", user, re.M)
            l = re.search(r"^\[(S\d+\.\d+)\] S\d+ · Summary · The giveaway winner was drawn", user, re.M)
            return json.dumps([{"earlier_ref": e.group(1), "later_ref": l.group(1),
                                "kind": "superseded", "resolution": "Drawn at the event."}]), "mock/c"
        return super().__call__(system, user)


def test_run_longform_annotates_and_logs_to_state(small_run, tmp_path):
    out = tmp_path / "out"
    mock = ConsistencyLLM()
    result = lf.run_longform(small_run, out, complete=mock, max_words=800, progress=lambda m: None)
    notes = result.summary_path.read_text()
    kinds = [k for k, _ in mock.calls]
    assert kinds.count("consistency") == 1
    intro = notes.split("## Introduction [", 1)[1].split("\n## ", 1)[0]
    assert re.search(r"announced Wednesday \[\d:\d\d:\d\d\] _\(superseded: see \[\d:\d\d:\d\d\] "
                     r'in ch\. 3 "Wrap Up"\)_', intro)
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["consistency"]["status"] == "applied" and state["consistency"]["applied"] == 1
    assert state["consistency"]["contradictions"][0]["later"]["title"] == "Wrap Up"
    assert state["assembly"]["consistency"]["applied"] == 1
    assert (out / cons.CACHE_FILENAME).exists()
    # checkpoints are not rewritten: annotation lives in the assembled notes only
    assert "_(superseded" not in (out / lf.CHUNK_DIR / "01_introduction_p1.md").read_text()
    # rerun: everything from checkpoints and caches, no call at all
    mock2 = ConsistencyLLM()
    lf.run_longform(small_run, out, complete=mock2, max_words=800, progress=lambda m: None)
    assert mock2.calls == []


# --- 2 + 6. prompt rules: quoted figures, Check:, ambiguous numbers --------------------------


def test_passage_prompt_has_quote_attribution_check_and_ambiguity_rules(small_run):
    plan = lf.build_plan(small_run, max_words=800, write=False)
    text = flat(lf.render_passage_prompt(plan, plan.passages[0]).text)
    assert "A quoted figure belongs to the quoter." in text
    assert '"Jason cited Morgan as saying 95%", never "Morgan\'s figure was 95%"' in text
    assert "note the discrepancy" in text
    assert "Correct a speaker's number only when the meaning is unambiguous." in text
    assert "present both readings" in text and "may mean watching at 2× speed" in text
    assert "EVERY computation the notes make carries `Check:`" in text
    assert "including Special Attention" in text
    # the Special Attention section instruction itself carries the rule
    assert "`### Special Attention` —" in text and "labelled `Check:` here too" in text


def test_check_rule_lint_flags_unlabelled_arithmetic():
    notes = ("- 43 of 60 is 71.7% [0:07:03]. Check: 43 / 60 = 71.7%\n"
             "- Two times twelve hours: 2 × 12 = 24 hours [0:03:12]\n"
             "- The exam has 60 questions [0:07:00]\n")
    found = lf.check_rule_findings(notes)
    assert len(found) == 1 and "2 × 12 = 24" in found[0]


# --- 3. ASR names -------------------------------------------------------------------------------


def test_outline_prompt_separates_host_sponsor_and_asks_for_variants():
    system = ol.OUTLINE_PROMPT.read_text()
    assert "speaks for MOST of the section" in system
    assert "host / organizer / MC apart from sponsor and guest speakers" in system
    assert "spelling heard MOST" in system and "speaker_variants" in system
    entries, _ = ol.parse_outline(json.dumps([
        {"start": "0:00:00", "title": "Welcome", "speaker": None, "role": "host"},
        {"start": "0:28:02", "title": "Nutanix", "speaker": "Harsha", "role": "speaker",
         "speaker_variants": ["Archer", "Harsha"]}]))
    assert entries[1].role == "speaker" and entries[1].speaker_variants == ["Archer"]
    old, _ = ol.parse_outline(GOOD_REPLY)            # cached replies keep working
    assert old[1].speaker == "Alice Example" and old[1].speaker_variants == []


def test_mishearing_hint_forbids_name_respelling():
    text = flat(lf.mishearing_hint(quality.TranscriptProvenance("asr", "parakeet", "m")))
    assert "never \"correct\" it toward another spelling that sounds the same" in text
    assert "unless that spelling appears in the source title, description or tags" in text


def test_hedge_title_only_for_unconfirmed_outline_speakers():
    sp = ["Harsha", "Alice Example"]
    assert names.hedge_title("Nutanix platforms (Harsha)", sp, set()) == \
        "Nutanix platforms — Harsha (ASR spelling)"
    assert names.hedge_title("Nutanix platforms (Harsha)", sp, {"harsha"}) == \
        "Nutanix platforms (Harsha)"
    assert names.hedge_title("Q&A (audience)", sp, set()) == "Q&A (audience)"


def test_name_respelling_rule():
    vocab = names.TranscriptVocab.build(segs((1, "Curtis O'Neill handles tech and when we"),
                                             (2, "Tim Nash does content"),
                                             (3, "Gardner says")))
    assert names.is_name_respelling("Curtis O'Neill", "Curtis O'Neal", vocab, set())
    # the metadata names the person: the spelling is settled, the fix applies
    assert not names.is_name_respelling("Curtis O'Neill", "Curtis O'Neal", vocab,
                                        names.metadata_words({"title": "Talk by Curtis O'Neal"}))
    assert not names.is_name_respelling("When Tim Nash", "And Tim Nash", vocab, set())
    assert not names.is_name_respelling("Gardner", "Gartner", vocab, set())
    assert not names.is_name_respelling("Curtis O'Neill", "Curtis Smith", vocab, set())


def _asr_outline_run(tmp_path):
    lines = _meetup_lines()
    lines = [(t, "Hi everyone, my name is Alice Exampel and I build agents." if t == 305 else x)
             for t, x in lines]
    lines = [(t, "Thanks to Bob Stone and Bob Stone for hosting." if t == 10 else x)
             for t, x in lines]
    run = _write_run(tmp_path, lines)
    reply = json.dumps([
        {"start": "0:00:00", "title": "Welcome and sponsors", "speaker": None},
        {"start": "0:05:20", "title": "Building agents that ship", "speaker": "Alice Example",
         "role": "speaker", "speaker_variants": ["Alice Exampel"]},
        {"start": "0:29:40", "title": "Networking for AI clusters", "speaker": None},
        {"start": "0:54:45", "title": "Audience Q&A", "speaker": None}])
    lf.build_plan(run, write=False, outline_complete=Recorder(reply))   # caches the outline
    return run


class NamesLLM(MockLLM):
    def _passage(self, user):
        out = super()._passage(user)
        out = out.replace("| claw | Claude | medium |",
                          "| claw | Claude | medium |\n| 0:00:10 | Bob Stone | Bob Stoane | medium |")
        return out.replace("### Key Concepts",
                           "- Bob Stone thanked the hosts [0:00:10]\n\n### Key Concepts", 1)


def test_asr_names_are_hedged_in_headings_course_map_and_listed(tmp_path):
    run = _asr_outline_run(tmp_path)
    result = lf.run_longform(run, tmp_path / "out", complete=NamesLLM(), progress=lambda m: None)
    notes = result.summary_path.read_text()
    title = "Building agents that ship — Alice Example (ASR spelling)"
    assert f"## {title} [" in notes                                # section heading
    course_map = notes.split("## Course Map", 1)[1].split("\n## ", 1)[0]
    assert f"| {title} |" in course_map                            # Course Map row
    assert not [l for l in notes.splitlines() if l.startswith("#") and "(Alice Example)" in l]
    assert not [l for l in course_map.splitlines() if l.startswith("| 2 | Building agents that ship (")]
    unc = notes.split("## Transcription Uncertainty", 1)[1].split("\n## ", 1)[0]
    names_md = unc.split("### Names (spelling unverified)", 1)[1]
    assert "| Alice Example | Example ×1, Exampel ×1, Alice ×2 | outline speaker; variants given: Alice Exampel |" \
        in names_md
    # the passage's "Bob Stone -> Bob Stoan" respelling is not applied as a correction
    assert "Bob Stoane" not in unc.split("### Names", 1)[0]
    assert '"Bob Stone" -> "Bob Stoane" suggested by the notes but not applied' in names_md
    assert "| Bob Stone | Stone ×2" in names_md
    state = json.loads((tmp_path / "out" / lf.STATE_FILENAME).read_text())
    assert state["assembly"]["uncertainty"]["name_respellings_not_applied"] == [
        {"heard": "Bob Stone", "probable": "Bob Stoane"}]
    # passage keys (checkpoints) keep the plain title
    assert any(p.name.startswith("02_building_agents_that_ship") for p in
               (tmp_path / "out" / lf.CHUNK_DIR).glob("*.md"))


def test_manual_captions_are_not_hedged(tmp_path):
    run = _asr_outline_run(tmp_path)
    t = next((run / "transcripts").glob("*.md"))
    t.write_text(t.read_text().replace("model: asr:test", "model: manual-captions"))
    plan = lf.build_plan(run, write=False)
    if plan.provenance is None or plan.provenance.kind != "manual-captions":
        pytest.skip("provenance not inferred as manual from this fixture")
    assert lf.display_title(plan, plan.chapters[1][0].title) == plan.chapters[1][0].title


# --- 4. event date from spoken relative dates ---------------------------------------------------


def test_e3b_relative_anchor_gives_september_october_2026():
    s = segs((6090, "In May of 26, so just like five months ago or four months ago, uh,"),
             (6095, "they realized, you know what?"),
             (2033, "So a few months ago, we opened our own agent software"),
             (145, "this group actually started in 2010."))
    est = sd.estimate_event(s, dt.date(2026, 10, 15), "captured")
    assert est is not None and est.kind == "relative"
    assert (est.month, est.month_end) == ("2026-09", "2026-10")
    assert est.frontmatter_value().startswith(
        '2026-09/2026-10 (inferred from spoken relative dates: "May of 26, so just like five '
        'months ago or four months ago" [1:41:30])')
    assert est.note_line().startswith("> Event date (inference): ~September–October 2026")
    assert "`as_of` is unchanged" in est.note_line()


def test_relative_offsets_last_month_and_weeks():
    s = segs((10, "back in August of 26, last month, we shipped it"))
    ev = sd.relative_evidence(s, dt.date(2026, 10, 15))
    assert ev and ev[0].implied == [dt.date(2026, 9, 15)]
    s2 = segs((10, "on March 3rd, two weeks ago, the beta opened"))
    ev2 = sd.relative_evidence(s2, dt.date(2026, 7, 20))
    assert ev2[0].implied == [dt.date(2026, 3, 17)]
    # "May 26" is a day, not a year; modal "may" is no date
    assert [m.text for m in sd.scan(segs((1, "on May 26 we met")), dt.date(2026, 7, 20))] == ["May 26"]
    assert sd.scan(segs((1, "you may of course retry")), dt.date(2026, 7, 20)) == []


def test_relative_estimate_with_known_publish_date_needs_a_lag():
    s = segs((10, "In June of 26, so about a month ago, they launched it"))
    assert sd.estimate_from_relative(s, dt.date(2026, 7, 20), "published") is None
    s2 = segs((10, "In January of 26, so a month ago, they launched it"))
    est = sd.estimate_event(s2, dt.date(2026, 7, 20), "published")
    assert est is not None and est.kind == "relative" and est.month == "2026-02"


def test_published_unknown_run_shows_event_line_and_keeps_as_of(tmp_path):
    run = _asr_outline_run(tmp_path)
    t = next((run / "transcripts").glob("*.md"))
    t.write_text(t.read_text().replace(
        "network talk line 2400 covering fabrics optics and rails",
        "In August of 26, so like two months ago, we built the fabric"))
    result = lf.run_longform(run, tmp_path / "out", complete=NamesLLM(), progress=lambda m: None)
    notes = result.summary_path.read_text()
    fm = notes.split("---", 2)[1]
    assert 'recorded_estimate: "2026-10 (inferred from spoken relative dates: ' in fm
    assert "as_of: 2026-10-15" in fm and 'as_of_basis: "captured"' in fm
    body = notes.split("---", 2)[2].lstrip().split("\n")
    assert body[0].startswith("# ") and body[2].startswith("> Event date (inference): ~October 2026")


# --- 5. fuzzy citation audit ---------------------------------------------------------------------


def _lines(put, n=120, filler="filler talk about sessions number {t}"):
    return [(float(t), put.get(t, filler.format(t=t))) for t in range(0, n * 5, 5)]


# E2b 6:59:52 — captions say "get ignore"; the notes' other words are said
# elsewhere in the passage, so the exact audit marked it unverified.
GITIGNORE = {55: "sessions. Also just going to go here and", 60: "add a get ignore. These node modules are",
             65: "always really giant. Uh get ignore or", 70: "sorry node modules.",
             75: "Okay. Want to ignore that big big folder",
             400: "keep a large entry for the dependency list", 450: "keep a large entry dependency",
             500: "a large entry to keep for each dependency"}
# E2b 10:12:06 — "use opus. It burns up my credits"; "opus"/"plan" are said
# all through the passage, "consumes faster usage" only elsewhere.
OPUS = {195: "what it thinks. I'm trying to avoid to", 200: "use opus. It burns up my credits like",
        205: "crazy. and so it goes into plan mode",
        450: "consumes faster usage", 480: "consumes faster usage", 510: "consumes faster usage"}
OPUS_FILLER = "filler talk about opus and the plan number {t}"


def test_misheard_terms_are_not_marked_unverified():
    gi = "- `.gitignore` entry to keep the large dependency folder out of git [0:01:00]\n"
    audited, log = cit.audit_notes(gi, _lines(GITIGNORE))
    assert audited == gi
    assert [e["rule"] for e in log] == ["citation:fuzzy_ok"] and log[0]["changed"] is False
    assert log[0]["fuzzy"]["strong_matched"] == [".gitignore", "git"]
    row = ("| claim | timestamp |\n|---|---|\n"
           "| Opus consumes plan usage much faster than Sonnet/Haiku (speaker's statement) | [0:03:20] |\n")
    audited, log = cit.audit_notes(row, _lines(OPUS, filler=OPUS_FILLER))
    assert audited == row
    assert [e["rule"] for e in log] == ["citation:fuzzy_ok"]
    assert set(log[0]["fuzzy"]["content_matched"]) == {"opus", "plan", "usage"}
    # without the fuzzy step both would have been marked (the E2b defect)
    import video2mdnotes.core.citations as c2
    orig = c2.fuzzy_support
    try:
        c2.fuzzy_support = lambda *a: None
        assert cit.UNVERIFIED_MARK in cit.audit_notes(gi, _lines(GITIGNORE))[0]
        assert cit.UNVERIFIED_MARK in cit.audit_notes(row, _lines(OPUS, filler=OPUS_FILLER))[0]
    finally:
        c2.fuzzy_support = orig


def test_earlier_unverified_mark_is_cleared_and_logged():
    notes = ("- `.gitignore` entry to keep the large dependency folder out of git [0:01:00] "
             + cit.UNVERIFIED_MARK + "\n")
    audited, log = cit.audit_notes(notes, _lines(GITIGNORE))
    assert audited == "- `.gitignore` entry to keep the large dependency folder out of git [0:01:00]\n"
    assert [e["rule"] for e in log] == ["citation:unmarked"] and log[0]["changed"] is True
    again, log2 = cit.audit_notes(audited, _lines(GITIGNORE))
    assert again == audited and [e["rule"] for e in log2] == ["citation:fuzzy_ok"]


def test_fuzzy_never_rescues_unrelated_claims_or_retargets():
    notes = "- `spinnerTipsEnabled` hides the tips [0:01:00]\n"
    audited, log = cit.audit_notes(notes, _lines(GITIGNORE))
    assert audited.rstrip().endswith(cit.UNVERIFIED_MARK)
    assert [e["rule"] for e in log] == ["citation:unverified"]
    assert not any(e["rule"] == "citation:retarget" for e in log)


def test_raw_transcript_lines_count_for_the_fuzzy_check():
    # The corrected copy rewrote the line; the raw line still says "hook script".
    corrected = _lines({60: "we add a Claude Code thing here"})
    raw = _lines({60: "we add a hook script here for the pre tool event"})
    notes = "- `PreToolUse` hook script runs before each tool event [0:01:00]\n"
    a1, l1 = cit.audit_notes(notes, corrected)
    assert cit.UNVERIFIED_MARK in a1
    a2, l2 = cit.audit_notes(notes, corrected, raw)
    assert cit.UNVERIFIED_MARK not in a2 and l2[0]["rule"] == "citation:fuzzy_ok"


def test_fuzzy_token_matching_primitives():
    assert cit.fuzzy_token_match("git", "get")
    assert cit.fuzzy_token_match("usage", "use")
    assert not cit.fuzzy_token_match("opus", "plan")
    assert cit._fuzzy_in("gitignore", ["add", "a", "get", "ignore", "these"])
    assert not cit._fuzzy_in("gitignore", ["add", "a", "get", "these"])


# --- 7. Course Map theme tagging -------------------------------------------------------------------


TOPIC_NOTES = {
    "a": "### Key Concepts & Vocabulary\n- `hooks` - run scripts on events [0:01:00]\n"
         "- **PreToolUse** — fires before a tool [0:01:10]\n- Plan mode: proposes first [0:01:20]\n",
    "b": "### Key Concepts & Vocabulary\n- `hooks` - again [0:11:00]\n- `subagents` - helpers [0:11:10]\n",
    "c": "### Key Concepts & Vocabulary\n- `MCP server` - external tools [0:21:00]\n",
}


def test_chapter_topics_and_tagging_post_check():
    from video2mdnotes.core.longform import Passage
    chs = [[Passage(k, i + 1, f"Ch {k}", k, 1, 1, 0, 1, (0, 1), 10)] for i, k in enumerate("abc")]
    topics = {str(i + 1): lf.chapter_topics(ch, TOPIC_NOTES) for i, ch in enumerate(chs)}
    assert topics["1"] == ["hooks", "PreToolUse", "Plan mode"]
    assert topics["2"] == ["hooks", "subagents"] and topics["3"] == ["MCP server"]
    themes, log = lf.tag_theme_chapters(
        ["Automating with hooks and plan mode (ch. 1)", "Connecting MCP server tools (ch. 2)"],
        topics)
    assert themes[0] == "Automating with hooks and plan mode (ch. 1, 2)"
    assert themes[1] == "Connecting MCP server tools (ch. 2, 3)"     # tags only added
    assert {(e["chapter"], e["topic"]) for e in log} == {("2", "hooks"), ("3", "MCP server")}
    # the assertion itself: a theme naming a term lists every chapter whose topics hold it
    for th in themes:
        text, cited = lf.split_theme(th)
        toks = cit.tokens(text)
        for label, ts in topics.items():
            if any(lf._contains(toks, lf._topic_toks(t)) for t in ts):
                assert label in cited, (th, label)


def test_generic_one_word_topics_never_tag():
    texts = {"1": "agent agent hooks", "2": "agent tools", "3": "agent memory", "4": "agent evals"}
    generic = lf.generic_topic_words(texts)
    assert "agent" in generic and "hook" not in generic
    themes, log = lf.tag_theme_chapters(["Moving agents to production (ch. 1)"],
                                        {"1": [], "2": ["Agent"]}, generic)
    assert themes == ["Moving agents to production (ch. 1)"] and log == []


def test_themes_call_receives_key_topics_per_chapter(small_run, tmp_path):
    seen = {}

    class TwoBatch(MockLLM):
        def __call__(self, system, user):
            if "consolidation" in system:
                seen["themes"] = user
                return "### Cross-chapter themes\n- Agents recur (ch. 1)\n", "mock/themes"
            return super().__call__(system, user)

    from video2mdnotes.core import summarizer
    old = summarizer._REDUCE_BATCH
    summarizer._REDUCE_BATCH = 1
    try:
        result = lf.run_longform(small_run, tmp_path / "out", complete=TwoBatch(), max_words=800,
                                 progress=lambda m: None)
    finally:
        summarizer._REDUCE_BATCH = old
    assert "— key topics: term1" in seen["themes"]
    assert "Tag a theme with EVERY chapter whose key topics include" in seen["themes"]
    state = json.loads((tmp_path / "out" / lf.STATE_FILENAME).read_text())
    assert state["course_map"]["theme_tags"]["topics"]["1"] == ["term1"]
    assert "### Cross-chapter themes" in result.summary_path.read_text()
