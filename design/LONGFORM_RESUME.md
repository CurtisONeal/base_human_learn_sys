# Resume notes — updated 2026-10-16 ~10:45 ET (usage-limit warning)

## Done
- All evals pass P1–P8 (E2d CCA-F, E3e meetup). See `LONGFORM_EXECUTION_PLAN.md` "Final status".
- Notes + talk splits + plan + Fable review copied to the vault:
  `~/Documents/obsidian_vault/syncing_vault2_clean_2026/000_inbox_new_notes/longform_notes_2026-10-16/`.
- Talk split timestamps: `previous_run_results/E3e_cle_ai_meetup/talk_splits.md`.
- FIXED_BUGS entries for late fixes committed (a32bd8a).
- ADR-045 appended to `agent_set_up/ADR_INSTALL_LOG.md` — **NOT committed** (agent_set_up has the
  user's own uncommitted edits: HUMANS.md, KEYBOARD_SETUP_TASKS.md deletion, bin/). Leave for the user.

## Status 15:30 ET
- WP-M1 task 0 (per-run cap) merged (fbdd3f4, 707 pass); `.env` has METERED_USD_CAP. Overtime Discord notice sent 15:2x.
- Tasks 1+3 NOT started: **waiting for the user to choose real test files** (asked in terminal). Do not generate fixtures or restart the agent until they answer.

## In flight (superseded — see Status)
- **WP-M1** agent (branch `worktree-agent-ace1b24c4d43ce4f4`): (0) per-run cost cap `RUN_USD_CAP`
  + `--usd-cap` flag on process/media/book/longform + API `usd_cap`; (1) caller audit + 
  `tests/test_integration_mvp.py` driving every entry point (book EPUB/PDF, process md/txt/html,
  YouTube with/without captions mocked, media clip + 2-part group, longform gate, API);
  (3) minimal fixtures `tests/fixtures/mvp/` (< 2 MB, synthetic `say` audio) + README +
  `scripts/smoke_all_paths.sh --dry-run`. If the branch has commits but no final report,
  check `git log longform-pipeline..worktree-agent-ace1b24c4d43ce4f4` and re-send the task.

## Next (user requests 2026-10-16 morning)
1. Review + merge WP-M1 into `longform-pipeline`; run full suite + `-m integration`.
2. README: confirm cumulative `METERED_USD_CAP` vs per-run `RUN_USD_CAP`/`--usd-cap` documented;
   add `METERED_USD_CAP=10.0` (comment: cumulative over the ledger) to the repo `.env`
   (append only; the file holds real keys — never print it).
3. Report caller-audit table to the user; then ask before merging `longform-pipeline` → `main`
   and before pushing (user wants to merge, then split talks and post to YouTube).
- Hard rules: never touch `/Volumes/*` or `~/Movies/CLE-AI-26_09_safety_copies/`.
- Deadline cron (Fri 13:37 ET) will send a Discord completion/overtime notice.
