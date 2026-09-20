#!/usr/bin/env bash
# Verify the whole installation: python, venv, dirs, env file, plist, launchd, heartbeat, app doctor.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
ok=0; bad=0
pass() { printf '%sPASS%s  %s\n' "$c_green" "$c_reset" "$*"; ok=$((ok+1)); }
failc() { printf '%sFAIL%s  %s\n' "$c_red" "$c_reset" "$*"; bad=$((bad+1)); }
warnc() { printf '%sWARN%s  %s\n' "$c_yellow" "$c_reset" "$*"; }

if PY="$(find_python)"; then pass "python: $PY ($("$PY" --version 2>&1))"; else failc "python >= 3.12 not found"; fi
if [[ -x "$(venv_python)" ]]; then pass "venv: $VENV_DIR"; else failc "venv missing: run ./install-macos.sh"; fi
if [[ -x "$(venv_bin solana-sniper)" ]]; then pass "cli: $(venv_bin solana-sniper)"; else failc "solana-sniper not installed in venv"; fi
for d in "$DB_DIR" "$LOG_DIR" "$STATE_DIR"; do
  if [[ -d "$d" && -w "$d" ]]; then pass "dir writable: $d"; else failc "dir missing/unwritable: $d"; fi
done
if [[ -f "$ENV_FILE" ]]; then
  perms="$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode)[-3:])' "$ENV_FILE")"
  if [[ "$perms" == "600" ]]; then pass "env file: $ENV_FILE (mode 600)"; else warnc "env file $ENV_FILE has mode $perms (expected 600)"; fi
  if grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -qiE 'PRIVATE|SECRET_KEY|SEED_PHRASE|MNEMONIC'; then warnc "env file mentions a private key/seed: the hot wallet key belongs in its own file under $WALLET_DIR (wallet create), never in the env file"; fi
else
  warnc "no env file at $ENV_FILE (optional API keys not configured)"
fi
if [[ -d "$WALLET_DIR" ]]; then
  wperms="$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode)[-3:])' "$WALLET_DIR")"
  if [[ "$wperms" == "700" ]]; then pass "wallet dir: $WALLET_DIR (mode 700)"; else warnc "wallet dir $WALLET_DIR has mode $wperms (expected 700): chmod 700 '$WALLET_DIR'"; fi
fi
if repo_ignores_secrets "$REPO_DIR"; then pass "git ignores secrets and runtime data (.env, sniper.env, data/, logs/) and none is tracked"
else
  case $? in
    1) failc "repository unsafe: $REPO_SAFETY_REASON" ;;
    *) warnc "cannot verify .gitignore: $REPO_SAFETY_REASON" ;;
  esac
fi
if is_macos; then
  if [[ -f "$PLIST_PATH" ]]; then
    if plutil -lint "$PLIST_PATH" >/dev/null 2>&1; then pass "plist valid: $PLIST_PATH"; else failc "plist invalid: $PLIST_PATH"; fi
    if grep -q -- '--dry-run' "$PLIST_PATH"; then pass "service mode: dry-run (simulated confirmations)"
    elif grep -q -- '--autonomous' "$PLIST_PATH"; then warnc "service mode: autonomous (REAL swaps from the hot wallet; stop with ./cmd.sh kill)"
    else warnc "service mode: signal (live signals; confirmations via ./cmd.sh)"; fi
  else
    failc "plist not installed: $PLIST_PATH (run ./install-macos.sh)"
  fi
  if service_loaded; then pid="$(service_pid)"; if [[ -n "$pid" && "$pid" != "0" ]]; then pass "launchd: running pid $pid"; else failc "launchd: loaded but not running"; fi; else failc "launchd: service not loaded"; fi
else
  warnc "not macOS ($(uname -s)): launchd checks skipped"
fi
if [[ -f "$STATUS_FILE" ]]; then
  if "$(venv_bin solana-sniper)" health --quiet-check >/dev/null 2>&1; then pass "heartbeat fresh and healthy: $STATUS_FILE"; else failc "heartbeat stale/unhealthy: $STATUS_FILE (see ./status.sh)"; fi
else
  warnc "no heartbeat yet at $STATUS_FILE"
fi
if [[ -x "$(venv_bin solana-sniper)" ]]; then
  info "application doctor (config, database, network, providers, quotes)"
  if (cd "$REPO_DIR" && "$(venv_bin solana-sniper)" doctor); then pass "application doctor"; else failc "application doctor reported failures"; fi
fi
echo
if [[ "$bad" -eq 0 ]]; then info "doctor: all $ok checks passed"; else fail "doctor: $bad check(s) failed, $ok passed"; fi
