"""WP-F9 (i)-(o): metadata casing, ephemeral filter, spoken dates, citation
leak variants, theme tagging, names/privacy, consistency annotations."""

import json
from pathlib import Path

import pytest

from video2mdnotes.core import quality as q
from video2mdnotes.core.transcriber import Segment

FIXTURE_META = Path(__file__).parent / "fixtures" / "metadata" / "cca_f_info.json"


def segs(*texts):
    return [Segment(start=i * 5.0, end=i * 5.0 + 4, text=t) for i, t in enumerate(texts)]


# --- (i) metadata acronym casing ------------------------------------------------


def test_promo_description_acronyms_do_not_become_casing_rules():
    meta = {"title": "FULL COURSE - Claude Certified Architect (CCA-F)",
            "description": "LIKE AND SUBSCRIBE, USE CODE SAVE20 for the MCP labs",
            "tags": []}
    transcript = ("this full course shows the code. like the course? the full code "
                  "path and full details of the course and code")
    terms = q.metadata_terms(meta, transcript)
    for bad in ("FULL", "COURSE", "LIKE", "SUBSCRIBE", "USE", "CODE", "SAVE20"):
        assert bad not in terms, bad
    assert "CCA-F" in terms and "MCP" in terms
    g = q.build_glossary(meta, transcript_text=transcript)
    out, corr = q.correct_segments(segs(transcript), g)
    assert out[0].text == transcript and corr == []


def test_title_acronym_kept_when_transcript_does_not_use_it_lowercase():
    meta = {"title": "NASA mission briefing", "description": "", "tags": []}
    assert "NASA" in q.metadata_terms(meta, "the nasa team and NASA again")


def test_today_product_claims_are_kept_and_session_glitches_dropped():
    from video2mdnotes.core import notes_format as nf
    keep = ["Claude Code today supports MCP servers",
            "The exam today has 60 questions",
            "Today the CLI ships with plan mode"]
    drop = ['Sonnet "is not today working as expected" for searching',
            "Search was down today for him",
            "He hit the usage cap today"]
    rows = [nf.ClaimRow(c, "[1:00:00]", "", "feature", "x") for c in keep + drop]
    kept, dropped = nf.filter_ephemeral(rows)
    assert [r.claim for r in kept] == keep
    assert [d["claim"] for d in dropped] == drop


def test_real_cca_f_metadata_still_yields_cca_f():
    from video2mdnotes.core.metadata import from_ytdlp_info
    meta = from_ytdlp_info(json.loads(FIXTURE_META.read_text(encoding="utf-8")))
    assert "CCA-F" in q.metadata_terms(meta, "welcome to the cca-f exam course")


# --- (k) spoken dates need an anchored mention ----------------------------------


def _tsegs(*pairs):
    return [Segment(start=t, end=t + 4, text=x) for t, x in pairs]


def test_two_unanchored_history_mentions_give_no_recording_estimate():
    from video2mdnotes.core import spoken_dates as sd
    s = _tsegs((100, "the new model was announced May 22nd with a big launch"),
               (5000, "so again, announced May 22nd, and the benchmarks followed"))
    assert sd.estimate_recording(s, "2026-07-20") is None
    assert sd.estimate_event(s, "2026-07-20") is None


def test_one_anchored_mention_still_estimates():
    from video2mdnotes.core import spoken_dates as sd
    s = _tsegs((37050, "my usage resets April 23rd 5:00 pm, so next time we continue"))
    est = sd.estimate_recording(s, "2026-07-20")
    assert est is not None and est.month == "2026-04"


# --- (l) citation-repair leak variants (real E2c strings) -----------------------

E2C_LEAKS = [
    ("- Claude routes around denied tools. Glob [8:58:25 is not used — see below]",
     "- Claude routes around denied tools. Glob [8:58:25]"),
    ("- `Validation-retry feedback loop` - specific validation errors are fed back into "
     "the loop so the model can correct itself, with a cap on retries [9:37:41 not used] ",
     "- `Validation-retry feedback loop` - specific validation errors are fed back into "
     "the loop so the model can correct itself, with a cap on retries [9:37:41] "),
    ("- Validation-retry: add specific validation errors to the prompt and retry, and "
     "limit the number of retries [9:39:33 not used]",
     "- Validation-retry: add specific validation errors to the prompt and retry, and "
     "limit the number of retries [9:39:33]"),
    ("- Glob was used instead [8:58:25] — see below", "- Glob was used instead [8:58:25]"),
    ("- x [1:02:03 (approximate, the caption is garbled)]", "- x [1:02:03]"),
]


@pytest.mark.parametrize("leak,fixed", E2C_LEAKS)
def test_e2c_bracket_commentary_variants_collapse(leak, fixed):
    from video2mdnotes.core import citations as cit
    out, entries = cit.normalize_citation_commentary(leak)
    assert out == fixed
    assert entries and all(e["rule"] == "citation:normalized" for e in entries)


def test_generic_rule_leaves_ranges_and_real_see_references():
    from video2mdnotes.core import citations as cit
    text = ("- ok [1:00:00] and [1:00:05–1:00:09] and [1:00:05 - 1:00:09] and "
            "[0:01:00 to 0:02:00] fine (see [1:00:10]); see chapter 3")
    assert cit.normalize_citation_commentary(text) == (text, [])


# --- (m) theme tagging: full term list + title; "(all chapters)" ---------------


def _e2c_like_plan():
    """Ch. 7 of E2c: eight settings terms come first; `plan mode` is the
    11th Key Concepts term, so the 8-topic list sent to the model cut it."""
    from types import SimpleNamespace
    from video2mdnotes.core import longform as lf
    from video2mdnotes.core.longform import Passage
    terms7 = ["managed settings", "user settings", "project settings", "local settings",
              "scope precedence", "TBAC", "allow / ask / deny", "rule evaluation order",
              "settings.local.json", "dontAsk", "plan mode"]
    notes = {
        "a": "### Key Concepts & Vocabulary\n- `plan mode` - proposes first [0:01:00]\n",
        "b": "### Key Concepts & Vocabulary\n"
             + "".join(f"- `{t}` - x [0:11:00]\n" for t in terms7),
        "c": "### Key Concepts & Vocabulary\n- `MCP server` - external tools [0:21:00]\n",
    }
    titles = {"a": "SDK Environment Setup",
              "b": "Claude code configuration permissions safety",
              "c": "MCP Ecosystem Integration"}
    chs = [[Passage(k, i + 1, titles[k], k, 1, 1, 0, 1, (0, 1), 10)]
           for i, k in enumerate("abc")]
    return SimpleNamespace(selected=chs), notes, lf


def test_plan_mode_theme_tags_chapter_whose_eleventh_term_is_plan_mode():
    plan, notes, lf = _e2c_like_plan()
    short, _ = lf.chapter_topic_lists(plan, notes)
    assert "plan mode" not in short["2"]                  # the cut that hid it in E2c
    full, _ = lf.chapter_topic_lists(plan, notes, full=True)
    assert "plan mode" in full["2"]
    themes, log = lf.tag_theme_chapters(
        ["Planning before implementation: plan mode and spec files (ch. 1)"], full)
    assert lf.split_theme(themes[0])[1] == ["1", "2"]
    assert {(e["chapter"], e["topic"]) for e in log} == {("2", "plan mode")}


def test_theme_naming_a_chapter_title_word_is_tagged():
    plan, notes, lf = _e2c_like_plan()
    full, _ = lf.chapter_topic_lists(plan, notes, full=True)
    themes, _ = lf.tag_theme_chapters(["Least-privilege permissions for agents (ch. 3)"], full)
    assert "2" in lf.split_theme(themes[0])[1]


def test_theme_on_ninety_percent_of_chapters_shows_all_chapters():
    from video2mdnotes.core import longform as lf
    labels = ["-"] + [str(i) for i in range(1, 11)]
    themes, marked = lf.mark_all_chapter_themes(
        ["Claude Code everywhere (ch. 1, 2, 3, 4, 5, 6, 7, 8, 9)",
         "Hooks (ch. 2, 5)"], labels)
    assert themes == ["Claude Code everywhere (all chapters)", "Hooks (ch. 2, 5)"]
    assert marked == ["Claude Code everywhere"]


def test_real_e2c_preamble_form_counts_toward_all_chapters():
    from video2mdnotes.core import longform as lf
    theme = ("Checking Claude-generated code, documentation claims and docs through hands-on "
             "labs and live demos (preamble, ch. 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)")
    text, chs = lf.split_theme(theme)
    assert chs[0] == "-" and chs[-1] == "13" and not text.endswith(")")
    labels = ["-"] + [str(i) for i in range(1, 14)]
    themes, _ = lf.mark_all_chapter_themes([theme], labels)
    assert themes == [text + " (all chapters)"]
    assert lf.render_theme("X", ["-", "2", "1"]) == "X (preamble, ch. 1, 2)"


# --- (n) names / privacy ------------------------------------------------------------

from tests.test_longform import flat, small_run  # noqa: E402,F401  (fixture)


def _meetup_segments():
    return _tsegs(
        (380, "Morgan, Harsha, Jason, Chris, and Louie have graciously taken some time out."),
        (400, "Ted handles operations and the person who does our tech is Curtis."),
        (1685, "So this is Harsha uh what you call it."),
        (1707, "Archer's work focuses on helping organizations build platforms."),
        (4340, "I'll go ahead and announce the winner from the submission."),
        (4346, "Greg Van Lundran from Clear result, congratulations."))


def test_transcript_variant_found_by_roll_call_and_introduction():
    from video2mdnotes.core import names
    segs_ = _meetup_segments()
    vocab = names.TranscriptVocab.build(segs_)
    speakers = [("Morgan", [], 391.0), ("Archer", [], 1682.0), ("Jason Tabling", [], 2707.0),
                ("Chris Halasey", ["Chris Halasi"], 4378.0), ("Louis", ["Louie"], 5501.0)]
    found = names.transcript_speaker_variants(speakers, segs_, vocab)
    assert [w for w, _ in found["Archer"]] == ["Harsha"]
    assert "roll call" in found["Archer"][0][1]
    assert found["Morgan"] == [] and found["Louis"] == []
    # introduction alone (no roll call) also finds it
    found2 = names.transcript_speaker_variants(speakers, segs_[2:], names.TranscriptVocab.build(segs_[2:]))
    assert [w for w, _ in found2["Archer"]] == ["Harsha"]
    assert "introduction" in found2["Archer"][0][1]
    # the heading prefers the spelling heard more often
    assert names.preferred_spelling("Archer", ["Harsha"], vocab) == "Harsha"
    assert names.hedge_title("Nutanix platforms (Archer)", ["Archer"], set(),
                             preferred={"Archer": "Harsha"}) == \
        "Nutanix platforms — Harsha (ASR spelling; also heard as Archer)"


def test_names_table_lists_transcript_variant_of_speaker():
    from video2mdnotes.core import names
    segs_ = _meetup_segments()
    vocab = names.TranscriptVocab.build(segs_)
    rows = names.names_rows(speakers=[("Archer", ["Harsha"])], notes=[], respellings=[],
                            vocab=vocab, meta_words=set(),
                            found={"Archer": [("Harsha", "roll call [0:06:20]")]})
    assert rows[0].spellings == [("Archer", 1), ("Harsha", 2)]
    assert "transcript calls this speaker Harsha" in rows[0].source


def test_backticked_organiser_names_are_found_and_hedged_but_products_are_not():
    from video2mdnotes.core import names
    notes = ("### Key Concepts & Vocabulary\n"
             "- `Ted Shoxal` - the evening's MC; handles most of the operations [0:02:57].\n"
             "- `Curtis O'Neill` - handles tech and recording [0:03:27].\n"
             "- `Amazon Bedrock`: model-provider service with API access to models [0:13:03].\n")
    vocab = names.TranscriptVocab.build(_tsegs(
        (1, "Ted Shoxal runs it, Curtis O'Neill records, Amazon Bedrock hosts models.")))
    found = names.note_names([notes], vocab, set())
    assert found == ["Ted Shoxal", "Curtis O'Neill"]
    out, n = names.hedge_names_in_body(notes, found)
    assert n == 2
    assert "`Ted Shoxal` (ASR spelling) - the evening's MC" in out
    assert "`Curtis O'Neill` (ASR spelling) - handles" in out
    again, n2 = names.hedge_names_in_body(out, found)       # idempotent
    assert again == out and n2 == 0


def test_raffle_winner_is_redacted_and_logged_but_organisers_kept():
    from video2mdnotes.core import names
    chapter = ("- After the break the organiser Ted Shoxal announced the winner [1:12:18].\n"
               "- The winner was picked at random from 112 submissions: Greg Van Lundran, "
               "Senior Business Analyst at Clear result [1:12:26] [1:12:35].\n"
               "- Ted Shoxal thanked the sponsors [1:13:00].\n"
               "- Another prize went to a winner from Acme Corp [1:14:00].\n")
    private = names.private_names([chapter], ["Ted Shoxal", "Greg Van Lundran"], ["Morgan"])
    assert private == ["Greg Van Lundran"]
    out, log = names.redact_private(chapter, private)
    assert "Lundran" not in out and "Clear result" not in out
    assert "112 submissions: an attendee (name withheld) [1:12:26]" in out
    assert "went to a winner [1:14:00]" in out
    assert "Ted Shoxal" in out
    assert {e["name"] for e in log} == {"Greg Van Lundran", "<organisation of a winner>"}


def test_passage_prompt_forbids_naming_private_individuals(small_run):
    from video2mdnotes.core import longform as lf
    plan = lf.build_plan(small_run, max_words=800, write=False)
    text = flat(lf.render_passage_prompt(plan, plan.passages[0]).text)
    assert "Never name private individuals who are not speakers or organisers" in text
    assert "raffle or giveaway winners, attendees" in text


# --- (o) consistency labels, prefix and pointers --------------------------------------


def _cons_secs(ch1, ch2):
    from video2mdnotes.core import consistency as cons
    return [cons.Section("5", "Advanced Agent Patterns", "3:00:00–4:15:46", [("k5", ch1)]),
            cons.Section("6", "Sessions", "4:15:46–5:30:00", [("k6", ch2)])]


E2C_CH5 = ("### Special Attention\n"
           "- **Logging suggestions** from Andrew: write structured JSON / JSONL logs so they "
           "can be parsed and ingested [3:59:48]. Usage/token logging was still missing; he "
           "guessed the Agent SDK might provide it \"for free\" (not verified) [4:01:58].\n")
E2C_CH6 = ("### Special Attention\n"
           "- The Agent SDK gives no visibility into a subagent's internal loop until it "
           "reports back, so you cannot track spend [4:39:03] [5:03:06].\n")


def test_pointer_is_the_citation_of_the_contradicting_clause():
    from video2mdnotes.core import consistency as cons
    secs = _cons_secs(E2C_CH5, E2C_CH6)
    sts = cons.extract_statements(secs)
    e = next(s.ref for s in sts if "Logging suggestions" in s.text)
    l = next(s.ref for s in sts if "no visibility" in s.text)
    pairs = [{"earlier_ref": e, "later_ref": l, "kind": "discrepancy",
              "resolution": "The later section says the Agent SDK gives no visibility, so "
                            "spend can't be tracked, against the guess that it provides "
                            "usage logging for free."}]
    out, log = cons.apply_contradictions({"k5": E2C_CH5, "k6": E2C_CH6}, sts, pairs)
    assert '_(discrepancy: see [4:01:58] in ch. 5 "Advanced Agent Patterns")_' in out["k6"]
    assert "[3:59:48] in ch. 5" not in out["k6"]
    assert log[0]["pointer"] == "[4:01:58]"


def test_superseded_leading_clause_is_prefixed_but_trailing_clause_is_not():
    from video2mdnotes.core import consistency as cons
    lead = ("### Special Attention\n- The giveaway winner was NOT announced at the event "
            "[0:01:53]. Entering requires giving your name [0:01:13].\n")
    trail = ("### Summary\n- Entering requires giving your name [0:01:13]. The giveaway "
             "winner was NOT announced at the event [0:01:53].\n")
    later = "### Summary\n- The winner was drawn at the event from 112 submissions [1:12:26].\n"
    for ch1, prefixed in ((lead, True), (trail, False)):
        secs = [cons.Section("1", "Welcome", "0:00:00–0:06:36", [("k1", ch1)]),
                cons.Section("6", "Intermission", "0:58:36–1:12:58", [("k6", later)])]
        sts = cons.extract_statements(secs)
        e = next(s.ref for s in sts if s.section == 1)
        l = next(s.ref for s in sts if s.section == 2)
        pairs = [{"earlier_ref": e, "later_ref": l, "kind": "superseded",
                  "resolution": "The giveaway winner was announced: drawn at the event."}]
        out, _ = cons.apply_contradictions({"k1": ch1, "k6": later}, sts, pairs)
        line = next(x for x in out["k1"].split("\n") if "_(superseded" in x)
        assert line.startswith("- (superseded) ") is prefixed, line


def test_partly_contradicted_and_unknown_kinds_are_not_superseded():
    from video2mdnotes.core import consistency as cons
    ch2 = ("### Summary\n- Setup of the Agent SDK and Claude Code is not covered in this "
           "course [0:08:30].\n")
    ch3 = "### Summary\n- He installed the Agent SDK with pip [0:26:10].\n"
    secs = [cons.Section("1", "Introduction", "0:00:47–0:10:17", [("k1", ch2)]),
            cons.Section("2", "SDK Environment Setup", "0:10:17–0:47:36", [("k2", ch3)])]
    sts = cons.extract_statements(secs)
    e = next(s.ref for s in sts if s.section == 1)
    l = next(s.ref for s in sts if s.section == 2)
    for kind, want in (("partly contradicted", "partly contradicted"),
                       ("partly_contradicted", "partly contradicted"),
                       ("contradicts", "discrepancy")):
        pairs = [{"earlier_ref": e, "later_ref": l, "kind": kind,
                  "resolution": "A quick pip install is shown later."}]
        out, log = cons.apply_contradictions({"k1": ch2, "k2": ch3}, sts, pairs)
        assert log[0]["kind"] == want
        joined = out["k1"] + out["k2"]
        assert f"_({want}: see [" in joined and "(superseded)" not in joined
    # partly contradicted annotates the EARLIER statement
    pairs = [{"earlier_ref": e, "later_ref": l, "kind": "partly contradicted",
              "resolution": "A quick pip install is shown later."}]
    out, _ = cons.apply_contradictions({"k1": ch2, "k2": ch3}, sts, pairs)
    assert "_(partly contradicted: see [0:26:10]" in out["k1"]


def test_consistency_prompt_restricts_superseded():
    from video2mdnotes.core import consistency as cons
    system = cons.CONSISTENCY_PROMPT.read_text(encoding="utf-8")
    assert '`"superseded"` ONLY when the later statement REPLACES' in system
    assert '`"partly contradicted"`' in system
    assert 'When unsure, use `"discrepancy"`' in system
