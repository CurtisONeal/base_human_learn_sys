"""WP-01: plaintext, markdown, and HTML as sources.

Extraction tests use real content, not mocks — the same discipline as
test_documents.py and test_media.py, for the same reason: a mocked HTML parser
is whatever the test author imagined, and every real bug in this pipeline so
far came from a real file. The one legitimate mock boundary is the network
call itself (`core/webfetch.fetch_text`), matching test_embeds.py's existing
pattern for the same reason: it fetches from the internet, this is a unit
test, and the parsing logic downstream of the fetch is what actually needs
covering.
"""

from pathlib import Path

import pytest

from video2mdnotes.core.documents import SUPPORTED, extract as extract_any
from video2mdnotes.core.documents.html import extract as extract_html, extract_url
from video2mdnotes.core.documents.plaintext import extract as extract_text


# --- plaintext.py -----------------------------------------------------------

def test_markdown_headings_become_a_nested_section_tree(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text(
        "# Building a RAG Pipeline\n\n"
        "Intro prose before any subheading.\n\n"
        "## Chunking Strategy\n\n"
        "Fixed-size chunking is naive.\n\n"
        "### Semantic Chunking\n\n"
        "Splits on sentence boundaries instead.\n\n"
        "## Retrieval\n\n"
        "Embed each chunk into a vector index.\n"
    )
    result = extract_text(md)
    assert result.provenance == "markdown (headings)"
    paths = [s.path for s in result.sections]
    assert paths == [
        ["Building a RAG Pipeline"],
        ["Building a RAG Pipeline", "Chunking Strategy"],
        ["Building a RAG Pipeline", "Chunking Strategy", "Semantic Chunking"],
        ["Building a RAG Pipeline", "Retrieval"],
    ]


def test_markdown_code_fence_survives_verbatim(tmp_path):
    """The whole point of not re-rendering markdown: nothing touches a fence."""
    md = tmp_path / "notes.md"
    code = "def chunk(text, size=500):\n    return [text[i:i+size] for i in range(0, len(text), size)]"
    md.write_text(f"## Chunking\n\nReference implementation:\n\n```python\n{code}\n```\n\nDone.\n")
    result = extract_text(md)
    assert code in result.sections[0].text
    assert result.sections[0].text.count("```") == 2


def test_markdown_locator_is_the_heading_not_a_fabricated_page(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# Some Section Name\n\nbody\n")
    result = extract_text(md)
    assert result.sections[0].locator == "§Some Section Name"
    assert "p." not in result.sections[0].locator


def test_markdown_with_no_headings_falls_back_to_line_range(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("Just prose.\nNo headings anywhere in this file.\n")
    result = extract_text(md)
    assert result.provenance == "markdown (none)"
    assert result.sections[0].locator.startswith("L1-")
    assert result.structure_is_inferred is True


def test_plain_txt_is_one_section_with_a_line_range_locator(tmp_path):
    txt = tmp_path / "plain.txt"
    txt.write_text("Line one.\nLine two.\nLine three.\n")
    result = extract_text(txt)
    assert result.provenance == "text (lines)"
    assert len(result.sections) == 1
    assert result.sections[0].locator == "L1-3"
    assert result.structure_is_inferred is True


def test_leading_text_before_the_first_markdown_heading_is_kept(tmp_path):
    """Content before any heading is still real content, not discarded."""
    md = tmp_path / "notes.md"
    md.write_text("Front matter paragraph.\n\n# First Real Heading\n\nbody\n")
    result = extract_text(md)
    assert any("Front matter paragraph" in s.text for s in result.sections)


def test_empty_file_fails_clearly(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("")
    with pytest.raises(ValueError, match="empty file"):
        extract_text(empty)


def test_missing_plaintext_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_text(tmp_path / "nope.md")


# --- html.py: local files ----------------------------------------------------

_ARTICLE_HTML = """<!DOCTYPE html>
<html><head><title>Understanding Attention</title>
<script>trackPageview()</script></head>
<body>
<nav>Home | Docs</nav>
<article>
<p>Lead paragraph before any heading.</p>
<h1>Scaled Dot-Product Attention</h1>
<p>Divides by the square root of the key dimension.</p>
<pre class="highlight">attn = softmax(QK^T / sqrt(d_k)) V</pre>
<h2>Why the scaling factor</h2>
<p>Without it, softmax saturates.</p>
</article>
<footer>Copyright 2026</footer>
</body></html>"""


def test_html_title_is_captured_from_head_despite_head_being_skipped(tmp_path):
    """<title> lives inside the skipped <head> and needs its own capture path."""
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    result = extract_html(f)
    assert result.title == "Understanding Attention"


def test_html_boilerplate_is_excluded_from_the_body(tmp_path):
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    result = extract_html(f)
    assert "trackPageview" not in result.full_text
    assert "Home | Docs" not in result.full_text
    assert "Copyright 2026" not in result.full_text


def test_html_code_is_fenced_via_class_not_just_the_pre_tag(tmp_path):
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    result = extract_html(f)
    assert "```" in result.full_text
    assert "softmax(QK" in result.full_text


def test_html_headings_split_into_a_nested_section_tree(tmp_path):
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    result = extract_html(f)
    paths = [s.path for s in result.sections]
    assert ["Scaled Dot-Product Attention"] in paths
    assert ["Scaled Dot-Product Attention", "Why the scaling factor"] in paths


def test_html_lead_text_before_first_heading_is_kept(tmp_path):
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    result = extract_html(f)
    assert any("Lead paragraph before any heading" in s.text for s in result.sections)


def test_html_structure_is_always_marked_inferred(tmp_path):
    """Headings-as-structure is a guess even when it works; say so."""
    f = tmp_path / "article.html"
    f.write_text(_ARTICLE_HTML)
    assert extract_html(f).structure_is_inferred is True


def test_html_with_no_headings_is_one_unplaced_section(tmp_path):
    f = tmp_path / "flat.html"
    f.write_text("<html><body><p>Just one paragraph, no headings.</p></body></html>")
    result = extract_html(f)
    assert len(result.sections) == 1
    assert result.sections[0].locator == "unplaced"


def test_missing_html_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_html(tmp_path / "nope.html")


# --- html.py: fetched articles (network mocked, parsing is real) -----------

def test_extract_url_parses_a_real_fetched_page(monkeypatch):
    from video2mdnotes.core.documents import html as html_mod
    monkeypatch.setattr(html_mod, "fetch_text", lambda url, **k: _ARTICLE_HTML)
    result = extract_url("https://example.test/attention")
    assert result.title == "Understanding Attention"
    assert result.origin == "https://example.test/attention"
    assert result.provenance == "html (fetched)"
    assert "trackPageview" not in result.full_text


def test_extract_url_propagates_fetch_hardening(monkeypatch):
    """extract_url must not bypass webfetch's scheme/size checks with its own fetch."""
    from video2mdnotes.core.documents import html as html_mod

    def _refuse(url, **k):
        raise ValueError("Refusing URL with scheme 'file'")

    monkeypatch.setattr(html_mod, "fetch_text", _refuse)
    with pytest.raises(ValueError, match="Refusing URL"):
        extract_url("file:///etc/passwd")


# --- documents/__init__.py dispatch ------------------------------------------

def test_supported_now_includes_text_markdown_and_html():
    for suffix in (".md", ".markdown", ".txt", ".text", ".html", ".htm", ".epub", ".pdf"):
        assert suffix in SUPPORTED


def test_dispatch_routes_markdown_to_plaintext(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# Heading\n\nbody\n")
    result = extract_any(f)
    assert result.extractor == "plaintext.extract/1"


def test_dispatch_routes_html_to_html_module(tmp_path):
    f = tmp_path / "page.html"
    f.write_text("<html><body><p>text</p></body></html>")
    result = extract_any(f)
    assert result.extractor == "html.extract/1"


# --- CLI: process() on a local document / article fallback ------------------

def test_process_on_a_local_markdown_file_summarizes_it(tmp_path, monkeypatch):
    """Design doc acceptance #1: `process ./notes.md` summarizes."""
    import datetime as dt
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.core.summarizer import SummaryResult
    from video2mdnotes.main import app

    notes = tmp_path / "notes.md"
    notes.write_text("# A Real Heading\n\nSome real content to summarize.\n")
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")
    monkeypatch.setattr(
        "video2mdnotes.core.summarizer.summarize_document",
        lambda source, **k: SummaryResult(
            source_file=Path(source.origin), model_name="test",
            summary_text="## Summary\n\nnotes", generated_at=dt.datetime.now()),
    )

    result = CliRunner().invoke(app, ["process", str(notes)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "articles" / "notes.summary.md").exists()


def test_process_falls_back_to_article_extraction_when_not_a_video(tmp_path, monkeypatch):
    """Design doc acceptance #2: `process <public article>` summarizes."""
    import datetime as dt
    import yt_dlp
    from typer.testing import CliRunner
    from video2mdnotes.config import settings
    from video2mdnotes.core.summarizer import SummaryResult
    from video2mdnotes.main import app

    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")
    monkeypatch.setattr(
        "video2mdnotes.main.probe_source",
        lambda url: (_ for _ in ()).throw(yt_dlp.utils.DownloadError("Unsupported URL")),
    )
    monkeypatch.setattr(
        "video2mdnotes.core.documents.html.fetch_text", lambda url, **k: _ARTICLE_HTML
    )
    monkeypatch.setattr(
        "video2mdnotes.core.summarizer.summarize_document",
        lambda source, **k: SummaryResult(
            source_file=Path(source.origin), model_name="test",
            summary_text="## Summary\n\nnotes", generated_at=dt.datetime.now()),
    )

    result = CliRunner().invoke(app, ["process", "https://example.test/attention"])
    assert result.exit_code == 0, result.output
    written = list((tmp_path / "out" / "articles").glob("*.summary.md"))
    assert written, "article fallback never wrote a summary"


def test_process_youtube_path_is_untouched_by_the_local_file_check(tmp_path, monkeypatch):
    """A URL must never be mistaken for a local path that happens not to exist."""
    from video2mdnotes.main import app
    from typer.testing import CliRunner

    calls = {"probed": False}

    def _fake_probe(url):
        calls["probed"] = True
        return []

    monkeypatch.setattr("video2mdnotes.main.probe_source", _fake_probe)
    CliRunner().invoke(app, ["process", "https://www.youtube.com/watch?v=aaaaaaaaaaa"])
    assert calls["probed"], "a YouTube URL was not routed through probe_source at all"


def test_doubled_hash_in_a_heading_title_is_stripped(tmp_path):
    """Found on a real vault note: '# # Real Title' — an export artifact.

    A heading's own title is metadata, not body content, so unlike a fenced
    code block it is fair to normalise rather than keep verbatim.
    """
    md = tmp_path / "notes.md"
    md.write_text("# # API Design and Fundamentals\n\nbody\n")
    result = extract_text(md)
    assert result.sections[0].path == ["API Design and Fundamentals"]
    assert "#" not in result.sections[0].locator.lstrip("§")
