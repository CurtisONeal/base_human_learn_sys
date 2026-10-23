#!/usr/bin/env bash
# Weekly collection update, run by launchd outside any Claude Code session
# (so it never shares a usage window with interactive work and costs no
# conversation context). Subscription only; waits through usage limits.
#
#   scripts/collection_weekly.sh ai_daily_brief            # real run
#   DRY_RUN=1 scripts/collection_weekly.sh ai_daily_brief  # environment check, no model calls
#
# Order (derived outputs once, at the end): select new episodes -> notes +
# packages -> week digests for weeks that have ended -> month digests for
# months that have ended -> people annotation -> status -> notice.
set -uo pipefail

NAME="${1:?usage: collection_weekly.sh <collection>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
export PATH="$HOME/Documents/agent_set_up/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LLM_MODELS='["claude-cli/opus","claude-cli/sonnet"]' ALLOW_METERED_FALLBACK=false ON_EXHAUSTION=wait

LOG_DIR="previous_run_results/collections/$NAME/weekly"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d_%H%M)"
LOG="$LOG_DIR/$STAMP.log"
PY=".venv/bin/python -m video2mdnotes.main"
VAULT_INBOX="$HOME/Documents/obsidian_vault/syncing_vault2_clean_2026/000_inbox_new_notes/agent_task_reports"

step() { echo "== $(date '+%F %T') $*" >>"$LOG"; "$@" >>"$LOG" 2>&1; local rc=$?; echo "exit=$rc" >>"$LOG"; return $rc; }

if [ -n "${DRY_RUN:-}" ]; then
  step claude --version
  step $PY collection select "$NAME" --last-days 10 --order newest --dry-run
  step $PY collection status "$NAME"
else
  step $PY collection select "$NAME" --last-days 10 --order newest
  step $PY collection run "$NAME" --package --digest week --closed-spans-only --usd-cap 0
  step $PY collection run "$NAME" --digest month --closed-spans-only --usd-cap 0
  step $PY annotate-people "$NAME" --max-web-calls 5
  step $PY collection status "$NAME"
fi

DONE=$(grep -c '^COLLECTION_ITEM_DONE=' "$LOG")
DIGS=$(grep -c '^COLLECTION_DIGEST=' "$LOG")
FAILS=$(grep -c '^exit=[1-9]' "$LOG")
STATES=$(grep -m1 '^States:' "$LOG" | tail -1)
MSG="${NAME} weekly${DRY_RUN:+ (dry run)}: ${DONE} episode(s) done, ${DIGS} digest(s), ${FAILS} step(s) non-zero. ${STATES} Log: ${REPO}/${LOG}"

mkdir -p "$VAULT_INBOX"
printf -- '---\ncreated: %s\nsource: collection_weekly.sh\n---\n\n%s\n' "$(date +%F)" "$MSG" \
  >"$VAULT_INBOX/${STAMP}_${NAME}_weekly.md"
if hermes send --to discord:#general "$MSG" >>"$LOG" 2>&1; then
  echo "notice sent" >>"$LOG"
else
  echo "notice FAILED (vault copy written)" >>"$LOG"
fi
