# Fixed bugs

**Why this file exists.** ADRs record *decisions* — why the pipeline is shaped the way it
is. Most bugs fixed here were not decisions; they were defects with a specific cause and a
specific test. Burying them in ADR prose makes them unfindable ("did we already fix the
403 thing?") and makes the ADRs worse at their actual job.

**The recurring pattern is the point:** almost every entry below was found by *running the
thing against real content*, not by reading code. Several contradicted a confident guess.
When a fix here has no test, that is called out — those are the ones likely to regress.

Newest first. Format: symptom → cause → fix → how we know.

---

### An 8,956-word note became 22 passages and four Overviews (WP-D1)
- **Symptom:** T8 (`process` on the Entra Obsidian note) produced notes with
  `## Overview` ×4, `## Key terms` ×3, four `# ` titles and topics split across
  seams ("Client secret" twice). The model wrote "the source shows these
  commands without a ``` fence" — because the fence it was given really had
  been cut off.
- **Cause (two):** (1) `documents/plaintext._split_markdown` was not
  fence-aware: every `# comment` line inside a ```bash block was an ATX
  heading. Measured: 62 sections, 22 top-level groups, 17 sections holding
  half a code block. (2) `chunk_sections` starts a new chunk at every
  top-level heading, so those 22 groups were 22 passages; the reduce then ran
  in 4 batches of 6, each writing its own title/Overview/Key terms, and the
  batches were concatenated.
- **Fix:** fence-aware heading split (43 sections, 0 cut code blocks);
  `chunking.pack_sections` (one chunk when the document fits 12k words,
  otherwise whole chapters packed); single `_WHOLE_SYSTEM` call for one-chunk
  documents; multi-passage reduce writes topics only, one overview call,
  key terms merged by `docnotes.consolidate`. `fences.repair_fences` runs on
  every assembled notes file and in the packager.
- **Note on T8's fence count:** `grep -c '```'` gives 95, but one is an inline
  mention on line 544; the 94 line-start fences pair up under CommonMark rules.
  The "unclosed fence" was inferred from the odd grep count, not observed in
  Obsidian.
- **How we know:** `tests/test_document_path_d1.py` (one call / one Overview
  on a 9k-word note with bash comments; packing; merged key terms; nine fence
  repair cases; markdown packaging). The T8b rerun is described in the WP-D1
  report.

### Every bookmarked PDF was titled after its last bookmark (WP-M1b)
- **Symptom:** `tiny_paper.pdf`, with metadata title "Chunk Size and Retrieval" and
  bookmarks Introduction/Method/Results, came out titled "Results". That wrong title
  appeared in the notes header, the `book` output and the catalog, so `catalog toc
  "Chunk Size"` found nothing. The bug has been present since ac21eec on `main`.
- **Cause:** in `documents/pdf.extract` the per-section loop assigned the truncated
  heading to `title`, the same variable that held the document title read just above.
- **Fix:** the loop variable is renamed. Existing catalog DBs keep the wrong titles
  until `catalog index --force`.
- **How we know:** `tests/test_documents.py::test_bookmarked_pdf_keeps_its_own_title`
  fails on the old code. `test_integration_mvp.py::test_catalog_index_and_browse`
  also covers it.

### P3 was unpassable for short media (WP-M1b)
- **Symptom:** a 15 s clip passed the ASR completeness gate but failed P3 at 66 %.
  Real parakeet on `talk_15s.m4a` puts its last cue at 0:00:07 of 0:00:15.
- **Cause:** P3 needs the last line's *start* ≥ 98 % of the duration. Lines carry
  start times only, so a final cue or trailing room tone always starts seconds before
  the end. That is harmless at 12 h and fatal at 15 s. WP-M2 is what put short
  sources on the evaluated long-form path.
- **Fix:** P3 also passes when the tail (duration − last start) is ≤ 30 s. That only
  changes the outcome below 25 min, and internal gaps are still checked.
- **How we know:** `tests/test_eval_longform.py::test_p3_short_clip_tail_slack`, plus
  every media run in `tests/test_integration_mvp.py` (scripted and real parakeet).

### An evaluation that ran on nothing (T3 harness, WP-M3)
- **Symptom:** the managing agent evaluated the Karpathy run with a glob
  `*let_s_build_gpt*`; the folder is `…_lets_build_gpt_…`, the glob matched
  nothing, and the evaluation "ran" without a run dir to read.
- **Cause:** the caller re-derived the folder name from the title instead of
  being told it, and `sanitize_filename` drops the apostrophe ("Let's" →
  `lets`) rather than turning it into `_`. Nothing in the pipeline reported
  where a run went, and the evaluator did not check that its input existed.
- **Fix:** `process`/`media`/`longform` print `RUN_DIR=<absolute path>` per run
  as the last stdout lines and write `previous_run_results/_last_run.json`
  (`core/runreport.py`); `process --out` pins the run dir; the evaluator exits 2
  with an `ERROR:` line when the run dir is missing or has no transcripts or
  notes; dated folder names come only from `downloader.run_dir_name`.
- **How we know:** `tests/test_short_path.py` (the title → folder case, a
  source scan for other date-prefixed names, RUN_DIR as the last line for
  `process` single/playlist and `longform`, `_last_run.json`, evaluator exit 2).

### Short sources got none of the long-form features (T2, T5, WP-M2)
- **Symptom:** transcripts ≤ 12k words went through the old single-call
  prompt. Azure Master Class Pt 1 (22 platform chapters) came out with one
  heading, no `published`/`as_of`, no time-sensitive table, no corrections —
  FAIL P1/P4/P5/P7; the ClePy FastAPI talk had 0 citations.
- **Cause:** a length gate in `main._summarize_transcript` kept short
  transcripts on `generate_summary`; every long-form feature lived only past it.
- **Fix:** no gate — every media transcript goes through `run_longform`. A
  source that fits one passage is ONE call (multi-chapter prompt with a
  `## Chapter <n>` marker per chapter, split back into per-chapter sections);
  the Course Map is then deterministic and the consistency call skipped.
  Longer sources pack short adjacent chapters (≥ 3k words or ≥ 8 min per
  call): Karpathy's 30 chapters are 10 calls instead of 30.
- **How we know:** `tests/test_short_path.py` asserts on the rendered pack
  prompt and the assembled notes (per-chapter headings with in-range
  citations, frontmatter dates, claims table, corrections.json, P1–P7 pass)
  and on a no-chapter source passing P1 via `structure.json`; real reruns
  in `previous_run_results/T2b_azure_p1` and `T5b_aac_clepy` (`eval/`).

### Correct citations marked "unverified" because captions misheard the term (E2b, WP-F8)
- **Symptom:** E2b's notes carried two `_(unverified citation)_` markers on
  citations the P8 reviewer found correct: `.gitignore` at 6:59:52 (captions:
  "add a get ignore") and "Opus consumes plan usage" at 10:12:06 (captions:
  "use opus. It burns up my credits").
- **Cause:** the audit matched terms exactly. `gitignore` never occurs as
  spelled, so the unit had no strong term "said anywhere", and the content
  words that do occur at the cite ("use", "plan") did not match "usage".
- **Fix:** before marking unverified, `citations.fuzzy_support` matches terms
  against the corrected AND raw caption lines in the cited window (edit
  distance ≤ 0.34, crude stems, compound split against adjacent words). A
  match leaves the unit unmarked (`citation:fuzzy_ok`) and clears an older
  mark (`citation:unmarked`). Retargeting is untouched.
- **How we know:** `tests/test_f8.py` reproduces both E2b cases (and asserts
  the exact-only audit still marks them); reaudit of an E2b copy cleared
  exactly those two marks and changed nothing else; E1/E1b replay tests pass.

### One ASR segment ran two minutes and failed P3 (E3, WP-F5)
- **Symptom:** E3 (CLE-AI meetup, parakeet-mlx) failed P3 with a 122.9 s
  start-to-start gap at 1:53:41. Nothing was missing: one 229-word segment
  (111 s in window `03_000`) covered it. The transcript stores start times
  only, so the evaluator saw a gap, and any citation into that stretch could
  only point at a two-minute line.
- **Cause:** parakeet-mlx splits sentences on punctuation only; an unpunctuated
  run stays one "sentence". No backend capped segment length.
- **Fix:** `core/asr/segmenting.py` splits any segment over
  `ASR_MAX_SEGMENT_S` (20 s) / `ASR_MAX_SEGMENT_WORDS` (60) at sentence ends,
  then clauses, then words, using the engine's token/word timings when present
  and proportional timing otherwise. All three backends and the stitch apply
  it. Checkpoints now keep per-segment `words` timings so re-capping never
  needs re-ASR. End times were not added to transcript lines: every parser
  requires `]` right after the start time.
- **How we know:** E3 re-transcribed into `previous_run_results/E3b_cle_ai_meetup/`
  (numbers in the WP-F5 report / commit). Tests: `tests/test_segmenting.py`
  (timed split, proportional split, text preserved, monotonic, clip-then-split,
  checkpoint round trip).

### The citation audit moved a correct citation on generic words (E1b, WP-F4)
- **Symptom:** E1b's audit retargeted "`/sandbox` didn't detect them until he
  started a new session" from [8:23:35] — where "sandbox" is said on the cited
  line — to [8:30:19], a line about something else that happens to say
  "starting" and "session". The move made a correct note wrong and logged it
  as a fix. Same run: `[7:55:55 n/a]` / `[7:55:53 n/a]` rendered as text, and
  the header said "4 of 14 chapter(s)" while the frontmatter said 13.
- **Cause:** retargeting compared overall window scores; the strong term was
  present at both places, so the content-word share alone decided it. The
  `n/a` markers were model output that `CITE_RE` did not match, so the audit
  passed them through. The header line still counted the preamble.
- **Fix:** a retarget now needs a strong or rare (≤2 lines) term at the new
  line and none at the cited line ±5 s (`ORIGIN_S`; narrower than the 60 s
  window on purpose, since E1's wrong jq citation has an unrelated "JQ" 8 s
  away). `normalize_citation_markers` strips `[H:MM:SS n/a]` in the audit and at
  assembly. One `_chapter_counts` feeds header and frontmatter. The Fact-Checked
  prompt asks for an impersonal voice (prompt only).
- **How we know:** replaying the audit on copies of E1 and E1b (E1b's recorded
  retargets undone first): E1 still makes both reviewer-confirmed moves; E1b
  makes only `Bash(npx)` [8:51:18]→[8:48:57]. Tests:
  `test_e1b_correct_sandbox_citation_is_not_retargeted` (real E1b lines in
  `tests/fixtures/citations/`), plus a control test showing the fixture still
  reproduces the old move without the guard, `test_e1b_replay_keeps_correct_citations`,
  `test_leftover_citation_marker_is_normalized`, `test_partial_header_excludes_preamble`.
  The first-person wording is checked only in the prompt; whether the model
  obeys it is untested until the next real run.

### `source_meta.json` had no `duration` for the evaluator; the last cue ran past the end
- **Symptom:** two contract mismatches found by running the long-form path on
  the CCA-F transcript and grading it with `eval/longform.py`. (1) The evaluator
  reads yt-dlp's `duration` from `source_meta.json`; `SourceMeta.write` wrote
  only `duration_s`, so every pipeline-written run dir would have failed P3
  with "duration unknown" — the evaluator prefers `source_meta.json` over
  `yt_info.json`, so the duration in the raw dump never got a look in. (Found
  by reading both sides before the first run, so this one is inferred, not
  observed.)
  (2) The last caption line's end is a speech-rate estimate (12:38:34 + 7
  words ≈ 12:38:37), one second past the real 12:38:36, so the last chapter's
  range and every heading citing it fell outside the duration: P2 181/185.
- **Fix:** `write()` adds the yt-dlp aliases (`duration`,
  `chapters[].start_time`; ignored on read). The long-form plan clamps the last
  segment's end to `duration_s`. After: P2 185/185, P3 PASS on the mocked full run.
- **Test:** `test_source_meta_json_carries_evaluator_aliases`,
  `test_cca_full_13_chapters_end_to_end` (P1–P7 asserted).

### UTF-16 notes reported as "empty file"; links in headings made locators unusable
- **Symptom:** two defects, both found only by running the extractor across
  all 1,650 real vault notes rather than the one note used to call it done.
  (1) 8 notes failed with "empty file, nothing to summarize" — one of them
  7 KB. (2) 38 notes produced locators like
  `§[uv](https://docs.astral.sh/uv/#uv)`.
- **Cause:** (1) those notes are UTF-16LE with a BOM (Windows tooling
  exports). `read_text(encoding="utf-8")` raises `UnicodeDecodeError`, which
  **is a subclass of ValueError** — so the `except ValueError` guarding the
  empty-file case caught it and reported the wrong reason entirely.
  (2) markdown links in a heading were kept verbatim; a locator exists to be
  searched for in the source, and a URL defeats that.
- **Fix:** (1) read bytes and honour an explicit BOM (UTF-16/32 LE+BE,
  UTF-8-sig), falling back to UTF-8 with replacement rather than refusing a
  whole note over a stray byte; strip the residual U+FEFF, which
  utf-16-le/be leave in the decoded string and which is invisible in a
  terminal but present in the summary. (2) reduce `[text](url)` to its link
  text in a heading, dropping bare `[](url)` permalink anchors entirely —
  same principle as the doubled-hash fix: a heading's own title is metadata
  to normalise, not content to preserve verbatim.
- **Test:** `test_utf16_file_is_read_not_reported_as_empty`,
  `test_byte_order_mark_never_reaches_the_text`,
  `test_markdown_link_in_a_heading_is_reduced_to_its_text`,
  `test_bare_permalink_anchor_in_a_heading_is_dropped`.
- **After:** 1,556 of 1,650 notes extract (was 1,548); 0 noisy locators (was
  38); the 94 remaining failures are genuinely empty files. EPUB A/B parity
  re-confirmed unchanged.

### A "deduplication" refactor silently changed EPUB extraction on 24 of 78 books
- **Symptom:** none visible. Extraction still ran, still produced plausible
  word counts, still passed all 45 document tests. 24 of 78 real EPUBs lost
  content — up to **9.6% of a book** (masteringgo: −17,918 words), −72,239
  words across the sample.
- **Cause:** extracting EPUB's HTML parser into a module shared with the new
  `html.py` was fine in itself. Two *behaviour* changes rode along inside the
  "pure refactor":
  1. `nav` and `footer` added to the shared skip set — correct for a web page
     (site chrome), wrong for EPUB, which uses `<footer>` for footnotes and
     `<nav>` for in-chapter structure.
  2. `highlight|hljs|codehilite` added to the code-class regex — technical
     EPUBs put `class="highlight"` on broad *wrappers* (masteringgo: 11,595
     occurrences), and a wrapper opening a code fence mis-tracks the fence
     nesting and drops text rather than merely re-fencing it.
- **Fix:** `skip_tags` and `code_class` became per-instance constructor
  arguments. The defaults are exactly what EPUB validated against the
  880-book corpus; `html.py` opts into `WEB_CHROME_TAGS` and `WEB_CODE_CLASS`
  explicitly. Verified by A/B-ing the pre-refactor extractor (restored from
  git) against the current one across 80 real EPUBs: **78 identical, 0
  different.**
- **Test:** `test_epub_default_parser_does_not_skip_nav_or_footer`,
  `test_epub_default_code_classes_exclude_syntax_highlighter_names`,
  `test_html_opts_into_web_semantics_explicitly`.
- **How it got through:** I verified the refactor against ONE book
  (llmops.epub) which happened to be unaffected, and reported it as
  "byte-identical extraction on the real corpus". One book is not a corpus.
  This is the exact failure the `verify-the-artifact-not-the-diff` memory
  describes, committed the same night that memory was written — verifying an
  artifact is not enough if the sample can't show the failure. **A refactor
  touching validated code needs an A/B across the population, not a spot
  check.**

### A refactor conflated "ordinary failure" with "exhaustion, stop the batch"
- **Symptom:** none shipped — caught while writing the refactor, before any
  test ran, by re-reading the original code's exact branch semantics rather
  than trusting the new shape looked equivalent.
- **Cause:** extracting `book`'s per-file summarize logic into a shared helper
  (`_summarize_and_write_document`, reused by `process`'s new local-file/article
  path) initially returned the same `"failed"` value for three different
  cases: an ordinary exception, a `SubscriptionExhausted` retry that failed
  again under `--on-exhaustion wait`, and immediate exhaustion under any other
  policy. The caller's loop then did `if _resolve_exhaustion_policy() != "wait":
  break` on every `"failed"`, which would have stopped an entire batch on one
  corrupt file whenever the operator had set `--on-exhaustion` to anything but
  `wait` — directly contradicting "one bad book must not stop the batch", the
  stated reason the per-file try/except exists at all.
- **Fix:** a fourth return value, `"stop"`, used only for the one case that
  should actually halt the batch (exhaustion, non-wait policy). Everything
  else — ordinary failures and a failed wait-then-retry — returns `"failed"`
  and the batch continues, matching the original code exactly.
- **Test:** none directly (behavioural equivalence to the pre-refactor code,
  verified by re-running the full suite: 273 passed, unchanged from before the
  refactor). Worth a dedicated regression test if this helper changes again.

### A doubled markdown heading marker leaked into a locator
- **Symptom:** running the new plaintext extractor against a real vault note
  (not a synthetic fixture) produced `[§# API Design and Fundamentals of…]` —
  a locator with a literal `#` inside it.
- **Cause:** the source file's first line was `# # API Design and
  Fundamentals...` — a doubled hash, almost certainly an artifact of whatever
  exported the note rather than something the author typed. The ATX heading
  regex correctly parsed the outer `#` as the marker and captured `"# API
  Design..."` as the title verbatim — technically correct, visibly wrong.
- **Fix:** a heading's own title is metadata a reader uses to find the
  section, not body content a claim quotes — unlike a fenced code block, which
  must survive untouched, a title is fair to normalise. Any leading `#+\s*`
  surviving into the captured title is now stripped.
- **Test:** `test_doubled_hash_in_a_heading_title_is_stripped`, plus the fix
  was verified against the real file that exposed it before being called done.

### Chapter-level locators were too coarse to check
- **Symptom:** every claim in book notes cited `[ch. 1]`. On LLMOps that is 33
  sections and 6,923 words, and the whole 259-section book collapsed to 15
  distinct locators. A human checking one claim about open-source trade-offs
  read through the vocabulary sections and never reached the passage — the
  claim lived four subsections away under "The Big Debate".
- **Cause:** `_locator_for()` extracted the full heading path and then returned
  only the chapter number from it. The precision existed and was discarded.
- **Fix:** locator carries the section: `[ch. 1 §The Big Debate: Open Source…]`.
  PDF matches: `[p. 22 §Decoder-Only LLMs]`. 15 -> 249 distinct locators.
- **Test:** covered by the document tests; verified against the real corpus.

### A chunk locator named one section as though it were the whole span
- **Symptom:** chunk spanning all of chapter 1 rendered as `[ch. 1 §References]`
  — the LAST section only, reading as though the chunk were that section.
- **Cause:** when the first section's locator had no `§title` the code fell back
  to `first_title or last_title`, silently taking whichever existed.
- **Fix:** section names appear only when BOTH ends are known; otherwise the
  honest coarser `[ch. 1]`. A locator that points somewhere real but wrong is
  worse than one that is merely vague.
- **Test:** `test_chunk_locator_*` in tests/test_documents.py.

### Auto model-select silently overrode an explicit FW_MODEL
- **Symptom:** the full test suite went from 5 minutes to **39**. Tests that pin
  `settings.fw_model = "tiny"` were loading 3.1 GB `large-v3`.
- **Cause:** `select_model()` consulted the auto-selector unconditionally and
  never looked at `settings.fw_model`. The docstring claimed an explicit
  FW_MODEL pins the model; it did not.
- **Fix:** auto-select chooses BETWEEN `large-v3` and `distil-large-v3` only.
  Any other value is a deliberate pin and is honoured. Suite back to 4.8s.
- **Test:** `test_an_explicit_fw_model_is_honoured_not_overridden`.

### Variable shadowing broke PDF extraction outright
- **Symptom:** `IndexError: string index out of range` on every PDF.
- **Cause:** a new local `pages = f"p. {first}"` shadowed the enclosing `pages`
  list of page texts, so `pages[p - 1]` indexed into a string.
- **Fix:** renamed to `page_ref`, with a comment naming the hazard.
- **Test:** caught immediately by running against the real corpus.

### Book notes described code instead of reproducing it
- **Symptom:** the EPUB extractor fed the model 140 fenced code blocks from
  `llmops` and the notes came back with ZERO. The model wrote prose about the
  code (72 inline spans) rather than copying it. For a corpus with 453,676 code
  blocks, notes without code are notes about a book you still have to open.
- **Cause:** the chunk prompt said "Preserve code, commands and API names
  exactly". "Preserve" reads as "do not corrupt what you quote", not "quote it".
- **Fix:** the instruction now says COPY, names the ``` fence explicitly,
  forbids paraphrase/abbreviation/"# rest of implementation", and asks for one
  sentence of context before each block. The reduce prompt gained a matching
  rule, because the reduce is the other place code silently disappears.
- **Test:** measured on the three code-densest llmops chunks — 120 blocks in,
  **73 out** (was 0). Not 100%: the model still consolidates or drops some, so
  this is an improvement, not a guarantee.

### Recursive reduce turned a book into 177 words of chat
- **Symptom:** `llmops.pdf` finished all 16 passages, then wrote a 177-word file
  reading "This is excellent work… ✓ Locators preserved… Do you want me to merge
  Chapters 1–5?" — the model reviewing its own input instead of assembling it.
  The EPUB run of the same book produced 15,247 usable words.
- **Cause:** batching to avoid a timeout introduced a *second* reduce round. Its
  input was already-assembled notes, which the model read as finished work to
  comment on. Recursive reduce also compounds detail loss at every round.
- **Fix:** exactly ONE round of batched reduces (6 passages each), then
  **concatenate** the batch outputs — each covers a contiguous, ordered span with
  its own locators, so joining them needs no second pass. The reduce prompt now
  forbids preamble, commentary and offers of further work.
- **Test:** `test_conversational_reduce_output_is_rejected`,
  `test_a_good_summary_with_a_chat_opener_is_kept_not_rejected`,
  `test_reduce_that_returns_a_fragment_is_rejected_by_proportion`.

### A phrase-matching quality guard rejected the best output produced
- **Symptom:** the first guard written for the bug above flagged the *good*
  15,247-word EPUB notes as conversational.
- **Cause:** it matched wording. That document opens "I'll assemble these chapter
  notes into a comprehensive single reference document" and is excellent. Opening
  phrasing does not predict output quality.
- **Fix:** judge by PROPORTION instead — a reduce returning under 10% of its
  input, or under 300 words from a substantial book, has lost the content
  whatever it says. A chat preamble above the first heading is now stripped
  rather than treated as grounds for rejection.
- **Test:** both real artifacts are asserted directly: the collapsed PDF output
  is rejected, the good EPUB output passes.

### A failed reduce discarded every passage of a book
- **Symptom:** `llmops.pdf` summarized all 16 passages successfully, then the
  single final reduce call timed out at 900s and the exception propagated. All
  16 model calls — the entire cost of the run — were thrown away, and no file
  was written.
- **Cause:** batching only kicked in above 20,000 words of notes. 16 passages
  came to roughly 16,000, so one reduce call received the lot. Worse, the reduce
  had no failure path: the per-passage notes existed in memory and were dropped.
- **Fix:** batch every 6 passages so no reduce call is large, and on any
  non-exhaustion failure emit the per-passage notes with a header explaining the
  reduce failed. `SubscriptionExhausted` still propagates so the caller can wait
  and resume.
- **Test:** `test_reduce_failure_still_yields_the_passage_notes` and
  `test_exhaustion_during_reduce_is_not_swallowed`. The media path already had
  this property for transcripts; the book path did not, and the asymmetry was
  invisible until a real 91,000-word book hit it.

### PDF outline order let one bookmark claim an entire book
- **Symptom:** `llmops.pdf` and `hands-onmachinelearning….pdf` each produced
  exactly ONE section spanning every page, titled
  `7x9_web_Rabbit.pdf > Blank Page` — while still reporting provenance
  `pdf (bookmarks)` and a plausible 90,694-word count.
- **Cause:** page ownership walked bookmarks in *outline* order, each claiming
  "my start page through the end". A PDF outline need not be monotonic and real
  ones are not: LLMOps ends its outline with three production artifacts
  (`7x9_web_Rabbit.pdf`, `Blank Page`, `Blank Page`) all pointing at page 1.
  Last in outline order, they overwrote ownership of all 284 pages.
- **Fix:** sort bookmark indices by (page, outline position) and give each page
  to the last bookmark starting at or before it; emit sections in page order.
  llmops now yields 172 sections, hands-on ML 372.
- **Test:** `test_non_monotonic_outline_does_not_swallow_the_book` and
  `test_pdf_sections_are_emitted_in_page_order` build real PDFs with a
  backwards outline. Found by `--extract-only` printing "1 sections" beside
  "pdf (bookmarks)", which is self-contradictory.

### Adding a second CLI command broke the bare-URL invocation
- **Symptom:** three existing tests failed after the `book` command was added.
  Nothing in the video path had changed.
- **Cause:** typer requires an explicit subcommand once more than one command is
  registered, so `python -m video2mdnotes.main <url>` — the form used by
  readme.md, the overnight sweep and any cron entry — became invalid.
- **Fix:** `_with_default_command()` inserts the implicit `process` verb when the
  first argument is neither a known command nor a flag.
- **Test:** `test_bare_url_still_reaches_process_without_a_verb` pins the
  bare-URL, explicit-verb, `book` and `--help` forms.

### Book chunks were summarized with the video prompt
- **Symptom:** book passages produced plausible notes with no locators and no
  SKIP behaviour. No error, no failing test.
- **Cause:** `_complete_claude_cli()` takes the system prompt as a *file*. When
  the failover loop was factored into `complete_with_failover(system_prompt, …)`,
  the CLI branch kept passing `settings.prompt_file` and silently ignored the
  new argument.
- **Fix:** write the caller's prompt to a `NamedTemporaryFile`, pass that path,
  delete it in a `finally`.
- **Test:** `test_claude_cli_receives_the_callers_system_prompt` sends a sentinel
  string and asserts the CLI reads exactly it. Originally caught by reading
  `ps aux` for the running job — adding a parameter can leave a call site
  ignoring it while every test stays green.

### PDF bookmark page ranges double-counted text
- **Symptom:** `llmops.pdf` extracted to 213,451 words. The same book's EPUB gave
  91,178; the PDF's own text layer holds 90,778. A 2.35× inflation.
- **Cause:** each bookmark was given the span from its own start page to the next
  bookmark's. Bookmarks routinely share a start page (92 of 264 here) and nested
  outlines let a chapter's span cover its own subsections, so pages were claimed
  repeatedly.
- **Fix:** partition pages so each belongs to exactly one bookmark. Duplication
  is now 1.00–1.01× across all 11 paired books.
- **Test:** `test_pdf_pages_are_never_double_counted`, plus
  `test_pdf_and_epub_of_the_same_book_agree_on_length` (within 10%). Found only
  because the corpus holds the same titles in two formats.

### A bare `uv sync` silently strips lint, tests and visual extraction
- **Symptom:** `ruff`, `pytest`, `ocrmac`, `imagehash` and `Pillow` disappear. The
  checkout still imports and still runs, so nothing announces the loss.
- **Cause:** OCR and dev dependencies live in optional extras; a bare `uv sync`
  installs neither and *removes* anything not in the base lock.
- **Fix:** always `uv sync --extra ocr --extra dev`. Documented in readme.md.
- **Test:** none — environment state, not code. Caught by importing after a sync.

### Yanked numpy 2.4.0 under the transcription engine
- **Symptom:** none observed. Surfaced as a `uv` warning during an unrelated lock.
- **Cause:** numpy 2.4.0 was yanked upstream for a backward-compatibility bug and
  sat beneath `ctranslate2` (Whisper inference), `onnxruntime` (VAD) and
  `ImageHash`. A numeric-library compat bug there surfaces as wrong numbers
  rather than crashes, which is the bad kind.
- **Fix:** `uv lock --upgrade-package numpy` → 2.4.6 (a newer non-yanked release,
  not a rollback). Verified with the integration suite, 5/5.
- **Trap:** `uv.lock` lists versions **per Python version**. 2.2.6 (py<3.11) and
  2.4.0 (py≥3.11) both appear; reading the first match reports the wrong answer.
- **Test:** integration suite exercises the real Whisper path.

### yt-dlp staleness causes universal HTTP 403 (twice in four days)
- **Symptom:** every download fails with `HTTP Error 403: Forbidden`. Looks like a site
  problem or an IP block.
- **Cause:** YouTube changed; the pinned yt-dlp is stale. Happened at 8 months stale
  (2026-08-16) and again at 6 weeks stale (2026-08-20).
- **Fix:** bump yt-dlp **in `uv.lock`**, not just the venv — the lockfile pin is what
  caused the staleness, and a venv-only update is undone by the next `uv sync`.
- **Diagnostic that works:** retry a video that downloaded successfully before. If it now
  fails too, the cause is upstream — stop investigating the content.
- **Test:** none possible (external dependency). See ADR-041.

### Intermittent "No such file or directory" during frame extraction
- **Symptom:** 1 video of 7 fails; succeeds on retry. Classic "flaky" write-off.
- **Cause:** `download_video` globbed `source_video.*` and took the first sorted match.
  yt-dlp leaves per-stream fragments (`source_video.f399.mp4`) beside the merged output and
  deletes them after merging — and `f` sorts before `m`, so it picked the doomed fragment.
- **Fix:** skip fragment-named files, pick the largest survivor.
- **Test:** `test_download_prefers_merged_output_over_fragments`.

### Vision model emitted `# Frame Description` inside a `###` block
- **Symptom:** notes' heading hierarchy silently corrupted.
- **Cause:** the model had no way to know its text was being inserted into an existing
  document.
- **Fix:** prompt forbids headings *and explains why*; any heading that survives is
  stripped — a prompt instruction is not a guarantee.
- **Test:** verified across 29 frame blocks, 0 headings.

### Frames could not be correlated with narration
- **Symptom:** a frame at `[3m40s]` had nothing to line up against.
- **Cause:** the summary appends the transcript as raw `full_text` with **no timestamps**.
- **Fix:** each frame block quotes the narration spoken at that moment.
- **Test:** 26 narration blocks across the Postman playlist.

### Fabricated dwell time flattened frame ranking
- **Symptom:** one frame scored 1.00, everything else near 0.
- **Cause:** final-frame dwell computed as `video_duration - timestamp`, but when
  `FRAME_MAX` truncates extraction the last keyframe is just where we stopped looking. A
  1120s video that stopped at 349s credited its last frame with 771 fabricated seconds.
- **Fix:** clamp to the longest gap actually *observed* — not a multiple of the median,
  because transition-heavy content has a ~1s median that would crush a held closing slide.
- **Test:** `test_final_frame_dwell_is_clamped`. (The first fix was wrong; a test caught it.)

### Scene detection under-samples continuously-animated video
- **Symptom:** 1 keyframe from a 25-second video whose screen carried the whole message.
- **Cause:** scene detection assumes cuts; animation has none.
- **Fix:** fall back to interval sampling when frame *count* is too low, so the failure
  self-corrects instead of going silent.
- **Test:** measured 1 → 9 frames; that video went from a 94-byte placeholder to 3797 bytes.

### Budget spent on the first N frames rather than the best N
- **Symptom:** a budget of 8 spent 6 frames inside one 10-second window of near-duplicates.
- **Cause:** escalation walked chronological order and stopped at the cap.
- **Fix:** rank by dwell + uniqueness + audio-cue proximity; spend on the top N.
- **Test:** `tests/test_ranking.py`; changed 6 of 8 selections on real frames.

### `player_client: ['android']` override caused 403s
- **Symptom:** downloads fail; half a course run lost.
- **Cause:** added long ago "for playlist reliability"; YouTube now requires a PO token for
  that client. Measured: android exposed **1** usable audio format vs **5** on defaults.
- **Fix:** removed. Playlists re-verified without it (10/10).
- **Test:** none directly; playlist behaviour covered by integration runs.

### Placeholder API keys reached the provider
- **Symptom:** confusing auth error instead of "no key configured".
- **Cause:** `.env.example` ships `ANTHROPIC_API_KEY=your_key_here`, which is **truthy**, so
  `if not key` passed it through.
- **Fix:** `real_key()` rejects known placeholders.
- **Test:** `test_placeholder_keys_resolve_to_none`.

### `litellm.anthropic_api_key` was a dead attribute
- **Symptom:** Anthropic auth "worked" locally, would fail on any clean machine.
- **Cause:** the real attribute is `anthropic_key`. The assignment created a stray module
  attribute holding the real secret; auth only worked because the key was in the shell env.
- **Fix:** per-call `api_key=`, resolved per provider prefix.
- **Test:** `test_chain_falls_back_to_next_entry` asserts per-provider keys.

### Unit tests silently made live subscription calls
- **Symptom:** suite time jumped 104s → 353s.
- **Cause:** tests patch `litellm.completion`, but the new default chain leads with a
  **subprocess** litellm never sees.
- **Fix:** `pinned_chain` fixture; every hermetic test must pin the chain.
- **Test:** suite runs in ~1s.

### One `None` playlist entry killed an entire run
- **Symptom:** `AttributeError: 'NoneType' object has no attribute 'get'`.
- **Cause:** yt-dlp yields `None` for private/deleted/geo-blocked items.
- **Fix:** skip with a warning naming the position.
- **Test:** `test_probe_source_skips_unavailable_playlist_entries`.

### Caption fetch accepted any URL scheme
- **Symptom:** none observed — found by review.
- **Cause:** caption URLs come from third-party metadata and `urllib` opens `file://`.
  Verified reading `/etc/hosts`.
- **Fix:** http/https only, 8 MB cap, shared by caption fetch and embed scraping.
- **Test:** `test_fetch_refuses_non_http_schemes`.

### CJK caption tracks were rejected as "no usable speech"
- **Symptom:** every Japanese/Chinese/Korean track skipped, with a misleading log line.
- **Cause:** the speech guard counted Latin words only; language selection never looked at
  them either. **Broken for one of the project's two stated domains** (Aikido).
- **Fix:** count CJK by character; `CAPTIONS_LANG` ordered preference with an `any` token.
- **Test:** verified live — 220 cues on a real Japanese track.

### Titles containing `/` truncated recorded provenance
- **Symptom:** `source:  Yamada Sensei.en.json3` — the title silently lost.
- **Cause:** `Path(f"{title}...").name` keeps only the trailing component. A real archived
  title (`Intro to "Aikido..." / Yamada Sensei`) triggered it.
- **Fix:** sanitize before use as a path.
- **Test:** `test_title_with_slash_does_not_truncate_recorded_source`.

### `original_url.txt` recorded an expiring CDN link
- **Symptom:** archived runs could not be re-fetched.
- **Cause:** `entry['url']` on YouTube is a signed, expiring link.
- **Fix:** prefer `webpage_url`.
- **Test:** `test_probe_source_records_canonical_watch_url`.

### Claude CLI "session limit" message not treated as exhaustion
- **Symptom:** E2d (2026-10-16): three passages failed with "You've hit your session limit · resets 9:30am (America/New_York)" instead of waiting for the reset.
- **Cause:** the exhaustion pattern knew "usage limit"/"rate limit" wording only, and the reset parser knew durations, not wall-clock times.
- **Fix:** match "session limit" / "hit your … limit"; parse "resets 9:30am" as the next occurrence of that local time.
- **Test:** `test_session_limit_wording_is_exhaustion_with_clock_reset`.

### Model cited a timestamp past the end of the video
- **Symptom:** E2d P2 2396/2397 — "[12:42:00]" in a 12:38:36 video, between two correct neighbours.
- **Cause:** the citation audit only checked citations against the passage's lines; nothing rejected times outside the passage.
- **Fix:** lone citations > 120 s outside the passage are dropped and logged (`citation:out_of_range`); a line left without a citation is marked unverified.
- **Test:** `test_out_of_range_citation_is_dropped_and_logged`.

### Camera reset clock became the recording date
- **Symptom:** the CLE-AI meetup (Sept 2026) would have been dated 2024-01-01 in `as_of`.
- **Cause:** a device date ≥ 2005 and not after capture was trusted; an unset camera clock boots to January 1.
- **Fix:** Jan 1 device dates are treated as reset defaults (as_of falls back to the captured date, labelled).
- **Test:** `test_recorded_date_used_when_plausible_and_reset_clock_ignored`.

### Speech lost at ASR window seams
- **Symptom:** none observed in output — found by the Fable final review: a sentence crossing a seam lost up to ~10 s, invisible to the completeness gate.
- **Cause:** windows had look-ahead overlap only; when both copies were edge-clipped the earlier one was dropped whole.
- **Fix:** symmetric overlap; a copy is only dropped when the other covers it; both-clipped copies keep their own side of the seam.
- **Test:** `test_seam_sentence_cut_by_both_edges_loses_nothing` (and three siblings).

### Whole-file audio conversion still reachable
- **Symptom:** none observed — review: `media` with an unreadable duration and `process` on a caption-less long video decoded the entire file to one wav (violates the "never whole-file" rule; the 4 GB WAV limit truncated a 12.6 h file silently once before).
- **Fix:** one windowed path (`longasr.transcribe_media`) for all local media and downloads; `to_wav` removed; unknown duration is measured with the null muxer or fails clearly.
- **Test:** `test_zero_probe_duration_is_measured_not_decoded_whole`, `test_process_without_captions_uses_windowed_asr`.

### Metered cap could be overshot at concurrency 3; reserved Anthropic credit unguarded
- **Cause:** cap check and record were separate steps per thread; `anthropic/*` routes were ordinary metered entries.
- **Fix:** atomic `reserve()`/`settle()` under the ledger lock; billed-but-failed calls keep their estimate; `anthropic/*` refused unless `ALLOW_ANTHROPIC_API=true`.
- **Test:** `test_cap_reservation_is_atomic_across_threads`, `test_anthropic_route_refused_before_ledger`.

### SD-card paths accepted by `media`
- **Fix:** `/Volumes/...` inputs refused with "make a verified safety copy" (originals are irreplaceable).
- **Test:** `test_media_cli_refuses_volumes`.

### Promo text in a description became transcript-wide casing rules; "today" dropped product claims
- **Cause:** any ALL-CAPS description word became a casing rule ("CODE", "LIKE"); the ephemeral filter dropped every row containing "today".
- **Fix:** acronyms from title/tags (description only for hyphenated/vowel-less forms like CCA-F); "today" dropped only with outage/usage words.
- **Test:** `test_promo_description_acronyms_do_not_become_casing_rules`, `test_today_product_claims_are_kept_and_session_glitches_dropped`.
