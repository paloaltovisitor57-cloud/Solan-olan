#!/usr/bin/env bash
# Shared helpers for the macOS deployment scripts. Sourced, never executed.
# All scripts are safe to re-run; nothing here signs or broadcasts anything.
# shellcheck disable=SC2034  # variables are consumed by the scripts that source this file

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${SNIPER_VENV:-$REPO_DIR/.venv}"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=12
SERVICE_LABEL="${SNIPER_SERVICE_LABEL:-com.solanasniper.agent}"
LAUNCH_AGENTS_DIR="${SNIPER_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$SERVICE_LABEL.plist"
PLIST_TEMPLATE="$REPO_DIR/scripts/launchd/com.solanasniper.agent.plist.template"

if [[ "$(uname -s)" == "Darwin" ]]; then
  DEFAULT_HOME="$HOME/Library/Application Support/SolanaSniper"
else
  DEFAULT_HOME="$HOME/.local/share/solana-sniper"
fi
SNIPER_HOME="${SNIPER_HOME:-$DEFAULT_HOME}"
export SNIPER_HOME
SNIPER_CONFIG="${SNIPER_CONFIG:-$REPO_DIR/configs/default.yaml}"
export SNIPER_CONFIG
DB_DIR="$SNIPER_HOME/db"
LOG_DIR="$SNIPER_HOME/logs"
STATE_DIR="$SNIPER_HOME/state"
ENV_FILE="$SNIPER_HOME/sniper.env"
STATUS_FILE="$STATE_DIR/status.json"
COMMANDS_FILE="$STATE_DIR/commands"
STDOUT_LOG="$LOG_DIR/service.out.log"
STDERR_LOG="$LOG_DIR/service.err.log"
APP_LOG="$LOG_DIR/sniper.log"

# Service mode. "dry-run" (default): live data, simulated confirmations.
# "signal": live signal mode, human confirms with ./cmd.sh b N / s N. Nothing ever broadcasts.
SNIPER_SERVICE_MODE="${SNIPER_SERVICE_MODE:-dry-run}"

c_red=$'\033[31m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_dim=$'\033[2m'; c_reset=$'\033[0m'
info()  { printf '%s==>%s %s\n' "$c_green" "$c_reset" "$*"; }
warn()  { printf '%sWARN%s %s\n' "$c_yellow" "$c_reset" "$*" >&2; }
fail()  { printf '%sERROR%s %s\n' "$c_red" "$c_reset" "$*" >&2; exit 1; }
dim()   { printf '%s%s%s\n' "$c_dim" "$*" "$c_reset"; }

is_macos() { [[ "$(uname -s)" == "Darwin" ]]; }

launchctl_bin() { command -v launchctl 2>/dev/null || true; }

require_launchd() {
  is_macos || fail "launchd is only available on macOS (this is $(uname -s)); use 'solana-sniper run' directly here."
  [[ -n "$(launchctl_bin)" ]] || fail "launchctl not found in PATH"
}

load_service_mode() {
  # sniper.env may override SNIPER_SERVICE_MODE; it is never committed to git.
  if [[ -f "$ENV_FILE" ]]; then
    local line
    line="$(grep -E '^SNIPER_SERVICE_MODE=' "$ENV_FILE" | tail -n1 || true)"
    if [[ -n "$line" ]]; then
      SNIPER_SERVICE_MODE="${line#SNIPER_SERVICE_MODE=}"
      SNIPER_SERVICE_MODE="${SNIPER_SERVICE_MODE//\"/}"
    fi
  fi
  case "$SNIPER_SERVICE_MODE" in
    dry-run|signal) ;;
    *) fail "SNIPER_SERVICE_MODE must be 'dry-run' or 'signal' (got '$SNIPER_SERVICE_MODE')" ;;
  esac
}

service_args() {
  # Arguments appended to `solana-sniper run` for the service.
  load_service_mode
  if [[ "$SNIPER_SERVICE_MODE" == "dry-run" ]]; then
    printf '%s\n' "run" "--dry-run" "--no-dashboard" "--quiet"
  else
    printf '%s\n' "run" "--no-dashboard" "--quiet"
  fi
}

find_python() {
  local candidate
  for candidate in "${SNIPER_PYTHON:-}" python3.13 python3.12 python3 python; do
    [[ -n "$candidate" ]] || continue
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
      then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

venv_python() { printf '%s\n' "$VENV_DIR/bin/python"; }
venv_bin() { printf '%s\n' "$VENV_DIR/bin/$1"; }

create_venv() {
  # Prefer uv (fast, no pip needed); fall back to the stdlib venv module.
  local py="$1"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python "$py" "$VENV_DIR" >/dev/null
  else
    "$py" -m venv "$VENV_DIR"
    "$(venv_python)" -m pip install --upgrade pip -q
  fi
}

pip_install() {
  # Install into the venv with whichever installer is available.
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$(venv_python)" -q "$@"
  elif "$(venv_python)" -m pip --version >/dev/null 2>&1; then
    "$(venv_python)" -m pip install -q "$@"
  else
    "$(venv_python)" -m ensurepip --upgrade >/dev/null 2>&1 || fail "no pip in $VENV_DIR and uv is not installed"
    "$(venv_python)" -m pip install -q "$@"
  fi
}

ensure_dirs() {
  mkdir -p "$DB_DIR" "$LOG_DIR" "$STATE_DIR"
  chmod 700 "$SNIPER_HOME" 2>/dev/null || true
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    {
      echo "# solana-sniper local configuration (never committed). Loaded by the service and the scripts."
      echo "# SNIPER_SERVICE_MODE=dry-run   # dry-run (default) or signal"
      echo "SNIPER_SERVICE_MODE=dry-run"
      echo
      grep -vE '^\s*$' "$REPO_DIR/.env.example" | sed 's/^\([A-Z_]*=\)$/#\1/'
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    info "created $ENV_FILE (edit it to add API keys; it stays outside git)"
  else
    chmod 600 "$ENV_FILE" 2>/dev/null || true
  fi
}

service_loaded() {
  # 0 if the agent is registered with launchd for this user
  local lc; lc="$(launchctl_bin)"
  [[ -n "$lc" ]] || return 1
  "$lc" print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1
}

service_pid() {
  local lc; lc="$(launchctl_bin)"
  [[ -n "$lc" ]] || { echo ""; return 0; }
  "$lc" print "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'
}

render_plist() {
  # Render the plist template with absolute paths; POSIX sed with a delimiter that cannot occur in paths.
  local args_xml=""
  local arg
  while IFS= read -r arg; do
    args_xml+="        <string>${arg}</string>"$'\n'
  done < <(service_args)
  local path_env="$VENV_DIR/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
  python3 - "$PLIST_TEMPLATE" "$PLIST_PATH" <<PY
import sys, pathlib
template = pathlib.Path(sys.argv[1]).read_text()
out = (template
    .replace("@@LABEL@@", "$SERVICE_LABEL")
    .replace("@@BIN@@", "$(venv_bin solana-sniper)")
    .replace("@@ARGS@@", """$args_xml""".rstrip("\n"))
    .replace("@@REPO_DIR@@", "$REPO_DIR")
    .replace("@@SNIPER_HOME@@", "$SNIPER_HOME")
    .replace("@@SNIPER_CONFIG@@", "$SNIPER_CONFIG")
    .replace("@@PATH@@", "$path_env")
    .replace("@@STDOUT@@", "$STDOUT_LOG")
    .replace("@@STDERR@@", "$STDERR_LOG"))
pathlib.Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(sys.argv[2]).write_text(out)
PY
}

validate_plist() {
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST_PATH" >/dev/null || fail "generated plist is invalid: $PLIST_PATH"
  else
    python3 -c "import plistlib,sys; plistlib.load(open(sys.argv[1],'rb'))" "$PLIST_PATH" || fail "generated plist is invalid"
  fi
}

bootstrap_service() {
  require_launchd
  local lc; lc="$(launchctl_bin)"
  if service_loaded; then
    "$lc" bootout "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
    sleep 1
  fi
  "$lc" bootstrap "gui/$(id -u)" "$PLIST_PATH"
  "$lc" enable "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
  "$lc" kickstart -k "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
}

bootout_service() {
  require_launchd
  local lc; lc="$(launchctl_bin)"
  if service_loaded; then
    "$lc" bootout "gui/$(id -u)/$SERVICE_LABEL"
  fi
}

wait_for_status() {
  # wait up to $1 seconds for a fresh heartbeat written by the new process
  local deadline=$(( $(date +%s) + ${1:-30} ))
  while (( $(date +%s) < deadline )); do
    if [[ -f "$STATUS_FILE" ]] && "$(venv_python)" -m solana_sniper.cli.main health --quiet-check >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}
