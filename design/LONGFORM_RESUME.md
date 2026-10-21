# Resume notes — updated 2026-10-17 evening (usage-limit warning)

## State
- `longform-pipeline` has everything (P3 silence fix af4d110, design b3814fc). **Merge to main + push
  NOT done** — auto-mode blocked it; the user runs it (reminder sent 20:07 via Discord):
  `git switch main && git merge --ff-only longform-pipeline && git push origin main longform-pipeline`.
  Do not attempt the push or inspect main's merge state yourself (blocked as out-of-place publication).
- SC-900 + CCA-F guide notes done (previous_run_results/books/), copies in vault
  `000_inbox_new_notes/longform_notes_2026-10-16/`.
- Tony/Ireland cassette file: NOT found on /Volumes/Expansion (only 2 2012 WAVs + 5 Timkoo AACs). Waiting
  for the user to give a name fragment / drive. Never touch /Volumes originals without a safety copy.

## In flight (WP-L1 merged 2026-10-17; privacy fix d4a5c2b)
- **WP-L1 learning packages** agent (branch `worktree-agent-a53afb0033a44fba2`): `package` command +
  `--package <subject>`; extracted text/ anchors, PDF page links into `<vault>/__sources/`, images/,
  YouTube ?t= links, transcript block links; packages CCA-F guide, SC-900 (azure_identity), Azure Pt 2,
  E3e meetup into `010_learning_system/by_subject/<subject>/<slug>/`. Spec: `design/LEARNING_PACKAGES.md`.
  If it stopped mid-task: check `git log longform-pipeline..worktree-agent-a53afb0033a44fba2`, then resume
  it with SendMessage. When done: review, merge into longform-pipeline, run tests, report package stats
  and spot-checked links to the user.

## Pending decisions for the user
- Document-path fix (short docs as one passage + unclosed code-fence check) — asked, no answer yet.
- User action: Obsidian Sync → exclude `__sources`.

## 2026-10-20 · AI Daily Brief track (user plan, staged)
- Stage 1 (running, agent branch from main): People section in summary prompts (+ `--prompt-variant legacy|current`), `process --limit/--items/--since/--until/--dry-run-list`, real A/B on newest 2 AIDB episodes → `previous_run_results/AB_aidb/AB_REPORT.md`.
- Stage 2 (after Stage 1 passes): real run newest 3; free index of all 1,202 episodes → vault note; span summarizer (weekly/monthly digest over per-episode notes) — design + implement.
- Stage 3: general batch-selection/run process for large playlists; then test: last week of AIDB with People + a week digest.
- Playlist: https://www.youtube.com/playlist?list=PLRYSuzHGhXPmKnOpd-f588cNNmTe2S9FP — 1,202 entries, 272 h, median 11 min, newest first.
- main is 4 commits ahead of origin (user pushes).
