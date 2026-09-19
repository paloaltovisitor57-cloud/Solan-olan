#!/usr/bin/env bash
# One-time macOS setup: python check → venv → deps → dirs → config → tests → launchd service.
# Re-runnable. Usage: ./install-macos.sh [--skip-tests] [--skip-service] [--no-path] [--mode dry-run|signal]
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"

SKIP_TESTS=0; SKIP_SERVICE=0; MODE_OVERRIDE=""; NO_PATH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-service) SKIP_SERVICE=1 ;;
    --no-path) NO_PATH=1 ;;
    --mode) shift; MODE_OVERRIDE="${1:-}" ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
  shift
done

info "solana-sniper macOS install"
dim "repo:   $REPO_DIR"
dim "home:   $SNIPER_HOME"
dim "config: $SNIPER_CONFIG"

# 1. python
PY="$(find_python || true)"
[[ -n "$PY" ]] || fail "Python >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} not found. Install with: brew install python@3.12"
info "python: $PY ($("$PY" --version 2>&1))"

# 2. virtual environment
if [[ ! -x "$(venv_python)" ]]; then
  info "creating virtual environment at $VENV_DIR"
  create_venv "$PY"
else
  info "virtual environment exists at $VENV_DIR"
fi

# 3. dependencies
info "installing dependencies"
pip_install -e "${REPO_DIR}[dev,web]"

# 4. directories + local env
ensure_dirs
ensure_env_file
if [[ -n "$MODE_OVERRIDE" ]]; then
  case "$MODE_OVERRIDE" in dry-run|signal) ;; *) fail "--mode must be dry-run or signal" ;; esac
  if grep -qE '^SNIPER_SERVICE_MODE=' "$ENV_FILE"; then
    tmp="$(mktemp)"; sed "s/^SNIPER_SERVICE_MODE=.*/SNIPER_SERVICE_MODE=$MODE_OVERRIDE/" "$ENV_FILE" > "$tmp" && mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  else
    echo "SNIPER_SERVICE_MODE=$MODE_OVERRIDE" >> "$ENV_FILE"
  fi
fi
load_service_mode
info "service mode: $SNIPER_SERVICE_MODE (dry-run = live data, simulated confirmations; signal = human confirms via ./cmd.sh)"
info "state: db=$DB_DIR logs=$LOG_DIR state=$STATE_DIR"

# 5. configuration + database schema
info "validating configuration"
(cd "$REPO_DIR" && "$(venv_bin solana-sniper)" config-check) || fail "configuration invalid"
info "applying database migrations"
(cd "$REPO_DIR" && "$(venv_bin solana-sniper)" migrate) || fail "database migration failed"

# 6. tests
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  info "running test suite (this takes a few minutes)"
  (cd "$REPO_DIR" && "$(venv_python)" -m pytest -q -p no:cacheprovider) || fail "tests failed; not installing the service"
else
  warn "tests skipped (--skip-tests)"
fi

# 7. launchd service
if [[ "$SKIP_SERVICE" -eq 1 ]]; then
  warn "service installation skipped (--skip-service)"
elif ! is_macos; then
  warn "not macOS: skipping launchd service. Run manually with: $(venv_bin solana-sniper) run --dry-run"
else
  info "installing launchd agent $SERVICE_LABEL"
  render_plist
  validate_plist
  bootstrap_service
  if wait_for_status 45; then
    info "service is running and healthy"
  else
    warn "service was bootstrapped but no healthy heartbeat yet; check ./status.sh and ./logs.sh"
  fi
fi

# 8. `solana-sniper` on PATH
LAUNCHER="$(install_launcher)"
info "launcher: $LAUNCHER"
if [[ "$NO_PATH" -eq 1 ]]; then
  dim "PATH not modified (--no-path); use $LAUNCHER or $(venv_bin solana-sniper)"
elif ensure_path_line; then
  dim "$HOME/.local/bin is already on PATH"
else
  warn "added $HOME/.local/bin to PATH in ~/.zprofile and ~/.bash_profile; open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\")"
fi

info "done."
echo
echo "  Paper trade in the foreground (live data, no real funds, Ctrl+C to stop):"
echo "      solana-sniper smoke-test"
echo "      solana-sniper paper --bankroll-sol 1"
echo "  Background service: solana-sniper service status | stop | start   (./status.sh ./logs.sh ./stop.sh still work)"
echo "  Inspect: solana-sniper status | positions | portfolio | evaluate"
