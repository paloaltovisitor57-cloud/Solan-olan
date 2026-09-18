#!/usr/bin/env bash
# Health monitor: launchd state + the engine heartbeat (status.json written every 5 s).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
if is_macos && [[ -n "$(launchctl_bin)" ]]; then
  if service_loaded; then
    pid="$(service_pid)"
    if [[ -n "$pid" && "$pid" != "0" ]]; then
      engine_pid="$(boot_field pid)"; starts="$(boot_field starts)"; prev="$(boot_field previous_exit)"
      printf '%sSERVICE%s   running  label=%s service-pid=%s engine-pid=%s starts=%s previous-exit=%s launchd-last-exit=%s\n' \
        "$c_green" "$c_reset" "$SERVICE_LABEL" "$pid" "${engine_pid:-?}" "${starts:-?}" "${prev:-none}" "$(service_last_exit)"
      if [[ -n "$engine_pid" && "$engine_pid" != "$pid" ]]; then
        printf '%sWARN%s      service pid %s differs from engine pid %s (restart in progress or stale record)\n' "$c_yellow" "$c_reset" "$pid" "$engine_pid"
      fi
    else
      printf '%sSERVICE%s   loaded but not running (crash loop or throttled?) label=%s\n' "$c_yellow" "$c_reset" "$SERVICE_LABEL"
    fi
  else
    printf '%sSERVICE%s   stopped (not loaded in launchd)\n' "$c_red" "$c_reset"
  fi
else
  dim "launchd not available on $(uname -s); showing heartbeat only"
fi
if [[ -x "$(venv_bin solana-sniper)" ]]; then
  (cd "$REPO_DIR" && "$(venv_bin solana-sniper)" health "$@")
else
  fail "not installed; run ./install-macos.sh"
fi
