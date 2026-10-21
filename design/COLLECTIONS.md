# Collections: batch processing for large playlists

Commands: `collection init | select | status | run | adopt`.
Code: `core/collection.py`, CLI in `main.py`. Tests: `tests/test_collection.py`.

## Question

How do we turn a 1,202-episode playlist into notes, packages and digests a slice at a
time, without:

- paying twice for work that is already done,
- guessing run folders,
- losing work to a usage limit or a crash,
- babysitting the run?

## The collection

A collection is a named set of sources with a subject and an output root. It lives in
`<root>/<name>/`. The default root is `OUTPUT_DIR/collections`.

| file | what |
|---|---|
| `collection.json` | The manifest: sources, subject, vault subject folder, index note, items, events, selections, digests, `pending_digests`, run history. |
| `listing.json` | A snapshot of the source listing: position, id, title, duration, publish date, description. |
| `runs/<YYYYMMDD>_<youtube id>/` | One run dir per item, named by publish date and id. It is never derived from the title. |
| `logs/<id>.log`, `logs/<id>.last_run.json` | The child command's output and its `_last_run.json`. |
| `_last_run.json` | The `RunSummary` of the last `collection run`. |
| `collection.lock` | The PID of a running `collection run`. A stale lock (dead PID) is taken over. |

The manifest is written atomically on every state change. Killing a run at any moment
leaves a consistent manifest.

### Sources

- **`--playlist <url>`.** `init` uses `playlist_index.build_index`: one flat listing,
  plus one watch-page read for each entry that is *not yet* in
  `_playlist_cache/<playlist_id>.json`. For The AI Daily Brief that cache is already
  complete, so `init` is one listing call (about 6 s).
- **`--urls-file`.** One yt-dlp metadata read per URL, kept in `listing.json`.

### Item states

```
selected ─► queued ─► transcribed ─► noted ─► packaged
                 └──────────┴───────────┴─► failed (after --max-attempts; error, attempts kept)
```

- **`selected`:** matched by `select --no-queue`; `run` does not touch it.
- **`transcribed`:** a transcript is on disk but there are no notes, because a call
  failed or ran out.
- **`noted`:** `summaries/*.summary.md` exists.
- **`packaged`:** a learning package exists in `<vault>/<LEARNING_ROOT>/<subject>/`.
- **`failed`:** the item failed `--max-attempts` times, or it produced no usable notes
  (empty source). An empty source is permanent and is never retried.

Each item records `attempts`, `error`, `calls` (the calls that produced its notes, read
from `longform_state.json`), `calls_spent`, `wall_s`, `asr`, `asr_seconds`, `media_s`,
`notes_words`, `eval` (P1–P7), `package` (citations linked, total, unresolved, broken)
and `timestamps` (`<state>_at`, `last_attempt_at`, `failed_at`).

## Selection: `collection select`

| flag | meaning |
|---|---|
| `--since`, `--until` | YYYY-MM-DD, inclusive |
| `--last-days N [--as-of D]` | since = D − N days, until = D. D defaults to today. |
| `--limit N` | the first N matches, in playlist order (newest first for AIDB) |
| `--items "1-3,7,10-"` | playlist positions (yt-dlp syntax) |
| `--match <regex>` | case-insensitive, on title **or** description (the cached 600 chars) |
| `--min-duration`, `--max-duration` | `20m`, `1h30m`, `1:05:00`, `25` (minutes) |
| `--order oldest\|newest` | queue order (default oldest) |
| `--dry-run` | print the matches and the estimate; change nothing |
| `--no-queue`, `--replace`, `--refresh` | record as selected only; drop never-started items that no longer match; re-list the source first (network) |

Selection reads `listing.json` only, so there is **no network** unless you pass
`--refresh`.

The printout shows every match with its state. `packaged*` means a package for that id
already exists and will be adopted, not rebuilt. The estimate counts only the items
that still need notes.

**Why the queue is oldest first.** A digest's "what changed" section reads the
previous span. Processing old to new means a run that stops early leaves complete
older weeks rather than a newest week with a hole behind it.

## Running: `collection run`

```
collection run <name> [--package] [--digest week|month] [--max-items N] [--max-calls N]
                      [--until-time HH:MM] [--max-attempts 3] [--backoff 60] [--retry-failed]
                      [--eval/--no-eval] [--usd-cap 0] [--on-exhaustion wait|stop] [--as-of D]
```

For each item, in queue order:

1. **Adopt.** With `--package`, if the subject folder already has a package whose
   `source_meta.json` has this YouTube id, the item becomes `packaged` with
   `reused: true`. Its run dir is taken from the package's `run_dir`. No call is made.
   Its metrics are read from that run dir, and its eval report too: an existing
   `eval/eval_report.json` is reused; otherwise P1–P7 are computed, deterministically.
2. **Notes**, only when the run dir has none:
   - **Limits are checked before the item starts.** The run stops if `--until-time` has
     passed, if `--max-items` items have been attempted, or if `calls used + estimate >
     --max-calls`. A run therefore stops *between* items, never inside one.
   - **The child command.** If the run dir has a transcript but no notes, the child is
     `longform <run_dir>`: no download, no ASR, and passage checkpoints resume.
     Otherwise it is `process <url> --out <run_dir>`. Either way the child gets
     `--on-exhaustion stop`, and `--usd-cap` / `--prompt-variant` are passed through.
     The outcome is read from the child's own `_last_run.json` (`LAST_RUN_FILE`). Its
     new `extra.stop_reason` field says `subscription_exhausted` (with `reset_at`) or
     `budget`. The run never parses free text and never guesses a folder.
   - **Failure.** `attempts += 1`. Below `--max-attempts`, the item goes to the back of
     this run's queue with `retry_after = now + backoff × 2^(attempts−1)`. The run sleeps
     only when it reaches that item again, and only if the wait does not cross
     `--until-time`. At `--max-attempts` the item becomes `failed`. Later runs skip it
     unless `--retry-failed` is given.
   - **Subscription exhausted.** This is not counted as an attempt. An `exhausted` event
     is recorded with `reset_at` and `policy`. `ON_EXHAUSTION=wait` (the default; `ask`
     counts as `wait` because nobody is at a TTY) sleeps until `reset_at` + 60 s, or
     `EXHAUSTION_WAIT_SECONDS` when no reset time is known, capped by
     `EXHAUSTION_MAX_WAIT_SECONDS`. The wait is recorded in the event (`waited_s`), and
     the same item is retried. If the reset falls after `--until-time`, or the policy is
     anything else, the run stops cleanly. The collection never escalates to metered or
     local backends on its own.
   - **Spend cap or Ctrl-C.** The run stops cleanly. The item keeps its progress: it
     goes back to `queued`, or to `transcribed` if its transcript exists.
3. **Package** (`--package`). The pipeline's own `package` is called in-process. It is
   deterministic. A packaging error leaves the item `noted`, and the next run packages
   it without a call.
4. **Report.** The run prints `COLLECTION_ITEM_DONE=<id> <dir>` (the package dir, or
   the run dir).

After the queue, **digests** (`--digest week|month`) are rebuilt for the affected spans
only:

- the spans of items that finished *in this run*;
- spans that have done items but no digest file yet;
- `pending_digests` left over by an earlier run;
- **cascade:** when a span's digest is rewritten, the next span is rebuilt too. Its
  prompt carries the previous span's headlines for "what changed", so its cache key
  changes anyway.

Digests use `digest.build_span` over the subject folder (with `--package`) or over the
collection's `runs/`. They are labelled partial by the usual digest rules: the span is
not over, or `_index.md` lists more episodes in the span than have notes. Each digest
is recorded in `digests["week:2026-W43"]` (citations kept and dropped, link_check,
partial) and printed as `COLLECTION_DIGEST=<span> <path>`.

`--max-calls` covers digests too. A span that does not fit is left in
`pending_digests`, and the next run builds it even when no item is new. If the run was
stopped by exhaustion, the spend cap or an interrupt, digests are not attempted. Their
spans go to `pending_digests` instead.

Last come the playlist-index note (`_index.md`), which is re-rendered from
`listing.json` and the cache with no network so that the new `packaged` links appear,
then `<collection>/_last_run.json`, the global `_last_run.json`, and the `RUN_DIR=`
lines. When the run stops early it also prints `COLLECTION_STOPPED=<reason>`. The exit
code is 1 if any item failed or any digest's link check failed, 3 if the collection is
locked by a live run, and 0 otherwise.

### Idempotence and the resume loop

`collection run` is safe to re-run blindly. A cron or resume loop can call
`collection run ai_daily_brief --package --digest week --until-time 07:00`:

- finished items are skipped, and adopted items are not rebuilt;
- a half-done item resumes from its transcript and passage checkpoints;
- digest calls are cached by prompt hash, so an unchanged span costs 0 calls and its
  file is left byte-identical;
- with nothing new, the run makes no call and writes no digest.

## Status: `collection status`

`status` shows:

- counts per state;
- the remaining items, with estimated calls and minutes;
- the next N items;
- failures with their errors;
- pending digests and built digests;
- the last 5 events and the last run's summary.

The estimates work like this:

- **Calls per item:** ⌈duration × 160 wpm / 12,000 words⌉. When that comes to more
  than one passage call, 2 more are added (course map and consistency). AIDB episodes
  (20–36 min) come to 1 call each.
- **Minutes:** once this collection has finished items of its own, the median observed
  wall time per media-minute. Before that, a fixed model: 1 min overhead + 0.05 × media
  minutes for ASR + 2.5 min per call.
- **What costs nothing:** items that already have notes or a package.

## Tests (mocked pipeline: `tests/test_collection.py`, 16 tests)

- Selection from the cached index makes no network call: the meta and listing
  functions raise if called. Covered: `--last-days`, regex on description, duration
  bounds, `--limit`, `--items`, oldest-first queue, re-selecting adds nothing.
- Resume after an interruption: Ctrl-C on item 2 → item 1 is noted, item 2 is still
  queued with 0 attempts, and the lock is released. A re-run runs only items 2 and 3.
  A third run is a no-op.
- `--max-calls` and `--max-items` stop between items. `--until-time` in the past starts
  nothing.
- A failure is retried with exponential backoff and later succeeds. A permanent
  failure ends as `failed` after 3 attempts and is skipped next run unless
  `--retry-failed`.
- Exhaustion with `wait` sleeps, records `waited_s` and retries the same item without
  counting an attempt. With `stop`, the run stops cleanly.
- An existing package is adopted (the pipeline is not called, metrics come from its
  run dir), and a noted item is only packaged.
- A live lock refuses a concurrent run.
- Digests are rebuilt only for affected spans: W42, then W43, then nothing. The real
  `digest.build_span` with a fake model shows the cascade (W42 rewritten → W43
  rebuilt) and a cached rerun with 0 calls. A `--max-calls` shortfall leaves
  `pending_digests`, and the next run builds them.
- `SubprocessRunner` resumes a run dir that has a transcript with `longform`.
- CLI `init` / `select` / `status` work with the listing mocked, and `select` touches
  no network.

## Real run (2026-10-21): "the last week of AIDB, with People and a week summary"

```
collection init ai_daily_brief --playlist https://www.youtube.com/playlist?list=PLRYSuzHGhXPmKnOpd-f588cNNmTe2S9FP --subject ai_daily_brief
collection select ai_daily_brief --last-days 7 --as-of 2026-10-21 --dry-run   # list first
collection select ai_daily_brief --last-days 7 --as-of 2026-10-21
collection run ai_daily_brief --package --digest week --usd-cap 0 --as-of 2026-10-21
```

Setup: subscription chain only (`claude-cli/opus` → `claude-cli/sonnet`,
`ALLOW_METERED_FALLBACK=false`, `--usd-cap 0`). Every call was served by
`claude-cli/opus`.

- **init:** one flat listing. All 1,202 entries were already in the cache, so no
  watch-page read was needed.
- **select:** 7 episodes (2026-10-14 … 2026-10-20). 3 of them were already packaged in
  Stage 2, which left an estimate of 4 calls for the other 4.

### Episodes

The 4 new episodes went through ASR → 1 opus passage call each (`wdXF-5nn-T4`, at
36 min, took 2) → package. That is 5 calls, about 13 minutes of wall time, and P1–P7
on each. The ASR is parakeet-mlx, about 45 s per 27 min.

`wdXF-5nn-T4` failed once with a YouTube HTTP 403 on the download. It was retried at
the end of the queue (backoff 60 s) and passed on attempt 2.

| date | id | state | calls | wall | media | P1–P7 | links |
|---|---|---|---:|---:|---:|---|---|
| 10-14 | SX9LQD3yINg | new | 1 | 155 s | 27 min | P6 ✗, rest ✓ | 193/193, 0 broken |
| 10-15 | wdXF-5nn-T4 | new (403, retried) | 2 | 216 s | 36 min | all ✓ | 394/394, 0 broken |
| 10-16 | xIFMwhvvUUo | new | 1 | 260 s | 27 min | all ✓ | 502/502, 0 broken |
| 10-16 | pmuGWJte_EY | new | 1 | 165 s | 25 min | all ✓ | 240/240, 0 broken |
| 10-19 | d6SO10ihpL4 | reused (Stage 2 r1) | 0 | – | 20 min | all ✓ | 181/181 |
| 10-20 | bw8nHS0_cl0 | reused (Stage 2 r2) | 0 | – | 29 min | all ✓ | 240/240 |
| 10-20 | FSX5JFJS9PQ | reused (Stage 2 r1) | 0 | – | 28 min | all ✓ | 248/248 |

The one P6 fail on SX9LQD3yINg is "Cloud Code → Claude Code". It hits the notes'
own Transcription Uncertainty row, which records that mishearing on purpose. This is
an evaluator false positive (inferred, not fixed here).

### Digests

| span | episodes | calls | citations | dropped | link_check | partial |
|---|---:|---:|---:|---:|---|---|
| 2026-W42 | 4 | 1 | 165 | 0 | pass | yes: 4 of 6 index episodes; 10-12 and 10-13 are outside the 7 days |
| 2026-W43 | 3 | 1 | 131 | 0 | pass | yes: the week is not over |

W43 had no new episode. It was rebuilt by the cascade, because W42 now exists, and it
gained a "What changed vs W42" section (for example, the OpenAI bugging case
escalated).

### Rerun

The rerun made 0 calls and printed "digests: no affected span". Both digest files are
byte-identical (same md5).

The vault `_index.md` now shows 7 packaged.
