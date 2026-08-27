"""Document sources: text that was written rather than spoken.

Dispatch is by extension, which is a deliberate limit — sniffing content to
guess a format invites reading a file the user did not mean to hand over. If
the extension is wrong, the extractor fails loudly rather than improvising.
"""

from pathlib import Path

from video2mdnotes.core.sources import SourceText

SUPPORTED = {".epub", ".pdf"}


def extract(path) -> SourceText:
    """Read a document file into a `SourceText`.

    Raises:
        ValueError: unsupported extension.
        ImportError: the optional `[documents]` extra is not installed.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".epub":
        from video2mdnotes.core.documents.epub import extract as _extract
    elif suffix == ".pdf":
        from video2mdnotes.core.documents.pdf import extract as _extract
    else:
        raise ValueError(
            f"Unsupported document type {suffix!r}. Supported: {', '.join(sorted(SUPPORTED))}"
        )
    return _extract(path)
