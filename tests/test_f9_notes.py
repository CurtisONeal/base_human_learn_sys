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
