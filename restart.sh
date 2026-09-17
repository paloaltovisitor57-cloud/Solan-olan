#!/usr/bin/env bash
# Graceful restart: stop (flush state) then start; open positions are restored from the database.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
"$REPO_DIR/stop.sh"
sleep 2
"$REPO_DIR/start.sh"
