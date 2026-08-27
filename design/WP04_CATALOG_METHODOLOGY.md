# WP-04 — The corpus catalog: methodology

Built 2026-08-24. Read §1–3 for the reasoning; §4 onward is implementation.

---

## 1. The problem, stated as a number

Your corpus is **396 files, 386 readable, 39,702,925 words.**

At the rate measured in WP-02 (~13 minutes and ~17 model calls per book),
summarizing all of it costs roughly **340 hours and 7,000 model calls**.

That single number decides the design. Any approach that starts by reading the
books is dead on arrival. Whatever answers "what should I read to learn X" has
to work on evidence that is already on disk and costs nothing to gather.

**The catalog indexes all 386 books in 65 seconds.** Re-indexing takes 0.0
seconds. That ratio — 65 seconds versus 340 hours, roughly 19,000× — is the
entire value proposition, and every choice below protects it.

---

## 2. The governing principle, applied a third time

This system already does the same move twice:

| Free signal | Gates | Saves |
|---|---|---|
| Manual captions exist? | Whisper transcription | minutes of CPU per video |
| OCR yield on a frame | The vision model | a paid call per frame |
| **Publisher's own TOC** | **Reading the book at all** | **~13 min + 17 calls per book** |

A publisher already wrote a table of contents, numbered the chapters, and named
every section. That is a structured description of the book's content, shipped
inside the file, free to read. Ignoring it and asking a model to tell you what
the book is about is paying for information you already have.

So: **the catalog reads what the author wrote about their own book, and nothing
else.** No model, no network, no inference.

---

## 3. Choice points

Six decisions where a different answer was defensible. These are the places to
argue with me.

### 3.1 Headings only — bodies are never stored

**Chosen:** index heading paths, locators and word counts. Never store body text.

**Rejected:** full-text index over the bodies, which would make "which book
explains X" answerable directly.

**Why.** Three reasons, in order of weight:

1. **Copyright** (design decision D4). A full-text index *is* a copy of the
   library in a derived artifact — one that could end up in a vault or a repo.
2. **Signal quality.** A heading match means the author gave the topic *its own
   section*. A body match means the word appears. On a 39.7M-word corpus the
   second is mostly noise: "graph" appears in every book that plots anything.
3. **Size.** Headings for 386 books are a few megabytes. Bodies are 7.7 GB.

**The cost of this choice is real:** a book that covers a topic thoroughly but
never names it in a heading is invisible to the catalog. That is a known blind
spot, not an oversight — see §7.

### 3.2 SQLite, not JSON

**Chosen:** one SQLite file with FTS5.

**Rejected:** a JSON index; one sidecar file per book.

**Why.** Queryable without loading everything (90,791 sections), atomic under
interruption, and one artifact to delete. The catalog is derived data: it must
be reconstructible with one command and disposable without touching anything
you own.

### 3.3 size+mtime, not a content hash

**Chosen:** fingerprint each file as `size:mtime`.

**Rejected:** SHA-256 of contents.

**Why.** Hashing 7.7 GB on every run to discover nothing changed costs minutes
and buys accuracy that does not matter. The failure mode of a stale fingerprint
is a book indexed with stale structure, which `--force` fixes. It is not data
loss. This is what makes re-indexing 0.0s and therefore habitual.

### 3.4 A failed file is a row, not an exception

**Chosen:** record unreadable files in the catalog with their reason; continue.

**Rejected:** skip them; or abort the run.

**Why.** 10 of 396 files are unreadable — 9 image-only graphic novels and 1
DRM-locked EPUB. If they were silently skipped, the catalog would quietly
under-report your library and you would have no way to notice. If they aborted
the run, one comic would cost you the other 385 books. This is the same lesson
as the `None` playlist entry that once killed an entire video run.

### 3.5 Multi-word queries are phrases

**Chosen:** quote multi-word queries automatically; pass FTS operators through.

**Rejected:** raw FTS5 semantics.

**Why — measured, not assumed.** FTS5 ORs bare terms. On the real corpus,
`graph rag` returned:

```
Plotting a Simple Line Graph          (Python Crash Course)
Adding and Deleting Matrix Rows       (Art of R Programming)
Exploring Graphs > Implementation     (C# Data Structures)
```

Not one about the topic. Quoted, the same query returns §7.2.3 *Building a
Graph RAG system*. Someone asking about graph RAG means the phrase. The escape
hatch survives: `NEAR(graph rag, 3)`, `graph*` and explicit quotes pass through
untouched.

### 3.6 One hit per book

**Chosen:** cap results at one section per title, keyed on title not path.

**Rejected:** return every matching section.

**Why.** Publishers nest sub-sections under a heading path, so one chapter
produces six matches with near-identical text. Worse, 11 titles exist in both
EPUB and PDF, doubling everything again. The useful shape of "who covers this"
is a list of *books* with the strongest section from each. Keying on title
rather than path is what collapses the format duplicates.

---

## 4. What it produces

```
386 books · 39,702,925 words · 90,791 sections · 453,676 code blocks
  epub 359 · pdf 27 · 10 unreadable (9 image-only, 1 DRM)
  provenance:  318 epub (nav.xhtml) · 41 epub (toc.ncx)
                25 pdf (bookmarks)  ·  2 pdf (headings)
  structure guessed rather than read: 2
```

Two things in that block matter beyond the counts:

- **453,676 code blocks.** This is overwhelmingly a programming library. That
  is why the code-in-notes fix (WP-02) mattered enough to do first.
- **Only 2 of 386 books have guessed structure.** 384 have structure the
  publisher actually wrote. The catalog is reading facts, not inferences.

---

## 5. The answer to your motivating question

```
catalog find "graph rag"
```

| Topic | Books with a section | Words on topic |
|---|---|---|
| **graph rag** | **3** | **2,483** |
| knowledge graph | 3 | 883 |
| prompt injection | 11 | 4,547 |
| kubernetes | 31 | 38,246 |
| quantum error correction | 1 | 992 |

The three graph-RAG sources:

```
Prompt Engineering for AI Systems     [ch. 7]  7.2.3 Building a Graph RAG system   1,922w
Mastering Retrieval-Augmented Gen.    [ch. 12] Complex Question Answering            290w
Transforming Financial Services       [ch. 4]  4.6.1.1 Graph RAG                     271w
```

**This is a thin answer, and that is the finding.** Across 386 books you own
about 2,500 words on graph RAG — one substantial section and two passing
treatments. Compare kubernetes at 38,246 words across 31 books.

You could not have known that without either this catalog or reading a lot of
books. It says: **for graph RAG, your library is not enough**, and the honest
next step is the Collector fetching more rather than a reading plan over three
thin sections.

That is the catalog earning its place. It answered a question in milliseconds
whose answer changes what you do next.

**Correction to something I told you earlier:** I called kubernetes a negative
control, assuming you owned nothing on it. You own 31 books with Kubernetes
sections. The corpus tripled while I was working from a stale picture of it.

---

## 6. Implementation

### Files

| File | Lines | What |
|---|---|---|
| `core/corpus/store.py` | 210 | Schema, fingerprinting, atomic upsert |
| `core/corpus/catalog.py` | 270 | Index + queries |
| `core/corpus/__init__.py` | 12 | Public surface |
| `tests/test_corpus.py` | 24 tests | incl. the no-LLM/no-network assertion |

### Schema

`books` holds one row per file: path, fingerprint, title, format, language,
provenance, `structure_inferred`, word/section/code counts, status, error.

`sections` holds one row per section: ordinal, depth, heading path, title,
locator, word count, href.

`section_fts` is an FTS5 external-content table over `heading_path` only, kept
in sync by triggers. External-content means the index stores no duplicate copy
of the text it indexes.

### Idempotence

`upsert_book` deletes and reinserts inside one transaction. Sections have no
stable identity across a re-parse — a fixed extractor legitimately produces
different boundaries — so matching them up would invent identity that is not
there. One transaction per book means a crash mid-write leaves the previous
version intact.

### The test that matters

`test_indexing_makes_no_llm_call_and_no_network` replaces
`complete_with_failover`, `_complete`, `_complete_claude_cli`, `socket.socket`
and `socket.create_connection` with functions that raise, then indexes a
library. If cataloguing ever reaches for a model or a socket, that test fails
rather than a 396-file run quietly costing money.

Every other test here is a convenience. That one is the design premise.

### CLI

```
catalog index --corpus ~/Documents/corpus     # 65s first run, 0.0s after
catalog overview                              # what you own
catalog find "graph rag"                      # who gave it a section
catalog list --order code                     # most code-heavy books
catalog toc "Mastering Retrieval"             # one book's structure
```

---

## 7. Known blind spots

Stated plainly, because a catalog that seems authoritative and is quietly
incomplete is worse than an obviously partial one.

1. **A topic discussed but never named in a heading is invisible.** This is the
   direct cost of §3.1. Mitigation if it bites: a body-scan fallback that reads
   original files on demand without storing them.
2. ~~**Synonyms do not match.**~~ **Fixed** — see §9. This was the measured
   lexical failure MASTER_DESIGN deferred embeddings for; embeddings are now
   built.
3. **Ten unreadable files are catalogued but not readable.** Correct behaviour,
   but if you want the graphic novels indexed, that needs OCR — explicitly out
   of scope.
4. **Word count is not quality.** Results rank by section size, so a long
   rambling section outranks a short precise one. Ranking is WP-05's job and
   costs money; the catalog deliberately reports evidence rather than
   recommending.
5. **`structure_inferred` is not surfaced in search results.** Two books have
   guessed structure; their headings are less trustworthy and the search does
   not currently say so.

---

## 8. What this unblocks, and what it changes

**Unblocked:** WP-05 (topic locate and reading plan) now has free evidence to
work from, and its own acceptance criterion — "locate is pure lexical, no LLM
call" — is already satisfied by `find_headings`.

**Changed:** I would now argue WP-05 is *less* urgent than it looked. For
several real questions the catalog's evidence answer is sufficient — "you own
31 books on kubernetes, here are the biggest sections" needs no model. WP-05
earns its cost when the answer is genuinely ambiguous, not when it is thin
(graph RAG) or abundant and obvious (kubernetes).

**Recommended next, in order:**

1. **Use the catalog for a week before building on it.** It is free and it may
   turn out that ranking is rarely the missing piece.
2. **Point the Collector at graph RAG.** The catalog says your library is thin
   there — that is a concrete, evidence-backed research request.
3. **WP-05 only when a query returns a genuinely ambiguous set** and you find
   yourself wanting the ordering rather than the list.

---

## 9. Semantic search and ranking

Added the same day, after the lexical blind spot in §7.2 proved real.

### 9.1 The cost question, measured before deciding

The instinct is that embeddings are the expensive part. Measured, they are not:

| | Tokens | API cost |
|---|---|---|
| Headings only | ~1M | **~$0.02** |
| Full bodies | ~53M | ~$1–7 |

**Two cents is not a cost problem, so cost did not decide this.** What decided
it: embedding bodies would mean shipping 39.7 million words of copyrighted
books to a third party, and once an API is in the loop that is one config flag
away. A local model makes the leaky option *structurally unavailable* rather
than merely discouraged.

Measured on this machine (M4, 32 GB), `BAAI/bge-small-en-v1.5`:

```
model download    67 MB, once
throughput        165 headings/second
full corpus       90,791 vectors in 502s (8.4 min)
storage           34.9 MB int8  (139 MB as float32)
catalog total     63.6 MB
marginal cost     $0 — and no book text leaves the machine
```

Three new packages only: `onnxruntime` and `numpy` were already installed, so
no torch.

### 9.2 Four levers on cost, in order of effect

1. **Local model** — zero marginal cost, zero data egress.
2. **Headings, not bodies** — 90,791 short strings instead of 39.7M words, ~50x
   less work. A heading is the author's own summary of the section.
3. **Embed once** — vectors key to the section row; adding ten books to a
   catalogued corpus costs seconds, not 8 minutes.
4. **int8 quantization** — 4x smaller. Measured impact on ranking: scores shift
   by ~0.0005, enough to swap two items across an exact rank boundary, not
   enough to change the candidate set (top-20 overlap 20/20). That tolerance is
   acceptable *because of the pipeline shape* — the cross-encoder reorders the
   candidates afterwards, so vector search only has to surface the right set.

### 9.3 Retrieve → fuse → rerank

The same cheap-gates-expensive shape, a fourth time:

| Stage | Cost | Breadth | Good at |
|---|---|---|---|
| Lexical (BM25) | ~1 ms | all 90,791 | exact terms, names, acronyms |
| Semantic | ~20 ms | all 90,791 | paraphrase, synonyms, concepts |
| **Rerank** (cross-encoder) | ~1 s | **top ~60** | does this heading *answer* the query |

**Why fuse rather than pick one** — they fail in opposite directions, measured:

* Lexical misses `GraphRAG` when you search `graph rag`. Different tokens, zero
  overlap.
* Semantic drifts. Asked for "graph rag" it returns "Plotting a Simple Line
  Graph" and "Visualizing Data with Graphs" — *about graphs*, not about the
  topic. Embeddings have no notion of a term of art.

**Reciprocal Rank Fusion, not score averaging.** BM25 is unbounded, cosine is
[-1, 1]. Normalizing them against each other means inventing a conversion
nobody can justify. RRF uses only rank position, which both retrievers produce
honestly.

**Why the reranker is last and bounded.** A cross-encoder reads query and
heading *together*, so it can separate "Graph RAG" (answers the query) from
"Plotting a Simple Line Graph" (shares a word) — a distinction no bi-encoder
can make, because a bi-encoder never sees the two texts at once. It is also two
orders of magnitude slower per pair: over 90,791 headings it would take half an
hour; over ~60 candidates it takes a second.

### 9.4 A bug worth recording

The first implementation reranked a flat top-50 and collapsed per book
*afterwards*. That looked right and was not: a few books own all 50 slots with
near-duplicate sections, so after collapsing to one per book only **two**
survived, and positions 3–6 came from the **unscored tail** — results the
reranker had never seen, ordered by fused score, and visibly wrong ("Doing Math
with Python > Visualizing Data with Graphs" for a graph-RAG query).

Fixed by thinning to a book-diverse shortlist (3 sections per book, cap 60)
*before* reranking, then reranking all of it. Every finalist is now scored;
`test_every_returned_hit_is_reranked_when_rerank_is_on` asserts it.

### 9.5 Results

```
catalog search "graph rag"                                    0.2s
  both     Mastering Retrieval-Augmented  [ch. 12] Building a Complete Graph RAG System   6.80
  both     Prompt Engineering for AI Sys  [ch.  7] 7.2.1 Components of a Graph RAG sys    5.90
  lexical  Transforming Financial Servic  [ch.  4] 4.6.1.1 Graph RAG                      3.78

catalog search "evaluating whether my RAG system actually works"
  semantic The Art of AI Product Develop  [ch.  7] 7.3.2 Evaluating your RAG system       1.46
  semantic LLMOps                         [ch.  7] Metrics for RAG Applications          -0.73
  semantic Mastering Retrieval-Augmented  [ch. 14] 14. RAG Evaluation: Measuring Quality  -0.95
```

Two things to note. **Lexical alone never found "Building a Complete Graph RAG
System"** — the chapter most directly on topic. And the second query is a
sentence, not keywords, answered from headings alone with no model call at
query time.

`found_by` is printed on every result (`both` / `lexical` / `semantic`) so you
can see which retriever surfaced a hit and judge it rather than trusting the
ranking blind.

Negative rerank scores are normal — ms-marco cross-encoders emit logits, not
probabilities. Only the ordering is meaningful.

### 9.6 New blind spots

1. **Query-time model load is ~6s cold**, then ~0.2s. The vector matrix is
   cached per process, so a long-lived process is fast and a one-shot CLI call
   pays the load every time.
2. **Brute-force matmul, no ANN index.** 90,791 x 384 is 35 MB and a matmul over
   it is milliseconds. Revisit past ~1M sections; adding faiss now would be a
   dependency and an index to keep in sync for a speedup nobody would notice.
3. **Still headings-only.** Semantic search widens *how* a heading can match,
   not *what* is indexed. A topic a book covers but never names in any heading
   remains invisible.
4. **The reranker is English-only** (`ms-marco-MiniLM-L-6-v2`). Fine for this
   corpus; a multilingual corpus would need `jina-reranker-v2-base-multilingual`
   at 1.1 GB.

### 9.7 Commands

```
catalog embed                    # one-time, ~8.4 min, local, free
catalog search "graph rag"       # hybrid + rerank
catalog search "…" --no-rerank   # fused only, faster
catalog search "…" --no-semantic # lexical only, no model load
```
