# Handoff for the next Claude Code session (written 2026-10-21, context at ~78 %)

Start here, then `design/LONGFORM_RESUME.md` (current track) and `design/LONGFORM_EXECUTION_PLAN.md` (history + eval log).

## Repo state
- Repo: `~/Documents/agent_set_up/repos/base_human_learn_sys` (GitHub CurtisONeal/base_human_learn_sys). Work on `main`
  locally; **the user pushes** (auto-mode blocks agent pushes). `main` is ahead of origin by the commits since
  7f62577 — tell the user to `git push origin main` after merges.
- Tests: `.venv/bin/python -m pytest tests/ -q -m "not integration"` (≈812 pass). Real-ASR integration:
  `pytest tests/test_integration_mvp.py -m integration` (never bare `-m integration`: old e2e tests hit network/paid).
- Venv extras: `uv sync --extra ocr --extra dev --extra documents --extra semantic --extra asr-mlx` (never bare `uv sync`).

## How work was run (reuse this setup)
- Managing agent sequences work packages; implementer agents run in git worktrees (`isolation: worktree`), told to
  `git merge --ff-only main` first, commit on their branch, never push; manager reviews, merges into `main`, runs tests.
- Independent reviewer agents grade P8 (20 sampled claims) and regressions; evaluator `scripts/eval_longform.py <run_dir>`
  does P1–P7 (exits 2 on missing/empty run dir).
- Real model calls: subscription only — `LLM_MODELS='["claude-cli/opus","claude-cli/sonnet"]' ALLOW_METERED_FALLBACK=false`,
  `--usd-cap 0`; always `--out <dir>` and read `RUN_DIR=`; never glob guessed folder names.
- Usage limits: keep an hourly CronCreate resume check while agents run (it fires only when the session is idle);
  resume stopped agents with SendMessage.
- Discord notices: `~/.local/bin/hermes send --to discord:#general "<text>"` (verify exit 0) + copy into
  `<vault>/000_inbox_new_notes/agent_task_reports/`.

## Hard rules (user)
- Never touch `/Volumes/*` originals (SD card), `~/Movies/CLE-AI-26_09_safety_copies/`, or the iMovie library; make a
  checksum-verified safety copy before processing any recording; windowed audio only (never whole-file).
- Anthropic API credit (~$10, ≤ ~$5.33 used by one Fable review) is reserved for user-directed reviews; `anthropic/*`
  routes are refused unless `ALLOW_ANTHROPIC_API=true`.
- Ask the user before choosing test files; don't merge to main remote or push.

## Current track: The AI Daily Brief (playlist PLRYSuzHGhXPmKnOpd-f588cNNmTe2S9FP, 1,202 eps, newest first)
- Stage 1 done (merged): People section (`--prompt-variant current|legacy`), `process --limit/--items/--since/--until/--dry-run-list`, A/B report `previous_run_results/AB_aidb/AB_REPORT.md` (possible Special Attention / Time-Sensitive shrinkage — verifying).
- Stage 2 (agent ae845abb8bc1ae114, branch worktree-agent-ae845abb8bc1ae114): variance check (+ People prompt fix if needed) →
  `AB_aidb/VARIANCE_REPORT.md`; newest-3 packaged under subject `ai_daily_brief`; `playlist-index` → vault
  `010_learning_system/by_subject/ai_daily_brief/_index.md`; `digest --by week|month` → `…/ai_daily_brief/digests/`.
- Stage 3 (next): general batch-selection/run process for large playlists (select by date/topic/regex/duration → queue with
  resume, usage-window aware → package → digest); then TEST: last week of AIDB with People + a week digest.

## Open items for the user
- Push `main`. Replace old unlinked inbox copies (`000_inbox_new_notes/longform_notes_2026-10-16/`) with a pointer note? Re-summarize SC-900 (≈7 calls) to remove its duplicate Overview? Meetup YouTube links (re-point meetup citations once talks are posted; talk splits in `previous_run_results/E3e_cle_ai_meetup/talk_splits.md`). Tony/Ireland cassette file not found (ask name/drive).
- Parked: compare `raiyanyahya/how-to-train-your-gpt` vs Karpathy notes; learning-plan (Curriculum Maker v4) integration plan in HUMAN_PLANS.md.

## Update 2026-10-21 (context ~91 %)
- Stages 1–3 merged into local `main` (847 pass; 15 commits unpushed — user pushes). Collection `ai_daily_brief` exists (`previous_run_results/collections/ai_daily_brief/`), last-7-days run done, digests 2026-W42/W43 in the vault.
- DONE (merged): WP-P3 web lookup (875 pass). DONE: WP-P2 agent — `## Sources Referenced` section (current prompt variant, generalized across media/docs, merged, into digests), `annotate-people` agentic command (person index + minimal training-knowledge bios + vague-role resolution as of episode date + sources index as listen/read-next queue), P6 evaluator fix (ignore Transcription Uncertainty). Real test on FSX5JFJS9PQ + W43 digest + annotate on W42/W43. When done: review, merge into main locally, test, report; then pattern-harvest.
- Skills repo `~/Documents/agent_set_up/repos/claude-work-patterns` v0.1.2 (local; user pushes). Vault note: `010_learning_system/references/claude/Claude Code work patterns and skills.md`.
- Open: phone not showing new vault notes — user checking Mac Sync remote/status (possible side effect of disconnecting the decoy `reports` vault).
- DONE (merged, 889 pass): WP-P4 agent — quote-first X/handle lookup for the 27 unknown people (tiers confirmed/probable/unknown), "AI voices" ranked table in sources/_index.md (signal score), role-on-date fix (date question only; dated evidence ≤90 days or live official page; change check; re-run OpenAI CEO). ≤5 web calls. When done: merge into main locally, test, report, pattern-harvest. main is 25+ commits ahead of origin (user pushes).
