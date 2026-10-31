#!/usr/bin/env bash
# Whole flow: set up the venv (first run), ingest ZIPs, restore, build the report.
# Usage: ./run.sh "C:\Users\me\Downloads\a.zip" ["C:\...\b.zip" ...]
#        ./run.sh --zip-dir "C:\Users\me\Downloads\drive"
#        ./run.sh                      # no ZIPs: restore what is already extracted
#        ./run.sh --ui                 # start the browser UI instead
# Extra options for the restore step can be set in RESTORE_OPTS, e.g. RESTORE_OPTS="--force --jobs 8".
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP/.venv"
if [ ! -x "$VENV/bin/photo-restore" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$APP[ui]"
fi
PR="$VENV/bin/photo-restore"
if [ "${1:-}" = "--ui" ]; then
  exec "$PR" ui
fi
ZIPARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --zip-dir) ZIPARGS+=(--zip-dir "$2"); shift 2 ;;
    *) ZIPARGS+=(--zip "$1"); shift ;;
  esac
done
if [ ${#ZIPARGS[@]} -gt 0 ]; then
  "$PR" ingest "${ZIPARGS[@]}"
fi
# shellcheck disable=SC2086
"$PR" restore ${RESTORE_OPTS:-}
"$PR" report
REPORT="${PHOTO_RESTORE_ROOT:-$HOME/photo-restore}/output/report/index.html"
echo "Open: $REPORT"
if command -v wslpath >/dev/null && command -v explorer.exe >/dev/null; then
  explorer.exe "$(wslpath -w "$REPORT")" || true
fi
