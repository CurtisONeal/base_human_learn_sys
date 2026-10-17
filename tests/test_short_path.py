"""WP-M2 (one path for every media transcript, packed calls) and WP-M3 (run-dir
reporting). Assertions are on the rendered prompts and the assembled
artifacts the evaluator reads — never on exit codes alone (AGENTS.md)."""

import datetime as dt
import json
import re
from pathlib import Path

import pytest

from video2mdnotes.core import longform as lf
from video2mdnotes.core.metadata import SourceMeta
from video2mdnotes.core.sources import Chapter
from video2mdnotes.eval import longform as ev
from tests.test_longform import MockLLM as _ClaudeMock, _transcript_md, flat

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


class MockLLM(_ClaudeMock):
    """test_longform's mock, minus its Claude-course wording ("cloud code"
    is a mishearing the default evaluator glossary flags in any notes)."""

    def __call__(self, system, user):
        text, model = super().__call__(system, user)
        return text.replace("configures cloud code hooks", "configures resource groups"), model


# --- fixtures ------------------------------------------------------------------------

CHAPTERS = [(0, 20, "Introduction"), (20, 110, "Cloud services"),
            (110, 260, "Private cloud"), (260, 420, "Public cloud"),
            (420, 600, "Hybrid cloud")]


@pytest.fixture
def short_run(tmp_path):
    """A 10-minute talk with 5 platform chapters, ~1k words: Azure Pt 1 in
    miniature (T2 got ONE heading for 22 chapters on the old short path)."""
    run = tmp_path / "short"
    (run / "transcripts").mkdir(parents=True)
    lines = []
    for start, end, title in CHAPTERS:
        word = title.split()[0].lower()
        for k, t in enumerate(range(start, end, 5)):
            lines.append((t, f"{word} point {k} explained with a {word} example"))
    (run / "transcripts" / "azure_mini.md").write_text(_transcript_md(lines))
    SourceMeta(
        source_id="mini", title="Azure Mini Class - Cloud 101",
        url="https://www.youtube.com/watch?v=mini",
        published_at=dt.date(2025, 1, 6), published_by="Test Trainer",
        duration_s=600.0, chapter_source="platform",
        chapters=[Chapter(start_s=a, end_s=b, title=t) for a, b, t in CHAPTERS],
        captured_at=dt.datetime(2026, 10, 16, 21, 0),
    ).write(run)
    return run


@pytest.fixture
def nochapter_run(tmp_path):
    """A 6-minute recording with no chapters at all (T5 / T4 shape)."""
    run = tmp_path / "talk"
    (run / "transcripts").mkdir(parents=True)
    lines = [(4 * k, f"fastapi line {k} about routes and pydantic models") for k in range(90)]
    (run / "transcripts" / "clepy_talk.md").write_text(_transcript_md(lines))
    SourceMeta(source_id="clepy", title="ClePy talk", url="/tmp/clepy.aac",
               duration_s=360.0, chapter_source="none",
               captured_at=dt.datetime(2026, 10, 16, 21, 0)).write(run)
    return run


def _sections_of(notes: str):
    """{chapter heading line: body} of the assembled notes' `##` sections."""
    out, cur = {}, None
    for line in notes.split("\n"):
        if line.startswith("## "):
            cur = line
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


# --- WP-M2: one call for a short multi-chapter source ------------------------------------


def test_short_source_is_one_call_with_per_chapter_sections(short_run, tmp_path):
    out = tmp_path / "out"
    mock = MockLLM()
    result = lf.run_longform(short_run, out, complete=mock, progress=lambda m: None)

    # ONE model call: no course-map call, no consistency call
    assert [k for k, _ in mock.calls] == ["passage"]
    assert result.model_calls == 1 and result.course_map_calls == 0
    user = mock.calls[0][1]
    # the rendered prompt: the multi-chapter instructions, every chapter's marker
    # with its range, and every chapter's start marked in the transcript
    assert "This passage covers SEVERAL chapters" in lf.render_pack_prompt(
        lf.build_plan(short_run, write=False), lf.build_plan(short_run, write=False).packs[0]).system
    assert "THIS PASSAGE: chapters 1–5 together (5 sections, summarized in one call)" in user
    for n, (a, b, title) in enumerate(CHAPTERS, 1):
        rng = f"{lf.hms(a)}–{lf.hms(b)}"
        assert f"## Chapter {n} — {title} [{rng}]" in user
        assert f'--- CHAPTER {n} "{title}" [{rng}] ---' in user

    notes = result.summary_path.read_text()
    secs = _sections_of(notes)
    for a, b, title in CHAPTERS:
        heading = f"## {title} [{lf.hms(a)}–{lf.hms(b)}]"
        assert heading in secs, heading
        cites = [ev.parse_ts(c) for c in re.findall(r"\[(\d+:\d{2}:\d{2})\]", secs[heading])]
        # each chapter's own citations, inside its own range
        assert cites and all(a <= c < b for c in cites), (title, cites)
    # long-form features the old short path lacked (T2/T5 FAIL P4/P5/P7)
    fm, _ = ev.split_frontmatter(notes)
    assert str(fm["published"]) == "2025-01-06" and str(fm["as_of"]) == "2025-01-06"
    assert fm["passage_calls"] == 1 and fm["chapters_total"] == 5
    assert notes.count("## Time-Sensitive Claims") == 1
    assert "Course map: deterministic" in notes
    assert "| 3 | Private cloud | [0:01:50–0:04:20] |" in notes
    assert (out / "corrections.json").exists()
    state = json.loads((out / lf.STATE_FILENAME).read_text())
    assert state["consistency"]["status"] == "skipped"
    assert state["assembly"]["passage_calls"] == 1
    assert {state["passages"][p]["pack"] for p in state["passages"]} == {"pack_01-05"}
    structure = json.loads((out / lf.STRUCTURE_FILENAME).read_text())
    assert structure["rung"] == "platform" and len(structure["sections"]) == 5
    crit = ev.evaluate(out)[0]["criteria"]
    for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert crit[k]["pass"], (k, crit[k]["summary"], crit[k]["details"])
    assert crit["P1"]["summary"].startswith("5/5")


def test_missing_chapter_block_is_rerun_on_its_own(short_run, tmp_path):
    """A reply that skips a chapter's block never borrows a neighbour's notes:
    that chapter gets one call of its own."""
    base = MockLLM()

    def lazy(system, user):
        text, model = base(system, user)
        if "## Chapter 3 — " in user:          # the pack prompt
            text = re.sub(r"## Chapter 3\n.*?(?=## Chapter 4\n)", "", text, flags=re.S)
        return text, model

    result = lf.run_longform(short_run, tmp_path / "out", complete=lazy,
                             progress=lambda m: None)
    passages = [u for k, u in base.calls if k == "passage"]
    assert len(passages) == 2
    assert 'THIS PASSAGE: chapter 3 "Private cloud" [0:01:50–0:04:20]' in passages[1]
    secs = _sections_of(result.summary_path.read_text())
    body = secs["## Private cloud [0:01:50–0:04:20]"]
    assert "private point" in body and "### Summary" in body
    state = json.loads((tmp_path / "out" / lf.STATE_FILENAME).read_text())
    assert state["passages"]["03_private_cloud_p1"]["pack_missing"] is True


def test_packed_run_resumes_from_per_chapter_checkpoints(short_run, tmp_path):
    out = tmp_path / "out"
    lf.run_longform(short_run, out, complete=MockLLM(), progress=lambda m: None)
    again = MockLLM()
    result = lf.run_longform(short_run, out, complete=again, progress=lambda m: None)
    assert again.calls == [] and result.passages_skipped == 1 and result.model_calls == 0


def test_split_pack_output_ignores_markers_in_code_and_unknown_chapters():
    pack = [lf.Passage(key=f"0{n}_c_p1", chapter_no=n, title=f"C{n}", group_id=f"ch0{n}",
                       part=1, parts=1, start_s=0, end_s=1, span=(0, 0), words=1)
            for n in (2, 3)]
    reply = ("Sure, here are the notes.\n## Chapter 2\n### Summary\n- a [0:00:01]\n"
             "```md\n## Chapter 3\n```\n## Chapter 9\n- stray\n## Chapter 3: C3\n"
             "### Summary\n- b [0:00:02]\n")
    blocks, missing = lf.split_pack_output(reply, pack)
    assert missing == []
    assert "Sure" not in blocks["02_c_p1"] and "## Chapter 3\n```" in blocks["02_c_p1"]
    assert blocks["03_c_p1"].startswith("### Summary\n- b")


# --- WP-M2: packing --------------------------------------------------------------------


def _p(n, start, end, words, part=1, parts=1):
    return lf.Passage(key=f"{n:02d}_c{n}_p{part}", chapter_no=n, title=f"C{n}",
                      group_id=f"ch{n:02d}", part=part, parts=parts, start_s=start,
                      end_s=end, span=(0, 0), words=words)


def test_pack_passages_rules():
    # 1-3 minute chapters (T3) pack until >= 8 min; never over the budget
    chapters = [[_p(n, 60 * (n - 1), 60 * n, 150)] for n in range(1, 21)]
    packs = lf.pack_passages(chapters, max_words=3000)
    assert [len(pk) for pk in packs] == [20]                   # all fit: ONE call
    packs = lf.pack_passages(chapters, max_words=1000)
    assert all(lf._pack_words(pk) <= 1000 for pk in packs)
    assert [p.chapter_no for pk in packs for p in pk] == list(range(1, 21))
    packs = lf.pack_passages(chapters, max_words=1500, min_words=10_000, min_s=8 * 60)
    assert [len(pk) for pk in packs] == [8, 8, 4]
    # a split chapter keeps one call per part, and is never packed
    split = [[_p(1, 0, 60, 100)], [_p(2, 60, 600, 900, 1, 2), _p(2, 600, 900, 900, 2, 2)],
             [_p(3, 900, 960, 100)], [_p(4, 960, 1000, 50)]]
    packs = lf.pack_passages(split, max_words=1000)
    assert [[p.key for p in pk] for pk in packs] == [
        ["01_c1_p1"], ["02_c2_p1"], ["02_c2_p2"], ["03_c3_p1", "04_c4_p1"]]
    # never across a gap in the selection (--only 1,3)
    assert [len(pk) for pk in lf.pack_passages([[_p(1, 0, 60, 10)], [_p(3, 120, 180, 10)]],
                                               max_words=1000)] == [1, 1]
    # an under-target tail joins the pack before it when it fits
    tail = [[_p(1, 0, 600, 400)], [_p(2, 600, 660, 50)]]
    assert [len(pk) for pk in lf.pack_passages(tail, max_words=420, min_words=300)] == [1, 1]
    assert [len(pk) for pk in lf.pack_passages(tail, max_words=460, min_words=300)] == [2]
    assert lf.pack_passages(chapters, max_words=1000, enabled=False) == [
        [ch[0]] for ch in chapters]


def test_plan_only_shows_the_packing(short_run, tmp_path):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    result = CliRunner().invoke(app, ["longform", str(short_run), "--plan-only"])
    assert result.exit_code == 0, result.output
    out = flat(result.output)
    assert "Packed: 1 passage call(s) for 5 passage(s); 1 call(s) cover several chapters" in out
    assert "pack_01-05: chapters 1–5 [0:00:00–0:10:00]" in out
    assert "Estimated model calls: 1 passage + 0 course map = 1" in out
    assert "single passage call: deterministic course map, no consistency call" in out
    assert "RUN_DIR=" not in result.output            # plan-only produces no run
    no_pack = CliRunner().invoke(app, ["longform", str(short_run), "--plan-only", "--no-pack"])
    assert "Estimated model calls: 5 passage + 1 course map = 6" in flat(no_pack.output)


# --- WP-M2: a short source with no chapters passes P1 via its own structure -----------------


def test_no_chapter_short_source_passes_p1_via_structure(nochapter_run, tmp_path):
    out = tmp_path / "out"
    mock = MockLLM()
    result = lf.run_longform(nochapter_run, out, complete=mock, progress=lambda m: None)
    assert [k for k, _ in mock.calls] == ["passage"]
    notes = result.summary_path.read_text()
    fm, _ = ev.split_frontmatter(notes)
    assert fm["structure"] == "windows" and fm["as_of_basis"] == "captured"
    structure = json.loads((out / lf.STRUCTURE_FILENAME).read_text())
    assert structure["rung"] == "windows" and structure["structure_is_inferred"] is True
    report = ev.evaluate(out)[0]
    p1 = report["criteria"]["P1"]
    assert p1["pass"] and p1["summary"].startswith("1/1"), p1
    assert "structure.json" in p1["details"]["source"] and "rung: windows" in p1["details"]["source"]
    assert "self-reported" in p1["details"]["source"]
    for k in ("P2", "P3", "P4", "P5", "P6", "P7"):
        assert report["criteria"][k]["pass"], (k, report["criteria"][k])


def test_p1_still_fails_without_structure_or_chapters(nochapter_run, tmp_path):
    out = tmp_path / "out"
    lf.run_longform(nochapter_run, out, complete=MockLLM(), progress=lambda m: None)
    (out / lf.STRUCTURE_FILENAME).unlink()
    p1 = ev.evaluate(out)[0]["criteria"]["P1"]
    assert not p1["pass"] and "no expected chapters" in p1["summary"]


# --- WP-M3: the evaluator never evaluates nothing ---------------------------------------


def test_eval_fails_loudly_on_a_missing_or_empty_run_dir(tmp_path, capsys):
    assert ev.main([str(tmp_path / "20261017_let_s_build_gpt")]) == 2
    assert "run dir does not exist" in capsys.readouterr().err
    (tmp_path / "empty" / "transcripts").mkdir(parents=True)
    assert ev.main([str(tmp_path / "empty")]) == 2
    assert "no transcripts/*.md" in capsys.readouterr().err
    (tmp_path / "empty" / "transcripts" / "t.md").write_text("- [0:00:01.000] x\n")
    assert ev.main([str(tmp_path / "empty")]) == 2
    assert "nothing to evaluate" in capsys.readouterr().err
    assert not (tmp_path / "empty" / "eval").exists()


def test_eval_script_exits_2_on_missing_dir(tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "scripts" / "eval_longform.py"
    proc = subprocess.run([sys.executable, str(script), str(tmp_path / "nope")],
                          capture_output=True, text=True)
    assert proc.returncode == 2 and "ERROR: run dir does not exist" in proc.stderr


# --- WP-M3: titles become folder names in ONE place ---------------------------------------


def test_sanitize_filename_lets_build_gpt():
    from video2mdnotes.core.downloader import run_dir_name, sanitize_filename
    title = "Let's build GPT: from scratch, in code, spelled out."
    assert sanitize_filename(title) == "lets_build_gpt_from_scratch_in_code_spelled_out"
    assert run_dir_name(title, dt.date(2026, 10, 16)) == (
        "20261016_lets_build_gpt_from_scratch_in_code_spelled_out")
    # the glob the harness guessed does NOT match the real folder
    assert not Path(run_dir_name(title, dt.date(2026, 10, 16))).match("*let_s_build_gpt*")


def test_no_other_module_builds_dated_run_dir_names():
    """The `<YYYYMMDD>_<title>` folder is composed only by run_dir_name; every
    title-to-name conversion goes through sanitize_filename."""
    src = Path(__file__).resolve().parents[1] / "src" / "video2mdnotes"
    offenders = []
    for path in src.rglob("*.py"):
        if path.name == "downloader.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"strftime\(['\"]%Y%m%d['\"]\)", text):
            offenders.append(path.name)
    assert offenders == []


# --- WP-M3: every command reports its run dir ---------------------------------------------


def test_longform_command_prints_run_dir_and_writes_last_run(short_run, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.core import runreport, summarizer
    from video2mdnotes.main import app

    monkeypatch.setattr(summarizer, "complete_with_failover", MockLLM())
    out = tmp_path / "T2b"
    result = CliRunner().invoke(app, ["longform", str(short_run), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output.rstrip().splitlines()[-1] == f"RUN_DIR={out.resolve()}"
    assert runreport.parse_run_dirs(result.output) == [str(out.resolve())]
    assert "Model calls: 1 (0 outline + 1 passage + 0 course map + 0 consistency)" in flat(
        result.output)
    last = json.loads(settings.last_run_file.read_text())
    assert last["run_dir"] == str(out.resolve()) and last["command"] == "longform"
    assert last["status"] == "ok" and last["started"] <= last["finished"]
    assert last["source"] == str(short_run.resolve())


def test_longform_command_reports_a_missing_run_dir_as_failed(tmp_path):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    result = CliRunner().invoke(app, ["longform", str(tmp_path / "nope")])
    assert result.exit_code == 2
    assert "RUN_DIR=" not in result.output
    last = json.loads(settings.last_run_file.read_text())
    assert last["run_dir"] is None and last["status"] == "failed"


def _fake_video(monkeypatch, tmp_path, titles):
    """process with no network: probe, captions and the model are faked."""
    from unittest.mock import MagicMock
    from video2mdnotes import main
    from video2mdnotes.core import summarizer
    from video2mdnotes.core.downloader import SourceInfo
    from video2mdnotes.core.transcriber import Segment, TranscriptResult

    sources = [SourceInfo(title=t, url=f"https://example/{i}") for i, t in enumerate(titles)]
    monkeypatch.setattr(main, "probe_source", lambda url: sources)

    def captions(title, subtitles):
        segs = [Segment(start=5.0 * k, end=5.0 * k + 4, text=f"gpt line {k} about tokens")
                for k in range(30)]
        md = "\n".join(["---", f'title: "{title}"', "model: manual-captions", "---", "",
                        *[f"- [0:{int(s.start) // 60:02d}:{int(s.start) % 60:02d}.000] {s.text}"
                          for s in segs]])
        return TranscriptResult(source_file=Path("x.en.vtt"), language="en", segments=segs,
                                full_text="", markdown_content=md, model_name="captions",
                                generated_at=dt.datetime.now(), transcript_source="captions")

    monkeypatch.setattr(main, "transcript_from_captions", captions)
    mock = MockLLM()
    monkeypatch.setattr(summarizer, "complete_with_failover", mock)
    return mock


def test_process_out_single_video_reports_the_real_folder(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    monkeypatch.setattr(settings, "output_dir", tmp_path / "results")
    mock = _fake_video(monkeypatch, tmp_path, ["Let's build GPT: from scratch, in code, spelled out."])
    out = tmp_path / "T3b"
    result = CliRunner().invoke(app, ["process", "https://example/v", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert [k for k, _ in mock.calls] == ["passage"]           # short: ONE long-form call
    notes = out / "summaries" / "lets_build_gpt_from_scratch_in_code_spelled_out.summary.md"
    assert notes.exists() and "## Time-Sensitive Claims" in notes.read_text()
    assert result.output.rstrip().splitlines()[-1] == f"RUN_DIR={out.resolve()}"
    assert not (tmp_path / "results").exists() or not list((tmp_path / "results").glob("2*"))
    # rerun: skipped, and still reported
    again = CliRunner().invoke(app, ["process", "https://example/v", "--out", str(out)])
    assert again.output.rstrip().splitlines()[-1] == f"RUN_DIR={out.resolve()}"
    last = json.loads(settings.last_run_file.read_text())
    assert last["runs"][0]["status"] == "skipped" and last["command"] == "process"


def test_process_playlist_prints_one_run_dir_per_video(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.core.downloader import run_dir_name
    from video2mdnotes.core.runreport import parse_run_dirs
    from video2mdnotes.main import app

    monkeypatch.setattr(settings, "output_dir", tmp_path / "results")
    _fake_video(monkeypatch, tmp_path, ["Let's build GPT", "Part 2: tokens!"])
    out = tmp_path / "playlist"
    result = CliRunner().invoke(app, ["process", "https://example/list", "--out", str(out)])
    assert result.exit_code == 0, result.output
    expected = [str((out / run_dir_name(t)).resolve()) for t in ("Let's build GPT", "Part 2: tokens!")]
    assert parse_run_dirs(result.output) == expected
    assert all(Path(d, "summaries").is_dir() for d in expected)
    tail = result.output.rstrip().splitlines()[-2:]
    assert tail == [f"RUN_DIR={d}" for d in expected]
    last = json.loads(settings.last_run_file.read_text())
    assert last["run_dir"] == expected[-1] and [r["status"] for r in last["runs"]] == ["ok", "ok"]
