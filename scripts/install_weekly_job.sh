#!/usr/bin/env bash
# Install the weekly AIDB job (launchd, Mondays 03:10) after a dry run proves
# the environment works. Run it yourself:   bash scripts/install_weekly_job.sh
# Remove it later:                          bash scripts/install_weekly_job.sh --uninstall
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.aidb-weekly"
SRC="$REPO/scripts/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
  rm -f "$DEST" && echo "removed $DEST"
  exit 0
fi

echo "1/3 Dry run (no model calls; sends one test notice to Discord)..."
# Same bare environment launchd gives a job, so a PATH problem shows up now.
env -i HOME="$HOME" USER="$USER" DRY_RUN=1 /bin/bash "$REPO/scripts/collection_weekly.sh" ai_daily_brief
LOG="$(ls -t "$REPO"/previous_run_results/collections/ai_daily_brief/weekly/*.log 2>/dev/null | head -1)"
if [ -z "$LOG" ]; then
  echo "FAILED: the dry run wrote no log. Not installing."; exit 1
fi
echo "    log: $LOG"
grep -E '^== |^exit=|notice' "$LOG" | sed 's/^/    /'
if grep -q '^exit=[1-9]' "$LOG"; then
  echo "FAILED: a dry-run step exited non-zero (see the log). Not installing."; exit 1
fi

echo "2/3 Checking the plist..."
plutil -lint "$SRC" || { echo "FAILED: plist invalid. Not installing."; exit 1; }

echo "3/3 Installing and loading..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true      # replace an older copy
cp "$SRC" "$DEST"
launchctl bootstrap "$DOMAIN" "$DEST" || { echo "FAILED: launchctl bootstrap"; exit 1; }
launchctl print "$DOMAIN/$LABEL" | grep -E 'state =|path =' | sed 's/^/    /'
echo "Installed: $LABEL runs Mondays 03:10. Uninstall: bash scripts/install_weekly_job.sh --uninstall"
