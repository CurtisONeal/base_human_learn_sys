"""Document extraction tests.

Structural tests build EPUBs in-process because that lets a test state exactly
which malformation it is about. The real-corpus tests are marked `corpus` and
skip when the books are absent — they exist because mocked tests already passed
136/136 while every real download 403'd (FIXED_BUGS), and because every bug
found in this module so far came from a real file, not an imagined one.
"""

import zipfile
from pathlib import Path

import pytest

from video2mdnotes.core.chunking import (
    Chunk, chunk_sections, drop_front_matter, is_front_matter,
)
from video2mdnotes.core.documents import extract as extract_any
from video2mdnotes.core.documents.epub import DRMError, extract as extract_epub
from video2mdnotes.core.sources import Section, SourceText, build_full_text

CORPUS = Path("/Users/s_admin/Documents/corpus")
corpus = pytest.mark.skipif(not CORPUS.exists(), reason="book corpus not present")


# --- helpers ---------------------------------------------------------------

def _make_epub(tmp_path, *, spine_docs, nav=None, ncx=None, encrypted=False):
    """Build a minimal but structurally valid EPUB."""
    path = tmp_path / "book.epub"
    manifest, spine = [], []
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        if encrypted:
            zf.writestr("META-INF/encryption.xml", "<encryption/>")
        for i, body in enumerate(spine_docs, 1):
            name = f"c{i}.xhtml"
            zf.writestr(f"OEBPS/{name}",
                f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body>{body}</body></html>")
            manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
        toc_attr = ""
        if nav is not None:
            zf.writestr("OEBPS/nav.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><body>' + nav + "</body></html>")
            manifest.append('<item id="nav" href="nav.xhtml" properties="nav" '
                            'media-type="application/xhtml+xml"/>')
        if ncx is not None:
            zf.writestr("OEBPS/toc.ncx", ncx)
            manifest.append('<item id="ncx" href="toc.ncx" '
                            'media-type="application/x-dtbncx+xml"/>')
            toc_attr = ' toc="ncx"'
        zf.writestr("OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="i"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Test Book</dc:title><dc:language>en</dc:language></metadata>"
            f"<manifest>{''.join(manifest)}</manifest>"
            f"<spine{toc_attr}>{''.join(spine)}</spine></package>")
    return path


# --- EPUB structure --------------------------------------------------------

def test_reads_spine_order_not_toc_order(tmp_path):
    """The spine is the reading order; a TOC may lie about it or omit entries."""
    nav = ('<nav epub:type="toc"><ol>'
           '<li><a href="c2.xhtml">Second</a></li>'
           '<li><a href="c1.xhtml">First</a></li></ol></nav>')
    book = _make_epub(tmp_path, spine_docs=["<p>alpha text</p>", "<p>beta text</p>"], nav=nav)
    result = extract_epub(book)
    assert result.full_text.index("alpha") < result.full_text.index("beta")


def test_chapter_missing_from_toc_is_still_extracted(tmp_path):
    """Ordering by TOC would silently drop content the publisher left out."""
    nav = '<nav epub:type="toc"><ol><li><a href="c1.xhtml">Only One</a></li></ol></nav>'
    book = _make_epub(tmp_path, spine_docs=["<p>first</p>", "<p>orphaned content</p>"], nav=nav)
    assert "orphaned content" in extract_epub(book).full_text


def test_springer_program_code_class_is_fenced(tmp_path):
    """Measured: Springer marks listings with class="ProgramCode", not <pre>.

    A bare <pre>/<code> test found zero code blocks across three real books
    that visibly contain code.
    """
    book = _make_epub(tmp_path, spine_docs=[
        '<p>prose</p><div class="ProgramCode">import os</div>'])
    text = extract_epub(book).full_text
    assert "```" in text and "import os" in text


def test_page_list_nav_is_not_treated_as_structure(tmp_path):
    """A nav page-list yielded 1833 bogus "sections" for a 31-chapter book."""
    nav = ('<nav epub:type="toc"><ol><li><a href="c1.xhtml#real">Real Chapter</a></li></ol></nav>'
           '<nav epub:type="page-list"><ol>'
           '<li><a href="c1.xhtml#p1">xxxix</a></li>'
           '<li><a href="c1.xhtml#p2">xl</a></li></ol></nav>')
    book = _make_epub(tmp_path, spine_docs=[
        '<div id="real"><p>body</p></div><div id="p1">x</div><div id="p2">y</div>'], nav=nav)
    titles = [s.path[-1] for s in extract_epub(book).sections]
    assert "xxxix" not in titles and "xl" not in titles


def test_ncx_toc_is_read_when_there_is_no_nav(tmp_path):
    ncx = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
           '<navMap><navPoint><navLabel><text>Chapter One</text></navLabel>'
           '<content src="c1.xhtml"/></navPoint></navMap></ncx>')
    book = _make_epub(tmp_path, spine_docs=["<p>body text</p>"], ncx=ncx)
    result = extract_epub(book)
    assert result.provenance == "epub (toc.ncx)"
    assert result.sections[0].path[-1] == "Chapter One"


def test_no_toc_marks_structure_as_inferred(tmp_path):
    """A guess must announce itself, or its locators look authoritative."""
    book = _make_epub(tmp_path, spine_docs=["<h1>Some Heading</h1><p>body</p>"])
    result = extract_epub(book)
    assert result.structure_is_inferred is True
    assert result.provenance == "epub (none)"


def test_drm_is_refused_not_circumvented(tmp_path):
    book = _make_epub(tmp_path, spine_docs=["<p>x</p>"], encrypted=True)
    with pytest.raises(DRMError, match="does not remove DRM"):
        extract_epub(book)


def test_adjacent_sections_sharing_a_path_are_merged(tmp_path):
    """Publishers put ids on figures and paragraphs, not only on headings."""
    nav = ('<nav epub:type="toc"><ol>'
           '<li><a href="c1.xhtml#a">Same Title</a></li>'
           '<li><a href="c1.xhtml#b">Same Title</a></li></ol></nav>')
    book = _make_epub(tmp_path, spine_docs=[
        '<div id="a"><p>first half</p></div><div id="b"><p>second half</p></div>'], nav=nav)
    sections = extract_epub(book).sections
    assert len(sections) == 1
    assert "first half" in sections[0].text and "second half" in sections[0].text


def test_locator_never_invents_a_page_number(tmp_path):
    """An EPUB has no pages; emitting "p. 14" would fabricate precision."""
    nav = '<nav epub:type="toc"><ol><li><a href="c1.xhtml">Preface</a></li></ol></nav>'
    book = _make_epub(tmp_path, spine_docs=["<p>body</p>"], nav=nav)
    assert all("p. " not in s.locator for s in extract_epub(book).sections)


def test_numbered_chapter_yields_a_chapter_locator(tmp_path):
    nav = '<nav epub:type="toc"><ol><li><a href="c1.xhtml">7. Indexing</a></li></ol></nav>'
    book = _make_epub(tmp_path, spine_docs=["<p>body</p>"], nav=nav)
    assert extract_epub(book).sections[0].locator == "ch. 7"


def test_unsupported_extension_fails_clearly(tmp_path):
    target = tmp_path / "notes.docx"
    target.write_text("x")
    with pytest.raises(ValueError, match="Unsupported document type"):
        extract_any(target)


# --- SourceText ------------------------------------------------------------

def test_document_has_no_timeline_so_visual_path_cannot_apply():
    """The visual plugin keys off segments; a book must have none."""
    doc = SourceText(source_id="b", title="B", kind="document", origin="/b.epub")
    assert doc.has_timeline is False and doc.segments == []


def test_build_full_text_does_not_repeat_unchanged_headings():
    sections = [
        Section(path=["Part I", "Ch 1"], text="a"),
        Section(path=["Part I", "Ch 1"], text="b"),
    ]
    assert build_full_text(sections).count("Part I") == 1


# --- chunking --------------------------------------------------------------

def _sec(top, title, words):
    return Section(path=[top, title], locator="ch. 1",
                   text=" ".join(["w"] * words), word_count=words)


def test_chunk_never_straddles_a_top_level_boundary():
    """One chunk, one chapter — that is what makes a citation attributable."""
    chunks = chunk_sections(
        [_sec("Ch 1", "a", 10), _sec("Ch 2", "b", 10)], max_words=10_000)
    assert len(chunks) == 2


def test_oversized_section_is_split_and_declares_itself_partial():
    big = Section(path=["Ch 1", "long"], locator="ch. 1",
                  text="\n\n".join(" ".join(["w"] * 100) for _ in range(50)),
                  word_count=5000)
    chunks = chunk_sections([big], max_words=1000)
    assert len(chunks) > 1
    assert all(c.is_partial for c in chunks)
    assert chunks[0].part == f"1 of {len(chunks)}"


def test_partial_chunk_warns_the_model_about_missing_context():
    big = Section(path=["Ch 1", "long"], locator="ch. 1",
                  text="\n\n".join(" ".join(["w"] * 100) for _ in range(30)),
                  word_count=3000)
    chunks = chunk_sections([big], max_words=500)
    assert "Continued passage" in chunks[-1].to_prompt_text()


def test_no_chunk_exceeds_the_budget():
    sections = [_sec("Ch 1", f"s{i}", 400) for i in range(40)]
    assert all(c.word_count <= 5000 for c in chunk_sections(sections, max_words=5000))


def test_chunk_locator_spans_first_to_last():
    chunk = Chunk(sections=[
        Section(path=["Ch 1"], locator="p. 10", text="a"),
        Section(path=["Ch 1"], locator="p. 14", text="b"),
    ])
    assert chunk.locator == "pp. 10–14"


def test_front_matter_is_dropped_but_preface_is_kept():
    """A preface states the book's thesis; a copyright page states nothing."""
    assert is_front_matter(Section(path=["Copyright Page"]))
    assert is_front_matter(Section(path=["About the Author"]))
    assert not is_front_matter(Section(path=["Preface"]))
    assert not is_front_matter(Section(path=["1. Introduction"]))


def test_front_matter_after_the_body_starts_is_not_dropped():
    """Bounded to the leading run — body text must never be sacrificed."""
    sections = [
        Section(path=["Copyright Page"], text="c", word_count=1),
        Section(path=["1. Intro"], text="body", word_count=1),
        Section(path=["Contents of the API"], text="real", word_count=1),
    ]
    kept = [s.path[0] for s in drop_front_matter(sections)]
    assert kept == ["1. Intro", "Contents of the API"]


# --- real corpus -----------------------------------------------------------

@corpus
def test_real_epub_yields_toc_backed_structure():
    result = extract_epub(CORPUS / "llmops.epub")
    assert result.provenance.startswith("epub (")
    assert result.structure_is_inferred is False
    assert result.word_count > 50_000
    assert any(s.locator.startswith("ch. ") for s in result.sections)


@corpus
def test_real_book_chunks_within_budget():
    result = extract_epub(CORPUS / "llmops.epub")
    chunks = chunk_sections(result.sections)
    assert chunks and all(c.word_count <= 12_000 for c in chunks)


@corpus
def test_real_drm_epub_is_refused():
    drm = CORPUS / "visualizinggenerativeai_howaipaintswritesandassists.epub"
    if not drm.exists():
        pytest.skip("DRM sample absent")
    with pytest.raises(DRMError):
        extract_epub(drm)


@corpus
def test_pdf_and_epub_of_the_same_book_agree_on_length():
    """Two independent extractors over two formats must converge.

    This is the test that caught PDF bookmark duplication: page ranges derived
    from bookmarks that share a start page emitted 213,451 words for a book
    holding 90,778.
    """
    pytest.importorskip("pymupdf")
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    stem = "llmops"
    if not (CORPUS / f"{stem}.pdf").exists():
        pytest.skip("paired sample absent")
    epub_words = extract_epub(CORPUS / f"{stem}.epub").word_count
    pdf_words = extract_pdf(CORPUS / f"{stem}.pdf").word_count
    assert abs(pdf_words - epub_words) / epub_words < 0.10


@corpus
def test_pdf_locators_are_pages_and_epub_locators_are_not():
    """Each format cites what it actually knows."""
    pytest.importorskip("pymupdf")
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    if not (CORPUS / "llmops.pdf").exists():
        pytest.skip("paired sample absent")
    pdf_result = extract_pdf(CORPUS / "llmops.pdf")
    assert any(s.locator.startswith(("p. ", "pp. ")) for s in pdf_result.sections)
    epub_result = extract_epub(CORPUS / "llmops.epub")
    assert not any(s.locator.startswith(("p. ", "pp. ")) for s in epub_result.sections)


# --- summarizer wiring -----------------------------------------------------

def test_claude_cli_receives_the_callers_system_prompt(tmp_path, monkeypatch):
    """The CLI takes a system prompt as a file; the caller's must reach it.

    Regression: the failover loop passed `settings.prompt_file` unconditionally
    and discarded its `system_prompt` argument, so every book chunk was
    summarized with the *video* prompt — losing the locator discipline and the
    SKIP rule entirely. It ran without error, which is why only reading the
    process arguments caught it.
    """
    from video2mdnotes.core import summarizer

    seen = {}

    def _fake_cli(model, system_prompt_file, user_message):
        seen["text"] = Path(system_prompt_file).read_text(encoding="utf-8")
        return "ok"

    monkeypatch.setattr(summarizer, "_complete_claude_cli", _fake_cli)
    monkeypatch.setattr(summarizer.settings, "llm_models", ["claude-cli/haiku"])

    output, model = summarizer.complete_with_failover("SENTINEL SYSTEM PROMPT", "body")
    assert output == "ok" and model == "claude-cli/haiku"
    assert seen["text"] == "SENTINEL SYSTEM PROMPT"


def test_chunk_prompt_demands_locators_and_allows_skip():
    """Both rules are what make book notes checkable and cheap."""
    from video2mdnotes.core.summarizer import _CHUNK_SYSTEM
    assert "locator" in _CHUNK_SYSTEM.lower()
    assert "SKIP" in _CHUNK_SYSTEM


# --- PDF structure ---------------------------------------------------------

def _make_pdf(tmp_path, pages, toc=None):
    """A real PDF with real text and a real outline."""
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for body in pages:
        page = doc.new_page()
        # Pad non-empty pages past the scanned-PDF threshold, so these tests
        # exercise structure rather than tripping the no-text-layer guard.
        text = f"{body} lorem ipsum dolor sit amet consectetur adipiscing" if body else ""
        page.insert_text((72, 100), text, fontsize=11)
    if toc:
        doc.set_toc(toc)
    doc.save(path)
    doc.close()
    return path


def test_non_monotonic_outline_does_not_swallow_the_book(tmp_path):
    """A PDF outline may point backwards; real ones do.

    LLMOps ends its outline with three production artifacts
    ("7x9_web_Rabbit.pdf", "Blank Page", "Blank Page") all pointing at page 1.
    Letting each entry claim "my page to the end" in *outline* order gave the
    last of those the entire 284-page book — one section, still reporting
    provenance "pdf (bookmarks)".
    """
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    book = _make_pdf(
        tmp_path,
        [f"content of page {i}" for i in range(1, 7)],
        toc=[[1, "Chapter One", 1], [1, "Chapter Two", 4], [1, "Blank Page", 1]],
    )
    sections = extract_pdf(book).sections
    assert len(sections) > 1
    # The real failure was one section spanning the entire document.
    assert all(s.locator != "pp. 1–6" for s in sections)
    assert any(s.locator.startswith(("p. 4", "pp. 4")) for s in sections)


def test_bookmarked_pdf_keeps_its_own_title(tmp_path):
    """The section loop reused `title`, so every bookmarked PDF was renamed
    after its LAST bookmark — the notes header, `book` output and the catalog
    all showed "Results" for a paper titled "Chunk Size and Retrieval"."""
    pymupdf = pytest.importorskip("pymupdf")
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    book = _make_pdf(tmp_path, [f"content of page {i}" for i in range(1, 4)],
                     toc=[[1, "Introduction", 1], [1, "Method", 2], [1, "Results", 3]])
    doc = pymupdf.open(book)
    doc.set_metadata({"title": "Chunk Size and Retrieval"})
    doc.saveIncr()
    doc.close()
    source = extract_pdf(book)
    assert source.title == "Chunk Size and Retrieval"
    assert [s.locator for s in source.sections] == [
        "p. 1 §Introduction", "p. 2 §Method", "p. 3 §Results"]
    # no metadata title: the file name, never a section heading
    (tmp_path / "plain").mkdir()
    plain = _make_pdf(tmp_path / "plain", [f"content of page {i}" for i in range(1, 3)],
                      toc=[[1, "Only Chapter", 1]])
    assert extract_pdf(plain).title == "book"


def test_pdf_sections_are_emitted_in_page_order(tmp_path):
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    book = _make_pdf(
        tmp_path,
        [f"page {i} text here" for i in range(1, 7)],
        toc=[[1, "Later", 4], [1, "Earlier", 1]],
    )
    titles = [s.path[-1] for s in extract_pdf(book).sections]
    assert titles.index("Earlier") < titles.index("Later")


def test_pdf_pages_are_never_double_counted(tmp_path):
    """Bookmarks sharing a start page must not each claim it."""
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    book = _make_pdf(
        tmp_path,
        [f"unique{i} words on this page" for i in range(1, 6)],
        toc=[[1, "A", 1], [2, "B", 1], [1, "C", 3]],
    )
    text = extract_pdf(book).full_text
    assert text.count("unique1") == 1


def test_pdf_without_bookmarks_says_structure_is_inferred(tmp_path):
    from video2mdnotes.core.documents.pdf import extract as extract_pdf
    book = _make_pdf(tmp_path, [f"plain page {i} of prose text" for i in range(1, 5)])
    result = extract_pdf(book)
    assert result.structure_is_inferred is True
    assert result.provenance in ("pdf (headings)", "pdf (pages only)")


def test_scanned_pdf_is_refused_rather_than_summarized(tmp_path):
    """Empty pages would summarize into confident nonsense."""
    from video2mdnotes.core.documents.pdf import ScannedPDFError, extract as extract_pdf
    book = _make_pdf(tmp_path, ["", "", "", ""])
    with pytest.raises(ScannedPDFError, match="looks scanned"):
        extract_pdf(book)


# --- CLI compatibility -----------------------------------------------------

def test_bare_url_still_reaches_process_without_a_verb():
    """Adding the `book` command must not break the published invocation.

    typer requires a subcommand once a second command exists, so
    `python -m video2mdnotes.main <url>` — used by the readme, the overnight
    sweep and any cron entry — would have started failing as a side effect of
    adding a feature.
    """
    from video2mdnotes.main import _with_default_command
    assert _with_default_command(["https://youtube.com/watch?v=x"]) == [
        "process", "https://youtube.com/watch?v=x"]
    assert _with_default_command(["process", "u"]) == ["process", "u"]
    assert _with_default_command(["book", "a.epub"]) == ["book", "a.epub"]
    assert _with_default_command(["--help"]) == ["--help"]
    assert _with_default_command([]) == []


def test_chunk_locator_merges_page_ranges_rather_than_concatenating():
    """Section locators are already ranges; joining them naively is unreadable."""
    chunk = Chunk(sections=[
        Section(path=["Ch 1"], locator="pp. 61–62", text="a"),
        Section(path=["Ch 1"], locator="pp. 93–94", text="b"),
    ])
    assert chunk.locator == "pp. 61–94"


def test_chunk_locator_keeps_non_page_locators_intact():
    """EPUB chapter locators have no numeric range to merge."""
    chunk = Chunk(sections=[
        Section(path=["Preface"], locator="Preface", text="a"),
        Section(path=["Preface"], locator="ch. 1", text="b"),
    ])
    assert chunk.locator == "Preface–ch. 1"


def test_reduce_failure_still_yields_the_passage_notes(monkeypatch):
    """A failed reduce must not discard per-passage work.

    Regression: 16 passages of a book summarized successfully, then the single
    final reduce call timed out at 900s and the exception propagated — throwing
    away every one of those calls. The media path already guarantees the
    equivalent for transcripts.
    """
    from video2mdnotes.core import summarizer

    calls = {"n": 0}

    def _fake(system_prompt, user_message):
        calls["n"] += 1
        if "assembling" in system_prompt:
            raise RuntimeError("timed out after 900 seconds")
        return f"notes for passage {calls['n']}", "claude-cli/haiku"

    monkeypatch.setattr(summarizer, "complete_with_failover", _fake)

    source = SourceText(
        source_id="b", title="Test Book", kind="document", origin="/b.epub",
        sections=[
            Section(path=["1. One"], locator="ch. 1", text="alpha " * 50, word_count=50),
            Section(path=["2. Two"], locator="ch. 2", text="beta " * 50, word_count=50),
        ],
    )
    # max_words=60: two passages (the whole document no longer fits one), so
    # there IS a reduce to fail (WP-D1 sends a sub-budget document in one call).
    result = summarizer.summarize_document(source, max_words=60)
    assert "notes for passage 1" in result.summary_text
    assert "notes for passage 2" in result.summary_text
    assert "timed out" in result.summary_text


def test_exhaustion_during_reduce_is_not_swallowed(monkeypatch):
    """Exhaustion must still propagate so the caller can wait and resume."""
    from video2mdnotes.core import summarizer

    def _fake(system_prompt, user_message):
        if "assembling" in system_prompt:
            raise summarizer.SubscriptionExhausted("out of capacity")
        return "passage notes", "claude-cli/haiku"

    monkeypatch.setattr(summarizer, "complete_with_failover", _fake)
    source = SourceText(
        source_id="b", title="B", kind="document", origin="/b.epub",
        sections=[Section(path=["1. One"], locator="ch. 1", text="x " * 50, word_count=50),
                  Section(path=["2. Two"], locator="ch. 2", text="y " * 50, word_count=50)],
    )
    with pytest.raises(summarizer.SubscriptionExhausted):
        summarizer.summarize_document(source, max_words=60)


def test_conversational_reduce_output_is_rejected(monkeypatch):
    """A model reviewing its input instead of transforming it must not ship.

    Regression: a two-round reduce produced "This is excellent work ✓ Locators
    preserved… Do you want me to merge Chapters 1–5?" — 177 words in place of a
    91,000-word book's notes, written to disk as if it were the summary.
    """
    from video2mdnotes.core import summarizer

    def _fake(system_prompt, user_message):
        if "assembling" in system_prompt:
            return ("This is excellent work. You've created a comprehensive "
                    "document.\n\nDo you want me to merge Chapters 1-5?"), "m"
        return "real passage notes " * 60, "m"

    monkeypatch.setattr(summarizer, "complete_with_failover", _fake)
    source = SourceText(
        source_id="b", title="B", kind="document", origin="/b.epub",
        sections=[Section(path=[f"{i}. Ch"], locator=f"ch. {i}",
                          text="x " * 100, word_count=100) for i in range(1, 4)],
    )
    result = summarizer.summarize_document(source, max_words=150)
    assert "Do you want me to" not in result.summary_text
    assert "real passage notes" in result.summary_text


def test_many_passages_use_one_reduce_round_not_recursion():
    """Recursive reduce compounds loss; batches are concatenated instead."""
    from video2mdnotes.core.summarizer import _REDUCE_BATCH
    assert _REDUCE_BATCH >= 4


def test_a_good_summary_with_a_chat_opener_is_kept_not_rejected():
    """Phrasing is a poor signal; the best real run opened conversationally.

    The 15,247-word EPUB notes began "I'll assemble these chapter notes into a
    comprehensive single reference document" and were excellent. A guard that
    rejects on wording would have thrown them away.
    """
    from video2mdnotes.core.summarizer import _reduce_collapsed, _strip_chat_preamble
    good = ("I'll assemble these chapter notes into one document.\n\n"
            "# LLMOps\n\n" + "substantive content about the book " * 400)
    notes = ["passage " * 500 for _ in range(8)]
    assert not _reduce_collapsed(good, notes)
    assert _strip_chat_preamble(good).startswith("# LLMOps")


def test_reduce_that_returns_a_fragment_is_rejected_by_proportion():
    from video2mdnotes.core.summarizer import _reduce_collapsed
    notes = ["passage content " * 500 for _ in range(16)]
    assert _reduce_collapsed("Sorry, here is a short note.", notes)


def test_image_only_epub_fails_by_name_not_generically(tmp_path):
    """Comics and manga are picture books, not broken files.

    9 of 396 real corpus files are graphic novels: a page image per spine
    document with no prose. "no readable text found" is true but unhelpful —
    the file is fine and the fix would be OCR, which is out of scope.
    """
    from video2mdnotes.core.documents.epub import ImageOnlyEPUBError
    import zipfile
    book = _make_epub(tmp_path, spine_docs=['<img src="p1.jpg"/>'])
    with zipfile.ZipFile(book, "a") as zf:
        for i in range(6):
            zf.writestr(f"OEBPS/images/p{i}.jpg", b"\xff\xd8\xff\xe0fake")
    with pytest.raises(ImageOnlyEPUBError, match="comic, manga or graphic novel"):
        extract_epub(book)


def test_textless_epub_without_images_is_still_a_plain_error(tmp_path):
    """Only claim "picture book" when there are actually pictures."""
    book = _make_epub(tmp_path, spine_docs=["<p>   </p>"])
    with pytest.raises(ValueError, match="no readable text"):
        extract_epub(book)


def test_every_section_carries_its_own_locator_into_the_prompt():
    """Improving Section.locator is useless if the model never sees it.

    Regression: the chunk printed ONE locator at the top and the model,
    reasonably, cited it for everything — 239 bare `[ch. N]` citations from a
    book with 249 distinct section locators. The data had improved; the prompt
    had not. A full 25-minute re-run was spent discovering this.
    """
    chunk = Chunk(sections=[
        Section(path=["2. Intro"], locator="ch. 2", text="alpha", word_count=1),
        Section(path=["2. Intro", "Four Goals"], locator="ch. 2 §Four Goals",
                text="beta", word_count=1),
        Section(path=["2. Intro", "Frameworks"], locator="ch. 2 §Frameworks",
                text="gamma", word_count=1),
    ])
    rendered = chunk.to_prompt_text()
    for section in chunk.sections:
        assert f"[{section.locator}]" in rendered, \
            f"{section.locator!r} never reached the model"
    assert "Cite the locator of the section" in rendered


def test_chunk_prompt_tells_the_model_not_to_use_the_span_locator():
    """Normalise whitespace first — the prompt is hard-wrapped, so the phrases
    are not contiguous in the source string."""
    from video2mdnotes.core.summarizer import _CHUNK_SYSTEM
    flat = " ".join(_CHUNK_SYSTEM.split()).lower()
    assert "not the passage-span locator at the top" in flat
    assert "bare chapter number is not good enough" in flat
