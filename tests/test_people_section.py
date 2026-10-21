"""People section (prompt variant "current") and the A/B switch to "legacy".

Assertions are on the rendered prompts (what the model receives) and on the
assembled notes (what the reader gets) — AGENTS.md."""

import json
import re
from pathlib import Path

import pytest

from video2mdnotes.config import settings
from video2mdnotes.core import docnotes, domains, longform as lf, people, summarizer, variants
from video2mdnotes.core.package import MediaResolver, rewrite_citations

import test_longform
from test_longform import MockLLM

small_run = test_longform.small_run   # reuse the fixture

FIX = Path(__file__).parent / "fixtures" / "prompt_variants"


@pytest.fixture(autouse=True)
def _restore_variant():
    before = settings.prompt_variant
    yield
    settings.prompt_variant = before


# --- the A/B switch: legacy is byte-identical to main before People ----------------------


def test_legacy_passage_and_pack_prompts_are_main_verbatim(small_run):
    variants.set_active("legacy")
    plan = lf.build_plan(small_run, write=False)
    assert plan.prompt_variant == "legacy"
    assert lf.passage_system_prompt(plan) == (FIX / "legacy_passage_system.txt").read_text()
    pack = next(pk for pk in plan.calls_units if len(pk) > 1)
    assert lf.render_pack_prompt(plan, pack).system == (FIX / "legacy_pack_system.txt").read_text()
    assert "People" not in lf._sections_block(plan.profile, "legacy")


def test_legacy_document_prompts_are_main_verbatim():
    variants.set_active("legacy")
    assert summarizer._chunk_system() == (FIX / "legacy_doc_chunk_system.txt").read_text()
    assert summarizer._whole_system() == (FIX / "legacy_doc_whole_system.txt").read_text()


def test_current_prompts_ask_for_people_next_to_key_terms(small_run):
    variants.set_active("current")
    plan = lf.build_plan(small_run, write=False)
    system = lf.passage_system_prompt(plan)
    heads = [h for h, _ in domains.longform_sections(plan.profile)]
    assert heads.index("People") == heads.index("Key Concepts & Vocabulary") + 1
    assert "`### People`" in system
    assert "| person | role | affiliation & background (as stated) | relevance | citations |" in system
    flat = " ".join(system.split())
    assert "Never fill this from your own knowledge" in flat
    assert "giveaway or raffle winners, attendees" in flat
    assert "NOT companies, products" in flat
    assert "the one answering is the interviewee or guest" in flat
    # it is not just appended: legacy is a strict subset in order
    legacy = (FIX / "legacy_passage_system.txt").read_text()
    assert len(system) > len(legacy)

    chunk, whole = summarizer._chunk_system(), summarizer._whole_system()
    assert "#### People" in chunk and chunk.index("#### People") < chunk.index("#### Key terms")
    assert "## People" in whole and whole.index("## People") < whole.index("- Last: `## Key terms`")
    assert "| person | role | affiliation & background (as stated) | relevance | locators |" in whole


def test_unknown_variant_is_refused():
    with pytest.raises(ValueError):
        variants.set_active("newest")


# --- merging -------------------------------------------------------------------------------


def _table(*rows):
    head = "| person | role | affiliation & background (as stated) | relevance | citations |\n|---|---|---|---|---|\n"
    return head + "\n".join("| " + " | ".join(r) + " |" for r in rows)


def test_merge_same_person_across_passages_asr_variants_and_bare_names():
    rows = people.parse_rows(_table(
        ("Nathaniel Whittemore (ASR spelling)", "host", "not stated",
         "Frames the episode.", "[0:00:05] [0:03:10]"),
        ("Dario Amodei", "person discussed", "CEO of Anthropic, as stated",
         "His essay is the topic.", "[0:02:00]"),
        ("Pat Winner", "raffle winner", "not stated", "Won a prize.", "[0:09:00]"),
    ))
    rows += people.parse_rows(_table(
        ("Nathaniel Whitmore", "host / interviewer", "host of The AI Daily Brief",
         "Interviews the guest about agents and asks about pricing.", "[0:12:00] [0:00:05]"),
        ("Jane Roe", "guest", "founder of Acme Labs (says so at intro)",
         "Argues agents will replace dashboards.", "[0:11:30]"),
        ("Nathaniel", "host", "", "Closes the show.", "[0:20:00]"),
        ("Amodei", "quoted", "", "Quoted on timelines.", "[0:02:30]"),
    ))
    merged = people.merge_people(rows, asr=True, count=lambda w: {"whittemore": 5}.get(w.lower(), 1))
    names = [p.shown for p in merged]
    assert names == ["Nathaniel Whittemore", "Jane Roe", "Dario Amodei"]   # speakers first
    host = merged[0]
    assert host.role == "host / interviewer"
    assert host.background == "host of The AI Daily Brief"                 # richest stated
    assert host.cites == ["[0:00:05]", "[0:03:10]", "[0:12:00]", "[0:20:00]"]
    assert host.also == ["Nathaniel Whitmore"]
    assert "Interviews the guest" in host.relevance
    amodei = merged[2]
    assert amodei.role == "quoted"                                        # quoted > discussed
    assert amodei.cites == ["[0:02:00]", "[0:02:30]"]
    md = people.render(merged, asr=True)
    assert md.count("## People") == 1
    assert "Nathaniel Whittemore (ASR spelling; also heard as Nathaniel Whitmore)" in md
    assert "Pat Winner" not in md                                          # private


def test_metadata_confirmed_name_is_not_hedged():
    rows = people.parse_rows(_table(("Jane Roe", "guest", "not stated", "Talks.", "[0:01:00]")))
    merged = people.merge_people(rows, asr=True, meta_words={"jane", "roe"})
    assert "(ASR" not in people.render(merged, asr=True, meta_words={"jane", "roe"})


def test_bare_name_matching_two_people_stays_separate():
    rows = people.parse_rows(_table(
        ("Sam Altman", "person discussed", "OpenAI CEO", "x", "[0:01:00]"),
        ("Sam Bankman", "person discussed", "y", "y", "[0:02:00]"),
        ("Sam", "quoted", "", "z", "[0:03:00]")))
    assert len(people.merge_people(rows)) == 3


# --- assembly: exactly one ## People, citations linked ----------------------------------------


class PeopleLLM(MockLLM):
    """MockLLM plus a People table in every chapter block."""

    def _passage(self, user):
        out = super()._passage(user)
        blocks = out.split("### Commands / Code / Config")
        res = blocks[0]
        for b in blocks[1:]:
            ts = re.findall(r"\[(\d+:\d{2}:\d{2})\]", res.split("### Summary")[-1])
            first = ts[0] if ts else "0:00:05"
            res += (f"### People\n{_table(('Alex Host', 'host', 'runs the channel, as stated', 'Teaches the course.', f'[{first}]'), ('Pat Winner', 'giveaway winner', 'not stated', 'Won.', f'[{first}]'))}\n\n"
                    "### Commands / Code / Config" + b)
        return res


def test_longform_notes_have_one_people_section_and_variant_recorded(small_run, tmp_path):
    variants.set_active("current")
    out = tmp_path / "cur"
    llm = PeopleLLM()
    res = lf.run_longform(small_run, out, complete=llm, max_words=800, progress=lambda m: None)
    notes = res.notes
    assert notes.count("\n## People") == 1
    assert "### People" not in notes and "#### People" not in notes
    assert notes.index("## Course Map") < notes.index("## People") < notes.index("## Introduction")
    people_md = notes.split("## People", 1)[1].split("\n## ", 1)[0]
    assert people_md.count("Alex Host") == 1          # merged across 4 passages
    assert "Pat Winner" not in notes                   # private, dropped
    assert 'prompt_variant: "current"' in notes.split("---", 2)[1]
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["prompt_variant"] == "current"
    assert all(p["prompt_variant"] == "current" for p in state["passages"].values())
    assert state["assembly"]["people"]["people"] == 1
    # the People citations are linked like every other citation
    linked, stats = rewrite_citations(notes, MediaResolver("abc123", 1250.0, None, out, None))
    row = next(ln for ln in linked.split("\n") if ln.startswith("| Alex Host"))
    assert "](https://youtu.be/abc123?t=" in row
    assert stats["linked"] == stats["citations"]


def test_legacy_run_has_no_people_and_checkpoints_are_per_variant(small_run, tmp_path):
    out = tmp_path / "ab"
    variants.set_active("legacy")
    llm = MockLLM()
    res = lf.run_longform(small_run, out, complete=llm, max_words=800, progress=lambda m: None)
    assert "## People" not in res.notes
    assert 'prompt_variant: "legacy"' in res.notes
    calls = sum(1 for k, _ in llm.calls if k == "passage")
    # same variant: everything from checkpoint
    again = lf.run_longform(small_run, out, complete=llm, max_words=800, progress=lambda m: None)
    assert again.passages_run == 0
    # other variant: the legacy checkpoints are NOT reused
    variants.set_active("current")
    llm2 = PeopleLLM()
    cur = lf.run_longform(small_run, out, complete=llm2, max_words=800, progress=lambda m: None)
    assert cur.passages_run == calls
    assert cur.notes.count("\n## People") == 1


# --- documents -------------------------------------------------------------------------------


def test_document_people_merged_once_before_key_terms():
    text = ("# Doc\n\n## Overview\n\nAbout.\n\n## Topic A\n\n- point [p. 1 §A]\n\n"
            "## People\n\n| person | role | affiliation & background (as stated) | relevance | locators |\n"
            "|---|---|---|---|---|\n| Ada Lovelace | author | mathematician, as stated | Wrote it. | [p. 1 §A] |\n\n"
            "## Topic B\n\n#### People\n\n| person | role | affiliation & background (as stated) | relevance | locators |\n"
            "|---|---|---|---|---|\n| Ada Lovelace | cited author |  | Cited again. | [p. 9 §B] |\n"
            "| Charles Babbage | person discussed | not stated | Built the engine. | [p. 9 §B] |\n\n"
            "## Key terms\n\n- **Engine**: machine [p. 9 §B]\n")
    done = docnotes.consolidate(text, people=True)
    out = done.text
    assert out.count("## People") == 1 and "#### People" not in out
    assert out.index("## People") < out.index("## Key terms")
    row = next(ln for ln in out.split("\n") if ln.startswith("| Ada Lovelace"))
    assert "[p. 1 §A] [p. 9 §B]" in row and "| author |" in row
    assert done.people_in == 3 and done.people_out == 2
    # a topic chapter called "People" (no people table) is left alone
    topic = "# Doc\n\n## People\n\nThe chapter about teams.\n\n## Key terms\n\n- **X**: y\n"
    assert "The chapter about teams." in docnotes.consolidate(topic, people=True).text
    # legacy: no People handling at all
    assert docnotes.consolidate(text).text.count("People") == 2


def test_asr_one_word_spelling_merges_and_role_words_are_not_hedged():
    rows = people.parse_rows(_table(
        ("Claire Vaux (ASR spelling)", "quoted", "content creator", "Hates Opus V.", "[0:22:35]"),
        ("Clairvaux (ASR spelling)", "quoted", "content creator", "Calls Muse delightful.",
         "[0:26:07]"),
        ("The president", "person discussed", "US president", "Confirmed the gift.", "[0:01:54]")))
    merged = people.merge_people(rows, asr=True)
    assert sorted(p.shown for p in merged) == ["Claire Vaux", "The president"]
    md = people.render(merged, asr=True, is_common=lambda w: w.lower() in {"the", "president"})
    assert "| The president |" in md
    assert "Claire Vaux (ASR spelling; also heard as Clairvaux)" in md


def test_description_block_only_in_current(small_run):
    variants.set_active("current")
    plan = lf.build_plan(small_run, write=False)
    p = plan.passages[0]
    assert "SOURCE DESCRIPTION" in lf.render_passage_prompt(plan, p).user
    variants.set_active("legacy")
    plan = lf.build_plan(small_run, write=False)
    assert "SOURCE DESCRIPTION" not in lf.render_passage_prompt(plan, p).user
