"""Build and query the corpus catalog. No model calls, no network.

The whole point of this module is that it is **free**. It is the same move the
video pipeline makes everywhere else — captions gate Whisper, OCR gates the
vision model — applied to a 39.7-million-word library: read the structure the
publishers already wrote, and use it to decide what is worth paying to read.

Three things follow from "free":

* **No LLM call, ever.** A test asserts it. The moment cataloguing costs money,
  it stops being the thing you run habitually, and the expensive stage loses
  the cheap filter in front of it.
* **No network access.** Everything comes from files already on disk.
* **A failure is data, not an abort.** Nine of 396 real files are image-only
  graphic novels. They are recorded with a reason and the run continues — the
  same lesson as the `None` playlist entry that once killed a whole run.

What it deliberately does NOT do: rank, match topics, embed, or summarize.
Those are WP-05 and cost money. This produces the free evidence they consume.
"""

import datetime as dt
from pathlib import Path
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel

from video2mdnotes.core.corpus.store import (
    BookRow, SectionRow, catalog as open_catalog, fingerprint, needs_reindex,
    upsert_book,
)
from video2mdnotes.core.documents import SUPPORTED, extract
from video2mdnotes.logger import logger


class IndexReport(BaseModel):
    """What one indexing run did. Printed, and useful in tests."""

    scanned: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    words: int = 0
    failures: Dict[str, str] = {}          # filename -> reason

    @property
    def summary(self) -> str:
        return (
            f"{self.indexed} indexed, {self.skipped_unchanged} unchanged, "
            f"{self.failed} failed — {self.words:,} words catalogued"
        )


def find_documents(root: Path) -> List[Path]:
    """Every supported document under root, deterministically ordered.

    Sorted so two runs process files in the same order. That matters for
    reproducing a failure: "it broke on file 217" has to mean the same file
    tomorrow.
    """
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )


def index(
    root: Path,
    db_path: Path,
    force: bool = False,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> IndexReport:
    """Catalog every document under `root`. Idempotent.

    Re-running is cheap by design: a file whose size and mtime are unchanged is
    not re-parsed. Cataloguing 396 files takes minutes the first time and
    seconds thereafter, which is what makes it reasonable to run after every
    corpus change rather than as a ceremony.
    """
    files = find_documents(root)
    report = IndexReport(scanned=len(files))

    with open_catalog(db_path) as conn:
        for position, path in enumerate(files, 1):
            if progress:
                progress(position, len(files), path.name)

            if not force and not needs_reindex(conn, path):
                report.skipped_unchanged += 1
                continue

            resolved = str(path.resolve())
            try:
                source = extract(path)
            except Exception as exc:  # noqa: BLE001 - a bad file is a fact to record
                reason = f"{type(exc).__name__}: {str(exc)[:200]}"
                report.failed += 1
                report.failures[path.name] = reason
                logger.warning(f"catalog: {path.name}: {reason}")
                upsert_book(conn, BookRow(
                    path=resolved,
                    fingerprint=fingerprint(path),
                    title=path.stem,
                    fmt=path.suffix.lower().lstrip("."),
                    provenance="unreadable",
                    indexed_at=dt.datetime.now().isoformat(timespec="seconds"),
                    status="failed",
                    error=reason,
                ), [])
                continue

            sections = [
                SectionRow(
                    book_id=0,                     # set by upsert_book
                    ordinal=ordinal,
                    depth=len(section.path),
                    heading_path=" > ".join(section.path),
                    title=section.path[-1] if section.path else "",
                    locator=section.locator,
                    word_count=section.word_count,
                    href=section.href,
                )
                for ordinal, section in enumerate(source.sections)
            ]

            upsert_book(conn, BookRow(
                path=resolved,
                fingerprint=fingerprint(path),
                title=source.title,
                fmt=path.suffix.lower().lstrip("."),
                language=source.language,
                provenance=source.provenance,
                structure_inferred=source.structure_is_inferred,
                word_count=source.word_count,
                section_count=len(source.sections),
                # Counted at index time because it is the cheapest signal for
                # "is this a programming book", and WP-05 will want it.
                code_blocks=source.full_text.count("```") // 2,
                indexed_at=dt.datetime.now().isoformat(timespec="seconds"),
            ), sections)

            report.indexed += 1
            report.words += source.word_count

    return report


# --- Queries ---------------------------------------------------------------

def overview(db_path: Path) -> Dict:
    """Corpus-level counts. The "what do I own" answer."""
    with open_catalog(db_path) as conn:
        row = conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(word_count),0) w,
                      COALESCE(SUM(section_count),0) s,
                      COALESCE(SUM(code_blocks),0) c
               FROM books WHERE status='ok'"""
        ).fetchone()
        failed = conn.execute(
            "SELECT COUNT(*) n FROM books WHERE status='failed'"
        ).fetchone()["n"]
        by_format = {
            r["fmt"]: r["n"] for r in conn.execute(
                "SELECT fmt, COUNT(*) n FROM books WHERE status='ok' GROUP BY fmt")
        }
        by_provenance = {
            r["provenance"]: r["n"] for r in conn.execute(
                """SELECT provenance, COUNT(*) n FROM books WHERE status='ok'
                   GROUP BY provenance ORDER BY n DESC""")
        }
        inferred = conn.execute(
            "SELECT COUNT(*) n FROM books WHERE status='ok' AND structure_inferred=1"
        ).fetchone()["n"]
    return {
        "books": row["n"], "words": row["w"], "sections": row["s"],
        "code_blocks": row["c"], "failed": failed, "by_format": by_format,
        "by_provenance": by_provenance, "structure_inferred": inferred,
    }


def list_books(db_path: Path, limit: int = 0, order: str = "title") -> List[Dict]:
    column = {"title": "title", "words": "word_count DESC",
              "sections": "section_count DESC", "code": "code_blocks DESC"}.get(order, "title")
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            f"""SELECT title, fmt, language, provenance, word_count, section_count,
                       code_blocks, structure_inferred, status, error, path
                FROM books ORDER BY status, {column}"""
        ).fetchall()
    out = [dict(r) for r in rows]
    return out[:limit] if limit else out


_FTS_OPERATORS = ("*", '"', "NEAR(", " OR ", " AND ", " NOT ", "^")


def _as_phrase(query: str) -> str:
    """Treat a multi-word query as a phrase unless the caller used FTS syntax.

    Measured on the real corpus: bare `graph rag` is OR-ed by FTS5 and returns
    "Plotting a Simple Line Graph", "Adding and Deleting Matrix Rows" and a C#
    graph-traversal chapter — none of them about the topic. Quoted, it returns
    §7.2 Graph RAG. Someone asking "what do I read about graph RAG" means the
    phrase, and the OR reading is close to useless at this corpus size.

    Escape hatch preserved: anything containing FTS operators is passed through
    untouched, so `NEAR(graph rag, 3)` and `graph*` still work.
    """
    query = (query or "").strip()
    if not query or any(op in query for op in _FTS_OPERATORS):
        return query
    if len(query.split()) > 1:
        return f'"{query}"'
    return query


def find_headings(
    db_path: Path,
    query: str,
    limit: int = 40,
    per_book: int = 1,
) -> List[Dict]:
    """Which books have a *heading* matching this query.

    Headings only, deliberately. A heading match means the author gave the topic
    its own section — far stronger evidence than the word appearing once in a
    paragraph, and it costs a millisecond against an index rather than a scan of
    39.7 million words. WP-05 decides what to do with the hits; this only
    reports them.

    `per_book` caps hits per title. Without it the answer to "who covers graph
    RAG" is one book listed six times: publishers nest sub-sections under a
    heading path, and a corpus holding the same title in EPUB and PDF doubles
    everything again. The useful shape of this answer is *which books*, with the
    strongest section from each.
    """
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            """SELECT b.title AS book, b.path, b.fmt, s.heading_path, s.locator,
                      s.word_count, s.depth
               FROM section_fts f
               JOIN sections s ON s.id = f.rowid
               JOIN books b ON b.id = s.book_id
               WHERE section_fts MATCH ? AND b.status = 'ok'
               ORDER BY s.word_count DESC""",
            (_as_phrase(query),),
        ).fetchall()

    seen: Dict[str, int] = {}
    out: List[Dict] = []
    for row in rows:
        # Key on title, not path: the same book in both formats is one book.
        key = row["book"].strip().lower()
        if seen.get(key, 0) >= per_book:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def topic_report(db_path: Path, query: str, limit: int = 25) -> Dict:
    """Everything free that can be said about one topic.

    This is the catalog's answer to "what do I read to learn X" — and it is
    deliberately an *evidence* answer, not a recommendation. It reports which
    books give the topic a section, how much text each devotes to it, and how
    many books mention it at all. Choosing and ordering is WP-05's job, and it
    costs money; this costs a millisecond and is often enough.
    """
    hits = find_headings(db_path, query, limit=limit)
    with open_catalog(db_path) as conn:
        breadth = conn.execute(
            """SELECT COUNT(DISTINCT b.id) n
               FROM section_fts f
               JOIN sections s ON s.id = f.rowid
               JOIN books b ON b.id = s.book_id
               WHERE section_fts MATCH ? AND b.status='ok'""",
            (_as_phrase(query),),
        ).fetchone()["n"]
    return {
        "query": query,
        "fts_query": _as_phrase(query),
        "books_with_a_section": breadth,
        "hits": hits,
        "words_on_topic": sum(h["word_count"] for h in hits),
    }


def book_toc(db_path: Path, title_like: str, max_depth: int = 2) -> List[Dict]:
    """The table of contents of one book, for deciding whether to read it."""
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            """SELECT s.ordinal, s.depth, s.heading_path, s.locator, s.word_count
               FROM sections s JOIN books b ON b.id = s.book_id
               WHERE b.title LIKE ? AND s.depth <= ?
               ORDER BY s.ordinal""",
            (f"%{title_like}%", max_depth),
        ).fetchall()
    return [dict(r) for r in rows]
