"""Sources Referenced section (WP-P2, prompt variant "current").

Asserts on what the model receives (rendered prompts) and what the reader
gets (assembled notes, package links, digests)."""

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from video2mdnotes.config import settings
from video2mdnotes.core import (cited_sources as cs, digest as dg, docnotes, domains,
                                longform as lf, summarizer, variants)
from video2mdnotes.core.package import MediaResolver, rewrite_citations

import test_longform
from test_people_section import PeopleLLM, _table as people_table

small_run = test_longform.small_run

FIX = Path(__file__).parent / "fixtures" / "prompt_variants"
HEAD = "| source | type | cited by | cited for | date / edition | citations |\n|---|---|---|---|---|---|\n"


@pytest.fixture(autouse=True)
def _restore_variant():
    before = settings.prompt_variant
    yield
    settings.prompt_variant = before


def _table(*rows):
    return HEAD + "\n".join("| " + " | ".join(r) + " |" for r in rows)


# --- prompts -----------------------------------------------------------------------------


def test_current_prompts_ask_for_sources_after_people(small_run):
    variants.set_active("current")
    plan = lf.build_plan(small_run, write=False)
    system = lf.passage_system_prompt(plan)
    heads = [h for h, _ in domains.longform_sections(plan.profile)]
    assert heads.index("Sources Referenced") == heads.index("People") + 1
    assert "`### Sources Referenced`" in system
    assert HEAD.split("\n")[0] in system
    flat = " ".join(system.split())
    assert "NOT people" in flat and "A person's OWN outlet is a source" in flat
    assert "(per model knowledge)" in flat
    pack = next(pk for pk in plan.calls_units if len(pk) > 1)
    assert "## Sources Referenced" in lf.render_pack_prompt(plan, pack).system
    chunk, whole = summarizer._chunk_system(), summarizer._whole_system()
    assert chunk.index("#### People") < chunk.index("#### Sources Referenced") < chunk.index("#### Key terms")
    assert whole.index("## People") < whole.index("## Sources Referenced") < whole.index("- Last: `## Key terms`")


def test_legacy_prompts_have_no_sources(small_run):
    variants.set_active("legacy")
    plan = lf.build_plan(small_run, write=False)
    assert "Sources Referenced" not in lf.passage_system_prompt(plan)
    assert lf.passage_system_prompt(plan) == (FIX / "legacy_passage_system.txt").read_text()
    assert "Sources Referenced" not in summarizer._chunk_system()


# --- merge -------------------------------------------------------------------------------


def test_merge_dedupes_by_normalised_name_and_unions_citations():
    rows = cs.parse_rows(_table(
        ("The Information", "outlet", "host", "Reported OpenAI's revenue.", "-", "[0:05:00] [0:01:00]"),
        ("the information", "outlet", "host", "Reported OpenAI's revenue.", "-", "[0:03:00]"),
        ("Hard Fork podcast", "podcast", "guest", "Discussed agents.", "2026-10-10", "[0:02:00]"),
        ("Hard Fork", "show", "host", "Quoted on agents again.", "-", "[0:09:00]"),
        ("Ben Thompson", "person", "host", "Wrote an essay.", "-", "[0:04:00]")))
    rows += cs.parse_rows("- None")
    merged, info = cs.merge_sources(rows, people_names=["Ben Thompson"])
    names = [s.shown for s in merged]
    assert names == ["The Information", "Hard Fork podcast"]       # ordered by first citation
    info_row = merged[0]
    assert info_row.cites == ["[0:01:00]", "[0:03:00]", "[0:05:00]"]
    assert info_row.cited_for == "Reported OpenAI's revenue."
    hf = merged[1]
    assert hf.cited_by == "guest; host" and hf.date == "2026-10-10" and hf.type == "podcast"
    assert info["dropped_people"] == ["Ben Thompson"]
    md = cs.render(merged)
    assert md.count("| The Information |") == 1 and "## Sources Referenced" in md
    assert "- None identified" in cs.render([])


# --- assembly ----------------------------------------------------------------------------


class SourcesLLM(PeopleLLM):
    """PeopleLLM plus a Sources Referenced table in every chapter block."""

    def _passage(self, user):
        out = super()._passage(user)
        blocks = out.split("### Commands / Code / Config")
        res = blocks[0]
        for b in blocks[1:]:
            ts = re.findall(r"\[(\d+:\d{2}:\d{2})\]", res.split("### Summary")[-1])
            first = ts[0] if ts else "0:00:05"
            res += ("### Sources Referenced\n" + _table(
                ("The Information", "outlet", "host", "Reported a funding round.", "-", f"[{first}]"),
                ("Alex Host", "person", "host", "A person, not a source.", "-", f"[{first}]"))
                + "\n\n### Commands / Code / Config" + b)
        return res


def test_longform_notes_have_one_sources_section_linked_by_package(small_run, tmp_path):
    variants.set_active("current")
    out = tmp_path / "cur"
    res = lf.run_longform(small_run, out, complete=SourcesLLM(), max_words=800,
                          progress=lambda m: None)
    notes = res.notes
    assert notes.count("\n## Sources Referenced") == 1
    assert "### Sources Referenced" not in notes and "#### Sources Referenced" not in notes
    assert notes.index("## People") < notes.index("## Sources Referenced") < notes.index("## Introduction")
    sec = notes.split("## Sources Referenced", 1)[1].split("\n## ", 1)[0]
    assert sec.count("| The Information |") == 1
    assert "Alex Host" not in sec                       # people are not sources
    row = next(ln for ln in sec.split("\n") if ln.startswith("| The Information"))
    assert len(re.findall(r"\[\d+:\d{2}:\d{2}\]", row)) >= 3   # union across passages
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["assembly"]["sources"]["sources"] == 1
    linked, stats = rewrite_citations(notes, MediaResolver("abc123", 1250.0, None, out, None))
    row = next(ln for ln in linked.split("\n") if ln.startswith("| The Information"))
    assert "](https://youtu.be/abc123?t=" in row
    assert stats["linked"] == stats["citations"]


def test_document_sources_merged_once_after_people():
    text = ("# Doc\n\n## Overview\n\nAbout.\n\n## Topic A\n\n- point [p. 1 §A]\n\n"
            "## People\n\n" + people_table(("Ada Lovelace", "author", "x", "Wrote it.", "[p. 1 §A]")) + "\n\n"
            "## Sources Referenced\n\n" + _table(("Nature", "outlet", "author", "A finding.", "-", "[p. 1 §A]"))
            + "\n\n## Topic B\n\n#### Sources Referenced\n\n"
            + _table(("nature", "outlet", "author", "Another finding.", "-", "[p. 9 §B]"))
            + "\n\n## Key terms\n\n- **Engine**: machine [p. 9 §B]\n")
    done = docnotes.consolidate(text, people=True, sources=True)
    out = done.text
    assert out.count("## Sources Referenced") == 1 and "#### Sources" not in out
    assert out.index("## People") < out.index("## Sources Referenced") < out.index("## Key terms")
    row = next(ln for ln in out.split("\n") if ln.startswith("| Nature"))
    assert "[p. 1 §A] [p. 9 §B]" in row
    assert done.sources_in == 2 and done.sources_out == 1


# --- digest ------------------------------------------------------------------------------


def _episode_notes(d: Path, yid: str, day: str, srcs) -> Path:
    d.mkdir(parents=True)
    (d / "source_meta.json").write_text(json.dumps({"url": f"https://youtu.be/{yid}",
                                                    "published_at": day, "duration_s": 1200}))
    body = (f"---\ntitle: \"Ep {yid}\"\npublished: {day}\nsource: https://youtu.be/{yid}\n"
            f"duration_s: 1200\n---\n# Ep\n\n## Window 1 [0:00–0:20]\n\n### Summary\n\n- A point [0:01:00]\n\n"
            "## Sources Referenced\n\n" + _table(*srcs) + "\n")
    (d / "notes.md").write_text(body)
    return d / "notes.md"


def test_digest_sources_of_the_week_merged_with_counts_and_links(tmp_path):
    coll = tmp_path / "coll"
    _episode_notes(coll / "a", "AAAAAAAAAAA", "2026-10-19",
                   [("The Information", "outlet", "host", "Funding.", "-", "[0:01:00]"),
                    ("Stratechery", "newsletter", "host", "Aggregation.", "-", "[0:01:00]")])
    _episode_notes(coll / "b", "BBBBBBBBBBB", "2026-10-20",
                   [("the Information", "outlet", "host", "Revenue.", "-", "[0:01:00]")])
    eps = dg.load_collection(coll)
    span = dg.group(eps, "week")[0]
    dg._label(span.episodes)
    out_dir = coll / "digests"
    md = "\n".join(dg.render_sources(span, span.episodes, out_dir))
    assert "## Sources of the week" in md
    rows = [ln for ln in md.split("\n") if ln.startswith("| ") and "---" not in ln][1:]
    assert rows[0].startswith("| The Information | outlet | [E1](../a/notes.md), [E2](../b/notes.md) | 2 |")
    assert "Stratechery" in rows[1]
    assert "](https://youtu.be/AAAAAAAAAAA?t=60)" in rows[0]
    out_dir.mkdir()
    assert dg.check_digest_links(md, out_dir, span.episodes) == []
