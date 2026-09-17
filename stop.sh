#!/usr/bin/env bash
# Stop the service gracefully (SIGTERM → engine flushes db, portfolio, positions, logs).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
require_launchd
if service_loaded; then
  pid="$(service_pid)"
  bootout_service
  info "service stopped${pid:+ (was pid $pid)}"
else
  info "service is not loaded"
fi
