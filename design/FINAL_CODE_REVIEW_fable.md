# Final review — `longform-pipeline`

## 1. Correctness bugs

**(a) `core/longasr.py` ≈352–366 (`dedupe_overlap`, the `clipped_a == clipped_b` branch) — silent loss of speech at every window seam.**
Windows have look‑ahead only (`plan_windows`: `ext_start == core_start`), so the next window's first segment *always* starts at the cut and is always "clipped". When prev's last segment also runs to `ext_end` (a sentence crossing `core_end+8s`), the code takes the `elif clipped_a: drop_prev = True` branch, and prev's words *before* `core_end` — which only prev heard — are dropped. Scenario: window seam at 1500 s, parakeet sentence 1490–1508 s in prev, 1500–1512 s in next → the 10 s 1490–1500 vanish; the gate's gap check (<120 s) cannot see it and P8 would not sample it. The comment justifying the branch ("the stretch it alone covers is < its look‑behind") assumes a look‑behind that does not exist. Fix: extract next windows from `core_start - overlap_s` (symmetric overlap) and, when both copies are cut, keep both rather than drop one; or align `core_end` to the nearest detected silence (silencedetect is already run) so no sentence straddles a seam.

**(b) `main.py` ≈204–219 (`_longform_waiting`) — `--no-resume` is replayed after a wait, redoing paid/subscription work.**
The retry loop re‑calls `run_longform(run_dir, **kwargs)` with the original `resume=False`. Scenario: `longform … --no-resume`, exhaustion after 10/17 passages, 5 h wait, then all 10 are re‑summarised. Fix: `kwargs["resume"] = True` before the retry (a fresh run only needs to be fresh once).

**(c) `main.py` ≈413 (`is_long = … total_s > asr_long_threshold_s`) — whole‑file conversion of a long recording when ffmpeg reports no duration.**
`probe()` leaves `duration_seconds` at 0 when the `Duration:` regex misses (exactly the AKASO power‑cut / broken‑moov case); `total_s == 0` → `is_long False` → `to_wav()` decodes the entire 4 GB file to one wav and `transcribe_audio` runs on it. Violates hard constraint 2. Fix: treat unknown duration as long (`is_long = len(unit) > 1 or not total_s or total_s > threshold`), or route every local file through `longasr` (see simplifications).

**(d) `core/summarizer.py` ≈253–266 (`_complete_metered`) — paid usage is not recorded when the call "fails" after being billed.**
`_complete_response` raises `ValueError("empty summary")` after a successful HTTP call, and litellm can time out after the server has billed; in both cases no `ledger.record` happens and the chain moves on, so the cap under‑counts by one worst‑case estimate per occurrence. Fix: `try: … except Exception: ledger.record(..., usd=estimate, estimated=True); raise`.

**(e) `core/costs.py` ≈228–258 (`CostLedger.check`) with `run_longform(concurrency=3)` — cap check is not atomic with record, inside one process.**
Three chapter threads each pass `check` against the same spent total, then each records; a cap of $10 can be overshot by up to 2 × (prompt + 16k output) estimate. The docstring says "one run per ledger", but the eval itself ran at concurrency 3 and the failover reaches OpenAI on any ordinary claude‑cli failure (e.g. the 900 s CLI timeout). Fix: hold `_locked()` across check+call+record, or reserve the estimate in the ledger inside `check` and true it up in `record`.

**(f) `main.py` `process` (unchanged `transcribe_audio(wav)` around the `Step 2` block, before ≈1104) — a long YouTube video without captions still goes through whole‑file Whisper.**
The windowed path exists only in `media`. Scenario: next source is a 3 h talk with no caption track → full download → single wav → single ASR call, no checkpoints, no completeness gate. Violates constraint 2 and architecture item 8. Fix: in `process`, when `duration_s > asr_long_threshold_s`, hand the downloaded audio to `longasr.transcribe_long`.

## 2. Hard‑constraint gaps

**(g) `core/summarizer.py` ≈226–227 (`resolve_route` fall‑through) — nothing stops `anthropic/*` from spending the reserved credit.**
An `anthropic/…` entry in `LLM_MODELS` (the deprecated `anthropic_model` field still exists) is routed as ordinary metered with `_api_key_for(entry)`; only `metered_usd_cap` applies, which treats the reserved $10 as spendable. Fix: refuse `anthropic/` routes unless an explicit `ALLOW_ANTHROPIC_API=true`, raising before the ledger check.

**(h) `main.py` ≈368 (`media` path loop) — no refusal of `/Volumes/…` inputs.**
`bench_asr.py` refuses removable media; the real command does not. A `media /Volumes/AKASO/DCIM` invocation reads the SD card directly. Fix: copy the `/Volumes/` guard from `bench_asr.py` into `media`.

## 3. Fragile heuristics (concrete failing input)

**(i) `core/quality.py` ≈295–321 (`metadata_terms` + `_casing_rule`) — any all‑caps word ≥3 letters in the YouTube description becomes a transcript‑wide rewrite.**
Description "FULL COURSE – LIKE AND SUBSCRIBE, USE CODE SAVE20" yields casing rules for `full`, `course`, `like`, `subscribe`, `code`; every lowercase "code"/"like" in a 12 h transcript becomes "CODE"/"LIKE" in the corrected copy, hundreds of entries land in `corrections.json`, and the passage prompt lists "Names expected in this source: …, CODE, LIKE". Fix: take acronyms from title/tags only, and drop any candidate whose lowercase form the transcript itself uses as a common word (e.g. > N occurrences), or require a digit/hyphen/no‑vowel shape.

**(j) `core/notes_format.py` ≈69 (`EPHEMERAL_PATTERNS["today"]`) — any claim containing "today" not preceded by "as of " is deleted from the table.**
Model writes "Claude Code today supports MCP servers" or "The exam today has 60 questions" → row dropped (logged, but gone from the notes). Fix: require "today" to co‑occur with an outage/usage word (`not working|down|glitch|usage|quota`) or anchor the pattern to session verbs; keep product claims.

**(k) `core/spoken_dates.py` ≈262–290 (`estimate_recording`) — two mentions of the same historical date produce a false "recorded ~Month" banner.**
Course published 2026‑07‑20 that says "the new model was announced May 22nd" twice (a launch, not the recording date) → two lagging mentions in one cluster, majority of qualifying → frontmatter `recorded_estimate: 2026‑05` and a banner telling readers claims predate the publish date. Fix: require at least one *anchored* mention (drop the `or len(best) >= 2` path), or require the cluster to be the *latest* dated mention in the talk.

## Three simplifications worth making

1. **One local‑media path.** Route every local file (and the `process` download) through `longasr.transcribe_long`; a 3‑minute clip is simply one window. This deletes the `to_wav`/`--keep-wav` branch in `media`, fixes (c) and (f), and makes the "never a whole‑file wav" rule structural instead of conditional.
2. **Shrink the citation audit to "verify or mark".** `citations.py` carries ~12 tunables (WINDOW/LOCAL/OK_CONTENT/RETARGET_MIN/MARGIN/ORIGIN/RARE_DF/FUZZY_*) to retarget 2–3 citations per run, and already produced one false retarget. Keep the fuzzy presence check (strong terms within ±60 s → fine, else `_(unverified citation)_`), drop retargeting and region clustering; a marked line is honest and a prompt rule already tells the model to reuse original citations.
3. **Demote `spoken_dates.py` (425 lines) to a logged signal.** Record the mentions in `longform_state.json` and show nothing in the notes unless an anchored mention exists; the relative‑date arithmetic ("May of 26 … five months ago") is one meetup's phrasing and will misread the next ("back in March 2024, a couple of years ago").