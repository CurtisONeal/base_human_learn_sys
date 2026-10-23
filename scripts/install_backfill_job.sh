#!/usr/bin/env bash
# Install the one-time AIDB backfill job (launchd local.aidb-backfill, every 2 h;
# it starts work after Sat 2026-10-24 07:05 and unloads itself when done).
#   bash scripts/install_backfill_job.sh              # dry run, then install
#   bash scripts/install_backfill_job.sh --uninstall  # stop it (progress is kept)
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.aidb-backfill"
SRC="$REPO/scripts/launchd/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
  rm -f "$DEST" && echo "removed $DEST (a running backfill keeps going; finished items are kept)"
  exit 0
fi

echo "1/3 Dry run in launchd's bare environment (no model calls)..."
LOG="$(env -i HOME="$HOME" USER="$USER" DRY_RUN=1 /bin/bash "$REPO/scripts/collection_backfill.sh" ai_daily_brief 2026-01-01)"
[ -f "$LOG" ] || { echo "FAILED: no dry-run log. Not installing."; exit 1; }
grep -E '^== |^exit=|^Selection|^States|^Remaining' "$LOG" | sed 's/^/    /'
if grep -q '^exit=[1-9]' "$LOG"; then echo "FAILED: a dry-run step exited non-zero ($LOG). Not installing."; exit 1; fi

echo "2/3 Checking the plist..."
plutil -lint "$SRC" || { echo "FAILED: plist invalid. Not installing."; exit 1; }

echo "3/3 Installing and loading..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
cp "$SRC" "$DEST"
launchctl bootstrap "$DOMAIN" "$DEST" || { echo "FAILED: launchctl bootstrap"; exit 1; }
launchctl print "$DOMAIN/$LABEL" | grep -E 'state =|path =|run interval' | sed 's/^/    /'
echo "Installed: work starts at the first 2-hourly tick after Sat 2026-10-24 07:05."
