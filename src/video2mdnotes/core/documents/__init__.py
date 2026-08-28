"""Document sources: text that was written rather than spoken.

Dispatch is by extension, which is a deliberate limit — sniffing content to
guess a format invites reading a file the user did not mean to hand over. If
the extension is wrong, the extractor fails loudly rather than improvising.
"""

from pathlib import Path

from video2mdnotes.core.documents.html import SUPPORTED as _HTML
from video2mdnotes.core.documents.plaintext import SUPPORTED as _PLAINTEXT
from video2mdnotes.core.sources import SourceText

SUPPORTED = {".epub", ".pdf"} | _PLAINTEXT | _HTML


def extract(path) -> SourceText:
    """Read a document file into a `SourceText`.

    Raises:
        ValueError: unsupported extension.
        ImportError: the optional `[documents]` extra is not installed (EPUB
            needs nothing; PDF needs `pymupdf`; plaintext/HTML need nothing).
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        from video2mdnotes.core.documents.epub import extract as _extract
    elif suffix == ".pdf":
        from video2mdnotes.core.documents.pdf import extract as _extract
    elif suffix in _PLAINTEXT:
        from video2mdnotes.core.documents.plaintext import extract as _extract
    elif suffix in _HTML:
        from video2mdnotes.core.documents.html import extract as _extract
    else:
        raise ValueError(
            f"Unsupported document type {suffix!r}. Supported: {', '.join(sorted(SUPPORTED))}"
        )
    return _extract(path)
