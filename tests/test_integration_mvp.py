"""WP-M1b: every entry point, end to end, on the committed MVP fixtures.

Drives the real CLI (typer CliRunner) and the real API (FastAPI TestClient)
with real parsing (EPUB/PDF/md/txt/html, json3 captions, yt-dlp info dicts),
real ffmpeg (probe, windowed extraction, silence detection) and the real
summarizer chain (`complete_with_failover` → the subscription route). Only
the edges are faked:

* the `claude` CLI subprocess (`summarizer._complete_claude_cli`) — a
  deterministic mock whose notes cite REAL timestamps / locators from the
  prompt it was given;
* the network — `yt_dlp.YoutubeDL` and `webfetch.fetch_text` route to the
  fixture info dicts, the json3 slice and the html pages;
* ASR in the default suite — a scripted backend that returns the known
  `say` script (the integration tests at the bottom run real parakeet).

Assertions are on artifacts (AGENTS.md): run-dir layout, the `RUN_DIR=` last
line, `_last_run.json`, transcript lines, frontmatter, chapter headings,
citations, corrections.json, the cost ledger, and `scripts/eval_longform.py`
P1–P7 on every media/video run dir.

Fixtures: tests/fixtures/mvp/ (see its README; regenerate with
make_fixtures.py).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yt_dlp
from typer.testing import CliRunner

from video2mdnotes.config import settings
from video2mdnotes.core import costs, longasr, summarizer
from video2mdnotes.core.asr.base import wav_duration
from video2mdnotes.core.runreport import RUN_DIR_PREFIX, parse_run_dirs
from video2mdnotes.core.transcriber import Segment
from video2mdnotes.eval import longform as ev
from video2mdnotes.main import app
from tests.test_short_path import MockLLM as MediaMockLLM

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "mvp"
EVAL_SCRIPT = ROOT / "scripts" / "eval_longform.py"
SCRIPTS = json.loads((FIX / "speech_scripts.json").read_text(encoding="utf-8"))

CAPTIONS_URL = "https://fixture.invalid/cca_slice.en.json3"
VIDEO_URL = "https://www.youtube.com/watch?v=reDRM0tqhNs"
ASR_URL = "https://www.youtube.com/watch?v=mvpTalk0001"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLmvpFixture"
EMBED_PAGE_URL = "https://course.example.org/lesson-1"
EMBED_VIDEO_URL = "https://www.youtube.com/watch?v=mvpEmbed001"
ARTICLE_URL = "https://blog.example.org/understanding-vector-databases"

SLICE_CHAPTERS = ["Welcome", "Introduction", "Study plan and costs", "Content outline"]
_TS = re.compile(r"\[(\d+:\d{2}:\d{2})\]")
_LINE = re.compile(r"^- \[(\d+):(\d{2}):(\d{2})\.(\d{3})\] \S")

runner = CliRunner()


# --- fakes -----------------------------------------------------------------------


class MvpLLM:
    """Stands in for the `claude` CLI subprocess only.

    Media prompts go to test_short_path's MockLLM (per-chapter blocks citing
    the passage's own caption lines). Document prompts get notes that cite
    the locator of the section each point came from; the reduce returns the
    passage notes assembled (the concatenation the real reduce must keep).
    """

    def __init__(self):
        self.media = MediaMockLLM()
        self.calls = []          # (kind, model)

    def __call__(self, model, system_prompt_file, user_message):
        system = Path(system_prompt_file).read_text(encoding="utf-8")
        if system == summarizer._CHUNK_SYSTEM:
            kind, out = "doc_chunk", self._chunk(user_message)
        elif system == summarizer._REDUCE_SYSTEM:
            kind, out = "doc_reduce", self._reduce(user_message)
        else:
            out, _ = self.media(system, user_message)
            kind = self.media.calls[-1][0]
        self.calls.append((kind, model))
        return out

    def kinds(self):
        return [k for k, _ in self.calls]

    @staticmethod
    def _chunk(user: str) -> str:
        """One bullet per section, citing that section's own `[locator]`."""
        out, loc = [], None
        for line in user.splitlines():
            m = re.match(r"^\[([^\]]+)\]", line.strip())
            if m and not line.startswith("Passage"):
                loc = m.group(1)
                continue
            words = line.split()
            if loc and len(words) >= 6 and not line.startswith(("Each section", "Book:")):
                out.append(f"- {' '.join(words[:14])} [{loc}]")
                loc = None
        if not out:                                  # fall back to the chunk locator
            m = re.search(r"locator \[([^\]]+)\]", user)
            out.append(f"- Passage summary [{m.group(1) if m else 'n/a'}]")
        return "\n".join(out)

    @staticmethod
    def _reduce(user: str) -> str:
        title, _, body = user.partition("\n\n")
        return f"# {title.replace('Book: ', '').strip()}\n\n{body.strip()}\n"


class ScriptedASR:
    """ASR backend returning the known `say` script, spread over the window.

    Real ffmpeg still extracts every window (so durations, truncation and
    silence detection are real); only the recognition is scripted.
    """

    name = "scripted"
    model = "say-script"
    supports_prompt = False

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []
        self.last_language = "en"

    def available(self):
        return True, "ok"

    def load(self):
        pass

    def transcribe(self, wav_path, initial_prompt=None, language=None):
        dur = wav_duration(wav_path) or 0.0
        text = self.scripts[min(len(self.calls), len(self.scripts) - 1)]
        self.calls.append((Path(wav_path).name, round(dur, 2)))
        sentences = [s.strip() + "." for s in text.split(".") if s.strip()]
        span = max(dur - 0.05, 0.0)
        return [Segment(start=round(span * i / len(sentences), 3),
                        end=round(span * (i + 1) / len(sentences), 3), text=" " + s)
                for i, s in enumerate(sentences)]


def _info(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def info_captions(variant: str = "platform") -> dict:
    """The CCA-F slice with its json3 track offered as a MANUAL track (the
    only kind `process` takes captions from)."""
    info = _info({"platform": "yt_info_platform_chapters.json",
                  "description": "yt_info_description_chapters.json",
                  "none": "yt_info.json"}[variant])
    info["subtitles"] = {"en": [{"ext": "json3", "url": CAPTIONS_URL}]}
    return info


def info_asr(video_id: str = "mvpTalk0001", title: str = "Resource Groups in 15 Seconds",
             audio: str = "talk_15s.m4a") -> dict:
    """A video with auto-captions only → native-audio download → windowed ASR."""
    info = _info("yt_info.json")
    info.update(id=video_id, display_id=video_id, title=title, fulltitle=title,
                webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                original_url=f"https://www.youtube.com/watch?v={video_id}",
                duration=15, description="A fifteen second lesson on resource groups.",
                tags=["azure", "resource groups"], _mvp_audio=audio)
    return info


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated output, subscription-only chain, no network, scripted ASR."""
    from video2mdnotes.core import captions as captions_mod
    from video2mdnotes.core import embeds as embeds_mod
    from video2mdnotes.core.documents import html as html_mod
    import litellm

    monkeypatch.setattr(settings, "output_dir", tmp_path / "results")
    monkeypatch.setattr(settings, "temp_dir", tmp_path / "temp")
    monkeypatch.setattr(settings, "llm_models", ["claude-cli/opus", "claude-cli/sonnet"])
    monkeypatch.setattr(settings, "allow_metered_fallback", False)
    monkeypatch.setattr(settings, "on_exhaustion", "stop")
    monkeypatch.setattr(settings, "extract_frames", False)
    monkeypatch.setattr(settings, "captions_first", True)

    llm = MvpLLM()
    monkeypatch.setattr(summarizer, "_complete_claude_cli", llm)

    def no_metered(*a, **k):
        raise AssertionError("a metered (litellm) call was attempted")
    monkeypatch.setattr(litellm, "completion", no_metered)

    routes: dict = {}
    pages = {
        CAPTIONS_URL: (FIX / "cca_slice.en.json3").read_text(encoding="utf-8"),
        EMBED_PAGE_URL: (FIX / "embed_page.html").read_text(encoding="utf-8"),
        ARTICLE_URL: (FIX / "article.html").read_text(encoding="utf-8"),
    }
    fetched = []

    def fetch_text(url, *a, **k):
        fetched.append(url)
        if url not in pages:
            raise ValueError(f"no fixture for {url} (network is off in this suite)")
        return pages[url]

    for mod in (captions_mod, embeds_mod, html_mod):
        monkeypatch.setattr(mod, "fetch_text", fetch_text)

    downloads = []

    class FakeYDL:
        def __init__(self, opts=None):
            self.opts = dict(opts or {})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if url not in routes:
                raise yt_dlp.utils.DownloadError(f"ERROR: Unsupported URL: {url}")
            info = copy.deepcopy(routes[url])
            if download:
                assert "postprocessors" not in self.opts, "whole-file wav conversion requested"
                target = Path(self.opts["outtmpl"].replace("%(ext)s", Path(info["_mvp_audio"]).suffix[1:]))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(FIX / info["_mvp_audio"], target)
                downloads.append(target)
                info["requested_downloads"] = [{"filepath": str(target)}]
            return info

        def prepare_filename(self, info):
            return self.opts["outtmpl"]

        def download(self, urls):
            raise AssertionError("legacy whole-file download path used")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)

    asr = ScriptedASR([SCRIPTS["talk_15s"]])
    monkeypatch.setattr(longasr, "select_backend",
                        lambda **k: (asr, "test: scripted ASR over real ffmpeg windows"))

    class Env:
        pass

    e = Env()
    e.tmp, e.llm, e.routes, e.asr, e.fetched, e.downloads = (
        tmp_path, llm, routes, asr, fetched, downloads)
    return e


# --- shared artifact checks --------------------------------------------------------


def last_line_run_dir(output: str) -> Path:
    lines = output.rstrip().splitlines()
    assert lines and lines[-1].startswith(RUN_DIR_PREFIX), lines[-5:]
    return Path(lines[-1][len(RUN_DIR_PREFIX):])


def last_run() -> dict:
    return json.loads(Path(settings.last_run_file).read_text(encoding="utf-8"))


def ledger_is_subscription_only(llm: MvpLLM):
    """No metered spend, and every model call booked at $0 as subscription."""
    ledger = costs.get_ledger()
    assert ledger.spent() == 0.0
    entries = ledger.entries()
    assert all(e.get("kind") == costs.SUBSCRIPTION for e in entries), entries
    assert len(entries) == len(llm.calls)
    assert all(model.startswith("claude-cli/") for _, model in llm.calls)


def raw_transcript(run: Path) -> Path:
    """The raw transcript (the quality pass writes `<name>.corrected.md`
    beside it and never edits the raw one)."""
    files = sorted(p for p in (run / "transcripts").glob("*.md")
                   if not p.name.endswith(".corrected.md"))
    assert len(files) == 1, files
    return files[0]


def transcript_seconds(run: Path):
    out = []
    for line in raw_transcript(run).read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m:
            h, mi, s, _ = map(int, m.groups())
            out.append(h * 3600 + mi * 60 + s)
    assert out, "no `- [H:MM:SS.mmm] text` lines in the transcript"
    return out


def eval_script(run: Path, *extra) -> dict:
    proc = subprocess.run([sys.executable, str(EVAL_SCRIPT), str(run), *extra],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    report = json.loads((run / "eval" / "eval_report.json").read_text(encoding="utf-8"))
    if isinstance(report, list):
        report = report[0]
    crit = report["criteria"]
    for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert crit[k]["pass"], (k, crit[k]["summary"])
    return crit


def check_media_run(run: Path, *, chapters=None, published=None, duration=None,
                    corrections_min=0):
    """Everything the evaluator and a reader rely on in one media/video run dir."""
    assert run.is_dir(), run
    notes_files = list((run / "summaries").glob("*.summary.md"))
    assert len(notes_files) == 1, notes_files
    notes = notes_files[0].read_text(encoding="utf-8")
    for name in ("source_meta.json", "structure.json", "longform_state.json",
                 "corrections.json"):
        assert (run / name).exists(), name
    seconds = transcript_seconds(run)

    fm, body_line = ev.split_frontmatter(notes)
    body = "\n".join(notes.splitlines()[body_line - 1:])
    assert "published" in fm and "as_of" in fm, fm
    if published:
        assert str(fm["published"]) == published and str(fm["as_of"]) == published
    assert body.count("## Time-Sensitive Claims") == 1
    if chapters:
        for title in chapters:
            assert re.search(rf"^## {re.escape(title)} \[\d+:\d\d:\d\d–\d+:\d\d:\d\d\]",
                             body, re.M), (title, re.findall(r"^## .*", body, re.M))
    cites = [ev.parse_ts(c) for c in _TS.findall(body)]
    assert cites, "notes cite no timestamps"
    known = set(seconds)
    assert all(c in known for c in cites), sorted(set(cites) - known)
    if duration:
        assert max(cites) <= duration
    corr = json.loads((run / "corrections.json").read_text(encoding="utf-8"))
    entries = corr if isinstance(corr, list) else corr.get("corrections", [])
    assert len(entries) >= corrections_min, corr
    for entry in entries:
        assert any(k in entry for k in ("timestamp", "ts", "start_s", "time")), entry
    crit = eval_script(run)
    return notes, fm, crit


# --- process: YouTube single video ---------------------------------------------------


@pytest.mark.parametrize("variant,rung", [("platform", "platform"),
                                          ("description", "description")])
def test_process_youtube_with_captions(env, variant, rung):
    """Manual json3 track → no audio, no ASR; chapters from the platform list
    or from the description; one packed long-form call."""
    env.routes[VIDEO_URL] = info_captions(variant)
    out = env.tmp / f"cca_{variant}"
    result = runner.invoke(app, ["process", VIDEO_URL, "--out", str(out), "--usd-cap", "0"])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert env.downloads == [] and env.asr.calls == []        # captions: no audio
    assert env.fetched == [CAPTIONS_URL]
    assert env.llm.kinds() == ["passage"]                     # 4 chapters, ONE call

    notes, fm, crit = check_media_run(out, chapters=SLICE_CHAPTERS, published="2026-07-20",
                                      duration=420, corrections_min=1)
    assert json.loads((out / "structure.json").read_text())["rung"] == rung
    assert crit["P1"]["summary"].startswith("4/4")
    assert "captions (manual, en)" in raw_transcript(out).read_text()
    assert (out / "original_url.txt").read_text() == VIDEO_URL
    meta = json.loads((out / "source_meta.json").read_text())
    assert meta["chapter_source"] == rung and meta["published_by"] == "freeCodeCamp.org"
    # the CCA captions' "cloud code" mishearing is corrected and logged, never in notes
    assert "cloud code" not in notes.lower()
    last = last_run()
    assert last["command"] == "process" and last["status"] == "ok"
    assert last["run_dir"] == str(out.resolve())
    ledger_is_subscription_only(env.llm)


def test_process_youtube_without_captions_uses_windowed_asr(env):
    """Auto-captions only → native audio (no wav conversion) → windowed ASR
    over real ffmpeg windows → long-form; the audio is archived."""
    env.routes[ASR_URL] = info_asr()
    out = env.tmp / "asr_video"
    result = runner.invoke(app, ["process", ASR_URL, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert len(env.downloads) == 1 and env.downloads[0].suffix == ".m4a"
    assert len(env.asr.calls) == 1 and env.asr.calls[0][1] == pytest.approx(15, abs=0.2)
    assert env.fetched == []                                   # auto captions never fetched
    assert env.llm.kinds() == ["passage"]
    notes, fm, crit = check_media_run(out, published="2026-07-20", duration=15)
    assert (out / "wav_files" / env.downloads[0].name).exists()  # native audio archived
    manifest = json.loads((out / longasr.MANIFEST_FILENAME).read_text())
    assert manifest["complete"] is True
    assert "resource group" in raw_transcript(out).read_text().lower()
    ledger_is_subscription_only(env.llm)


def test_process_playlist_one_run_dir_per_video(env):
    env.routes[PLAYLIST_URL] = {"_type": "playlist", "id": "PLmvpFixture",
                                "title": "MVP Fixture Playlist",
                                "entries": [info_captions("platform"), None, info_asr()]}
    env.routes[ASR_URL] = info_asr()           # the download re-extracts the entry
    out = env.tmp / "playlist"
    result = runner.invoke(app, ["process", PLAYLIST_URL, "--out", str(out)])
    assert result.exit_code == 0, result.output
    dirs = [Path(d) for d in parse_run_dirs(result.output)]
    assert len(dirs) == 2 and all(d.parent == out.resolve() for d in dirs)
    assert result.output.rstrip().splitlines()[-1] == f"{RUN_DIR_PREFIX}{dirs[-1]}"
    check_media_run(dirs[0], chapters=SLICE_CHAPTERS, published="2026-07-20")
    check_media_run(dirs[1], published="2026-07-20", duration=15)
    assert [r["status"] for r in last_run()["runs"]] == ["ok", "ok"]
    # resume: a rerun skips both and still reports them
    calls = len(env.llm.calls)
    again = runner.invoke(app, ["process", PLAYLIST_URL, "--out", str(out)])
    assert again.exit_code == 0 and len(env.llm.calls) == calls
    assert [Path(d) for d in parse_run_dirs(again.output)] == dirs
    assert [r["status"] for r in last_run()["runs"]] == ["skipped", "skipped"]
    ledger_is_subscription_only(env.llm)


def test_process_page_with_embedded_video(env):
    """yt-dlp refuses the page; the embed is scraped and processed."""
    env.routes[EMBED_VIDEO_URL] = info_asr("mvpEmbed001", "Lesson 1 video")
    out = env.tmp / "embed"
    result = runner.invoke(app, ["process", EMBED_PAGE_URL, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert env.fetched == [EMBED_PAGE_URL]
    assert (out / "original_url.txt").read_text() == EMBED_VIDEO_URL
    check_media_run(out, duration=15)
    ledger_is_subscription_only(env.llm)


# --- process: documents -------------------------------------------------------------------


def _doc_notes(out: Path, stem_like: str) -> str:
    files = [p for p in out.glob("*.summary.md") if stem_like in p.name]
    assert len(files) == 1, list(out.iterdir())
    return files[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("name,locators", [
    ("guide.md", ["What a resource group is", "Naming and tags", "Deleting a group"]),
    ("notes.txt", None),
    ("article.html", ["Indexes", "When not to use one"]),
    ("tiny_book.epub", ["ch. 1", "ch. 2", "ch. 3"]),
    ("tiny_paper.pdf", ["p. 1", "p. 2", "p. 3"]),
])
def test_process_local_document(env, name, locators):
    out = env.tmp / "docs"
    result = runner.invoke(app, ["process", str(FIX / name), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert env.downloads == [] and env.asr.calls == [] and env.fetched == []
    kinds = env.llm.kinds()
    assert kinds[-1] == "doc_reduce" and set(kinds[:-1]) == {"doc_chunk"}
    notes = _doc_notes(out, "")
    cited = re.findall(r"\[([^\]\n]+)\]", notes)
    assert cited, notes
    for loc in locators or []:
        assert any(loc.lower() in c.lower() for c in cited), (loc, cited)
    assert "provenance:" in notes and "structure_is_inferred:" in notes
    assert last_run()["command"] == "process" and last_run()["status"] == "ok"
    ledger_is_subscription_only(env.llm)


def test_process_article_url(env):
    """Not a video, no embeds → read as an article."""
    out = env.tmp / "article"
    result = runner.invoke(app, ["process", ARTICLE_URL, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert env.fetched == [ARTICLE_URL, ARTICLE_URL]          # embed scan, then the article
    notes = _doc_notes(out, "")
    assert "html (fetched)" in notes and ARTICLE_URL in notes
    assert any("Indexes" in c for c in re.findall(r"\[([^\]\n]+)\]", notes))
    ledger_is_subscription_only(env.llm)


def test_process_refuses_volumes_and_unsupported_local_files(env):
    result = runner.invoke(app, ["process", "/Volumes/NO_SUCH_CARD/clip.md"])
    assert result.exit_code == 2 and "Refused" in result.output
    result = runner.invoke(app, ["process", str(FIX / "talk_15s.m4a")])
    assert result.exit_code == 1 and "Unsupported file type" in result.output
    assert env.llm.calls == []


# --- book --------------------------------------------------------------------------------


@pytest.mark.parametrize("name,locators", [("tiny_book.epub", ["ch. 1", "ch. 2", "ch. 3"]),
                                           ("tiny_paper.pdf", ["p. 1", "p. 2", "p. 3"])])
def test_book(env, name, locators):
    out = env.tmp / "books"
    result = runner.invoke(app, ["book", str(FIX / name), "--out", str(out), "--usd-cap", "0"])
    assert result.exit_code == 0, result.output
    assert "1 summarized" in result.output
    assert last_line_run_dir(result.output) == out.resolve()
    notes = _doc_notes(out, "")
    cited = re.findall(r"\[([^\]\n]+)\]", notes)
    for loc in locators:
        assert any(loc in c for c in cited), (loc, cited)
    assert last_run()["command"] == "book" and last_run()["status"] == "ok"
    ledger_is_subscription_only(env.llm)
    # rerun: skipped, no model call, still reported
    calls = len(env.llm.calls)
    again = runner.invoke(app, ["book", str(FIX / name), "--out", str(out)])
    assert "1 skipped" in again.output and len(env.llm.calls) == calls
    assert last_line_run_dir(again.output) == out.resolve()


def test_book_extract_only_makes_no_call_and_no_run(env):
    for name in ("tiny_book.epub", "tiny_paper.pdf"):
        result = runner.invoke(app, ["book", str(FIX / name), "--extract-only"])
        assert result.exit_code == 0, result.output
        assert "3 sections" in result.output and "passages" in result.output
        assert RUN_DIR_PREFIX not in result.output
    assert env.llm.calls == []
    assert not Path(settings.last_run_file).exists()
    assert not (settings.output_dir / "books").exists() or not list(
        (settings.output_dir / "books").glob("*.summary.md"))


# --- media -----------------------------------------------------------------------------------


def test_media_single_file(env):
    out = env.tmp / "talk"
    result = runner.invoke(app, ["media", str(FIX / "talk_15s.m4a"), "--out", str(out),
                                 "--usd-cap", "0"])
    assert result.exit_code == 0, result.output
    assert "Completeness gate PASS" in result.output
    assert last_line_run_dir(result.output) == out.resolve()
    notes, fm, _ = check_media_run(out, duration=15)
    assert str(fm["published"]).lower() in ("unknown", "none", "")  # a file has no publisher
    assert fm["as_of_basis"] == "captured"
    assert env.llm.kinds() == ["passage"]
    assert last_run()["command"] == "media"
    ledger_is_subscription_only(env.llm)


def test_media_truncated_aac_recorder_file(env):
    """Timkoo-style ADTS .aac whose last frame is cut: the container's
    duration is an estimate; windows, gate and notes still work."""
    env.asr.scripts = [SCRIPTS["timkoo_truncated"]]
    out = env.tmp / "timkoo"
    result = runner.invoke(app, ["media", str(FIX / "timkoo_truncated.aac"), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Completeness gate PASS" in result.output
    assert last_line_run_dir(result.output) == out.resolve()
    check_media_run(out, duration=7)
    ck = [json.loads(p.read_text()) for p in (out / "asr").glob("**/*.json")
          if p.name != longasr.MANIFEST_FILENAME]
    assert ck and all(c.get("actual_s", 0) > 5 for c in ck if "actual_s" in c)
    ledger_is_subscription_only(env.llm)


def test_media_camera_split_group(env):
    """Two .mov parts with contiguous creation_time are auto-grouped into
    one recording on one timeline (part 2 offset by part 1's duration)."""
    env.asr.scripts = [SCRIPTS["cam_0001"], SCRIPTS["cam_0002"]]
    out = env.tmp / "meetup"
    result = runner.invoke(app, ["media", str(FIX / "cam_0002.mov"), str(FIX / "cam_0001.mov"),
                                 "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "2 parts" in result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert [n for n, _ in env.asr.calls] and len(env.asr.calls) == 2
    seconds = transcript_seconds(out)
    assert min(seconds) < 10 <= max(seconds) < 20              # part 2 lands after 0:00:10
    text = raw_transcript(out).read_text()
    assert text.index("welcome to the meetup") < text.index("Vector databases store")
    notes, fm, _ = check_media_run(out, duration=20)
    meta = json.loads((out / "source_meta.json").read_text())
    assert [p.get("name", p.get("path", "")).split("/")[-1] for p in meta["parts"]] == ["cam_0001.mov", "cam_0002.mov"], meta["parts"]
    ledger_is_subscription_only(env.llm)


def test_media_probe_is_free_and_reports_the_group(env):
    result = runner.invoke(app, ["media", str(FIX / "cam_0001.mov"), str(FIX / "cam_0002.mov"),
                                 str(FIX / "timkoo_truncated.aac"), "--probe"])
    assert result.exit_code == 0, result.output
    assert "Recording group (2 parts" in result.output and "Probed 3 files" in result.output
    assert RUN_DIR_PREFIX not in result.output
    assert env.asr.calls == [] and env.llm.calls == []


def test_media_transcribe_only_then_longform(env):
    """--transcribe-only makes no model call; `longform` on that run dir then
    produces the notes into --out — the two-step path the E3 runs used."""
    run = env.tmp / "t_only"
    result = runner.invoke(app, ["media", str(FIX / "talk_15s.m4a"), "--transcribe-only",
                                 "--out", str(run)])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == run.resolve()
    assert env.llm.calls == [] and not list(run.glob("summaries/*.summary.md"))
    transcript_seconds(run)
    # the same command again: skipped (transcript exists), still reported
    again = runner.invoke(app, ["media", str(FIX / "talk_15s.m4a"), "--transcribe-only",
                                "--out", str(run)])
    assert "Skipping" in again.output and len(env.asr.calls) == 1

    plan = runner.invoke(app, ["longform", str(run), "--plan-only"])
    assert plan.exit_code == 0 and RUN_DIR_PREFIX not in plan.output and env.llm.calls == []

    out = env.tmp / "lf_out"
    result = runner.invoke(app, ["longform", str(run), "--out", str(out), "--usd-cap", "0"])
    assert result.exit_code == 0, result.output
    assert last_line_run_dir(result.output) == out.resolve()
    assert env.llm.kinds() == ["passage"]
    assert (out / "summaries").is_dir() and not list(run.glob("summaries/*.summary.md"))
    # the output dir is self-contained for the evaluator
    for name in ("transcripts", "source_meta.json"):
        if not (out / name).exists():
            src = run / name
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, out / name)
    check_media_run(out, duration=15)
    ledger_is_subscription_only(env.llm)


def test_media_refuses_volumes(env):
    result = runner.invoke(app, ["media", "/Volumes/NO_SUCH_CARD/DCIM/clip.mov"])
    assert result.exit_code == 2 and "Refused" in result.output
    assert env.asr.calls == []


# --- catalog ------------------------------------------------------------------------------------


def test_catalog_index_and_browse(env):
    corpus = env.tmp / "corpus"
    corpus.mkdir()
    for name in ("tiny_book.epub", "tiny_paper.pdf", "guide.md"):
        shutil.copyfile(FIX / name, corpus / name)
    db = env.tmp / "catalog.db"
    result = runner.invoke(app, ["catalog", "index", "--corpus", str(corpus), "--db", str(db)])
    assert result.exit_code == 0, result.output and db.exists()
    overview = runner.invoke(app, ["catalog", "overview", "--db", str(db)])
    assert overview.exit_code == 0 and "3" in overview.output.split("books")[0]
    listing = runner.invoke(app, ["catalog", "list", "--db", str(db)])
    assert "Tiny Agents Handbook" in listing.output
    found = runner.invoke(app, ["catalog", "find", "Evaluation", "--db", str(db)])
    assert found.exit_code == 0 and "Evaluation" in found.output
    # the PDF is catalogued under its own title, not its last bookmark
    toc = runner.invoke(app, ["catalog", "toc", "Chunk Size", "--db", str(db)])
    assert "Results" in toc.output and "Method" in toc.output
    search = runner.invoke(app, ["catalog", "search", "resource group", "--db", str(db),
                                 "--no-semantic", "--no-rerank"])
    assert search.exit_code == 0, search.output
    assert env.llm.calls == [] and env.fetched == []


# --- API and the Chrome extension -----------------------------------------------------------------


def test_api_process_captions_and_asr(env):
    from fastapi.testclient import TestClient
    from video2mdnotes.api import app as api_app
    from video2mdnotes.core.downloader import run_dir_name

    env.routes[VIDEO_URL] = info_captions("description")
    env.routes[ASR_URL] = info_asr()
    client = TestClient(api_app)
    for url, title in ((VIDEO_URL, env.routes[VIDEO_URL]["title"]),
                       (ASR_URL, env.routes[ASR_URL]["title"])):
        resp = client.post("/process", json={"url": url, "usd_cap": 0})
        assert resp.status_code == 202, resp.text
        run = settings.output_dir / run_dir_name(title)
        last = last_run()
        assert last["command"] == "api /process" and last["status"] == "ok", last
        assert last["run_dir"] == str(run.resolve())
    check_media_run(settings.output_dir / run_dir_name(env.routes[VIDEO_URL]["title"]),
                    chapters=SLICE_CHAPTERS, published="2026-07-20", corrections_min=1)
    check_media_run(settings.output_dir / run_dir_name(env.routes[ASR_URL]["title"]),
                    duration=15)
    ledger_is_subscription_only(env.llm)


def test_api_rejects_a_non_url_and_a_negative_cap(env):
    from fastapi.testclient import TestClient
    from video2mdnotes.api import app as api_app

    client = TestClient(api_app)
    assert client.post("/process", json={"url": "not a url"}).status_code == 422
    assert client.post("/process", json={"url": VIDEO_URL, "usd_cap": -1}).status_code == 422
    assert env.llm.calls == []


def test_chrome_extension_posts_what_the_api_accepts(env):
    """popup.js → POST localhost:8000/process {url: tab.url}: same route, same
    body shape the API model accepts (url only; usd_cap optional)."""
    from fastapi.testclient import TestClient
    from video2mdnotes.api import ProcessRequest, app as api_app

    js = (ROOT / "chrome_extension" / "popup.js").read_text(encoding="utf-8")
    assert "fetch('http://localhost:8000/process'" in js and "method: 'POST'" in js
    body_keys = re.findall(r"JSON\.stringify\(\{\s*(\w+)\s*:", js)
    assert body_keys == ["url"]
    assert set(body_keys) <= set(ProcessRequest.model_fields)
    assert not any(f.is_required() for k, f in ProcessRequest.model_fields.items()
                   if k not in body_keys)
    env.routes[ASR_URL] = info_asr()
    resp = TestClient(api_app).post("/process", json={"url": ASR_URL},
                                    headers={"Origin": "chrome-extension://abc"})
    assert resp.status_code == 202
    assert resp.headers.get("access-control-allow-origin") in ("*", "chrome-extension://abc")
    assert last_run()["status"] == "ok"


# --- real ASR (parakeet) -------------------------------------------------------------------------


def _parakeet_or_skip():
    from video2mdnotes.core.asr import get_backend
    backend = get_backend("parakeet-mlx")
    ok, why = backend.available()
    if not ok:
        pytest.skip(f"parakeet-mlx unavailable: {why}")
    return backend


def _words(text: str):
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.fixture
def real_asr(env, monkeypatch):
    backend = _parakeet_or_skip()
    monkeypatch.setattr(longasr, "select_backend",
                        lambda **k: (backend, "integration: real parakeet-mlx"))
    return backend


@pytest.mark.integration
@pytest.mark.parametrize("files,script_keys,duration", [
    (["talk_15s.m4a"], ["talk_15s"], 15),
    (["timkoo_truncated.aac"], ["timkoo_truncated"], 7),
    (["cam_0001.mov", "cam_0002.mov"], ["cam_0001", "cam_0002"], 20),
])
def test_media_real_parakeet(env, real_asr, files, script_keys, duration):
    out = env.tmp / "real"
    result = runner.invoke(app, ["media", *[str(FIX / f) for f in files], "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "Completeness gate PASS" in result.output
    assert last_line_run_dir(result.output) == out.resolve()
    text = raw_transcript(out).read_text(encoding="utf-8")
    heard = _words(text)
    spoken = _words(" ".join(SCRIPTS[k] for k in script_keys))
    recall = len(spoken & heard) / len(spoken)
    assert recall >= (0.6 if "timkoo" in files[0] else 0.8), (recall, sorted(spoken - heard))
    check_media_run(out, duration=duration)
    ledger_is_subscription_only(env.llm)


@pytest.mark.integration
def test_process_no_captions_real_parakeet(env, real_asr):
    env.routes[ASR_URL] = info_asr()
    out = env.tmp / "real_video"
    result = runner.invoke(app, ["process", ASR_URL, "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = raw_transcript(out).read_text(encoding="utf-8").lower()
    assert "resource group" in text
    check_media_run(out, published="2026-07-20", duration=15)
    ledger_is_subscription_only(env.llm)
