# Long-form execution plan

**Branch:** `longform-pipeline` · **Created:** 2026-10-15 · **Hard stop:** Friday 2026-10-16 14:00 America/New_York
**Owner:** managing agent (Claude Code main session). This file is the single source of truth
for status — update the status table after every work package, so a run that dies at a
Claude usage window can be resumed by reading this file alone.

## Intent (what "done" means)

The pipeline turns a **long** recording (hours, not minutes) into notes that are complete,
traceable, dated, and free of known transcription errors — whether or not a human
transcript exists. Proven on:

1. **E-CCA:** the 12.6 h freeCodeCamp/ExamPro CCA-F course (YouTube `reDRM0tqhNs`),
   auto-captions only, 13 chapters listed in the description.
2. **E-CLE:** the September CLE-AI meetup `.MOV` files (AKASO camera, split at ~4.19 GB,
   no captions — Whisper/ASR must do all of it). Read from
   `~/Movies/CLE-AI-26_09_safety_copies/` only. Never from the SD card.

### Evaluation — pass criteria (approved 2026-10-15)

A full run passes only if **all** hold:

| # | Criterion | Check |
|---|---|---|
| P1 | All chapters present in notes (13 for E-CCA) | deterministic |
| P2 | Every timestamp cited resolves to the right place (±30 s of cited text) | deterministic + sampled |
| P3 | Completeness gate: last transcript timestamp ≥ 98 % of duration; no gap > limit | deterministic |
| P4 | `published` and `as_of` present in frontmatter | deterministic |
| P5 | Every time-sensitive claim carries a date | deterministic (table parse) |
| P6 | Zero residual glossary mishearings in notes ("cloud code", "Clawed", …) | deterministic |
| P7 | Every correction logged in `corrections.json` with timestamp | deterministic |
| P8 | 20 randomly sampled claims each trace to the transcript at the cited timestamp | independent reviewer agent |

## Hard constraints

- **Safety copy first.** Originals are copied + SHA-256 verified + read-only before any use.
- **Never convert a whole long video to one audio file.** Extract mono 16 kHz audio in
  20–30 min windows, transcribe, checkpoint, delete the window.
- **Raw transcripts are never edited in place.** Corrections produce a separate file + log.
- **Providers:** Claude subscription (`claude-cli/…`) primary. OpenAI fallback, hard cap
  **$10 total** for this effort, enforced by a cost ledger. Nous (via Hermes) only after the
  OpenAI cap or OpenAI failure; needs `hermes login` (refresh token invalid since 2026-09-25).
- **Anthropic API credit (~$10) is reserved** for a Fable (`claude-fable-5-1`) advisor on
  logical dead ends and for the final repo code review. The pipeline must never spend it:
  no `anthropic/*` model in `LLM_MODELS` for eval runs.
- **Exhaustion:** if the subscription window runs out, checkpoint and wait for reset;
  do not switch to paid APIs beyond the caps above.
- **Do not touch** the Hermes gateway's launchd service (known-bad path; see
  `scripts/start_hermes_gateway_manual.py`).
- **Past 14:00 Friday:** notify via `hermes send --to discord:#general` (no-model sender),
  verify exit status, and also write the message to the vault inbox. **Verified delivered
  2026-10-15 22:01 ET** (arrives as "Bob" in #general).

## Architecture changes (the "why")

1. **Length gate + routing.** Media transcripts above `DEFAULT_MAX_WORDS` become a
   `SourceText` with time-ranged `sections` and go through `summarize_document()`
   (map → reduce), not the single-call `generate_summary()`.
2. **Structure ladder.** yt-dlp `chapters` → description timestamp list (≥3 entries,
   monotonic, within duration) → cue/embedding topic shifts → fixed windows.
   Record which rung was used (`structure_is_inferred`, `provenance`). Oversize chapters
   split at caption-line boundaries.
3. **Metadata capture.** Persist yt-dlp info (upload_date, uploader/channel, duration,
   chapters, description, tags, live_status) to `source_meta.json`; add
   `published_at`, `published_by`, `duration_s`, `chapters` to `SourceText`; prompt header.
4. **Context carry across passages.** Three layers:
   - *Course skeleton* (built once from chapters/description/title; given to every passage);
   - *Running ledger* (bounded ~400–800 words: concepts+definitions, terms+corrections,
     open threads, last point) passed **sequentially within a split chapter only**;
   - *Overlap tail* (last 2–3 min of previous passage, "context only").
   Chapters remain parallelisable; sub-passages of one chapter are sequential.
5. **Checkpoint + resume.** Each passage's notes **and ledger-out** saved to disk on
   completion; re-runs skip completed passages and restore the ledger.
6. **Transcript quality.** Provenance label (manual / auto-caption / ASR engine);
   glossary correction pass (title/description/tags + domain glossary) → corrected copy +
   `corrections.json`; per-passage "Transcription Uncertainty" flags; deterministic
   post-notes lint against the glossary.
7. **Time sensitivity.** `## Time-Sensitive Claims` table (claim | timestamp | as-of |
   type | verify-by); `as_of` + material age in frontmatter.
8. **Long ASR.** Windowed extraction from the copy, per-window checkpoint, completeness
   gate, multi-part (camera-split) stitching by file order with cumulative offsets.
   Pluggable ASR backend interface; benchmark faster-whisper (distil/large-v3) vs
   parakeet-mlx vs mlx-whisper (vs Apple SpeechAnalyzer if cheap) on a 10-min sample.
9. **Prompts.** Exam-prep domain profile; length-aware output budgets; reduce produces a
   course map (chapter table + cross-chapter themes), concatenating chapter notes —
   never a re-summary (the 177-word collapse rule stands).
10. **Provider routing.** Cost ledger with hard cap; failover chain extended to Nous.
11. **Curriculum hand-off (stretch).** Per-chapter YAML in Curriculum Maker v4 input shape.

## Work packages

Dependency order. `‖` = may run in parallel (disjoint files, separate worktrees).

| WP | Title | Depends | Files (primary) | Acceptance (tests against intent) |
|---|---|---|---|---|
| WP-0 | Eval harness + fixtures | — | `tests/eval/`, `scripts/eval_longform.py` | P1–P7 computed from a run dir; P8 sampler emits 20 claims for reviewer; runs on a hand-made pass + fail fixture |
| WP-1 ‖ | Metadata capture | — | `core/downloader.py`, `core/sources.py`, new `core/metadata.py` | `source_meta.json` written; CCA-F fixture yields upload_date 2026-07-20 and 13 description chapters |
| WP-6a ‖ | ASR backend interface + benchmark | — | new `core/asr/`, `scripts/bench_asr.py` | same `TranscriptResult` from ≥2 engines; benchmark table (RTF, WER vs captions on 10-min sample, glossary hits) |
| WP-2 | Structure ladder | WP-1 | new `core/structure.py`, `core/chunking.py` | rung selection unit-tested; CCA-F → 13 sections; long chapters split at line boundaries; fallback rungs tested |
| WP-4 ‖ | Transcript quality | WP-1 | new `core/quality.py` | corrected copy + `corrections.json`; raw untouched; lint finds planted errors; no false fix of real "cloud" (e.g. "cloud provider") in tests |
| WP-8 ‖ | Provider routing + cost cap | — | `core/summarizer.py`, `config.py` | ledger blocks call past cap (unit); Nous base via config; subscription-first order preserved |
| WP-3 | Media → document path + context carry + checkpoint/resume | WP-2, WP-4 | `main.py`, `core/summarizer.py`, new `core/carry.py` | length gate routes; skeleton+ledger+overlap present in prompts (assert on rendered prompt); kill/resume skips done passages and restores ledger |
| WP-5 ‖ | Time-sensitivity | WP-1 | prompts, `core/summarizer.py` | frontmatter `published`/`as_of`; claims table parsed |
| WP-7 ‖ | Prompts: exam-prep profile, budgets, course map | WP-3 | `prompts/`, `core/domains.py` | reduce output has chapter table; per-chapter notes keep all sections |
| WP-6b | Long ASR: windowing, stitching, completeness gate | WP-6a | `core/media.py`, `core/asr/` | 2-file synthetic stitched offsets correct; gate fails a truncated fixture; no full-length audio file ever written |
| E1 | Pilot eval: 3 chapters of CCA-F | WP-0..5,7,8 | — | P1–P8 on chapters (Intro+SDK setup, Sessions mgmt, Claude Code config [split test]) |
| E2 | Full eval: CCA-F 12.6 h | E1 pass | — | P1–P8 |
| E3 | Full eval: CLE-AI meetup MOVs via ASR | WP-6b, E2 | — | P1–P8 (P1 = inferred structure rung recorded; P4 published = recording date if known else labelled unknown) |
| WP-9 | Curriculum hand-off (stretch) | E2 | `core/handoff.py` | per-chapter YAML validates against v4 input fields |

## Roles

- **Managing agent (main session):** owns this file, sequencing, merges, budget.
- **Implementer agents:** one per WP, in a git worktree; must add tests that assert on the
  *artifact / rendered prompt*, not only on exit codes.
- **Reviewer agent:** per WP before merge — checks diff against the WP's acceptance and the
  Intent section; may reject.
- **Evaluator agent (independent):** runs P8 claim tracing; never sees implementer reasoning.

## Failure loop

On a failed eval: record failing criteria below → architect fix WPs in this same table
style (`WP-F1…`) → implement → review → re-run the failing eval. Iterate until pass or
the hard stop.

## Status

| WP | Status | Commit | Notes |
|---|---|---|---|
| Safety copies | in progress | — | `~/Movies/CLE-AI-26_09_safety_copies/`, SHA256SUMS.txt |
| WP-0 | in progress (agent, worktree) | | started 2026-10-15 ~22:00 ET |
| WP-1 | **done** — merged | 11775c3 | 15 new tests; 320 pass. Note for WP-6b: `media` still calls whole-file `to_wav()` — must become windowed |
| WP-6a | in progress (agent, worktree) | | started 2026-10-15 ~22:00 ET |
| WP-2 | pending | | |
| WP-4 | pending | | |
| WP-8 | in progress (agent, worktree) | | started 2026-10-15 ~22:00 ET |
| WP-3 | pending | | |
| WP-5 | pending | | |
| WP-7 | pending | | |
| WP-6b | pending | | |
| E1 | pending | | |
| E2 | pending | | |
| E3 | pending | | |

## Eval log

_(append each run: date, eval, P1–P8 results, cost, decision)_
