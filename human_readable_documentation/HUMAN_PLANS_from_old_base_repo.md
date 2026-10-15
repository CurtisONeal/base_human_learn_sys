# HUMAN_PLANS.md

Scratchpad for edits, ideas, and open questions. Park things here instead of
starting speculative work or losing them in chat history.

**How to use this:** add to *Inbox* freely. Things graduate to *Next up* once
decided. Move to *Done* with a date so the reasoning stays findable.

---

## Read this first — what this repo actually is

A fork of `CurtisONeal/video2notes-v2`, taken 2026-07-30. It downloads a video's
audio, transcribes it locally with faster-whisper, and summarizes the transcript
with an LLM into structured markdown notes.

**Name vs. reality.** It is called a "learning system" but today it is a
*transcription and summarization pipeline*. Nothing in it does spaced repetition,
retention checking, or study-material generation. Current scope is to extend the
pipeline, not to build a learning system on top — if that changes, it's a
different project shape and worth saying so explicitly.

**Relationship to `video2notes-v2`.** Separate GitHub remotes, no shared history
tracking. **Fixes made in one must be ported by hand.** This has already bitten
twice — see below.

**They are NOT at parity. This file previously claimed they were; that was
wrong** (corrected 2026-08-15). The earlier "verified 2026-08-14" check compared
`summarize_prompt.txt` only and generalized from it. Actually diffing the trees
shows this fork is *behind* upstream on `config.py`, `summarizer.py`, and
`tests/test_summarizer.py`.

The gap is one upstream commit, `d28e734` (2026-06-30, LLM provider/model
selection) — landed the *same day* as the int8 fix `912a71c`. The fork was cut
from a June 12 clone, so it missed both. ADR-034 found and ported the int8 one
and closed out, never noticing its sibling. Same bug class as ADR-034, one layer
down: an assumed currency nobody diffed.

Since then upstream has moved much further — it now has the summarization
backend chain (ADR-037) and captions-first (ADR-038), both built there by
decision. Porting to this fork means bringing `d28e734` along too.

Before starting work here, actually diff the trees:
`diff -rq video2notes-v2/src base_human_learn_sys/src`

## Current state — verified 2026-08-14

- Clean working tree, in sync with `origin/main` at `4a4c7ba`
- 20 tests collected and passing
- `.env` present (22 settings) — needed, or summarization fails with no key
- Two real runs archived in `previous_run_results/`: an Aikido clip and a
  24-minute AI Daily Brief podcast episode
- The full CLI path works end to end:
  `.venv/bin/python3 -m video2mdnotes.main <url>`

## Gotchas that have already cost time

- **`fw_compute` must be `int8` on macOS.** ctranslate2 has no CUDA backend on
  macOS, so it runs on CPU for both Intel and Apple Silicon, and CPU float16 is
  unsupported. The old auto-detect branched on `arm` and picked float16, which is
  backwards. Fixed at the config default; do not reintroduce the architecture
  check.
- **Long runs get auto-backgrounded.** A 24-minute video takes ~11 minutes to
  process (transcription dominates). Any agent-run Bash tool will background a
  command past ~120s, and a one-shot `claude -p` session has no later turn for a
  completion notification to arrive in. If a command backgrounds, **poll its
  output file** for `main.py`'s own markers (`Summary Generated.` /
  `Processing failed:`) rather than waiting for a notification that cannot come.
- **Empty transcripts used to hallucinate.** A silent or music-only video yields
  zero segments, and the LLM would invent a confident, entirely fabricated
  summary — including a UI element that did not exist. `generate_summary()` now
  short-circuits to a placeholder before any LLM call. Do not remove that guard.
- **`initial_prompt` matters.** Without a vocabulary hint, Whisper transcribes
  "Claude" as "Cloud" throughout. `build_initial_prompt()` derives one from
  yt-dlp metadata (tags, else a stopword-filtered pass over the description).
- **The summarizer has no web search.** Its "fact-checked notes" section is
  working from training data with a cutoff, which is why the prompt instructs it
  to hedge rather than assert. Don't read that section as verified.

## Inbox — unsorted, undecided

- ~~**Batch / playlist processing.**~~ **Verified 2026-08-15** (upstream, see
  ADR-038 addendum 2): full CLI run over a real 10-video playlist, 10/10
  archived, no collisions, no leaked state between iterations. Still untested:
  a playlist that *mixes* caption and Whisper videos in one run.
- **Resumability.** A playlist run that dies partway has no way to skip what it
  already did. `previous_run_results/<date>_<title>/` could act as the ledger.
- **Non-YouTube sources.** yt-dlp handles many sites generically, but the only
  ones actually tested are YouTube. Skilljar (Anthropic's course platform) was
  tried and is blocked behind a login wall — that was correctly not circumvented
  and should stay that way.
- **Captions-first shortcut.** When a source already has a human-authored
  transcript, downloading audio and running Whisper for ~10 minutes is wasted
  work. Pulling existing captions was proven to work via yt-dlp in an earlier
  investigation but was **never built into the pipeline** — it only ever existed
  as ad-hoc commands. This is probably the single highest-value pipeline change.
- **Whisper model size.** Currently `medium`. Larger would improve accuracy at
  real speed cost on CPU. Worth measuring before assuming it's an upgrade,
  especially now that `initial_prompt` has fixed the main accuracy complaint.
- **Speaker diarization** for multi-speaker content (podcasts, interviews).
- **Output beyond markdown files** — the notes currently land on disk and stay
  there. No index, no search, no way to find "that thing about agents."

## Next up — decided, not built

- *(nothing yet — this file was created at the start of the pipeline-extension
  work; fill in as decisions get made)*

## Done

- 2026-07-30 — Forked from `video2notes-v2`; dependencies verified; test suite run.
- 2026-08-01 — `initial_prompt` derived from video metadata, fixing
  Claude→Cloud. `beam_size` raised 1→5.
- 2026-08-08 — Ported the upstream int8 fix that this fork's stale base had
  missed. Empty-transcript hallucination guard added. Summarize prompt
  redesigned: domain-adaptive (technical **and** Aikido), anti-fabrication
  grounding, a sponsor/ad-content section, fact-check confidence calibration,
  and removal of a redundant Transcript instruction that was duplicating the
  section `summarizer.py` already appends. Validated against real Aikido and
  real podcast content.

---

## Open questions

- **Is the fork worth maintaining separately from `video2notes-v2`?** They are
  at parity and every fix has had to be applied twice. If this repo is going to
  diverge meaningfully, that justifies it; if it isn't, the duplication is pure
  cost and merging back would be cheaper.
- **What is the actual goal of the notes?** Reference material to search later,
  or study material to learn from? The prompt currently optimizes for the former
  (structured recall notes). The latter would mean a different prompt and
  probably a different output format.
- **Does the domain-adaptive prompt hold up beyond two domains?** It was
  validated on technical content and Aikido specifically. A third domain may
  need the Role section broadened again, or a different approach entirely.
