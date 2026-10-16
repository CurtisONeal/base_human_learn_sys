# ASR backend benchmark (WP-6a)

**Run:** 2026-10-15, Apple M4 (10 cores), 34 GB, macOS 26.5.
**Harness:** `scripts/bench_asr.py`. Raw results: `design/asr_benchmark/bench_04736.json`, `bench_41546.json`.
**Source:** CCA-F course audio (archived 44.1 kHz stereo wav, valid for ~6.76 h). Two 600 s windows were
extracted with `ffmpeg -ss … -t 600 … -ac 1 -ar 16000` into a temp dir and deleted afterwards. The long file
was never converted whole.

| Window | Start | Content |
|---|---|---|
| W1 | 0:47:36 | Agentic loop / "what is Claude Code" lecture: dense product names |
| W2 | 4:15:46 | Hands-on demo: agent SDK, sub-agents, on-screen prompts read aloud |

**Engines:**

| Backend | Model | Runtime | Prompt | Notes |
|---|---|---|---|---|
| faster-whisper | `large-v3`, `distil-large-v3` | CTranslate2, CPU int8 | yes | the production path (beam 5, VAD on) |
| parakeet-mlx 0.5.3 | `mlx-community/parakeet-tdt-0.6b-v3` | MLX / Metal GPU | **no** | 120 s chunks, 15 s overlap |
| mlx-whisper 0.4.3 | `whisper-large-v3-turbo`, `whisper-large-v3-mlx` | MLX / Metal GPU | yes | no VAD |

Whisper backends got the production vocabulary hint (`build_initial_prompt` from yt-dlp title and description):
*"Claude Certified Architect - Foundations – Prepare for and pass the exam!. Keywords: Claude, freecodecamp,
Architect, Foundations, exam, Learn, Patterns, code, Certified, CCA-F, Anthropic, official, …"*. Parakeet has
no prompt input, so it got no hint.

## Results

Columns: **Decode s** is wall time for `transcribe()` with the model already loaded. **RTF** = decode s / audio s.
**WER vs captions** compares against YouTube auto-captions whose start time falls in the window. **WER, no
fillers** is the same comparison with uh/um/hmm/ah/er removed from both sides. **Glossary** columns count terms
in each output. **Rep. 5-grams** counts surplus repeats of word 5-grams, which is how Whisper looping shows up.
**ts Δ** is the median and p90 gap in seconds between each engine's word times and the captions' word times,
over runs of 4 or more words that agree.

Some columns were added after the run, once the transcripts had been read: clod/claud, STK, Rep. 5-grams, "WER,
segs ≤ window" and "WER, no fillers". They were computed from the saved per-engine transcripts with the
committed `core/asr/metrics.py`. The committed JSON predates them, so its `glossary` lacks the two later
mishearing keys. `bench_asr.py` now emits `repeated_5grams` and `wer_in_window` directly.

### W1: 0:47:36 + 600 s

| Backend | Decode s | × real-time | RTF | Words | WER vs captions | WER, segs ≤ window | WER, no fillers | Claude | Claude Code | MCP | SDK | Anthropic | cloud code | clod/claud | clawed | STK | Rep. 5-grams | Segs | Segs past end | ts Δ median / p90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| *YouTube auto-captions (reference)* | | | | 1862 | — | — | — | 6 | 3 | 1 | 2 | 0 | 10 | 0 | 2 | 0 | | 254 lines | | |
| faster-whisper large-v3 (CPU int8) | 551.7 | 1.1 | 0.920 | 1812 | 6.7% | 6.7% | 4.0% | **13** | **10** | 1 | 2 | 0 | 5 | 0 | 0 | 0 | 3 | 71 | 0 | 0.39 / 2.22 |
| faster-whisper distil-large-v3 (CPU int8) | 106.3 | 5.6 | 0.177 | 1813 | 7.1% | 7.1% | 4.5% | 8 | 6 | 1 | 2 | 0 | 5 | 1 | 0 | 0 | 11 | 103 | 0 | 0.28 / 0.79 |
| **parakeet-tdt-0.6b-v3 (MLX)** | **15.0** | **40.0** | **0.025** | 1856 | **3.4%** | 3.4% | **3.3%** | 8 | 7 | 1 | 2 | 0 | 6 | 4 | 1 | 0 | 10 | 114 | 0 | **0.24 / 0.69** |
| mlx-whisper large-v3-turbo | 68.8 | 8.7 | 0.115 | 2005 | 19.6% | 18.7% | 16.7% | 10 | 7 | 1 | 2 | 0 | 8 | 3 | 0 | 0 | **190** | 128 | **73** | 0.64 / 4.13 |
| mlx-whisper large-v3 | 109.9 | 5.5 | 0.183 | 1858 | 10.1% | 10.1% | 7.8% | 11 | 10 | 1 | 2 | 0 | 5 | 0 | 0 | 0 | 19 | 168 | 0 | 0.38 / 1.55 |

### W2: 4:15:46 + 600 s

| Backend | Decode s | × real-time | RTF | Words | WER vs captions | WER, segs ≤ window | WER, no fillers | Claude | Claude Code | MCP | SDK | Anthropic | cloud code | clod/claud | clawed | STK | Rep. 5-grams | Segs | Segs past end | ts Δ median / p90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| *YouTube auto-captions (reference)* | | | | 1772 | — | — | — | 3 | 0 | 0 | 4 | 0 | 3 | 0 | 0 | 0 | | 248 lines | | |
| faster-whisper large-v3 (CPU int8) | 341.8 | 1.8 | 0.570 | 1681 | 7.8% | 7.8% | **4.7%** | 6 | 2 | 0 | 4 | 0 | 1 | 0 | 0 | 0 | 19 | 19 | 0 | 1.05 / 3.74 |
| faster-whisper distil-large-v3 (CPU int8) | 102.5 | 5.9 | 0.171 | 1719 | 8.7% | 8.7% | 5.7% | 3 | 0 | 0 | 4 | 0 | 1 | 0 | 0 | 0 | 34 | 156 | 0 | 0.33 / 1.58 |
| **parakeet-tdt-0.6b-v3 (MLX)** | **15.0** | **40.0** | **0.025** | 1743 | **7.6%** | 7.6% | 7.2% | 6 | 2 | 0 | 1 | 0 | 0 | 1 | 0 | **3** | 15 | 140 | 0 | **0.25 / 0.89** |
| mlx-whisper large-v3-turbo | 48.3 | 12.4 | 0.081 | 1733 | 11.4% | 11.4% | 8.5% | 6 | 2 | 0 | 4 | 0 | 1 | 0 | 0 | 0 | 75 | 43 | 1 | 1.11 / 4.74 |
| mlx-whisper large-v3 | 123.9 | 4.8 | 0.207 | 1652 | 17.7% | 17.7% | 15.2% | 6 | 2 | 0 | 4 | 0 | 1 | 0 | 0 | 0 | 38 | 165 | 0 | 0.38 / 1.19 |

Domain-term mishearings per engine. This sums "cloud" before code/desktop/sonnet/opus, clod/claud, clawed, and
STK. Lower is better:

| | captions | fw large-v3 | fw distil | parakeet | mlx turbo | mlx large-v3 |
|---|---:|---:|---:|---:|---:|---:|
| W1 | 12 | **6** | 6 | 12 | 11 | 7 |
| W2 | 3 | **1** | 1 | 4 | 1 | 1 |

## What was measured and what is inferred

**Measured** (numbers in the tables, from one run per engine per window):
- Decode time, word counts, WER against the captions, glossary counts, repeated 5-grams, segment counts, and
  timestamp agreement with the captions.
- **Load time** is reported separately in the JSON and kept out of decode time. First-run downloads landed in
  load time: parakeet 229 s, mlx turbo 155 s, mlx large-v3 318 s. Cached loads took 0.6 to 2.4 s.
- **mlx-whisper large-v3-turbo looped.** In W1 it repeated "so in the end I'm going …" 13 times, giving 190
  surplus 5-grams. It also kept decoding into the padding past the end of the audio: 73 segments of "C" or ""
  between 600 and 613 s. It also emitted segments past 3 s on 3 s of synthetic silence (integration test).
- **mlx-whisper large-v3 hallucinated in W2.** From 417 to 444 s it emitted multilingual nonsense
  (Korean/Japanese/Cyrillic: "느낌이 Madison Nova shoveボ美味", "Надо"). It also emitted one segment whose end
  came before its start (443.84 → 418.30). The captions have 92 words in that span, which matches its 183
  deletions.
- **Parakeet's distinct errors:** "Cloud Code" for Claude Code (W1, 6 times), "clod" or "Claud" (W1, 4), and
  "agent STK" for SDK (W2, 3). It keeps fillers ("uh", "um"). Whisper drops them.
- **Every engine** writes "Cloud Code" for Claude Code at least sometimes (5 to 8 times in W1). So do the
  captions (10 times).

**Inferred, not proven:**
- **WER here measures disagreement with YouTube's own ASR, not error against the truth.** The reference is
  machine output: it says "cloud code" 10 times in W1. A low WER partly means "transcribes like YouTube".
  Parakeet's raw-WER lead is partly about style. Like YouTube, it keeps fillers. Removing fillers moves Whisper
  large-v3 from 6.7% to 4.0% (W1) and 7.8% to 4.7% (W2), while parakeet barely changes (3.4% → 3.3%,
  7.6% → 7.2%). **On content words, faster-whisper large-v3 and parakeet are roughly tied.** Parakeet is ahead
  in W1 and behind in W2. Two 10-minute windows from one speaker can't separate them further.
- **The glossary lead of Whisper large-v3 is most likely the vocabulary prompt** ("Claude", "Anthropic"
  appear in it). Parakeet cannot take one. Engines were not run with the prompt off, so this is untested.
- **CPU timings are noisy.** Other agents were running in sibling worktrees (load average 4.4 at start). The
  large-v3 decode took 552 s in W1 and 342 s in W2 for the same length of audio. MLX numbers were identical
  across windows (15.0 s both times), which suggests the GPU path was not contended.
- **Timestamp agreement is approximate on both sides.** Caption lines and engine segments have no per-word
  times, so word times are spread evenly within each line or segment. Parakeet has the tightest agreement
  (p90 0.69 s and 0.89 s). It derives sentence times from token times. faster-whisper large-v3 made 30-second
  segments in W2 (median segment 29.98 s), which coarsens its timestamps (p90 3.74 s).
- **Full-course projection:** 12.6 h of audio at the measured RTFs comes to about 19 min (parakeet),
  2.2 h (distil), and 7 to 11.6 h (large-v3, CPU). This is extrapolated from 20 minutes of measurement.

## Recommendation

**Use parakeet-tdt-0.6b-v3 (MLX) for English long-form ASR. Follow it with the WP-4 glossary-correction pass,
and keep faster-whisper as the fallback.** On this machine it decodes at 40× real time, against 1.1 to 1.8×
for the production faster-whisper large-v3. The 12.6 h course takes about 19 minutes instead of an overnight
run. On content words its accuracy is roughly tied with large-v3 (filler-free WER 3.3%/7.2% vs 4.0%/4.7%). It
had the best timestamp agreement in both windows, and it showed no looping or hallucination. Its weakness is
domain vocabulary without a prompt: about twice large-v3's mishearings in W1 (12 vs 6). Those errors are a
small, repetitive, phonetic set: Cloud Code→Claude Code, clod/Claud→Claude, STK→SDK. That is exactly what the
WP-4 deterministic lint can repair and log without touching the raw transcript. Large-v3's advantage there is
the prompt, and it costs roughly 23–37× the decode time.

**Constraints to keep:**
1. Parakeet v3 covers 25 European languages and **no Japanese**. The `asr_select` rule that sends non-English
   context to multilingual large-v3 still applies, and parakeet must not override it.
2. Use **faster-whisper distil-large-v3** as the CPU fallback (5.7× real time, filler-free WER 4.5%/5.7%) where
   MLX is unavailable.
3. **Do not use mlx-whisper for unattended long-form runs** in either model. Turbo looped and decoded into the
   padding. Large-v3 hallucinated 27 s of multilingual text over real speech. Neither has a VAD filter. The
   backends now drop segments that start after the audio ends and repair end-before-start segments
   (`clip_to_duration`). That fixes the stitching hazard for WP-6b, not the in-window hallucination.

Not benchmarked: Apple SpeechAnalyzer (marked "if cheap" in the plan; there is no Python binding in the venv)
and repeated runs for variance.

Provenance: Anthropic Claude Code / claude-opus-5-5 / wp-6a-asr-backends
