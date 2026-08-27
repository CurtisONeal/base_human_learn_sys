"""Corpus catalog: what you own, what is in it, and where it actually lives.

Cataloguing and lexical search are free. Semantic search is free after a
one-time local embedding pass — no API, no book text leaving the machine.
Verification is free and should be run often: paths rot.
"""

from video2mdnotes.core.corpus.catalog import (
    IndexReport, book_toc, find_headings, index, list_books, overview, topic_report,
)
from video2mdnotes.core.corpus.embed import build_vectors, vector_count
from video2mdnotes.core.corpus.locate import (
    MISSING, OFFLINE, PRESENT, VerifyReport, VolumeStatus, presence_of,
    refresh_volumes, relocate, verify, volume_status,
)
from video2mdnotes.core.corpus.rank import Hit, search

__all__ = [
    "IndexReport", "Hit", "MISSING", "OFFLINE", "PRESENT", "VerifyReport",
    "VolumeStatus", "book_toc", "build_vectors", "find_headings", "index",
    "list_books", "overview", "presence_of", "refresh_volumes", "relocate",
    "search", "topic_report", "vector_count", "verify", "volume_status",
]
