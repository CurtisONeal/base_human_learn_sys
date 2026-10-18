# HUMAN_PLANS — base_human_learn_sys (formerly video2notes-v2)

> **Renamed 2026-10-15.** This repo was `video2notes-v2`; it now owns the name
> `base_human_learn_sys`. The old repo of that name is archived on GitHub as
> `base_human_learn_sys-archived` (local: `repos/base_human_learn_sys-archived`,
> which still holds the large .wav files in `temp_files/`). Its uncommitted plan
> notes are preserved in `human_readable_documentation/HUMAN_PLANS_from_old_base_repo.md`.
> Dated handoffs and ADRs still say `video2notes-v2` — they are historical records.

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
- [x] **WP-01 text/md/html — done 2026-08-27.** `process ./notes.md` and
      `process <article-url>` both work now. Picked for that night's run
      specifically because its own acceptance test doesn't need my judgment
      at the end, unlike WP-05's — bounded, testable against real files, like
      EPUB/PDF were. Found one real bug worth remembering: a real vault note
      had a doubled heading marker (`# # Real Title`, an export artifact),
      producing a locator with a literal `#` in it. Fixed by treating a
      heading's own title as metadata to normalise, not body content to keep
      verbatim — the fence-preservation rule doesn't apply to a heading's text.
- [ ] **Volume residency.** 329 books live on an external drive. `catalog
      verify` reports present/offline/missing; deciding whether to copy them
      local is still open.
- [ ] **Point the Collector at thin topics.** The catalog can now say what the
      library is thin on — that is an evidence-backed research request.

- [ ] **Integrate the long-form transcription path with the learning plan** — once it
      has survived more edge cases (see `design/LONGFORM_EXECUTION_PLAN.md`, open items).
      Needs its own plan: how notes feed Curriculum Maker v4 (Google Drive, Human Learning
      Loop), and **variations by length and kind of knowledge** — e.g. a 5-min clip vs a
      2-h talk vs a 12-h course; lecture vs hands-on lab vs meetup/panel vs book vs a
      from-scratch coding walkthrough. Likely one capture packet per chapter, with the
      lesson shape chosen by kind. The WP-9 hand-off stub was never built.
- [ ] **Parked for comparison:** https://github.com/raiyanyahya/how-to-train-your-gpt —
      compare against the Karpathy "Let's build GPT" notes
      (`previous_run_results/20261016_lets_build_gpt_from_scratch_in_code_spelled_out/`)
      **after the short-path fix lands** (see below). Question: do the notes capture what
      a code-first repo teaches, and what do they miss without the code?
- [ ] **Short-path gap (found 2026-10-16).** Sources under 12k words (≈ < 90 min) still use
      the old single-call path: no dates, chapters, time-sensitive table, corrections or
      (sometimes) citations. Seen on Azure Master Class Part 1 (22 chapters → 1) and the
      ClePy FastAPI recording (0 citations). Fix before merge.

- [ ] **Learning packages (decided 2026-10-17, `design/LEARNING_PACKAGES.md`).** Notes go into
      `010_learning_system/by_subject/<subject>/<source>/` with extracted text, images and working
      relative citation links; binaries in `__sources/` (excluded from Obsidian Sync — set that once).
- [ ] **Main-machine layout:** corpus + catalog DB on the 20 TB drive, GTD and vault on the same
      machine; replicate corpus + DB (second drive or cloud, checksum-verified); code on GitHub.

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
