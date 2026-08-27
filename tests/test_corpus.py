"""Corpus catalog tests.

The load-bearing test in here is `test_indexing_makes_no_llm_call_and_no_network`.
Everything else about this module is a convenience; that property is the reason
it exists. The moment cataloguing costs money it stops being the thing you run
habitually, and the expensive stage loses the cheap filter in front of it.
"""

import zipfile
from pathlib import Path

import pytest

from video2mdnotes.core.corpus import (
    book_toc, find_headings, index, list_books, overview,
)
from video2mdnotes.core.corpus.store import catalog, needs_reindex

CORPUS = Path("/Users/s_admin/Documents/corpus")
corpus_present = pytest.mark.skipif(
    not CORPUS.exists(), reason="book corpus not present"
)


def _epub(path: Path, title: str, chapters):
    """A minimal valid EPUB with a nav TOC."""
    manifest, spine, nav = [], [], []
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        for i, (heading, body) in enumerate(chapters, 1):
            zf.writestr(f"OEBPS/c{i}.xhtml",
                f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<body><h1>{heading}</h1><p>{body}</p></body></html>")
            manifest.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            nav.append(f'<li><a href="c{i}.xhtml">{heading}</a></li>')
        zf.writestr("OEBPS/nav.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            f'<nav epub:type="toc"><ol>{"".join(nav)}</ol></nav></body></html>')
        manifest.append('<item id="nav" href="nav.xhtml" properties="nav" '
                        'media-type="application/xhtml+xml"/>')
        zf.writestr("OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0" unique-identifier="i"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:language>en</dc:language></metadata>"
            f"<manifest>{''.join(manifest)}</manifest>"
            f"<spine>{''.join(spine)}</spine></package>")
    return path


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    _epub(root / "rag.epub", "Mastering RAG", [
        ("1. Introduction to RAG", "retrieval augmented generation " * 60),
        ("2. Graph RAG and Knowledge Graphs", "graph traversal over entities " * 60),
    ])
    _epub(root / "ops.epub", "LLMOps", [
        ("1. Deployment", "serving models in production " * 60),
        ("2. Monitoring", "drift detection and alerting " * 60),
    ])
    return root


# --- The property this module exists for -----------------------------------

def test_indexing_makes_no_llm_call_and_no_network(library, tmp_path, monkeypatch):
    """Cataloguing must be free. This is the whole design premise.

    Both the model call and the socket are replaced with something that raises,
    so any attempt to reach either fails the test rather than quietly costing
    money on a 396-file run.
    """
    import socket
    from video2mdnotes.core import summarizer

    def _no_llm(*args, **kwargs):
        raise AssertionError("catalog made an LLM call")

    def _no_socket(*args, **kwargs):
        raise AssertionError("catalog opened a network connection")

    monkeypatch.setattr(summarizer, "complete_with_failover", _no_llm)
    monkeypatch.setattr(summarizer, "_complete", _no_llm)
    monkeypatch.setattr(summarizer, "_complete_claude_cli", _no_llm)
    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(socket, "create_connection", _no_socket)

    report = index(library, tmp_path / "cat.db")
    assert report.indexed == 2


# --- Idempotence and change detection --------------------------------------

def test_reindexing_unchanged_files_is_a_no_op(library, tmp_path):
    db = tmp_path / "cat.db"
    assert index(library, db).indexed == 2
    second = index(library, db)
    assert second.indexed == 0 and second.skipped_unchanged == 2


def test_a_changed_file_is_reindexed(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    _epub(library / "ops.epub", "LLMOps Second Edition", [
        ("1. Deployment", "new content entirely " * 60),
    ])
    report = index(library, db)
    assert report.indexed == 1
    assert any(b["title"] == "LLMOps Second Edition" for b in list_books(db))


def test_force_reindexes_everything(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    assert index(library, db, force=True).indexed == 2


def test_reindex_replaces_sections_rather_than_accumulating(library, tmp_path):
    """Delete-then-insert: sections have no stable identity across a re-parse."""
    db = tmp_path / "cat.db"
    index(library, db)
    before = len(book_toc(db, "Mastering RAG", max_depth=9))
    index(library, db, force=True)
    assert len(book_toc(db, "Mastering RAG", max_depth=9)) == before


# --- Failures are data, not aborts -----------------------------------------

def test_an_unreadable_file_is_recorded_and_the_run_continues(library, tmp_path):
    """The `None` playlist-entry lesson: one bad input must not kill the run."""
    (library / "broken.epub").write_bytes(b"not a zip at all")
    report = index(library, tmp_path / "cat.db")
    assert report.indexed == 2 and report.failed == 1
    assert "broken.epub" in report.failures


def test_a_failed_book_is_queryable_with_its_reason(library, tmp_path):
    db = tmp_path / "cat.db"
    (library / "broken.epub").write_bytes(b"not a zip")
    index(library, db)
    failed = [b for b in list_books(db) if b["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["error"]


def test_overview_counts_only_readable_books(library, tmp_path):
    db = tmp_path / "cat.db"
    (library / "broken.epub").write_bytes(b"nope")
    index(library, db)
    stats = overview(db)
    assert stats["books"] == 2 and stats["failed"] == 1


# --- Queries ---------------------------------------------------------------

def test_heading_search_finds_the_book_that_gave_the_topic_a_section(library, tmp_path):
    """A heading match means the author gave the topic its own section."""
    db = tmp_path / "cat.db"
    index(library, db)
    hits = find_headings(db, "graph")
    assert hits and hits[0]["book"] == "Mastering RAG"
    assert "Graph RAG" in hits[0]["heading_path"]


def test_heading_search_returns_nothing_rather_than_guessing(library, tmp_path):
    """An empty result is a real answer: nothing you own covers it."""
    db = tmp_path / "cat.db"
    index(library, db)
    assert find_headings(db, "kubernetes") == []


def test_search_covers_headings_not_bodies(library, tmp_path):
    """Bodies are never stored; only headings are searchable — by design.

    Storing 39.7 million words to make bodies searchable would put a copy of
    the whole library in a derived artifact, which is both a size and a
    copyright problem (D4).
    """
    db = tmp_path / "cat.db"
    index(library, db)
    # "traversal" appears only in a chapter BODY, never in a heading.
    assert find_headings(db, "traversal") == []
    assert find_headings(db, "Knowledge") != []


def test_no_book_text_is_stored_in_the_catalog(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    blob = db.read_bytes()
    assert b"retrieval augmented generation retrieval" not in blob


def test_toc_returns_sections_in_reading_order(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    rows = book_toc(db, "Mastering RAG")
    assert [r["ordinal"] for r in rows] == sorted(r["ordinal"] for r in rows)


def test_list_can_order_by_size(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    counts = [b["word_count"] for b in list_books(db, order="words")]
    assert counts == sorted(counts, reverse=True)


def test_fingerprint_changes_when_the_file_does(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    with catalog(db) as conn:
        assert not needs_reindex(conn, library / "ops.epub")
    _epub(library / "ops.epub", "LLMOps", [("1. New", "different " * 80)])
    with catalog(db) as conn:
        assert needs_reindex(conn, library / "ops.epub")


def test_nested_directories_are_scanned(library, tmp_path):
    nested = library / "sub" / "deeper"
    nested.mkdir(parents=True)
    _epub(nested / "extra.epub", "Buried Book", [("1. One", "content " * 60)])
    assert index(library, tmp_path / "cat.db").indexed == 3


def test_scan_order_is_deterministic(library):
    """"It broke on file 217" must mean the same file tomorrow."""
    from video2mdnotes.core.corpus.catalog import find_documents
    assert find_documents(library) == find_documents(library)


# --- Real corpus -----------------------------------------------------------

@corpus_present
def test_real_corpus_indexes_without_aborting(tmp_path):
    """396 real files including DRM, comics and a faucet diagram."""
    sample = sorted(CORPUS.glob("*.epub"))[:12]
    if len(sample) < 5:
        pytest.skip("not enough corpus files")
    staging = tmp_path / "sample"
    staging.mkdir()
    for path in sample:
        (staging / path.name).symlink_to(path)
    report = index(staging, tmp_path / "cat.db")
    assert report.scanned == len(sample)
    assert report.indexed + report.failed == len(sample)


# --- Query shape (measured against the real corpus) -------------------------

def test_multiword_query_is_treated_as_a_phrase(library, tmp_path):
    """FTS5 ORs bare terms, which is near-useless at corpus scale.

    Measured: bare `graph rag` returned "Plotting a Simple Line Graph",
    "Adding and Deleting Matrix Rows" and a C# graph-traversal chapter — none
    about the topic. Quoted, it returns the Graph RAG section.
    """
    from video2mdnotes.core.corpus.catalog import _as_phrase
    assert _as_phrase("graph rag") == '"graph rag"'
    assert _as_phrase("kubernetes") == "kubernetes"


def test_fts_operators_are_passed_through_untouched():
    """The escape hatch must survive, or power queries break."""
    from video2mdnotes.core.corpus.catalog import _as_phrase
    for query in ['"graph rag"', "NEAR(graph rag, 3)", "graph*", "rag OR graph"]:
        assert _as_phrase(query) == query


def test_one_book_is_not_listed_six_times(library, tmp_path):
    """Sub-sections share a heading path prefix; without a cap one book floods."""
    db = tmp_path / "cat.db"
    _epub(library / "many.epub", "Graph Heavy", [
        (f"{i}. Graph Topic Number {i}", "content " * 60) for i in range(1, 7)
    ])
    index(library, db)
    hits = find_headings(db, "Graph")
    assert sum(1 for h in hits if h["book"] == "Graph Heavy") == 1


def test_same_title_in_two_formats_counts_once(library, tmp_path):
    """The corpus holds 11 titles in both EPUB and PDF."""
    db = tmp_path / "cat.db"
    _epub(library / "dup_a.epub", "Doubled Book", [("1. Graph Chapter", "x " * 60)])
    _epub(library / "dup_b.epub", "Doubled Book", [("1. Graph Chapter", "x " * 60)])
    index(library, db)
    assert sum(1 for h in find_headings(db, "Graph")
               if h["book"] == "Doubled Book") == 1


def test_topic_report_reports_breadth_and_depth(library, tmp_path):
    db = tmp_path / "cat.db"
    index(library, db)
    from video2mdnotes.core.corpus import topic_report
    report = topic_report(db, "Graph RAG")
    assert report["books_with_a_section"] == 1
    assert report["words_on_topic"] > 0
    assert report["fts_query"] == '"Graph RAG"'


def test_topic_report_on_an_uncovered_topic_is_empty_not_wrong(library, tmp_path):
    """Reporting "you own nothing on this" is a useful, honest answer."""
    db = tmp_path / "cat.db"
    index(library, db)
    from video2mdnotes.core.corpus import topic_report
    report = topic_report(db, "quantum error correction")
    assert report["books_with_a_section"] == 0 and report["hits"] == []


# --- Semantic search, fusion, rerank ---------------------------------------

def test_quantization_preserves_the_candidate_set():
    """int8 is 4x smaller; it must not lose relevant candidates.

    Measured on near-orthogonal vectors, the hardest case: quantization shifts
    scores by ~0.0005, which is enough to swap two items across an exact rank
    boundary but not enough to change which items are in contention. Top-10 set
    overlap 9/10, top-20 overlap 20/20.

    Set membership rather than exact order is the right assertion *because of
    how this pipeline is built*: vector search only has to surface the right
    candidates, and the cross-encoder reorders them afterwards. Asserting exact
    order would pin behaviour the design does not depend on.
    """
    import numpy as np
    from video2mdnotes.core.corpus.embed import dequantize, quantize
    rng = np.random.default_rng(0)
    vectors = rng.normal(size=(50, 384)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    query = vectors[0]

    exact = vectors @ query
    q, scales = quantize(vectors)
    approx = dequantize(q, scales) @ query

    # The scores themselves barely move.
    assert np.abs(np.sort(exact)[::-1][:20] - np.sort(approx)[::-1][:20]).max() < 0.005
    # And the candidate set the reranker will see is unchanged.
    assert set(np.argsort(-exact)[:20]) == set(np.argsort(-approx)[:20])


def test_quantization_round_trip_is_close():
    import numpy as np
    from video2mdnotes.core.corpus.embed import dequantize, quantize
    original = np.array([[0.5, -0.25, 1.0, 0.0]], dtype=np.float32)
    q, scales = quantize(original)
    assert np.allclose(dequantize(q, scales), original, atol=0.01)


def test_rrf_rewards_agreement_between_retrievers():
    """A result both retrievers rank highly must beat one only either found."""
    from video2mdnotes.core.corpus.rank import fuse
    scores = fuse({"lexical": [1, 2, 3], "semantic": [2, 1, 4]})
    assert scores[2] > scores[3] and scores[1] > scores[3]


def test_rrf_uses_rank_not_score():
    """BM25 and cosine are not on a common scale; only position is honest."""
    from video2mdnotes.core.corpus.rank import RRF_K, fuse
    assert fuse({"a": [7]})[7] == pytest.approx(1.0 / (RRF_K + 1))


def test_search_is_lexical_only_when_no_vectors_exist(library, tmp_path):
    """Degrade honestly rather than erroring or silently returning nothing."""
    from video2mdnotes.core.corpus import search
    db = tmp_path / "cat.db"
    index(library, db)
    hits = search(db, "Graph RAG", use_rerank=False)
    assert hits and all(h.found_by == "lexical" for h in hits)


def test_search_never_embeds_when_semantic_is_disabled(library, tmp_path, monkeypatch):
    """`--no-semantic` must not pay the model-load cost."""
    from video2mdnotes.core.corpus import search
    from video2mdnotes.core.corpus import embed as embed_mod

    def _boom(*a, **k):
        raise AssertionError("loaded an embedding model with semantic disabled")

    monkeypatch.setattr(embed_mod.Embedder, "model", property(_boom))
    db = tmp_path / "cat.db"
    index(library, db)
    assert search(db, "Graph", use_semantic=False, use_rerank=False)


def test_rerank_failure_falls_back_to_fused_order(library, tmp_path, monkeypatch):
    """A missing reranker must not break search."""
    from video2mdnotes.core.corpus.rank import Hit, rerank
    hits = [Hit(section_id=i, book="B", heading_path=f"h{i}", locator="ch. 1",
                word_count=10, fused_score=1.0 / (i + 1)) for i in range(5)]
    monkeypatch.setitem(__import__("sys").modules, "fastembed.rerank.cross_encoder", None)
    out = rerank("q", list(hits))
    assert [h.section_id for h in out] == [h.section_id for h in hits]


def test_per_book_collapse_happens_after_ranking(library, tmp_path):
    """Collapsing early would let a weak section block a strong one."""
    from video2mdnotes.core.corpus import search
    db = tmp_path / "cat.db"
    _epub(library / "many.epub", "Graph Heavy", [
        (f"{i}. Graph Topic {i}", "content " * 60) for i in range(1, 6)
    ])
    index(library, db)
    hits = search(db, "Graph", use_rerank=False)
    assert sum(1 for h in hits if h.book == "Graph Heavy") == 1


def test_found_by_reports_which_retriever_surfaced_a_hit():
    from video2mdnotes.core.corpus.rank import Hit
    assert Hit(1, "b", "h", "l", 1, lexical_rank=1, semantic_rank=2).found_by == "both"
    assert Hit(1, "b", "h", "l", 1, lexical_rank=1).found_by == "lexical"
    assert Hit(1, "b", "h", "l", 1, semantic_rank=1).found_by == "semantic"


def test_embedding_is_incremental(library, tmp_path, monkeypatch):
    """Adding books to a catalogued corpus must not re-embed everything."""
    import numpy as np
    from video2mdnotes.core.corpus import build_vectors
    from video2mdnotes.core.corpus import embed as embed_mod

    calls = {"texts": 0}

    def _fake_encode(self, texts, batch_size=256):
        texts = list(texts)
        calls["texts"] += len(texts)
        return np.ones((len(texts), 8), dtype=np.float32)

    monkeypatch.setattr(embed_mod.Embedder, "encode", _fake_encode)
    db = tmp_path / "cat.db"
    index(library, db)
    first = build_vectors(db)
    assert first > 0 and calls["texts"] == first

    calls["texts"] = 0
    assert build_vectors(db) == 0 and calls["texts"] == 0


def test_every_returned_hit_is_reranked_when_rerank_is_on(library, tmp_path):
    """Unscored tail results must never reach the user.

    Regression: rerank ran on a flat top-50 and per-book collapse happened
    afterwards. A few books owned all 50 slots with near-duplicate sections, so
    only two survived the collapse and positions 3-6 came from the unscored
    tail — visibly wrong results ("Doing Math with Python > Visualizing Data
    with Graphs" for a graph-RAG query) carrying no rerank score at all.
    """
    from video2mdnotes.core.corpus import search
    from video2mdnotes.core.corpus import rank as rank_mod

    for i in range(8):
        _epub(library / f"b{i}.epub", f"Book {i}", [
            (f"{j}. Graph Chapter {j}", "graph content " * 60) for j in range(1, 8)
        ])
    monkey_scores = {}

    def _fake_rerank(query, hits, model_name=None, top_k=50):
        for n, hit in enumerate(hits):
            hit.rerank_score = -float(n)
        monkey_scores["seen"] = len(hits)
        return hits

    db = tmp_path / "cat.db"
    index(library, db)
    _orig = rank_mod.rerank
    rank_mod.rerank = _fake_rerank
    try:
        hits = search(db, "Graph", limit=6)
    finally:
        rank_mod.rerank = _orig

    assert hits
    assert all(h.rerank_score is not None for h in hits), \
        "an unscored tail result reached the output"


# --- Volume residency and locating moved files -----------------------------

def test_offline_is_distinguished_from_missing(tmp_path):
    """An unmounted drive is not a broken catalog.

    Treating OFFLINE as an error would mark half the catalog broken every time
    a drive is unplugged, and a report that is wrong half the time gets
    ignored — which is how you stop noticing the MISSING entries that matter.
    """
    from video2mdnotes.core.corpus import MISSING, OFFLINE, PRESENT, presence_of
    real = tmp_path / "here.epub"
    real.write_bytes(b"x")
    assert presence_of(str(real), None) == PRESENT
    assert presence_of("/Volumes/NotMounted/a.epub", "NotMounted") == OFFLINE
    assert presence_of(str(tmp_path / "gone.epub"), None) == MISSING


def test_verify_counts_the_three_states(library, tmp_path):
    from video2mdnotes.core.corpus import verify
    db = tmp_path / "cat.db"
    index(library, db)
    report = verify(db)
    assert report.present == 2 and report.missing == 0
    (library / "ops.epub").unlink()
    after = verify(db)
    assert after.missing == 1 and after.needs_attention


def test_volume_is_recorded_for_external_paths():
    from video2mdnotes.core.corpus.store import volume_of
    assert volume_of(Path("/Volumes/BigDrive/books/a.epub")) == "BigDrive"
    assert volume_of(Path("/Users/x/Documents/corpus/a.epub")) is None
    # The boot disk shows up by name and is not removable.
    assert volume_of(Path("/Volumes/Macintosh HD/Users/x/a.epub")) is None


def test_relocate_finds_a_book_that_moved(library, tmp_path):
    """Humans and agents move files. The catalog should recover, not rot."""
    from video2mdnotes.core.corpus import relocate, verify
    db = tmp_path / "cat.db"
    index(library, db)
    moved_to = tmp_path / "elsewhere"
    moved_to.mkdir()
    (library / "ops.epub").rename(moved_to / "ops.epub")
    assert verify(db).missing == 1

    planned = relocate(db, [moved_to], dry_run=True)
    assert len(planned) == 1 and planned[0]["to"].endswith("elsewhere/ops.epub")
    assert verify(db).missing == 1, "dry run must not change anything"

    relocate(db, [moved_to], dry_run=False)
    assert verify(db).missing == 0


def test_relocate_refuses_a_same_name_different_size_match(library, tmp_path):
    """A different edition is not the same book.

    Citing "ch. 12" of the wrong edition is a confident, checkable-looking
    claim that is wrong — worse than admitting the book is missing.
    """
    from video2mdnotes.core.corpus import relocate, verify
    db = tmp_path / "cat.db"
    index(library, db)
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (library / "ops.epub").unlink()
    _epub(decoy_dir / "ops.epub", "LLMOps Different Edition",
          [(f"{i}. Chapter {i}", "quite different content " * 200) for i in range(1, 9)])

    assert relocate(db, [decoy_dir], dry_run=False) == []
    assert verify(db).missing == 1, "a size mismatch must not be adopted"


def test_relocate_leaves_offline_books_alone(tmp_path):
    """A book on an unmounted drive is not lost and must not be repointed."""
    from video2mdnotes.core.corpus import relocate
    from video2mdnotes.core.corpus.store import BookRow, catalog, upsert_book
    db = tmp_path / "cat.db"
    with catalog(db) as conn:
        upsert_book(conn, BookRow(
            path="/Volumes/Gone/books/ops.epub", fingerprint="10:1",
            title="LLMOps", fmt="epub", provenance="epub (nav.xhtml)",
            indexed_at="2026-01-01", volume="Gone"), [])
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "ops.epub").write_bytes(b"x" * 10)
    assert relocate(db, [decoy], dry_run=False) == []


def test_volume_status_reports_mounted_state(library, tmp_path):
    from video2mdnotes.core.corpus import volume_status
    db = tmp_path / "cat.db"
    index(library, db)
    statuses = volume_status(db)
    assert statuses and statuses[0].name is None and statuses[0].mounted


def test_refresh_volumes_fixes_the_column_after_a_move(library, tmp_path):
    from video2mdnotes.core.corpus import refresh_volumes
    from video2mdnotes.core.corpus.store import catalog
    db = tmp_path / "cat.db"
    index(library, db)
    with catalog(db) as conn:
        conn.execute("UPDATE books SET volume = 'Stale'")
        conn.commit()
    assert refresh_volumes(db) == 2
