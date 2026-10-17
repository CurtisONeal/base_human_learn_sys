#!/usr/bin/env bash
# Smoke-test every CLI path on the MVP fixtures against the REAL subscription
# chain (claude CLI), never a metered API. bash 3.2 compatible.
#
#   scripts/smoke_all_paths.sh --dry-run                 # print the commands only
#   scripts/smoke_all_paths.sh                           # run them (uses the subscription)
#   scripts/smoke_all_paths.sh --real <url-or-path> ...  # also run your own sources
#
# Guarantees:
#   * ALLOW_METERED_FALLBACK=false and ON_EXHAUSTION=stop for every command;
#     --usd-cap 0 on every command that takes it (process, book, media,
#     longform) -> no metered call can be made.
#   * all output under one temp folder (OUTPUT_DIR, LAST_RUN_FILE, every --out);
#     nothing is written to previous_run_results/.
#   * each run dir is read from the command's `RUN_DIR=` line, never guessed;
#     media/video run dirs are then graded with scripts/eval_longform.py.
#   * never reads /Volumes (media/process refuse it anyway).
#
# --real accepts YouTube/article URLs (-> process), .epub/.pdf (-> book) and
# audio/video files (-> media). Pass safety copies, never an SD card.
#
# Paths covered on fixtures: process (local md / txt / html / epub / pdf),
# book (epub, pdf, --extract-only), media (single m4a, truncated aac,
# camera-split group, --probe, --transcribe-only), longform (on the
# transcribe-only run dir), catalog (index / overview / find / toc).
# Online-only paths (YouTube with or without captions, playlist, embed page,
# article URL, api /process) need a real URL: give them with --real.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# PY=... overrides. A worktree (<repo>/.claude/worktrees/<x>) has no .venv of
# its own and uses the main checkout's. Never runs `uv sync`.
PY="${PY:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(cd "$REPO/../../.." && pwd)/.venv/bin/python"
FIX="$REPO/tests/fixtures/mvp"
DRY=0
REAL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --real) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do REAL="$REAL
$1"; shift; done; continue ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$DRY" = 1 ]; then
  TMP="\$SMOKE_TMP"
else
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/v2m_smoke.XXXXXX")"
fi
export PYTHONPATH="$REPO/src"
export ALLOW_METERED_FALLBACK=false ON_EXHAUSTION=stop
export OUTPUT_DIR="$TMP/results" LAST_RUN_FILE="$TMP/results/_last_run.json"
CLI="$PY -m video2mdnotes.main"
PASS=0; FAIL=0; SUMMARY=""

note() { SUMMARY="$SUMMARY
$1"; }

# run <label> <grade:yes|no> <command...>
run() {
  label="$1"; grade="$2"; shift 2
  if [ "$DRY" = 1 ]; then
    printf '%q ' "$@"; echo
    [ "$grade" = yes ] && echo "  -> $PY $REPO/scripts/eval_longform.py <RUN_DIR from the line above>"
    return 0
  fi
  log="$TMP/$label.log"
  echo "== $label"
  "$@" >"$log" 2>&1
  rc=$?
  dir="$(grep '^RUN_DIR=' "$log" | tail -1 | sed 's/^RUN_DIR=//')"
  if [ $rc -ne 0 ]; then
    FAIL=$((FAIL + 1)); note "FAIL $label (exit $rc) — $log"; return 0
  fi
  if [ "$grade" = yes ]; then
    if [ -z "$dir" ]; then
      FAIL=$((FAIL + 1)); note "FAIL $label (no RUN_DIR= line) — $log"; return 0
    fi
    if "$PY" "$REPO/scripts/eval_longform.py" "$dir" >"$log.eval" 2>&1; then
      PASS=$((PASS + 1)); note "PASS $label  P1-P7  $dir"
    else
      FAIL=$((FAIL + 1)); note "FAIL $label  eval — $log.eval"
    fi
  else
    PASS=$((PASS + 1)); note "PASS $label  ${dir:-(no run dir)}"
  fi
}

[ "$DRY" = 1 ] && echo "# SMOKE_TMP=<mktemp -d>; env: ALLOW_METERED_FALLBACK=false ON_EXHAUSTION=stop OUTPUT_DIR=\$SMOKE_TMP/results PYTHONPATH=$REPO/src"

# documents (summarize_document)
for f in guide.md notes.txt article.html tiny_book.epub tiny_paper.pdf; do
  run "process_${f%%.*}_${f##*.}" no $CLI process "$FIX/$f" --usd-cap 0 --out "$TMP/docs"
done
run book_epub no $CLI book "$FIX/tiny_book.epub" --usd-cap 0 --out "$TMP/books"
run book_pdf no $CLI book "$FIX/tiny_paper.pdf" --usd-cap 0 --out "$TMP/books"
run book_extract_only no $CLI book "$FIX/tiny_paper.pdf" --extract-only

# media (windowed ASR -> long-form)
run media_probe no $CLI media "$FIX/cam_0001.mov" "$FIX/cam_0002.mov" "$FIX/timkoo_truncated.aac" --probe
run media_m4a yes $CLI media "$FIX/talk_15s.m4a" --usd-cap 0 --out "$TMP/media_m4a"
run media_truncated_aac yes $CLI media "$FIX/timkoo_truncated.aac" --usd-cap 0 --out "$TMP/media_aac"
run media_camera_group yes $CLI media "$FIX/cam_0001.mov" "$FIX/cam_0002.mov" --usd-cap 0 --out "$TMP/media_group"
run media_transcribe_only no $CLI media "$FIX/talk_15s.m4a" --transcribe-only --out "$TMP/t_only"
run longform yes $CLI longform "$TMP/t_only" --usd-cap 0

# catalog (free: no model, no network)
mkdir_cmd="mkdir -p $TMP/corpus && cp $FIX/tiny_book.epub $FIX/tiny_paper.pdf $FIX/guide.md $TMP/corpus/"
if [ "$DRY" = 1 ]; then echo "$mkdir_cmd"; else mkdir -p "$TMP/corpus" && cp "$FIX/tiny_book.epub" "$FIX/tiny_paper.pdf" "$FIX/guide.md" "$TMP/corpus/"; fi
run catalog_index no $CLI catalog index --corpus "$TMP/corpus" --db "$TMP/catalog.db"
run catalog_overview no $CLI catalog overview --db "$TMP/catalog.db"
run catalog_find no $CLI catalog find Evaluation --db "$TMP/catalog.db"
run catalog_toc no $CLI catalog toc "Chunk Size" --db "$TMP/catalog.db"

# the user's own sources
n=0
while IFS= read -r src; do
  [ -z "$src" ] && continue
  n=$((n + 1))
  case "$src" in
    /Volumes/*) note "SKIP real_$n: refusing /Volumes ($src)"
                [ "$DRY" = 1 ] && echo "# SKIP real_$n (refused: /Volumes): $src"
                continue ;;
    http://*|https://*) run "real_${n}_process" yes $CLI process "$src" --usd-cap 0 --out "$TMP/real_$n" ;;
    *.epub|*.pdf|*.EPUB|*.PDF) run "real_${n}_book" no $CLI book "$src" --usd-cap 0 --out "$TMP/real_$n" ;;
    *) run "real_${n}_media" yes $CLI media "$src" --usd-cap 0 --out "$TMP/real_$n" ;;
  esac
done <<EOF
$REAL
EOF

if [ "$DRY" = 0 ]; then
  echo "$SUMMARY"
  echo
  echo "smoke: $PASS passed, $FAIL failed — logs in $TMP"
  [ "$FAIL" = 0 ]
fi
