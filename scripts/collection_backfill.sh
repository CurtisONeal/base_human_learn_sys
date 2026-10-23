#!/usr/bin/env bash
# One-time collection backfill, started and resumed by launchd (local.aidb-backfill,
# every 2 h). Each tick is safe to repeat:
#   before START_AFTER          -> exit (not yet)
#   backfill.done exists        -> unload this job, exit
#   a run holds the lock        -> exit (it is running, or waiting through a limit)
#   otherwise                   -> select (since SINCE, through today) and run the
#                                  queue; when the queue is empty: week + month
#                                  digests (closed spans), people, notice, done.
# Order: every episode's notes first; digests and people once, at the end,
# oldest span first (each digest reads the previous one; nothing is rebuilt).
#
#   scripts/collection_backfill.sh ai_daily_brief 2026-01-01 "2026-10-24 07:05"
set -uo pipefail

NAME="${1:?usage: collection_backfill.sh <collection> <since YYYY-MM-DD> [start-after 'YYYY-MM-DD HH:MM']}"
SINCE="${2:?since date required}"
START_AFTER="${3:-}"
LABEL="local.aidb-backfill"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
export PATH="$HOME/Documents/agent_set_up/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export LLM_MODELS='["claude-cli/opus","claude-cli/sonnet"]' ALLOW_METERED_FALLBACK=false ON_EXHAUSTION=wait

COL_DIR="previous_run_results/collections/$NAME"
LOG_DIR="$COL_DIR/backfill"
DONE="$LOG_DIR/backfill.done"
mkdir -p "$LOG_DIR"
PY=".venv/bin/python -m video2mdnotes.main"
VAULT_INBOX="$HOME/Documents/obsidian_vault/syncing_vault2_clean_2026/000_inbox_new_notes/agent_task_reports"

notify() {
  local msg="$1" stamp; stamp="$(date +%Y-%m-%d_%H%M)"
  mkdir -p "$VAULT_INBOX"
  printf -- '---\ncreated: %s\nsource: collection_backfill.sh\n---\n\n%s\n' "$(date +%F)" "$msg" \
    >"$VAULT_INBOX/${stamp}_${NAME}_backfill.md"
  hermes send --to discord:#general "$msg" >/dev/null 2>&1 || echo "notice FAILED (vault copy written)"
}

if [ -n "${DRY_RUN:-}" ]; then                 # environment check: no model calls
  LOG="$LOG_DIR/dryrun_$(date +%Y-%m-%d_%H%M).log"
  for c in "claude --version" "$PY collection select $NAME --since $SINCE --order oldest --dry-run" \
           "$PY collection status $NAME"; do
    echo "== $c" >>"$LOG"; $c >>"$LOG" 2>&1; echo "exit=$?" >>"$LOG"
  done
  echo "$LOG"; exit 0
fi
if [ -n "$START_AFTER" ] && [ "$(date '+%Y-%m-%d %H:%M')" \< "$START_AFTER" ]; then
  exit 0
fi
if [ -f "$DONE" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  exit 0
fi
LOCK="$COL_DIR/collection.lock"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  exit 0
fi

LOG="$LOG_DIR/$(date +%Y-%m-%d_%H%M).log"
step() { echo "== $(date '+%F %T') $*" >>"$LOG"; "$@" >>"$LOG" 2>&1; local rc=$?; echo "exit=$rc" >>"$LOG"; return $rc; }

FIRST=""
[ -z "$(ls "$LOG_DIR"/2*.log 2>/dev/null)" ] && FIRST=1
[ -n "$FIRST" ] && notify "${NAME} backfill starting: episodes since ${SINCE}. Log: ${REPO}/${LOG}"

step $PY collection select "$NAME" --since "$SINCE" --order oldest
step $PY collection run "$NAME" --package --usd-cap 0
step $PY collection status "$NAME"
LEFT=$(grep -oE 'Remaining: [0-9]+' "$LOG" | tail -1 | grep -oE '[0-9]+')
STATES=$(grep '^States:' "$LOG" | tail -1)

if [ "${LEFT:-1}" != "0" ]; then
  notify "${NAME} backfill paused with ${LEFT:-?} item(s) left; the next tick (2 h) resumes. ${STATES}"
  exit 0
fi

step $PY collection run "$NAME" --digest week --closed-spans-only --usd-cap 0
step $PY collection run "$NAME" --digest month --closed-spans-only --usd-cap 0
step $PY annotate-people "$NAME" --max-web-calls 5
step $PY collection status "$NAME"
STATES=$(grep '^States:' "$LOG" | tail -1)
FAILS=$(grep -c '^exit=[1-9]' "$LOG")
if [ "$FAILS" = "0" ]; then
  date >"$DONE"
  notify "${NAME} backfill DONE. ${STATES} Digests and people built. Log: ${REPO}/${LOG}"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
else
  notify "${NAME} backfill: notes done, ${FAILS} digest/people step(s) non-zero; the next tick retries. Log: ${REPO}/${LOG}"
fi
