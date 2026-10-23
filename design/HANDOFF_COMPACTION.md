# Compaction handoff — nuances to keep (2026-10-22, context ≈ 90 %)

Read with `HANDOFF_NEXT_SESSION.md` (repo state, setup, track) — this file adds the things a summary tends to lose.

## In flight right now
- **Update 2026-10-22 afternoon:** phase B finished. 17 episodes processed, all P1–P7 PASS. 25 packaged in total, 0 failed. Weeks W39–W43 and the 2026-10 month digest are built, links pass. `annotate-people` stopped at the subscription session limit (resets 6 pm ET); finished batches are saved, so re-run `annotate-people ai_daily_brief --max-web-calls 5` to continue. Approved items 1 and 2 below are done: auto-refresh (`375a664`) and the originals index (`previous_run_results/T9_anthropic_4h_course/ORIGINALS_INDEX.md`).
- **Phase B** (background shell, started from the MAIN session, subscription only): `collection select ai_daily_brief --last-days 30 --as-of 2026-10-22 --order newest --replace --refresh` → `collection run … --package --digest week --usd-cap 0` → `digest --by month 2026-10` → `annotate-people ai_daily_brief --max-web-calls 5` → status. Log: `previous_run_results/collections/ai_daily_brief/phaseB.log`. It waits on usage limits by itself (ON_EXHAUSTION=wait). Report: episodes processed/failed, evals, digests (weeks + 2026-10), people/voices/sources counts.
- Phase A done: 6 older eps re-summarized (People + Sources), all P1–P7 PASS, re-packaged 0 broken; 10-21 episode added; W42 165 / W43 155 citations, links pass.

## Approved, not started (do next, in this order)
1. **Collection select auto-refresh**: `collection select` must refresh the cached listing when the cache is older than the requested window (phase A silently selected 0 new because the cache stopped at 10-20; needed `--refresh`). Small WP + test.
2. **Find the original Anthropic talks** behind the 3:46:21 compilation (YouTube re-uploads `QRlkSPF87jQ`, `xI3q41CBbsA`; our run `previous_run_results/T9_anthropic_4h_course`, P1–P7 PASS). Speakers in its People section: an Applied AI engineer in London ("Margot Van Laar", ASR), Will, Maya, Kevin (Anthropic Applied AI), an unnamed Claude Code engineering leader (ex-Meta/Microsoft), an Anthropic architect, a Google Cloud developer advocate ("Ivan Nardini", ASR). Likely Code with Claude / AI Engineer / Google Cloud talks. **Scope: find, index, report back only** — no summarizing.
3. Pending user choice: pod access to private repo `CurtisONeal/claude-work-patterns` — personal-repo collaborators can only get write; options: invite ecodad, drjat42, xdg, benton with write, or create a GitHub org for read-only. Pod members = ecodad, drjat42, xdg, benton (user confirmed).

## Nuances (easy to lose)
- **zsh vs bash**: never `M="python -m …"; $M …` in zsh (no word split → nothing runs). Use `bash -c '…'` or a script file. `IFS=… read a b c` also fails in zsh.
- **Agents in worktrees can be refused by the isolation guard** when running the pipeline with `PYTHONPATH=src` + runtime paths. Then they stop and report; the manager runs real pipelines from the MAIN checkout only with the user's OK (given for AIDB runs on 2026-10-22). Don't launch worktree agents from another repo's cwd (they get the wrong repo).
- **Pushes**: user approved pushing `base_human_learn_sys` main on 2026-10-22 (done, origin a87bb94) and creating the private skills repo (done). Approval is per occasion — ask again for later pushes.
- **Never guess folders**: `--out`, `RUN_DIR=`, `COLLECTION_ITEM_DONE=`; eval exits 2 on empty dirs.
- **Prompt variants**: `current` (People + Sources Referenced) vs `legacy` (byte-identical old). One opus run varies ±20 % per section — A/B needs ≥ 2 runs per arm.
- **annotate-people**: model knowledge labelled; web lookups only for unknown / post-cutoff (model cutoff 2026-06), ≤ 3 authoritative sources, quote-first X lookup, tiers confirmed/probable/unknown, roles answered "who held <role> on <date>" with dated evidence; private people never looked up; duplicates merged after lookup (Kyo → Theo). Re-packaging an annotated episode needs `--force`.
- **Vault**: `__sources/` excluded from Obsidian Sync. Phone was not showing new notes on 10-21 — user was to check the Mac's Sync remote/status after the decoy `reports` vault incident (unresolved as of this writing).
- **Privacy**: raffle winners/attendees withheld from notes and packaged transcripts.
- **Communication style the user likes**: short, plain status; say what was observed vs inferred; own mistakes plainly; numbered open decisions; ask questions in plain text (AskUserQuestion fails on the phone).
- **Pattern harvest**: after merges/evals/handoffs, update `~/Documents/agent_set_up/repos/claude-work-patterns` (v0.1.3; private GitHub; linked into `~/.claude/skills/`). Pending harvest items: auto-refresh stale caches before selecting; zsh word-splitting pitfall; worktree guard behaviour.
