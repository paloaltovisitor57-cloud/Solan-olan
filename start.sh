#!/usr/bin/env bash
# Start (or re-register) the launchd service.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
require_launchd
[[ -x "$(venv_bin solana-sniper)" ]] || fail "not installed; run ./install-macos.sh first"
ensure_dirs; ensure_env_file
render_plist; validate_plist
bootstrap_service
if wait_for_status 45; then info "service running (pid $(service_pid))"; else warn "started, but no healthy heartbeat yet: ./logs.sh"; fi
