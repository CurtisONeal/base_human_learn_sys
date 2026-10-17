# Caller audit — every entry point on `longform-pipeline` (WP-M1b)

**Date:** 2026-10-17. **Baseline:** `longform-pipeline` at f60bf2e (WP-M2/M3 merged), compared with `main`.
**Test:** `tests/test_integration_mvp.py` runs each row below end to end on
`tests/fixtures/mvp/`. It uses the real CLI or API, real parsing and real ffmpeg. Only the
`claude` CLI subprocess, the network and (in the default suite) ASR recognition are faked.
`scripts/smoke_all_paths.sh` runs the offline rows against the real subscription chain.

## How the paths split since WP-M2

```
media / video transcript ──► main._summarize_transcript ──► longform.run_longform   (always; single pack = 1 call)
document (book, process <local .md/.txt/.html/.pdf/.epub>, article URL)
                         ──► main._summarize_and_write_document ──► summarizer.summarize_document (map → reduce)
local audio/video and caption-less downloads ──► longasr.transcribe_media (windowed, checkpointed, gated)
every model call ──► summarizer.complete_with_failover ──► claude-cli (subscription) │ metered (cap-checked ledger)
```

## Entry points

Risk: **H** = changed heavily on this branch and on a hot path, **M** = changed but covered,
**L** = unchanged code or a thin change.

| # | Entry point | Modules reached | Changed on this branch | Regression risk | Covered by |
|---|---|---|---|---|---|
| 1 | `process <youtube>`, manual captions | downloader.probe_source → metadata.from_ytdlp_info → captions.transcript_from_captions (json3) → `source_meta.json` → longform (structure ladder: platform/description chapters → pack → quality corrections → citation audit → notes_format) → runreport | probe adds `SourceMeta`; the old `generate_summary` is replaced by long-form; `--out`; RUN_DIR | **H**: every note feature is new code. Tested with both chapter rungs; 4 chapters → 1 call; 7 real "cloud code" corrections logged; P1–P7 PASS | `test_process_youtube_with_captions[platform/description]` |
| 2 | `process <youtube>`, auto-captions only (no manual track) | as 1, plus `fetch_audio(convert=False)` (native m4a/webm, no wav) → longasr windows → completeness gate → native audio archived in `wav_files/` | whole-file wav and `transcribe_audio` removed; windowed ASR | **H**: download contract changed (`extract_info(download=True)` + `requested_downloads`). The test asserts no `postprocessors` and no legacy `download()` call | `test_process_youtube_without_captions_uses_windowed_asr`, integration `test_process_no_captions_real_parakeet` |
| 3 | `process <playlist>` | as 1/2 per entry; `None` entries skipped; `--out` = root, `<out>/<YYYYMMDD>_<title>` per video; resume skip by title | run_dir_name, per-video RUN_DIR, `_last_run.json` runs[] | **M**: the resume lookup `_existing_summary` matches `summaries/<safe_title>.summary.md`, which is the name long-form writes. Verified with a rerun: 0 calls, both `skipped`, both still reported | `test_process_playlist_one_run_dir_per_video` |
| 4 | `process <page with embedded video>` | probe_source → DownloadError → embeds.scrape_embed_urls (fetch_text) → probe each embed → as 2 | unchanged (embeds), downstream as 2 | **L/M** | `test_process_page_with_embedded_video` |
| 5 | `process <local .md/.txt/.html/.pdf/.epub>` | documents.extract → chunking → summarize_document → `<out or OUTPUT_DIR/articles>/<source_id>.summary.md` → runreport (RUN_DIR = notes dir) | `/Volumes` refusal; `--out`; RUN_DIR; `BudgetExhausted` re-raised | **L** for flow. **Found a pre-existing PDF bug (fixed, below).** Notes have no frontmatter/published/as_of (documents are not on long-form; out of scope, see plan) | `test_process_local_document[guide.md, notes.txt, article.html, tiny_book.epub, tiny_paper.pdf]`, `test_process_refuses_volumes_and_unsupported_local_files` |
| 6 | `process <article URL>` | probe_source → DownloadError → embed scan (fetch 1) → html.extract_url (fetch 2) → as 5 | unchanged | **L**. The page is fetched twice (embed scan, then the article), which is pre-existing | `test_process_article_url` |
| 7 | `book <epub/pdf/dir>` | documents.extract → as 5, output `OUTPUT_DIR/books` | `--usd-cap`; **this WP:** `--out` + RUN_DIR/`_last_run.json` (it was the only producing command without them) | **L** | `test_book[epub/pdf]` (incl. rerun skip) |
| 8 | `book --extract-only` | documents.extract → chunking; no model | unchanged; no run reported (like `longform --plan-only`) | **L** | `test_book_extract_only_makes_no_call_and_no_run` |
| 9 | `media <file>` | media.refuse_removable → media.probe → longasr.ensure_durations → metadata.from_local_media → transcribe_media → transcript → longform | whole new ASR path; `to_wav` removed; `/Volumes` refused; `--out/--title/--asr-backend/--usd-cap`; long-form notes | **H**. `published: unknown`, `as_of_basis: captured`. P3 failed on any short clip (evaluator, fixed below) | `test_media_single_file`, `test_media_truncated_aac_recorder_file`, integration `test_media_real_parakeet[m4a, aac]` |
| 10 | `media part1 part2` (camera-split) | as 9 + longasr.group_parts (creation_time contiguous, same codec/size) → from_local_group → stitched timeline | new | **M**. Part 2 lands at 0:00:10; order follows file name even when given reversed | `test_media_camera_split_group`, integration `test_media_real_parakeet[mov group]` |
| 11 | `media --probe` | media.probe, group_parts; no ASR, no model, no run | group report new | **L** | `test_media_probe_is_free_and_reports_the_group` |
| 12 | `media --transcribe-only` | as 9 up to the transcript; no model | new flag | **L**. A rerun skips (transcript exists) and still reports the run dir | `test_media_transcribe_only_then_longform` |
| 13 | `longform <run_dir> [--plan-only] [--out]` | longform.build_plan / run_longform on an existing run dir | new command | **M**. `--out` writes notes + state only. Transcripts and `source_meta.json` stay in the source dir, so the evaluator needs them copied (the test does this). Not a defect, but worth knowing when grading an `--out` dir | `test_media_transcribe_only_then_longform` |
| 14 | `catalog index/overview/list/find/toc/search` | corpus.* → documents.extract; sqlite; local embeddings only for `embed`/semantic | unchanged | **L**. Inherits the PDF-title bug (fixed): bookmarked PDFs were catalogued under their last bookmark | `test_catalog_index_and_browse` |
| 15 | `api POST /process` | api.process_pipeline (lock, per-request run_id/usd_cap) → probe_source → captions or windowed ASR → `_summarize_transcript` (long-form) → `_last_run.json` | rewritten: windowed ASR, long-form, per-request cap, RunReport | **M**. Same pipeline as the CLI. Pre-existing gaps, not regressions: no article fallback (a non-video URL fails), no skip of already-done videos (re-spends on resubmit), one failure ends a playlist, and with the default `ON_EXHAUSTION=wait` the background task sleeps holding the pipeline lock | `test_api_process_captions_and_asr`, `test_api_rejects_a_non_url_and_a_negative_cap` |
| 16 | `chrome_extension` popup → API | popup.js `POST http://localhost:8000/process {url: tab.url}` | unchanged; API model gained optional `usd_cap` | **L**. Body shape still accepted; CORS `*`. An article tab hits gap 15 | `test_chrome_extension_posts_what_the_api_accepts` |
| — | bare `python -m video2mdnotes.main <url>` | `_with_default_command` → process | `_COMMANDS` gained `media`, `longform` | **L** | existing `test_bare_url_still_reaches_process_without_a_verb` |

## Regressions / defects found by this audit

1. **PDF title replaced by the last bookmark.** This is a pre-existing bug from `main`
   (ac21eec), not something this branch introduced. In `documents/pdf.extract` the
   section loop reassigned `title`, so every bookmarked PDF got its last section's heading
   as its document title. `tiny_paper.pdf` ("Chunk Size and Retrieval") came out as
   "Results". It reached `book`/`process` notes headers and the catalog
   (`catalog toc "<real title>"` found nothing). **Fixed:** the loop variable is renamed.
   Test: `test_documents.py::test_bookmarked_pdf_keeps_its_own_title`, which fails on
   the old code. Existing catalog databases keep the wrong titles until `catalog index --force`.
2. **P3 could not pass on short media.** WP-M2 put short sources on the evaluated
   long-form path. P3 requires the last transcript line's START ≥ 98 % of the duration.
   A 15 s clip would need a cue starting in its final 0.3 s, so it failed (66 %) even
   though the ASR completeness gate passed. Real parakeet on `talk_15s.m4a`: the last cue
   starts at 0:00:07 of 0:00:15. **Fixed in the evaluator:** P3 also passes when the
   tail (duration − last start) is ≤ 30 s. That only changes the outcome for recordings
   under 25 min (2 % of 25 min = 30 s), and internal gaps are still checked. Test:
   `test_eval_longform.py::test_p3_short_clip_tail_slack`. This adjusts how the
   approved P3 bar is applied to short media; the plan file is not edited.
3. **`book` did not report its run** (no `RUN_DIR=`, no `_last_run.json`, no `--out`),
   unlike every other producing command after WP-M3. **Added**, with exit codes unchanged.
   Test: `test_integration_mvp.py::test_book`.

No regression was found in the media/video paths themselves. The runs from all 16 entry
points pass their artifact checks, and every media/video run dir passes
`scripts/eval_longform.py` P1–P7.

## Caveats

- `pytest -m integration` across the whole suite also selects the older
  `tests/e2e/test_full_pipeline.py` and `tests/test_downloader.py` integration tests.
  Those hit the real network, and the e2e test calls a metered provider when an API key
  is configured. Run the MVP real-ASR tests on their own:
  `pytest tests/test_integration_mvp.py -m integration`.
- Online paths (rows 1–4, 6, 15 against real YouTube/web) are covered only with fixtures.
  `scripts/smoke_all_paths.sh --real <url>` runs them for real.
