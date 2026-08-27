"""Where the catalog lives, and why it lives there.

The catalog is a SQLite file. Three properties drove that choice over a JSON
index or a directory of sidecar files:

* **Queryable without loading everything.** 396 files today, 39.7 million words.
  "Which books mention X" must not mean parsing a 40 MB JSON blob into memory.
* **Atomic and concurrent-safe.** Indexing runs for minutes; an interrupted run
  must leave a usable catalog, not a half-written file.
* **One artifact to delete.** The catalog is derived data. It must be
  reconstructible from the corpus with one command, and disposable without
  touching anything the user owns.

**The book text itself is never stored here.** Only structure: titles, section
headings, locators, word counts, and a content hash. That is a copyright
decision (design decision D4) and a size decision — the headings of 396 books
are a few megabytes, their text is 7.7 GB. Anything that needs the body reads
it from the original file on demand.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from pydantic import BaseModel

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id                   INTEGER PRIMARY KEY,
    path                 TEXT NOT NULL UNIQUE,
    -- size+mtime, not a content hash of 7.7 GB: re-indexing must be cheap, and
    -- a book that has not been touched does not need to be re-parsed.
    fingerprint          TEXT NOT NULL,
    title                TEXT NOT NULL,
    fmt                  TEXT NOT NULL,          -- epub | pdf
    language             TEXT,
    provenance           TEXT NOT NULL,          -- epub (nav.xhtml), pdf (bookmarks), …
    structure_inferred   INTEGER NOT NULL DEFAULT 0,
    word_count           INTEGER NOT NULL DEFAULT 0,
    section_count        INTEGER NOT NULL DEFAULT 0,
    code_blocks          INTEGER NOT NULL DEFAULT 0,
    indexed_at           TEXT NOT NULL,
    -- A file that cannot be read is RECORDED, not skipped silently. Nine of the
    -- 396 real files are image-only graphic novels; the catalog should be able
    -- to say so rather than leave a hole the user has to notice.
    status               TEXT NOT NULL DEFAULT 'ok',   -- ok | failed
    error                TEXT,
    -- Volume this book lives on, or NULL for internal storage. 329 of 715 books
    -- sit on an external drive; unmount it and nearly half the catalog points
    -- at files that are not there. Recording the volume lets a search say "on
    -- external drive (not mounted)" instead of returning a hit that silently
    -- goes nowhere — a wrong answer that looks like a right one, which is the
    -- failure mode this project keeps having to design against.
    volume               TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id           INTEGER PRIMARY KEY,
    book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    depth        INTEGER NOT NULL,
    heading_path TEXT NOT NULL,      -- "Part I > 3. Indexing"
    title        TEXT NOT NULL,
    locator      TEXT NOT NULL,
    word_count   INTEGER NOT NULL,
    href         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sections_book ON sections(book_id);
CREATE INDEX IF NOT EXISTS idx_books_status  ON books(status);

-- Full-text search over HEADINGS ONLY, never bodies. This is what makes
-- "which books discuss graph RAG" free: a heading index over 396 books is a
-- few megabytes and answers in milliseconds, where scanning 39.7 million words
-- of body text does not. Bodies stay in the original files.
CREATE VIRTUAL TABLE IF NOT EXISTS section_fts USING fts5(
    heading_path,
    content='sections',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO section_fts(rowid, heading_path) VALUES (new.id, new.heading_path);
END;
CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO section_fts(section_fts, rowid, heading_path)
    VALUES ('delete', old.id, old.heading_path);
END;
"""


class BookRow(BaseModel):
    """One catalogued book. Mirrors the `books` table."""

    id: Optional[int] = None
    path: str
    fingerprint: str
    title: str
    fmt: str
    language: Optional[str] = None
    provenance: str = ""
    structure_inferred: bool = False
    word_count: int = 0
    section_count: int = 0
    code_blocks: int = 0
    indexed_at: str = ""
    status: str = "ok"
    error: Optional[str] = None
    volume: Optional[str] = None


class SectionRow(BaseModel):
    book_id: int
    ordinal: int
    depth: int
    heading_path: str
    title: str
    locator: str
    word_count: int
    href: Optional[str] = None


def volume_of(path: Path) -> Optional[str]:
    """The removable volume a path lives on, or None for internal storage.

    macOS mounts everything except the boot disk under /Volumes, so the second
    path component is the volume name. "Macintosh HD" is the boot disk showing
    up by name and is treated as internal.
    """
    parts = Path(path).resolve().parts
    if len(parts) > 2 and parts[1] == "Volumes" and parts[2] != "Macintosh HD":
        return parts[2]
    return None


def volume_is_mounted(volume: Optional[str]) -> bool:
    """Whether a recorded volume is attached right now."""
    return volume is None or Path("/Volumes", volume).exists()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a catalog was first built.

    Re-indexing 715 books to gain one column would be a 10-minute penalty for a
    schema change, which is the kind of friction that stops people upgrading.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
    if "volume" not in existing:
        conn.execute("ALTER TABLE books ADD COLUMN volume TEXT")
        conn.execute("""UPDATE books SET volume =
            CASE WHEN path LIKE '/Volumes/%'
                 THEN substr(path, 10, instr(substr(path, 10), '/') - 1)
                 ELSE NULL END""")
        conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the catalog database."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL so a long index run does not block a concurrent read of the catalog.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


@contextmanager
def catalog(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def fingerprint(path: Path) -> str:
    """Cheap change detection: size and mtime.

    Deliberately not a content hash. Hashing 7.7 GB on every run to discover
    that nothing changed costs minutes and buys accuracy that does not matter
    here — the failure mode of a stale fingerprint is a book indexed with old
    structure, which `--force` fixes, not data loss.
    """
    stat = Path(path).stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def needs_reindex(conn: sqlite3.Connection, path: Path) -> bool:
    """Whether this file is absent from the catalog or has changed."""
    row = conn.execute(
        "SELECT fingerprint FROM books WHERE path = ?", (str(path.resolve()),)
    ).fetchone()
    return row is None or row["fingerprint"] != fingerprint(path)


def upsert_book(conn: sqlite3.Connection, book: BookRow, sections: List[SectionRow]) -> int:
    """Replace a book and its sections atomically.

    Delete-then-insert rather than a diff: sections have no stable identity
    across a re-parse (a fixed extractor legitimately produces different
    boundaries), so trying to match them up would invent identity that is not
    there. The whole book is one transaction, so a crash mid-write leaves the
    previous version intact.
    """
    with conn:
        conn.execute("DELETE FROM books WHERE path = ?", (book.path,))
        cursor = conn.execute(
            """INSERT INTO books
               (path, fingerprint, title, fmt, language, provenance,
                structure_inferred, word_count, section_count, code_blocks,
                indexed_at, status, error, volume)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (book.path, book.fingerprint, book.title, book.fmt, book.language,
             book.provenance, int(book.structure_inferred), book.word_count,
             book.section_count, book.code_blocks, book.indexed_at,
             book.status, book.error, book.volume or volume_of(Path(book.path))),
        )
        book_id = cursor.lastrowid
        if sections:
            conn.executemany(
                """INSERT INTO sections
                   (book_id, ordinal, depth, heading_path, title, locator,
                    word_count, href)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(book_id, s.ordinal, s.depth, s.heading_path, s.title,
                  s.locator, s.word_count, s.href) for s in sections],
            )
    return book_id
