"""WP-L1 learning packages: assertions on the artifacts written into a vault.

Every test builds a package into a tmp vault and then OPENS the files: a link
counts only if its target exists and its #anchor / #page / #^block lands on
something real in that target.
"""

import io
import json
import random
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from video2mdnotes.core import package as pk

pymupdf = pytest.importorskip("pymupdf")
PIL = pytest.importorskip("PIL.Image")

LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\s]+)\)")
EMBED = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")


def _png(w, h, seed):
    rnd = random.Random(seed)
    img = PIL.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(w * h)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Fixtures: a tiny EPUB and PDF with figures, plus notes that cite them
# --------------------------------------------------------------------------- #

CH2_TITLE = "2. Retrieval: Ranking & Scoring (BM25)"


@pytest.fixture
def tiny_epub(tmp_path):
    path = tmp_path / "corpus" / "tiny_figs.epub"
    path.parent.mkdir()
    fig_a, fig_b, icon = _png(200, 150, 1), _png(160, 160, 2), _png(32, 32, 3)
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Tiny Search Book</dc:title>
<dc:language>en</dc:language></metadata>
<manifest>
 <item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>
 <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
 <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
 <item id="fa" href="img/postings.png" media-type="image/png"/>
 <item id="fb" href="img/bm25.png" media-type="image/png"/>
 <item id="ic" href="img/icon.png" media-type="image/png"/>
</manifest>
<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""
    nav = f"""<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="toc"><ol>
 <li><a href="ch1.xhtml">1. Indexing Basics</a><ol>
   <li><a href="ch1.xhtml#inv">Inverted Indexes</a></li></ol></li>
 <li><a href="ch2.xhtml">{CH2_TITLE}</a><ol>
   <li><a href="ch2.xhtml#score">Scoring Functions</a></li></ol></li>
</ol></nav></body></html>"""
    ch1 = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>1. Indexing Basics</h1><p>An index makes search fast. # not a heading</p>
<img src="img/icon.png" alt="icon"/>
<h2 id="inv">Inverted Indexes</h2><p>An inverted index maps each term to a postings list.</p>
<figure><img src="img/postings.png" alt="postings"/><figcaption>Figure 1-1 A postings list</figcaption></figure>
</body></html>"""
    ch2 = f"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>{CH2_TITLE}</h1><p>Ranking orders the matching documents.</p>
<h2 id="score">Scoring Functions</h2><p>BM25 saturates term frequency.</p>
<img src="img/bm25.png" alt="BM25 saturation curve"/>
<img src="img/postings.png" alt="same figure again"/>
</body></html>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/nav.xhtml", nav)
        zf.writestr("OEBPS/ch1.xhtml", ch1)
        zf.writestr("OEBPS/ch2.xhtml", ch2)
        zf.writestr("OEBPS/img/postings.png", fig_a)
        zf.writestr("OEBPS/img/bm25.png", fig_b)
        zf.writestr("OEBPS/img/icon.png", icon)
    return path


EPUB_NOTES = f"""# Tiny Search Book: Notes

## 1. Indexing

- Inverted indexes map terms to postings [ch. 1 §Inverted Indexes].
- The chapter as a whole [ch. 1].

---

## 2. Ranking

- BM25 saturates term frequency [ch. 2, §Scoring Functions]; see also [§{CH2_TITLE}].
- A truncated locator [ch. 2 §Scoring Fun…].
- Missing [ch. 9 §Nope] and a page that an EPUB does not have [p. 3].
- Not citations: [sic], [a link](https://example.com), `[ch. 1]` and
- [ ] a checkbox.

```
[ch. 1] inside a fence stays put
```
"""


@pytest.fixture
def tiny_pdf(tmp_path):
    path = tmp_path / "corpus" / "tiny figs paper.pdf"
    path.parent.mkdir(exist_ok=True)
    doc = pymupdf.open()
    logo = _png(150, 150, 9)
    texts = {
        1: ["Introduction", "This paper studies vector search for retrieval."],
        2: ["Chapter 1: Vectors", "Embeddings place text in a vector space."],
        3: ["Cosine Similarity", "Cosine similarity compares the angle between vectors."],
        4: ["Chapter 2: Indexes", "HNSW builds a layered proximity graph."],
    }
    for pno in range(1, 5):
        page = doc.new_page()
        y = 72
        for line in texts[pno]:
            page.insert_text((72, y), line, fontsize=12)
            y += 20
        page.insert_text((72, 110), "Body text continues here with enough words to count.", fontsize=11)
        page.insert_image(pymupdf.Rect(450, 20, 500, 70), stream=logo)       # page furniture
    doc[2].insert_image(pymupdf.Rect(72, 150, 272, 300), stream=_png(200, 150, 4))
    doc[2].insert_text((72, 320), "Figure 1-1 The angle between two vectors", fontsize=10)
    doc[3].insert_image(pymupdf.Rect(72, 150, 232, 310), stream=_png(160, 160, 5))
    doc.set_toc([[1, "Introduction", 1], [1, "Chapter 1: Vectors", 2],
                 [2, "Cosine Similarity", 3], [1, "Chapter 2: Indexes", 4]])
    doc.set_metadata({"title": "Tiny Vector Paper"})
    doc.save(path)
    return path


PDF_NOTES = """# Tiny Vector Paper: Notes

## Vectors

- Cosine similarity compares angles [p. 3 §Cosine Similarity].
- Embeddings and similarity [pp. 2–3].

## Indexes

- HNSW is a layered graph [p. 4 §Chapter 2: Indexes; p. 1 §Introduction].
- Out of range [p. 99].
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _headings(text):
    return [m.group(2) for m in re.finditer(r"^(#{1,6})\s+(.*?)\s*$", text, re.M)]


def _assert_every_link_lands(pkg_dir: Path, expect_pdf_pages=None):
    """Open every relative link target in notes.md; check anchor/page/block."""
    notes = (pkg_dir / "notes.md").read_text(encoding="utf-8")
    checked = 0
    for _, target in LINK.findall(notes):
        if target.startswith("http"):
            continue
        path_part, _, frag = target.partition("#")
        dest = (pkg_dir / unquote(path_part)).resolve()
        assert dest.exists(), target
        if frag.startswith("page="):
            with pymupdf.open(dest) as d:
                assert 1 <= int(frag[5:]) <= d.page_count, target
        elif frag.startswith("^"):
            assert re.search(rf"\s\^{re.escape(frag[1:])}$", dest.read_text(), re.M), target
        elif frag:
            heads = _headings(dest.read_text(encoding="utf-8"))
            assert any(pk.anchor_matches(frag, h) for h in heads), (target, heads)
        checked += 1
    for _, target in EMBED.findall(notes):
        assert (pkg_dir / unquote(target)).exists(), target
    assert pk.check_links(pkg_dir) == []
    return checked


# --------------------------------------------------------------------------- #
# Anchor form
# --------------------------------------------------------------------------- #

def test_obsidian_anchor_form():
    # Heading text, case kept, spaces as %20 (not GitHub kebab).
    assert pk.obsidian_anchor("Inverted Indexes") == "Inverted%20Indexes"
    # Obsidian-reserved characters (: # | ^ [ ]) become spaces and collapse;
    # parentheses are percent-encoded so they cannot end the markdown link.
    assert pk.obsidian_anchor(CH2_TITLE) == "2.%20Retrieval%20Ranking%20&%20Scoring%20%28BM25%29"
    assert pk.anchor_matches(pk.obsidian_anchor(CH2_TITLE), CH2_TITLE)
    assert not pk.anchor_matches("inverted-indexes", "Inverted Indexes")
    assert pk.block_id(754.9) == "t0-12-34"
    assert pk.youtube_id("https://www.youtube.com/watch?v=megA6BPpYqo") == "megA6BPpYqo"
    assert pk.youtube_id("https://meetu.ps/e/Q4QCV/wKlcy/i") is None


# --------------------------------------------------------------------------- #
# EPUB
# --------------------------------------------------------------------------- #

def test_epub_package_tree_links_and_figures(tmp_path, tiny_epub):
    vault = tmp_path / "vault"
    notes_src = _write(tmp_path / "runs" / "tiny.summary.md", EPUB_NOTES)
    res = pk.package(notes_src, "search_basics", source=tiny_epub, vault=vault)

    pkg = vault / "010_learning_system/by_subject/search_basics/tiny_search_book"
    assert res.package_dir == pkg
    assert (pkg / "notes.md").exists() and (pkg / "source_meta.json").exists()
    binary = vault / "__sources/search_basics/tiny_search_book/tiny_search_book.epub"
    assert binary.read_bytes() == tiny_epub.read_bytes()

    texts = sorted(p.name for p in (pkg / "text").glob("*.md"))
    assert texts == ["01_1_indexing_basics.md", "02_2_retrieval_ranking_scoring_bm25.md"]
    ch2 = (pkg / "text" / texts[1]).read_text()
    assert f"# {CH2_TITLE}" in ch2 and "## Scoring Functions" in ch2
    # Prose that starts with '#' cannot become a heading.
    ch1 = (pkg / "text" / texts[0]).read_text()
    assert _headings(ch1) == ["1. Indexing Basics", "Inverted Indexes"]

    notes = (pkg / "notes.md").read_text()
    assert "[ch. 1 §Inverted Indexes](text/01_1_indexing_basics.md#Inverted%20Indexes)" in notes
    assert "[ch. 1](text/01_1_indexing_basics.md#1.%20Indexing%20Basics)" in notes
    assert "[ch. 2, §Scoring Functions](text/02_2_retrieval_ranking_scoring_bm25.md#Scoring%20Functions)" in notes
    assert "[ch. 2 §Scoring Fun…](text/02_2_retrieval_ranking_scoring_bm25.md#Scoring%20Functions)" in notes
    # Non-citations and code are untouched.
    assert "[sic]" in notes and "[a link](https://example.com)" in notes
    assert "`[ch. 1]`" in notes and "[ch. 1] inside a fence stays put" in notes
    assert "- [ ] a checkbox." in notes
    # Unresolvable citations stay as text and are reported, never dropped.
    assert "[ch. 9 §Nope]" in notes and "[p. 3]" in notes
    report = json.loads((pkg / "package_report.json").read_text())
    unresolved = {u["citation"] for u in report["citations"]["unresolved"]}
    assert unresolved == {"ch. 9 §Nope", "p. 3"}
    assert report["citations"]["linked"] == 5
    assert _assert_every_link_lands(pkg) == 6       # 5 citations + the banner link to the EPUB

    # Figures: icon skipped (small), duplicate skipped, two embedded.
    imgs = sorted(p.name for p in (pkg / "images").iterdir())
    assert len(imgs) == 2, imgs
    assert report["images"]["extracted"] == 2
    assert report["images"]["skipped"] == {"small": 1, "duplicate": 1}
    embeds = EMBED.findall(notes)
    assert [c for c, _ in embeds] == ["Figure 1-1 A postings list", "BM25 saturation curve"]
    # Each lands at the end of the notes section citing its chapter.
    sec1, sec2 = notes.split("## 2. Ranking")
    assert "Figure 1-1 A postings list" in sec1 and sec1.index("A postings list") < sec1.index("\n---")
    assert "BM25 saturation curve" in sec2
    # And in the extracted chapter text.
    assert "](../images/" in ch2 and (pkg / "images" / EMBED.findall(ch2)[0][1].split("/")[-1]).exists()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def test_pdf_page_links_relative_path_and_figures(tmp_path, tiny_pdf):
    vault = tmp_path / "vault"
    notes_src = _write(tmp_path / "runs" / "paper.summary.md", PDF_NOTES)
    pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault)
    pkg = vault / "010_learning_system/by_subject/vectors/tiny_vector_paper"
    notes = (pkg / "notes.md").read_text()

    rel = "../../../../__sources/vectors/tiny_vector_paper/tiny_vector_paper.pdf"
    assert f"[p. 3]({rel}#page=3)" in notes
    assert f"[pp. 2–3]({rel}#page=2)" in notes
    assert (pkg / unquote(rel)).resolve() == (vault / "__sources/vectors/tiny_vector_paper/tiny_vector_paper.pdf")
    assert (pkg / rel).resolve().read_bytes() == tiny_pdf.read_bytes()
    # Page + section become two links; the colon heading resolves.
    assert re.search(r"\[§Chapter 2: Indexes\]\(text/\d\d_chapter_2_indexes\.md#Chapter%202%20Indexes\)", notes)
    assert "[§Cosine Similarity](text/02_chapter_1_vectors.md#Cosine%20Similarity)" in notes
    assert "[p. 99]" in notes                                   # outside the PDF: stays text
    report = json.loads((pkg / "package_report.json").read_text())
    assert [u["citation"] for u in report["citations"]["unresolved"]] == ["p. 99"]
    _assert_every_link_lands(pkg)

    # Section text carries the source headings and page markers that link back.
    ch1 = (pkg / "text/02_chapter_1_vectors.md").read_text()
    assert _headings(ch1) == ["Chapter 1: Vectors", "Cosine Similarity"]
    assert "Cosine similarity compares the angle" in ch1.split("## Cosine Similarity")[1]
    assert "#page=3)*" in ch1

    # Figures: the logo on every page is furniture; the two diagrams are kept.
    assert report["images"]["extracted"] == 2
    assert report["images"]["skipped"]["decorative"] == 4
    caps = [c for c, _ in EMBED.findall(notes)]
    assert caps == ["Figure 1-1 The angle between two vectors", "Figure p.4"]
    vec, idx = notes.split("## Indexes")
    assert "Figure 1-1" in vec and "Figure p.4" in idx


def test_pdf_custom_learning_root_paths_still_resolve(tmp_path, tiny_pdf):
    vault = tmp_path / "vault"
    notes_src = _write(tmp_path / "paper.summary.md", PDF_NOTES)
    pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault, learning_root="study")
    pkg = vault / "study/vectors/tiny_vector_paper"
    assert "](../../../__sources/vectors/tiny_vector_paper/tiny_vector_paper.pdf#page=3)" in (pkg / "notes.md").read_text()
    _assert_every_link_lands(pkg)


# --------------------------------------------------------------------------- #
# Idempotency and user-edit protection
# --------------------------------------------------------------------------- #

def test_rerun_is_idempotent_and_protects_user_edits(tmp_path, tiny_pdf):
    vault = tmp_path / "vault"
    notes_src = _write(tmp_path / "paper.summary.md", PDF_NOTES)
    first = pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault)
    pkg = first.package_dir
    snapshot = {p: p.read_bytes() for p in pkg.rglob("*") if p.is_file()}

    again = pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault)
    assert again.written == []
    assert again.report["notes_status"] == "unchanged"
    assert {p: p.read_bytes() for p in pkg.rglob("*") if p.is_file()} == snapshot

    edited = (pkg / "notes.md").read_text() + "\nMy own note.\n"
    (pkg / "notes.md").write_text(edited)
    kept = pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault)
    assert not kept.notes_written
    assert kept.report["notes_status"].startswith("user-edited")
    assert (pkg / "notes.md").read_text() == edited

    forced = pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault, force=True)
    assert forced.notes_written and "My own note." not in (pkg / "notes.md").read_text()
    # After a forced write the stored hash matches again: plain reruns are quiet.
    assert pk.package(notes_src, "vectors", source=tiny_pdf, vault=vault).written == []


def test_dry_run_writes_nothing(tmp_path, tiny_epub):
    vault = tmp_path / "vault"
    notes_src = _write(tmp_path / "tiny.summary.md", EPUB_NOTES)
    res = pk.package(notes_src, "search_basics", source=tiny_epub, vault=vault, dry_run=True)
    assert res.dry_run and not vault.exists()
    assert res.report["citations"]["linked"] == 5


def test_refuses_protected_locations(tmp_path):
    notes_src = _write(tmp_path / "n.summary.md", PDF_NOTES)
    with pytest.raises(pk.PackageError):
        pk.package(notes_src, "vectors", source=Path("/Volumes/SD/book.pdf"), vault=tmp_path)
    with pytest.raises(pk.PackageError):
        pk.package(notes_src, "Bad Subject!", source=None, vault=tmp_path)


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #

MEDIA_NOTES = """---
title: "Some Talk"
---
# Some Talk

| # | chapter | time range |
|---|---|---|
| 1 | Intro | [0:00:00–0:01:05] |

- Opening claim [0:00:05].
- Later claim [0:01:05] and a past-the-end one [9:00:00].
"""


def _media_run(tmp_path, url, duration=120.0):
    run = tmp_path / "run"
    _write(run / "summaries" / "some_talk.summary.md", MEDIA_NOTES)
    _write(run / "source_meta.json", json.dumps({"title": "Some Talk", "url": url,
                                                 "duration_s": duration}))
    _write(run / "transcripts" / "some_talk.md",
           "# Notes\n\n- [0:00:00.320] Hello.\n- [0:00:03.439] Second line.\n"
           "- [0:00:03.900] Same second.\n- [0:00:05.900] Opening claim here.\n"
           "- [0:01:04.100] Later claim.\n")
    _write(run / "transcripts" / "some_talk.corrected.md", "# corrected copy\n")
    return run


def test_youtube_time_links(tmp_path):
    run = _media_run(tmp_path, "https://www.youtube.com/watch?v=megA6BPpYqo")
    res = pk.package(run, "talks", vault=tmp_path / "vault")
    notes = (res.package_dir / "notes.md").read_text()
    assert "[0:00:05](https://youtu.be/megA6BPpYqo?t=5)" in notes
    assert "[0:01:05](https://youtu.be/megA6BPpYqo?t=65)" in notes
    assert "[0:00:00–0:01:05](https://youtu.be/megA6BPpYqo?t=0)" in notes   # range → start
    assert "[9:00:00]" in notes and "past the end" in res.report["citations"]["unresolved"][0]["citation"]
    assert not (res.package_dir / "transcript.md").exists()
    assert notes.startswith("---\ntitle:")                                    # frontmatter kept


def test_local_recording_links_to_transcript_block_ids(tmp_path):
    run = _media_run(tmp_path, "https://meetu.ps/e/xyz")
    res = pk.package(run, "community_events", vault=tmp_path / "vault")
    pkg = res.package_dir
    transcript = (pkg / "transcript.md").read_text()
    assert "- [0:00:03.439] Second line. ^t0-00-03" in transcript
    assert "- [0:00:03.900] Same second.\n" in transcript       # block ids stay unique
    assert "Hello. ^t0-00-00" in transcript
    notes = (pkg / "notes.md").read_text()
    # Cited time → the latest line starting at or before it.
    assert "[0:00:05](transcript.md#^t0-00-05)" in notes
    assert "[0:01:05](transcript.md#^t0-01-04)" in notes
    assert "[0:00:00–0:01:05](transcript.md#^t0-00-00)" in notes
    _assert_every_link_lands(pkg)
    assert res.report["citations"]["linked"] == 3
    # The raw transcript is used, not the corrected copy.
    assert "corrected copy" not in transcript


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_package_command(tmp_path, tiny_pdf):
    from typer.testing import CliRunner
    from video2mdnotes.main import app

    notes_src = _write(tmp_path / "paper.summary.md", PDF_NOTES)
    vault = tmp_path / "vault"
    runner = CliRunner()
    dry = runner.invoke(app, ["package", str(notes_src), "--subject", "vectors",
                              "--source", str(tiny_pdf), "--vault", str(vault), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "DRY RUN" in dry.output and not vault.exists()
    real = runner.invoke(app, ["package", str(notes_src), "--subject", "vectors",
                               "--source", str(tiny_pdf), "--vault", str(vault)])
    assert real.exit_code == 0, real.output
    assert "broken links: 0" in real.output and "unresolved line" in real.output
    assert (vault / "010_learning_system/by_subject/vectors/tiny_vector_paper/notes.md").exists()


def test_book_package_option_packages_existing_notes(tmp_path, tiny_pdf, monkeypatch):
    """`book --package` on an already-summarized book: no model call, package built."""
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.main import app

    out = tmp_path / "books"
    # source_id is the file stem; the existing summary makes `book` skip the model.
    _write(out / f"{tiny_pdf.stem}.summary.md", PDF_NOTES)
    monkeypatch.setattr(settings, "obsidian_vault", tmp_path / "vault")
    monkeypatch.setattr(settings, "last_run_file", tmp_path / "_last_run.json")
    result = CliRunner().invoke(app, ["book", str(tiny_pdf), "--out", str(out),
                                      "--package", "vectors"])
    assert result.exit_code == 0, result.output
    assert "Package:" in result.output
    assert (tmp_path / "vault/010_learning_system/by_subject/vectors/tiny_vector_paper/notes.md").exists()
    assert result.output.strip().splitlines()[-1].startswith("RUN_DIR=")


def test_pdf_wrapped_bookmark_title_is_not_left_in_the_text(tmp_path):
    path = tmp_path / "wrap.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Skill 2.4: Describe the identity protection and", fontsize=14)
    page.insert_text((72, 92), "governance capabilities of Entra", fontsize=14)
    page.insert_text((72, 130), "This objective covers advanced features.", fontsize=11)
    doc.set_toc([[1, "Skill 2.4: Describe the identity protection and governance capabilities of Entra", 1]])
    doc.save(path)
    notes = _write(tmp_path / "w.summary.md", "# Wrap: Notes\n\n## A\n\n- Claim [p. 1 §Skill 2.4].\n")
    res = pk.package(notes, "wrap", source=path, vault=tmp_path / "vault")
    text = next((res.package_dir / "text").glob("*.md")).read_text()
    body = text.split("\n", 3)[-1]
    assert "governance capabilities" not in body.split("\n", 2)[-1]
    assert "This objective covers advanced features." in body
    assert res.report["citations"]["unresolved"] == []          # label prefix resolves
    _assert_every_link_lands(res.package_dir)



def test_pdf_bullet_glyph_lines_become_list_items():
    raw = "Capabilities\n\u25a0\n\u25a0\nContinuous discovery\n-\u200b\nPrioritization\n- already fine"
    assert pk.join_pdf_bullets(raw) == (
        "Capabilities\n- Continuous discovery\n- Prioritization\n- already fine")
