#!/usr/bin/env bash
# Send a command to the running service (headless manual confirmation).
# Examples: ./cmd.sh b 1      confirm BUY #1        ./cmd.sh r 1   reject BUY #1
#           ./cmd.sh s 2      confirm SELL #2       ./cmd.sh i 2   ignore SELL #2
#           ./cmd.sh b 1 0.12 950000 <txsig>        record the actual fill you made in your wallet
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
[[ $# -gt 0 ]] || { sed -n '2,5p' "$0"; exit 1; }
mkdir -p "$STATE_DIR"
printf '%s\n' "$*" >> "$COMMANDS_FILE"
info "queued: $*  (the service polls $COMMANDS_FILE every 0.5 s; see ./status.sh / ./logs.sh app)"
