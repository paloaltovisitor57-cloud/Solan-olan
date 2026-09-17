#!/usr/bin/env bash
# Tail the service logs. Usage: ./logs.sh [-n LINES] [app|out|err|all]
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
LINES=50; WHICH="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n) shift; LINES="${1:-50}" ;;
    app|out|err|all) WHICH="$1" ;;
    *) fail "usage: ./logs.sh [-n LINES] [app|out|err|all]" ;;
  esac
  shift
done
files=()
case "$WHICH" in
  app) files=("$APP_LOG") ;;
  out) files=("$STDOUT_LOG") ;;
  err) files=("$STDERR_LOG") ;;
  all) files=("$APP_LOG" "$STDOUT_LOG" "$STDERR_LOG") ;;
esac
existing=()
for f in "${files[@]}"; do [[ -f "$f" ]] && existing+=("$f"); done
[[ ${#existing[@]} -gt 0 ]] || fail "no log files yet in $LOG_DIR"
exec tail -n "$LINES" -F "${existing[@]}"
