#!/usr/bin/env python3
"""Regenerate the WP-M1b integration fixtures in this directory.

    .venv/bin/python tests/fixtures/mvp/make_fixtures.py            # everything
    .venv/bin/python tests/fixtures/mvp/make_fixtures.py --only docs,captions

Groups (each idempotent; files are overwritten):

* docs     — guide.md, notes.txt, article.html, embed_page.html,
             tiny_book.epub (zip written by hand), tiny_paper.pdf (pymupdf).
* captions — cca_slice.en.json3 + yt_info*.json, trimmed READ-ONLY from the
             CCA-F captions run in previous_run_results/ (first 7 minutes).
* audio    — synthetic speech via macOS `say` + ffmpeg: talk_15s.m4a,
             timkoo_truncated.aac (ending cut mid-frame, like a recorder that
             lost power), cam_0001.mov + cam_0002.mov (black video track,
             contiguous creation_time = one camera recording split in two).

The spoken scripts are written to speech_scripts.json so tests know the words
(the default suite's fake ASR emits them; the integration test checks real
parakeet output against them).

Never reads /Volumes, ~/Movies/CLE-AI-26_09_safety_copies or the iMovie
library. The CCA source dir is only read.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

CCA_DIR_NAME = (
    "20260906_claude_certified_architect_foundations_prepare_for_and_pass_the_exam_captions")
SLICE_S = 420  # seconds of the CCA-F captions kept (0:00–7:00)

VOICE = "Samantha"
SPEECH = {
    "talk_15s": (
        "Welcome to this short lesson on resource groups. "
        "A resource group holds related cloud resources so you can manage them together. "
        "Finally, delete the group to remove every resource inside it."
    ),
    "timkoo_truncated": (
        "This is a voice memo about the quarterly planning meeting. "
        "We agreed to ship the reporting feature first and review the budget next week."
    ),
    "cam_0001": (
        "Good evening everyone and welcome to the meetup. "
        "Tonight our first talk covers vector databases."
    ),
    "cam_0002": (
        "Vector databases store embeddings for similarity search. "
        "Thanks for coming, and see you next month."
    ),
}


# --- helpers ---------------------------------------------------------------------


def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("ffmpeg not on PATH")
    return found


def _say(text: str, out_aiff: Path) -> None:
    if not shutil.which("say"):
        raise SystemExit("macOS `say` not available; audio fixtures cannot be regenerated")
    _run(["say", "-v", VOICE, "-r", "175", "-o", str(out_aiff), text])


# --- docs ------------------------------------------------------------------------

GUIDE_MD = """# Field Guide to Resource Groups

A short guide used by the integration tests. Every section has its own heading
so the plaintext extractor yields one locator per section.

## What a resource group is

A resource group is a container that holds related resources for one
solution. Resources in a group share a lifecycle: they are deployed, updated
and deleted together.

## Naming and tags

Use a naming convention such as rg-app-env-region. Tags record the owner, the
cost centre and the environment, and they are inherited by nothing.

## Deleting a group

Deleting a resource group deletes every resource inside it. Lock production
groups with a delete lock before anyone runs a cleanup script.
"""

NOTES_TXT = """Meeting notes - quarterly planning

The team agreed to ship the reporting feature first. Sales asked for CSV
export, and engineering estimated two sprints for it.

The budget review moves to next week. Finance wants the cloud bill broken
down by team before approving new spend.

Action items: Dana drafts the export spec; Lee prepares the cost breakdown.
"""

ARTICLE_HTML = """<!doctype html>
<html><head><title>Understanding Vector Databases</title></head>
<body>
<article>
<h1>Understanding Vector Databases</h1>
<p>A vector database stores embeddings: lists of numbers that place similar
items close together. Search becomes a nearest-neighbour query instead of a
keyword match.</p>
<h2>Indexes</h2>
<p>Approximate indexes such as HNSW trade a little recall for a large speed-up.
The index is built once and updated as vectors arrive.</p>
<h2>When not to use one</h2>
<p>If your data fits in memory and changes rarely, a flat array and a dot
product are simpler and exact. Measure before adding infrastructure.</p>
</article>
</body></html>
"""

EMBED_PAGE_HTML = """<!doctype html>
<html><head><title>Course lesson 1</title></head>
<body>
<h1>Lesson 1: the exam</h1>
<p>Watch the lesson below, then try the quiz.</p>
<iframe width="560" height="315" src="https://www.youtube.com/embed/mvpEmbed001"
  title="Lesson video" allowfullscreen></iframe>
</body></html>
"""

EPUB_CHAPTERS = [
    ("ch1.xhtml", "1. Foundations",
     "<p>An agent is a loop: the model chooses a tool, the tool runs, and the "
     "result goes back to the model. The loop ends when the model answers.</p>"
     "<p>Keep tool descriptions short and specific so the model picks the right one.</p>"),
    ("ch2.xhtml", "2. Tools",
     "<p>Each tool has a name, a description and an input schema. Validate inputs "
     "before acting on them.</p>"
     "<pre><code>def add(a: int, b: int) -&gt; int:\n    return a + b</code></pre>"),
    ("ch3.xhtml", "3. Evaluation",
     "<p>Evaluate an agent on a fixed set of tasks. Record the pass rate, the "
     "number of tool calls and the cost of every run.</p>"),
]


def _xhtml(title: str, body: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            f"<title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>")


def make_epub(path: Path) -> None:
    manifest = "".join(
        f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, (href, _, _) in enumerate(EPUB_CHAPTERS, 1))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(EPUB_CHAPTERS) + 1))
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:identifier id="id">mvp-tiny-book</dc:identifier>'
           '<dc:title>Tiny Agents Handbook</dc:title><dc:creator>MVP Fixture</dc:creator>'
           '<dc:language>en</dc:language></metadata>'
           f'<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
           f'properties="nav"/>{manifest}</manifest><spine>{spine}</spine></package>')
    nav_items = "".join(f'<li><a href="{href}">{title}</a></li>'
                        for href, title, _ in EPUB_CHAPTERS)
    nav = ('<?xml version="1.0" encoding="utf-8"?>'
           '<html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>'
           f'<body><nav epub:type="toc"><ol>{nav_items}</ol></nav></body></html>')
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        zf.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        for href, title, body in EPUB_CHAPTERS:
            zf.writestr(f"OEBPS/{href}", _xhtml(title, body), zipfile.ZIP_DEFLATED)


PDF_PAGES = [
    ("Introduction", "This paper measures how chunk size changes retrieval quality. "
     "We test chunks of 200, 500 and 1000 words on one corpus."),
    ("Method", "Each chunk is embedded once. Queries are scored by recall at ten. "
     "The corpus has 4,000 documents and 300 queries."),
    ("Results", "Chunks of 500 words gave the best recall at ten, 0.81, against 0.74 "
     "for 200 words and 0.77 for 1000 words."),
]


def make_pdf(path: Path) -> None:
    import pymupdf as fitz

    doc = fitz.open()
    for title, body in PDF_PAGES:
        page = doc.new_page(width=420, height=595)
        page.insert_text((40, 60), title, fontsize=18)
        page.insert_textbox(fitz.Rect(40, 80, 380, 560), body, fontsize=11)
    doc.set_metadata({"title": "Chunk Size and Retrieval", "author": "MVP Fixture"})
    doc.set_toc([[1, title, i] for i, (title, _) in enumerate(PDF_PAGES, 1)])
    doc.save(str(path), garbage=4, deflate=True)
    doc.close()


def make_docs() -> None:
    (HERE / "guide.md").write_text(GUIDE_MD, encoding="utf-8")
    (HERE / "notes.txt").write_text(NOTES_TXT, encoding="utf-8")
    (HERE / "article.html").write_text(ARTICLE_HTML, encoding="utf-8")
    (HERE / "embed_page.html").write_text(EMBED_PAGE_HTML, encoding="utf-8")
    make_epub(HERE / "tiny_book.epub")
    make_pdf(HERE / "tiny_paper.pdf")


# --- captions + yt-dlp info dicts --------------------------------------------------

# Fitted to the 7-minute slice from what is said there (the real description's
# 13 chapters lie beyond it, except "Introduction" at 0:47).
SLICE_CHAPTERS = [(0, 47, "Welcome"), (47, 174, "Introduction"),
                  (174, 349, "Study plan and costs"), (349, SLICE_S, "Content outline")]

INFO_KEYS = ("id", "title", "fulltitle", "upload_date", "uploader", "uploader_id",
             "channel", "channel_id", "tags", "categories", "live_status",
             "webpage_url", "original_url", "language", "extractor", "extractor_key")


def _hms(s: int) -> str:
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def make_captions() -> None:
    src = None
    for base in [REPO, *REPO.parents]:
        cand = base / "previous_run_results" / CCA_DIR_NAME
        if (cand / "yt_info.json").exists():
            src = cand
            break
    if src is None:
        raise SystemExit(f"CCA-F captions run not found ({CCA_DIR_NAME})")
    info = json.loads((src / "yt_info.json").read_text(encoding="utf-8"))
    raw = json.loads((src / "reDRM0tqhNs.en-orig.json3").read_text(encoding="utf-8"))

    events = []
    for ev in raw.get("events") or []:
        if not ev.get("segs"):
            continue                       # window/style records
        start = int(ev.get("tStartMs") or 0)
        if start >= SLICE_S * 1000:
            break
        keep = {k: ev[k] for k in ("tStartMs", "dDurationMs", "wWinId", "aAppend") if k in ev}
        keep["dDurationMs"] = min(int(ev.get("dDurationMs") or 0), SLICE_S * 1000 - start)
        keep["segs"] = [{k: s[k] for k in ("utf8", "tOffsetMs") if k in s} for s in ev["segs"]]
        events.append(keep)
    (HERE / "cca_slice.en.json3").write_text(
        json.dumps({"wireMagic": raw.get("wireMagic", "pb3"), "events": events},
                   separators=(",", ":")), encoding="utf-8")

    desc_head = (info.get("description") or "").split("⭐️ Chapter ⭐️")[0].rstrip()
    base = {k: info.get(k) for k in INFO_KEYS if info.get(k) is not None}
    base.update({
        "duration": SLICE_S,
        "duration_string": "7:00",
        "description": desc_head + "\n\n(MVP fixture: first 7 minutes only.)\n",
        "chapters": None,
        "subtitles": {},
        # The real video has auto captions only; the URL is a fixture key the
        # tests route to cca_slice.en.json3 (no network).
        "automatic_captions": {"en-orig": [{"ext": "json3",
                                            "url": "https://fixture.invalid/cca_slice.en.json3",
                                            "name": "English (Original)"}]},
        "_mvp_fixture": f"trimmed from previous_run_results/{CCA_DIR_NAME}/yt_info.json; "
                        f"duration and chapters fitted to the 0-{SLICE_S}s caption slice",
    })
    (HERE / "yt_info.json").write_text(json.dumps(base, indent=1, ensure_ascii=False),
                                       encoding="utf-8")

    platform = dict(base)
    platform["chapters"] = [{"start_time": float(a), "end_time": float(b), "title": t}
                            for a, b, t in SLICE_CHAPTERS]
    (HERE / "yt_info_platform_chapters.json").write_text(
        json.dumps(platform, indent=1, ensure_ascii=False), encoding="utf-8")

    described = dict(base)
    described["description"] = (
        desc_head + "\n\n⭐️ Chapter ⭐️\n"
        + "".join(f"- {_hms(a)} {t}\n" for a, _, t in SLICE_CHAPTERS))
    (HERE / "yt_info_description_chapters.json").write_text(
        json.dumps(described, indent=1, ensure_ascii=False), encoding="utf-8")


# --- audio -----------------------------------------------------------------------


def make_audio() -> None:
    ff = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="mvp-fixtures-") as tmp:
        tmp = Path(tmp)
        for name, text in SPEECH.items():
            _say(text, tmp / f"{name}.aiff")

        # ~15 s m4a (AAC in MP4), mono 16 kHz, low bitrate
        _run([ff, "-hide_banner", "-loglevel", "error", "-y",
              "-i", str(tmp / "talk_15s.aiff"), "-af", "apad=whole_dur=15",
              "-t", "15", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
              str(HERE / "talk_15s.m4a")])

        # ADTS .aac (what Timkoo-style recorders write), then cut the tail
        # mid-frame: the container has no index, the duration is estimated,
        # and the last frame is incomplete.
        full = tmp / "timkoo_full.aac"
        _run([ff, "-hide_banner", "-loglevel", "error", "-y",
              "-i", str(tmp / "timkoo_truncated.aiff"), "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k",
              "-f", "adts", str(full)])
        data = full.read_bytes()
        cut = int(len(data) * 0.9) | 1           # odd offset: inside a frame
        (HERE / "timkoo_truncated.aac").write_bytes(data[:cut])

        # Two 10 s .mov parts, black 160x120 video, contiguous creation_time
        for i, (name, created) in enumerate((("cam_0001", "2026-09-28T22:00:00Z"),
                                              ("cam_0002", "2026-09-28T22:00:10Z"))):
            _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", "color=c=black:s=160x120:r=5:d=10",
                  "-i", str(tmp / f"{name}.aiff"),
                  "-filter:a", "apad=whole_dur=10", "-t", "10",
                  "-map", "0:v:0", "-map", "1:a:0",
                  "-c:v", "libx264", "-preset", "veryslow", "-crf", "40",
                  "-pix_fmt", "yuv420p", "-g", "50",
                  "-c:a", "aac", "-b:a", "32k", "-ac", "1", "-ar", "16000",
                  "-metadata", f"creation_time={created}",
                  "-movflags", "+faststart", str(HERE / f"{name}.mov")])

    (HERE / "speech_scripts.json").write_text(json.dumps(SPEECH, indent=1) + "\n",
                                              encoding="utf-8")


GROUPS = {"docs": make_docs, "captions": make_captions, "audio": make_audio}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default=",".join(GROUPS),
                    help=f"comma list of groups ({', '.join(GROUPS)})")
    args = ap.parse_args()
    for name in [g.strip() for g in args.only.split(",") if g.strip()]:
        GROUPS[name]()
        print(f"made {name}")
    total = sum(p.stat().st_size for p in HERE.iterdir() if p.is_file())
    print(f"fixtures dir: {total / 1024:.0f} KiB")


if __name__ == "__main__":
    main()
