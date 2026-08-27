"""Semantic vectors for section headings. Local, persisted, quantized.

The catalog's lexical search cannot match "Graph RAG" to "GraphRAG" to
"knowledge graph retrieval" — three queries, three different result sets. That
is the measured lexical failure MASTER_DESIGN said embeddings should wait for.

**Why local rather than an embeddings API.** The cost argument is real but
small: 90,791 headings is roughly a million tokens, about two cents at current
API prices. The argument that actually decides it is different — embedding
*bodies* would mean sending 39.7 million words of copyrighted books to a third
party, and once you have an API in the loop the temptation to do that is one
config flag away. A local model makes the expensive, leaky option structurally
unavailable rather than merely discouraged.

Measured on this machine (M4, 32 GB), `BAAI/bge-small-en-v1.5`:

    model download      67 MB, once
    throughput          165 headings/second
    full corpus         9.2 minutes for 90,791 headings
    storage float32     139 MB
    storage int8        35 MB          <- what we keep

**Four things keep the cost down**, in descending order of effect:

1. **Local model** — zero marginal cost, and no book text leaves the machine.
2. **Headings, not bodies** — 90,791 short strings instead of 39.7M words. A
   heading is the author's own summary of the section; embedding the body would
   cost ~50x more to answer a question the heading usually answers.
3. **Embed once** — vectors are keyed to the section row and survive re-indexing
   unless the file changed, reusing the catalog's fingerprint logic.
4. **int8 quantization** — 4x smaller on disk and in memory, with negligible
   ranking impact at this scale (see `quantize`).
"""

import sqlite3
import struct
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import numpy as np

from video2mdnotes.core.corpus.store import catalog as open_catalog
from video2mdnotes.logger import logger

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIM = 384

_EMBED_SCHEMA = """
CREATE TABLE IF NOT EXISTS section_vectors (
    section_id INTEGER PRIMARY KEY REFERENCES sections(id) ON DELETE CASCADE,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    -- int8-quantized unit vector plus the scale needed to reconstruct it.
    scale      REAL NOT NULL,
    vector     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON section_vectors(model);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_EMBED_SCHEMA)
    conn.commit()


def quantize(vectors: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Float32 unit vectors → int8 plus per-vector scale.

    Symmetric per-vector quantization: divide by the largest absolute component
    and scale to [-127, 127]. Cosine similarity is preserved to well within the
    gap between adjacent results at this corpus size — the ranking question is
    "is this heading about the topic", not "is it 0.003 more about it than the
    next one". Four times smaller on disk and in RAM is worth that.
    """
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    scales = np.abs(vectors).max(axis=1)
    scales[scales == 0] = 1.0
    quantized = np.round(vectors / scales[:, None] * 127.0).astype(np.int8)
    return quantized, scales.astype(np.float32)


def dequantize(quantized: np.ndarray, scales: np.ndarray) -> np.ndarray:
    return (quantized.astype(np.float32) * scales[:, None]) / 127.0


class Embedder:
    """Lazily-loaded local embedding model.

    Loading costs ~6 seconds and 67 MB, so it is deferred until something
    actually needs a vector. Importing this module must stay free — the catalog
    imports it to check whether vectors exist, and paying six seconds for that
    would make `catalog overview` feel broken.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - env dependent
                raise ImportError(
                    "Semantic search needs the optional 'semantic' extra:\n"
                    "    uv sync --extra ocr --extra dev --extra documents --extra semantic"
                ) from exc
            logger.info(f"Loading embedding model {self.model_name} (first use)")
            self._model = TextEmbedding(self.model_name)
        return self._model

    def encode(self, texts: Iterable[str], batch_size: int = 256) -> np.ndarray:
        vectors = list(self.model.embed(list(texts), batch_size=batch_size))
        return np.asarray(vectors, dtype=np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


def build_vectors(
    db_path: Path,
    model_name: str = DEFAULT_MODEL,
    force: bool = False,
    batch_size: int = 256,
    progress=None,
) -> int:
    """Embed every section heading that does not already have a vector.

    Incremental by construction: a section whose vector exists is skipped, so
    adding 10 books to a catalogued corpus costs seconds rather than 9 minutes.
    `force` re-embeds everything, which is what you want after changing models.
    """
    embedder = Embedder(model_name)
    written = 0

    with open_catalog(db_path) as conn:
        ensure_schema(conn)
        if force:
            conn.execute("DELETE FROM section_vectors WHERE model = ?", (model_name,))
            conn.commit()

        rows = conn.execute(
            """SELECT s.id, s.heading_path
               FROM sections s
               JOIN books b ON b.id = s.book_id
               LEFT JOIN section_vectors v
                      ON v.section_id = s.id AND v.model = ?
               WHERE v.section_id IS NULL AND b.status = 'ok'
                 AND TRIM(s.heading_path) != ''
               ORDER BY s.id""",
            (model_name,),
        ).fetchall()

        if not rows:
            return 0

        total = len(rows)
        for start in range(0, total, batch_size):
            batch = rows[start:start + batch_size]
            vectors = embedder.encode([r["heading_path"] for r in batch])
            quantized, scales = quantize(vectors)
            conn.executemany(
                """INSERT OR REPLACE INTO section_vectors
                   (section_id, model, dim, scale, vector) VALUES (?,?,?,?,?)""",
                [
                    (row["id"], model_name, quantized.shape[1],
                     float(scale), quantized[i].tobytes())
                    for i, (row, scale) in enumerate(zip(batch, scales))
                ],
            )
            conn.commit()
            written += len(batch)
            if progress:
                progress(written, total)

    return written


def vector_count(db_path: Path, model_name: str = DEFAULT_MODEL) -> int:
    with open_catalog(db_path) as conn:
        ensure_schema(conn)
        return conn.execute(
            "SELECT COUNT(*) n FROM section_vectors WHERE model = ?", (model_name,)
        ).fetchone()["n"]


def load_matrix(
    db_path: Path, model_name: str = DEFAULT_MODEL
) -> Tuple[List[int], Optional[np.ndarray]]:
    """All vectors as one float32 matrix, plus their section ids.

    A brute-force matrix multiply, deliberately. 90,791 x 384 float32 is 139 MB
    and one numpy matmul over it takes milliseconds on this machine — an ANN
    index (faiss, hnswlib, sqlite-vec) would add a dependency and an index to
    keep in sync for a speedup nobody would notice. Revisit past ~1M sections.
    """
    with open_catalog(db_path) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            """SELECT section_id, dim, scale, vector FROM section_vectors
               WHERE model = ? ORDER BY section_id""",
            (model_name,),
        ).fetchall()

    if not rows:
        return [], None

    dim = rows[0]["dim"]
    ids: List[int] = []
    quantized = np.empty((len(rows), dim), dtype=np.int8)
    scales = np.empty(len(rows), dtype=np.float32)
    for i, row in enumerate(rows):
        ids.append(row["section_id"])
        quantized[i] = np.frombuffer(row["vector"], dtype=np.int8, count=dim)
        scales[i] = row["scale"]

    matrix = dequantize(quantized, scales)
    # Normalize so cosine similarity is a single dot product.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, matrix / norms


def _pack(vector: np.ndarray) -> bytes:      # pragma: no cover - debugging aid
    return struct.pack(f"{len(vector)}f", *vector)
