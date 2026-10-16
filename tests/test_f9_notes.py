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
