"""Retrieve, fuse, rerank — the same cheap-gates-expensive shape, applied to search.

Three stages, each narrower and costlier than the last:

    stage        cost              breadth      what it is good at
    ─────────────────────────────────────────────────────────────────────────
    lexical      ~1 ms             all 90,791   exact terms, names, acronyms
    semantic     ~20 ms            all 90,791   paraphrase, synonyms, concepts
    rerank       ~1 s for 50       top ~50      does this heading ANSWER this
                                                query, not merely resemble it

The first two run on everything and are fused. The third sees only what the
first two surfaced — which is the whole point: a cross-encoder scores one
query-document pair at a time, so running it over 90,791 headings would take
half an hour. Over the 50 candidates that survived fusion it takes a second.

**Why fuse instead of picking one.** They fail in opposite directions, measured
on this corpus:

* Lexical alone misses `GraphRAG` when you search `graph rag`, and vice versa —
  different tokens, zero overlap.
* Semantic alone drifts: asked for "graph rag" it happily returns "Plotting a
  Simple Line Graph" and "Network Filtration", which are *about graphs* and not
  about the topic. Embeddings have no notion of a term of art.

Reciprocal Rank Fusion is used rather than score averaging because the two
scores are not comparable — BM25 is unbounded, cosine is [-1, 1], and
normalizing them against each other means inventing a conversion nobody can
justify. RRF only uses *rank position*, which both produce honestly.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from video2mdnotes.core.corpus.catalog import _as_phrase
from video2mdnotes.core.corpus.embed import (
    DEFAULT_MODEL, Embedder, load_matrix, vector_count,
)
from video2mdnotes.core.corpus.store import catalog as open_catalog
from video2mdnotes.logger import logger

# RRF's damping constant. 60 is the value from the original paper and the one
# every implementation uses; it makes the contribution of rank 1 vs rank 2 a
# gentle slope rather than a cliff, which is what you want when neither
# retriever is authoritative.
RRF_K = 60

DEFAULT_RERANKER = "Xenova/ms-marco-MiniLM-L-6-v2"


@dataclass
class Hit:
    section_id: int
    book: str
    heading_path: str
    locator: str
    word_count: int
    fmt: str = ""
    lexical_rank: Optional[int] = None
    semantic_rank: Optional[int] = None
    fused_score: float = 0.0
    rerank_score: Optional[float] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def found_by(self) -> str:
        """How this result was surfaced — shown so the user can judge it."""
        if self.lexical_rank is not None and self.semantic_rank is not None:
            return "both"
        if self.lexical_rank is not None:
            return "lexical"
        return "semantic"


def _fetch_sections(db_path: Path, section_ids: List[int]) -> Dict[int, Dict]:
    if not section_ids:
        return {}
    placeholders = ",".join("?" * len(section_ids))
    with open_catalog(db_path) as conn:
        rows = conn.execute(
            f"""SELECT s.id, b.title AS book, b.fmt, s.heading_path, s.locator,
                       s.word_count
                FROM sections s JOIN books b ON b.id = s.book_id
                WHERE s.id IN ({placeholders}) AND b.status = 'ok'""",
            section_ids,
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def lexical_search(db_path: Path, query: str, limit: int = 100) -> List[int]:
    """FTS5 over headings. Returns section ids in relevance order.

    Ordered by bm25() rather than section size: for the *candidate* stage what
    matters is term match quality, and letting a long section win on length
    would push a precise short heading out of the candidate set before the
    reranker ever sees it. The catalog's own `find_headings` still orders by
    size, because there it is reporting evidence rather than feeding a ranker.
    """
    with open_catalog(db_path) as conn:
        try:
            rows = conn.execute(
                """SELECT s.id FROM section_fts f
                   JOIN sections s ON s.id = f.rowid
                   JOIN books b ON b.id = s.book_id
                   WHERE section_fts MATCH ? AND b.status='ok'
                   ORDER BY bm25(section_fts) LIMIT ?""",
                (_as_phrase(query), limit),
            ).fetchall()
        except Exception as exc:          # malformed FTS expression
            logger.warning(f"lexical search failed for {query!r}: {exc}")
            return []
    return [r["id"] for r in rows]


def semantic_search(
    db_path: Path,
    query: str,
    limit: int = 100,
    model_name: str = DEFAULT_MODEL,
    _cache: Dict = {},
) -> List[int]:
    """Cosine similarity over heading vectors. Returns section ids in order.

    The matrix is cached per (db, model) for the life of the process. Loading
    139 MB from SQLite takes about a second; doing it per query would make
    interactive search feel broken.
    """
    key = (str(db_path), model_name)
    if key not in _cache:
        _cache[key] = load_matrix(db_path, model_name)
    ids, matrix = _cache[key]
    if matrix is None or not ids:
        return []

    vector = Embedder(model_name).encode_query(query)
    norm = np.linalg.norm(vector)
    if norm == 0:
        return []
    scores = matrix @ (vector / norm)

    top = np.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
    top = top[np.argsort(-scores[top])]
    return [ids[i] for i in top]


def fuse(rankings: Dict[str, List[int]], k: int = RRF_K) -> Dict[int, float]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank).

    Uses only rank position, never the underlying scores, because BM25 and
    cosine are not on a common scale and any normalization between them would
    be a number we invented.
    """
    scores: Dict[int, float] = {}
    for ids in rankings.values():
        for rank, section_id in enumerate(ids, 1):
            scores[section_id] = scores.get(section_id, 0.0) + 1.0 / (k + rank)
    return scores


def rerank(
    query: str,
    hits: List[Hit],
    model_name: str = DEFAULT_RERANKER,
    top_k: int = 50,
) -> List[Hit]:
    """Cross-encoder rescoring of the top candidates only.

    A cross-encoder reads the query and the heading *together*, so it can tell
    "Graph RAG" (answers the query) from "Plotting a Simple Line Graph" (shares
    a word) — a distinction no bi-encoder can make, because a bi-encoder never
    sees the two texts at the same time.

    It is also two orders of magnitude slower per pair, which is exactly why it
    runs last and on a bounded set. Failure is non-fatal: if the model cannot
    load, the fused order stands rather than the search erroring out.
    """
    if not hits:
        return hits
    candidates = hits[:top_k]
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        encoder = TextCrossEncoder(model_name)
        scores = list(encoder.rerank(query, [h.heading_path for h in candidates]))
    except Exception as exc:  # noqa: BLE001 - degrade to fused order
        logger.warning(f"rerank unavailable ({exc}); keeping fused order")
        return hits

    for hit, score in zip(candidates, scores):
        hit.rerank_score = float(score)
        hit.reasons.append(f"rerank={score:.2f}")
    candidates.sort(key=lambda h: -(h.rerank_score or 0.0))
    return candidates + hits[top_k:]


def search(
    db_path: Path,
    query: str,
    limit: int = 20,
    candidates: int = 100,
    use_semantic: bool = True,
    use_rerank: bool = True,
    per_book: int = 1,
    model_name: str = DEFAULT_MODEL,
) -> List[Hit]:
    """Hybrid search: lexical + semantic, fused, optionally reranked.

    Degrades honestly. With no vectors built it is pure lexical and says so
    through `Hit.found_by` rather than silently returning worse results.
    """
    rankings: Dict[str, List[int]] = {"lexical": lexical_search(db_path, query, candidates)}

    if use_semantic and vector_count(db_path, model_name):
        rankings["semantic"] = semantic_search(db_path, query, candidates, model_name)
    elif use_semantic:
        logger.info("No vectors built; lexical only. Run: catalog embed")

    fused = fuse(rankings)
    if not fused:
        return []

    lexical_pos = {sid: i + 1 for i, sid in enumerate(rankings.get("lexical", []))}
    semantic_pos = {sid: i + 1 for i, sid in enumerate(rankings.get("semantic", []))}
    meta = _fetch_sections(db_path, list(fused))

    hits = [
        Hit(
            section_id=sid,
            book=meta[sid]["book"],
            heading_path=meta[sid]["heading_path"],
            locator=meta[sid]["locator"],
            word_count=meta[sid]["word_count"],
            fmt=meta[sid]["fmt"],
            lexical_rank=lexical_pos.get(sid),
            semantic_rank=semantic_pos.get(sid),
            fused_score=score,
        )
        for sid, score in sorted(fused.items(), key=lambda kv: -kv[1])
        if sid in meta
    ]

    def collapse(rows: List[Hit], keep: int, cap: int = 0) -> List[Hit]:
        seen: Dict[str, int] = {}
        out: List[Hit] = []
        for hit in rows:
            key = hit.book.strip().lower()
            if seen.get(key, 0) >= keep:
                continue
            seen[key] = seen.get(key, 0) + 1
            out.append(hit)
            if cap and len(out) >= cap:
                break
        return out

    if use_rerank:
        # Thin to a bounded, book-diverse shortlist BEFORE reranking, then
        # rerank all of it.
        #
        # Reranking a flat top-50 and collapsing afterwards looked right and was
        # not: a handful of books can own all 50 slots with near-duplicate
        # sections, so after collapsing to one per book only two survived and
        # positions 3-6 came from the unscored tail. Those tail results were
        # visibly wrong ("Doing Math with Python > Visualizing Data with
        # Graphs" for a graph-RAG query) and carried no rerank score at all —
        # the reranker had never seen them.
        #
        # Keeping a few sections per book preserves the "do not let a weak
        # section block a strong one from the same book" property, while
        # guaranteeing every finalist is actually scored.
        shortlist = collapse(hits, keep=max(3, per_book * 3), cap=60)
        shortlist = rerank(query, shortlist, top_k=len(shortlist))
        hits = shortlist

    return collapse(hits, keep=per_book, cap=limit)
