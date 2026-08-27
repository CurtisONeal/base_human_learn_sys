# HUMAN_PLANS — video2notes-v2

Context for picking this back up. What was in my head, not what the code says.

---

## Where it stands (2026-08-27)

Working, 274 tests. Six sources: YouTube, playlists, embedded pages, EPUB, PDF,
local audio/video.

```
corpus     879 books · 72.4M words · 123,695 vectors
catalog    65s to index 386 books · 0.0s to re-index · 90 MB for 9.3 GB of books
search     lexical + semantic + local rerank, all free, ~0.3s
```

## The number that shapes every decision

**~13 minutes and ~17 model calls per book.** Across the corpus that is roughly
340 hours. So the catalog is not a convenience — it is the only way in. Search
and locate cost nothing; summarizing is the expensive act and must be chosen
deliberately.

## Open, in the order I would do them

- [ ] **Locator spot-check at the desk.** The one thing I cannot delegate.
      `~/Documents/locator_check_v2.html` — 10 claims, each shown beside the
      exact section it cites. Include one of the **85 books with inferred
      structure**, not just a clean Springer EPUB; those have the weakest
      citations.
- [ ] **WP-05 plan stage.** Locate is done. But I am no longer sure ranking is
      the missing piece — the free evidence answer is often enough. Use the
      catalog for a week before building on it.
- [ ] **WP-01 text/md/html.** Half a session. Closes the `.md`/`.txt`/article
      gap and unblocks the HF docs around the courses.
- [ ] **Volume residency.** 329 books live on an external drive. `catalog
      verify` reports present/offline/missing; deciding whether to copy them
      local is still open.
- [ ] **Point the Collector at thin topics.** The catalog can now say what the
      library is thin on — that is an evidence-backed research request.

## Decisions I keep re-making

- **Both formats are worth keeping.** 11 titles exist as EPUB *and* PDF. The
  pairing caught 3 of 6 bugs by disagreeing, and one EPUB is DRM-locked while
  its PDF twin is not.
- **Local models over API for embeddings.** Cost was never the argument — two
  cents. The argument is that embedding bodies would ship 39.7M words of
  copyrighted books to a third party, and an API in the loop makes that one flag
  away.
- **Compression does not pay.** Measured 2.7% on real books; EPUB is already a
  zip. The catalog *is* the compressed form at ~100:1.

## Things I keep re-learning

- **A clean exit code means nothing here.** Check the artifact.
- **Improving extraction does not improve notes** unless the improvement reaches
  the prompt. Made that mistake twice in one session.
- **Measure before choosing.** I argued for `large-v3` from parameter counts;
  `distil-large-v3` measured twice the words with no hallucination loops. I have
  been wrong more often reasoning from specs than from a one-minute probe.
