---
note_type: reference
title: "video2notes-v2 — seams and splits"
tags: [from-octobob, claude, architecture, video2notes]
updated: 2026-08-25
---

# Seams and splits

Where the system is cut, and why each cut is where it is. A seam in the wrong
place is worse than no seam — it invites a dependency that should not exist and
then makes it look sanctioned.

## The seams that exist

### 1. Source ↔ interpretation — `SourceText`

**Cut:** everything that *obtains* text is on one side; everything that
*interprets* it is on the other.

**Why here:** it is the only place every source family already converged. Six
input paths (YouTube, playlist, embed-scraped page, EPUB, PDF, local media)
produce one type, and the summarizer never learns which.

**What it buys:** adding a source is a new module and a dispatch entry. WP-02
(books) and WP-03 (local media) each cost one session because of this cut.

**Deliberate compromise:** `TranscriptResult` was NOT rewritten into
`SourceText`. It is adapted at the boundary by `SourceText.from_transcript()`.
The media path is the one part demonstrably working end to end, and an
overnight refactor of it would have traded a working feature for tidiness.

### 2. Time ↔ structure — how you point at a claim

**Cut:** `segments[]` (time) vs `sections[]` (structure). Not media vs document.

**Why here:** the real difference between a video and a book is not the
container, it is what a citation looks like. `at 04:12` and `ch. 3, §Indexing`
are both locators; nothing else about the two families differs at this level.

**What it buys:** the visual-extraction plugin keys off `segments` being
non-empty, so a book skips the frame path by **having nothing to match** rather
than by a special case in `main()`. The plugin boundary is a data fact, not an
`if`.

### 3. Free ↔ paid — the gate

**Cut:** every expensive operation sits behind something free that decides
whether it runs.

```
captions exist?     →  Whisper
OCR yield           →  vision model
publisher TOC       →  reading the book
lexical + vector    →  cross-encoder rerank
```

**Why here:** it is the only cut that scales with corpus size. 39.7M words makes
"read everything then decide" impossible; "look at what is already written down
then decide" stays cheap forever.

### 4. Subscription ↔ metered

**Cut:** `claude-cli/*` (keychain OAuth, subscription) vs litellm (API key,
metered). Different code paths, not a config value.

**Why here:** because the failure mode of getting it wrong is spending money
nobody authorised. `SubscriptionExhausted` is a distinct exception precisely so
exhaustion cannot be mistaken for ordinary failure and fall through to a paid
backend.

### 5. Extraction ↔ catalog ↔ ranking

**Cut:** `documents/` reads files. `corpus/catalog` records structure.
`corpus/rank` orders results. None imports the summarizer.

**Why here:** the catalog must be provably free. There is a test that replaces
`complete_with_failover`, `_complete`, `_complete_claude_cli`, `socket.socket`
and `socket.create_connection` with functions that raise, then indexes a
library. Cataloguing cannot cost money without failing that test.

### 6. Collection ↔ learning — with a feedback edge

**Cut:** the learning layer never *calls* extraction.

**But it is not one-directional.** Teaching and research legitimately discover
gaps in best practice that need new material. The learning layer **emits a
research request** that the Collector consumes as ordinary input. That is a
queue, not a call — the coupling stays one-way while the information flows both
ways.

*(This corrects an earlier claim of mine that the learning layer "never reaches
back into extraction". I had conflated coupling with feedback.)*

## Splits deliberately NOT made

**Notes text is not stored in the catalog.** Headings, locators and word counts
only. Storing bodies would put a copy of 39.7M words of copyrighted books into a
derived artifact that could land in a vault or a repo (decision D4). The cost:
a topic covered but never named in a heading is invisible.

**No ANN index for vectors.** 90,791 × 384 is 35 MB and one numpy matmul is
milliseconds. faiss would add a dependency and an index to keep in sync for a
speedup nobody would notice. Revisit past ~1M sections.

**Book text is not appended to summaries.** Transcripts are; books are not. Same
copyright reason.

**No `if source_is_book` in the visual path.** See seam 2 — that would be the
special case the data model exists to avoid.

## The rule that came out of doing this

**Never schedule a seam refactor as its own session.** The original Phase 1
("separate capture from interpretation") was a pure refactor delivering nothing
usable, and it sat unstarted for six months. Bundling it with a real second
consumer (books) made the refactor a side effect of a feature, and it shipped in
one night.

## Where the next cut probably goes

Not yet made, listed in order of likely value:

1. **Locator verification.** Notes locators are model output and unverified; a
   checker comparing a cited claim against the section it names would close the
   biggest open trust gap in the system.
2. **Query ↔ plan.** `catalog search` returns evidence. Turning that into a
   prerequisite-ordered reading plan (WP-05) is a different job with a different
   cost profile, and should be a separate module for that reason.
3. **Vault read/write.** Now unblocked — the vault is at
   `~/Documents/obsidian_vault/syncing_vault2_clean_2026`.
