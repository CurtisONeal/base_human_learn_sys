# Resume notes (written 2026-10-15 ~23:30 ET before a usage-limit warning)

If the managing session stopped at a usage limit, resume from here.

## In flight at the time of writing
- **WP-6a** (ASR backend interface + benchmark) — agent was running real model benchmarks.
  Branch `worktree-agent-aee6e32206e410827`. If it has a commit with `design/ASR_BENCHMARK.md`,
  review + merge. If not, relaunch WP-6a with the spec in the plan (Architecture 8; WP-6a row):
  `core/asr/` backends (faster-whisper, parakeet-mlx, mlx-whisper; lazy import; extra `asr-mlx`;
  sync with ALL extras), `scripts/bench_asr.py`, 10-min windows at 0:47:36 and 4:15:46 from the
  archived 7.5 GB wav (valid to 6.76 h) vs the CCA-F captions; WER + glossary hits; never read /Volumes.
- **WP-3 (+WP-7)** — the integration package. Branch `worktree-agent-ad40ce487598e2d0d`.
  If incomplete, relaunch with: wire metadata/structure/quality/notes_format/costs into a new
  `core/longform.py`; length gate in `process` and `media`; CLI `longform <run_dir>` with
  `--only`, `--plan-only`, `--resume`, `--concurrency`, `--out`; context carry = prompt header +
  course skeleton + (for part i>1) ledger from part i-1 + ~150 s overlap tail labeled CONTEXT ONLY
  + timestamped lines + mishearing hint + uncertainty block + time-sensitive fragment; fenced
  ```ledger JSON (≤800 words) parsed/stripped; checkpoint `chunk_summaries/NN_slug_pI.md`,
  `ledger/NN_pI.json`, `longform_state.json`; assembly = frontmatter + Course Map (one batched
  reduce returning only a table + themes, deterministic fallback) + per-chapter sections
  `## <title> [H:MM:SS–H:MM:SS]` (parts as `### Part i/n`) + merged Uncertainty + merged claims
  (merge → validate(fill=True) → stamp_notes) + Fact-Checked; lint_notes → deterministic fix →
  zero residual; no transcript appended. Tests on rendered prompts, resume, mocked end-to-end
  eval on CCA-F (P1–P7 pass), plan-only = 17 passages. One real smoke call allowed on
  `--only "MCP Ecosystem"` via claude-cli only.

## Next after those
1. WP-6b (windowed long ASR from `~/Movies/CLE-AI-26_09_safety_copies/`, per-window checkpoint,
   completeness gate, 4-part stitch; replace whole-file `to_wav()` in `media`).
2. E1 pilot: `longform <CCA-F run dir> --only` Intro, SDK Setup, Sessions mgmt, Claude Code config
   → eval P1–P7 + P8 reviewer agent (independent). Log in plan.
3. E2 full CCA-F → E3 meetup.

## Scheduled prompts (session-only)
Hourly resume check at :17; one-shot deadline check Fri 13:37 ET (Discord via `hermes send`, verified working).
