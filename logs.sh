#!/usr/bin/env bash
# Show the service logs.
#   ./logs.sh                 last 50 lines of every log, then exit
#   ./logs.sh -n 100          last 100 lines
#   ./logs.sh app|out|err     one log only (application log / service stdout / service stderr)
#   ./logs.sh -f, --follow    keep following (Ctrl+C stops viewing; the service keeps running)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
LINES=50; WHICH="all"; FOLLOW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) shift; LINES="${1:-50}" ;;
    -n*) LINES="${1#-n}" ;;
    -f|--follow) FOLLOW=1 ;;
    app|out|err|all) WHICH="$1" ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) fail "usage: ./logs.sh [-n LINES] [-f|--follow] [app|out|err|all]" ;;
  esac
  shift
done
[[ "$LINES" =~ ^[0-9]+$ ]] || fail "-n expects a number (got '$LINES')"
files=()
case "$WHICH" in
  app) files=("$APP_LOG") ;;
  out) files=("$STDOUT_LOG") ;;
  err) files=("$STDERR_LOG") ;;
  all) files=("$APP_LOG" "$STDOUT_LOG" "$STDERR_LOG") ;;
esac
existing=()
for f in "${files[@]}"; do [[ -f "$f" ]] && existing+=("$f"); done
[[ ${#existing[@]} -gt 0 ]] || fail "no log files yet in $LOG_DIR (is the service or a paper session running?)"
if [[ "$FOLLOW" -eq 1 ]]; then
  echo "Following logs. Press Ctrl+C to stop viewing logs; this does not stop the trading service." >&2
  exec tail -n "$LINES" -F "${existing[@]}"
fi
tail -n "$LINES" "${existing[@]}"
echo "(showing the last $LINES lines of ${#existing[@]} file(s) in $LOG_DIR; ./logs.sh -f follows)" >&2
