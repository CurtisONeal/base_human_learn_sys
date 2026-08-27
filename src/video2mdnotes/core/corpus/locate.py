"""Keeping the catalog honest about where files actually are.

The catalog stores absolute paths. Absolute paths rot, for three reasons that
all happen constantly:

* **A removable volume gets unplugged.** 329 of 715 books live on an external
  drive. Unmounted, every one of those paths resolves to nothing.
* **Someone moves a file.** People reorganise their libraries. So do agents.
* **Someone copies a file** and now the same book exists in two places, one of
  which the catalog does not know about.

The distinction that matters, and which a naive "does this path exist" check
gets wrong:

    PRESENT   the file is where the catalog says
    OFFLINE   its volume is simply not mounted — nothing is wrong
    MISSING   its volume IS mounted (or it was internal) and the file is gone

**OFFLINE is not an error.** Treating it as one would mark half the catalog
broken every time a drive is unplugged, and a report that is wrong half the
time gets ignored — which is how you stop noticing the MISSING entries that
actually matter.

Relocation is deliberately conservative: a candidate must match on **name and
byte size** before the catalog is repointed at it. Name alone would happily
attach a different edition of the same title, and silently citing chapter 12 of
the wrong edition is a worse failure than admitting a book is missing.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from video2mdnotes.core.corpus.store import (
    catalog as open_catalog, fingerprint, volume_is_mounted, volume_of,
)
from video2mdnotes.logger import logger

PRESENT, OFFLINE, MISSING = "present", "offline", "missing"


@dataclass
class VolumeStatus:
    name: Optional[str]          # None = internal storage
    books: int
    mounted: bool

    @property
    def label(self) -> str:
        if self.name is None:
            return "internal"
        return f"{self.name} ({'mounted' if self.mounted else 'NOT MOUNTED'})"


@dataclass
class VerifyReport:
    present: int = 0
    offline: int = 0
    missing: int = 0
    volumes: List[VolumeStatus] = field(default_factory=list)
    missing_books: List[Dict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{self.present} present · {self.offline} offline "
                f"(volume unmounted) · {self.missing} MISSING")

    @property
    def needs_attention(self) -> bool:
        """Only genuinely missing files warrant action."""
        return self.missing > 0


def volume_status(db_path: Path) -> List[VolumeStatus]:
    """Which volumes the catalog references, and whether they are attached."""
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            """SELECT volume, COUNT(*) n FROM books WHERE status='ok'
               GROUP BY volume ORDER BY n DESC"""
        ).fetchall()
    return [
        VolumeStatus(name=r["volume"], books=r["n"],
                     mounted=volume_is_mounted(r["volume"]))
        for r in rows
    ]


def presence_of(path: str, volume: Optional[str]) -> str:
    """PRESENT / OFFLINE / MISSING for one catalogued book."""
    if Path(path).exists():
        return PRESENT
    if volume and not volume_is_mounted(volume):
        return OFFLINE
    return MISSING


def verify(db_path: Path) -> VerifyReport:
    """Check every catalogued book against the filesystem. Free and fast.

    Reads no book content — only `Path.exists()` per row — so verifying 715
    books costs milliseconds and can be run as often as you like.
    """
    report = VerifyReport(volumes=volume_status(db_path))
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            "SELECT id, path, title, volume, fmt FROM books WHERE status='ok'"
        ).fetchall()

    for row in rows:
        state = presence_of(row["path"], row["volume"])
        if state == PRESENT:
            report.present += 1
        elif state == OFFLINE:
            report.offline += 1
        else:
            report.missing += 1
            report.missing_books.append(dict(row))
    return report


def _index_candidates(roots: Iterable[Path]) -> Dict[str, List[Path]]:
    """name(lowercased) -> paths, for every book under the given roots."""
    found: Dict[str, List[Path]] = {}
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".epub", ".pdf"}:
                found.setdefault(path.name.lower(), []).append(path)
    return found


def relocate(
    db_path: Path,
    roots: Iterable[Path],
    dry_run: bool = True,
) -> List[Dict]:
    """Find MISSING books that have moved, and repoint the catalog at them.

    Matching requires **name AND byte size**. Name alone would attach a
    different edition of the same title — and a citation to "ch. 12" of the
    wrong edition is a confident, checkable-looking claim that is wrong, which
    is the exact failure class this project keeps designing against.

    Books whose volume is merely unmounted are untouched: they are not lost.
    `dry_run` is the default because repointing paths in bulk on a fuzzy match
    is not something to do without looking first.
    """
    report = verify(db_path)
    if not report.missing_books:
        return []

    candidates = _index_candidates(roots)
    moves: List[Dict] = []

    with open_catalog(db_path) as conn:
        for book in report.missing_books:
            old = Path(book["path"])
            options = candidates.get(old.name.lower(), [])
            if not options:
                continue

            # Size is the cheap discriminator between editions. The old file is
            # gone, so its size comes from the stored fingerprint.
            row = conn.execute(
                "SELECT fingerprint FROM books WHERE id = ?", (book["id"],)
            ).fetchone()
            want_size = None
            if row and ":" in (row["fingerprint"] or ""):
                try:
                    want_size = int(row["fingerprint"].split(":", 1)[0])
                except ValueError:
                    want_size = None

            match = None
            for option in options:
                if want_size is None or option.stat().st_size == want_size:
                    match = option
                    break
            if match is None:
                logger.info(
                    f"relocate: {old.name} found by name but size differs — "
                    "left alone (probably a different edition)"
                )
                continue

            moves.append({
                "id": book["id"], "title": book["title"],
                "from": str(old), "to": str(match.resolve()),
            })
            if not dry_run:
                conn.execute(
                    "UPDATE books SET path = ?, volume = ?, fingerprint = ? WHERE id = ?",
                    (str(match.resolve()), volume_of(match),
                     fingerprint(match), book["id"]),
                )
        if not dry_run:
            conn.commit()

    return moves


def refresh_volumes(db_path: Path) -> int:
    """Recompute the `volume` column from current paths.

    Needed after books are copied between drives, and after a schema migration
    that back-filled from path strings. Cheap: one UPDATE per book, no file
    reads.
    """
    changed = 0
    with open_catalog(db_path) as conn:
        rows = conn.execute("SELECT id, path, volume FROM books").fetchall()
        for row in rows:
            actual = volume_of(Path(row["path"]))
            if actual != row["volume"]:
                conn.execute("UPDATE books SET volume = ? WHERE id = ?",
                             (actual, row["id"]))
                changed += 1
        conn.commit()
    return changed
