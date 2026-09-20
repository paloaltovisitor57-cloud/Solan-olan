#!/usr/bin/env bash
# Shared helpers for the macOS deployment scripts. Sourced, never executed.
# All scripts are safe to re-run. The scripts never handle key material: only
# `solana-sniper run --autonomous` signs, from the hot wallet file under $SNIPER_HOME/wallet/.
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
WALLET_DIR="$SNIPER_HOME/wallet"
ENV_FILE="$SNIPER_HOME/sniper.env"
STATUS_FILE="$STATE_DIR/status.json"
COMMANDS_FILE="$STATE_DIR/commands"
STDOUT_LOG="$LOG_DIR/service.out.log"
STDERR_LOG="$LOG_DIR/service.err.log"
APP_LOG="$LOG_DIR/sniper.log"

# Service mode. "dry-run" (default): live data, simulated confirmations.
# "signal": live signal mode, human confirms with ./cmd.sh b N / s N; nothing is broadcast.
# "autonomous": the bot signs and broadcasts real swaps from its hot wallet, within the caps
#               recorded by `solana-sniper arm`; stop with ./cmd.sh kill or `solana-sniper kill`.
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
    dry-run|signal|autonomous) ;;
    *) fail "SNIPER_SERVICE_MODE must be 'dry-run', 'signal' or 'autonomous' (got '$SNIPER_SERVICE_MODE')" ;;
  esac
}

service_args() {
  # Arguments appended to `solana-sniper run` for the service.
  load_service_mode
  case "$SNIPER_SERVICE_MODE" in
    dry-run) printf '%s\n' "run" "--dry-run" "--no-dashboard" "--quiet" ;;
    autonomous) printf '%s\n' "run" "--autonomous" "--no-dashboard" "--quiet" ;;
    *) printf '%s\n' "run" "--no-dashboard" "--quiet" ;;
  esac
}

describe_service_mode() {
  case "$SNIPER_SERVICE_MODE" in
    dry-run) echo "dry-run (live data, simulated confirmations)" ;;
    signal) echo "signal (live signals; a human confirms via ./cmd.sh; nothing is broadcast)" ;;
    autonomous) echo "autonomous (REAL swaps signed from the hot wallet within the armed caps)" ;;
    *) echo "$SNIPER_SERVICE_MODE" ;;
  esac
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
  mkdir -p "$DB_DIR" "$LOG_DIR" "$STATE_DIR" "$WALLET_DIR"
  chmod 700 "$SNIPER_HOME" 2>/dev/null || true
  chmod 700 "$WALLET_DIR" 2>/dev/null || true
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    {
      echo "# solana-sniper local configuration (never committed). Loaded by the service and the scripts."
      echo "# SNIPER_SERVICE_MODE=dry-run   # dry-run (default), signal, or autonomous (after wallet create + arm)"
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
  # Exactly one process: `bootstrap` already launches the agent (RunAtLoad). The old
  # `kickstart -k` here killed that fresh process and started a second one, which produced two
  # sessions seconds apart. Now kickstart (never -k) is used only if nothing is running.
  require_launchd
  local lc; lc="$(launchctl_bin)"
  if service_loaded; then
    "$lc" bootout "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
    sleep 1
  fi
  "$lc" bootstrap "gui/$(id -u)" "$PLIST_PATH"
  "$lc" enable "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
  local i pid=""
  for i in 1 2 3 4 5 6; do
    pid="$(service_pid)"
    [[ -n "$pid" && "$pid" != "0" ]] && break
    sleep 0.5
  done
  if [[ -z "$pid" || "$pid" == "0" ]]; then
    "$lc" kickstart "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 || true
  fi
}

service_last_exit() {
  # launchd's view of the previous run: "last exit code = N" (absent before the first exit)
  local lc; lc="$(launchctl_bin)"
  [[ -n "$lc" ]] || { echo ""; return 0; }
  "$lc" print "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null | awk -F'= ' '/last exit code/{print $2; exit}'
}

boot_field() {
  # Read one field from the engine's boot record ($STATE_DIR/boot.json)
  local key="$1"
  [[ -f "$STATE_DIR/boot.json" ]] || { echo ""; return 0; }
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print("" if v is None else v)' "$STATE_DIR/boot.json" "$key" 2>/dev/null || echo ""
}

install_launcher() {
  # Put `solana-sniper` on PATH via ~/.local/bin (a tiny wrapper around the venv binary).
  local bin_dir="$HOME/.local/bin" launcher
  mkdir -p "$bin_dir"
  launcher="$bin_dir/solana-sniper"
  # The launcher pins the runtime home chosen at install time, so the CLI and the service always
  # look at the same database wherever it is run from (an explicit SNIPER_HOME still wins).
  local home_line
  home_line="export SNIPER_HOME=\"\${SNIPER_HOME:-$SNIPER_HOME}\""
  cat > "$launcher" <<EOF2
#!/usr/bin/env bash
# solana-sniper launcher (generated by install-macos.sh; safe to delete and re-create)
$home_line
exec "$(venv_bin solana-sniper)" "\$@"
EOF2
  chmod 755 "$launcher"
  echo "$launcher"
}

ensure_path_line() {
  # Add ~/.local/bin to PATH for zsh/bash logins once (idempotent, marked).
  # shellcheck disable=SC2016  # the literal line is written to the rc file unexpanded on purpose
  local marker="# solana-sniper: launcher on PATH" line='export PATH="$HOME/.local/bin:$PATH"'
  case ":$PATH:" in *":$HOME/.local/bin:"*) return 0 ;; esac
  local rc
  for rc in "$HOME/.zprofile" "$HOME/.bash_profile"; do
    if [[ -f "$rc" ]] && grep -qF "$marker" "$rc"; then continue; fi
    printf '\n%s\n%s\n' "$marker" "$line" >> "$rc"
  done
  return 1
}

bootout_service() {
  require_launchd
  local lc; lc="$(launchctl_bin)"
  if service_loaded; then
    "$lc" bootout "gui/$(id -u)/$SERVICE_LABEL"
  fi
}

repo_ignores_secrets() {
  # Usage: repo_ignores_secrets DIR. Exit 0 when secrets and runtime data are git-ignored and
  # untracked, 1 when the checkout is unsafe (REPO_SAFETY_REASON explains), 2 when git cannot
  # tell (not a checkout, git missing). Uses paths relative to DIR and file paths inside ignored
  # directories, so it works on a fresh clone with no data/ directory, whatever HOME/SNIPER_HOME are.
  local dir="$1" rel res
  REPO_SAFETY_REASON=""
  command -v git >/dev/null 2>&1 || { REPO_SAFETY_REASON="git not found"; return 2; }
  [[ -d "$dir/.git" ]] || { REPO_SAFETY_REASON="$dir is not a git checkout"; return 2; }
  git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { REPO_SAFETY_REASON="git cannot read $dir"; return 2; }
  for rel in .env sniper.env data/sniper.db data/state/status.json logs/sniper.log; do
    if git -C "$dir" check-ignore -q -- "$rel"; then :; else
      res=$?
      if [[ "$res" -eq 1 ]]; then REPO_SAFETY_REASON="$rel is not git-ignored"; return 1; fi
      REPO_SAFETY_REASON="git check-ignore failed ($res)"; return 2
    fi
  done
  local tracked
  tracked="$(git -C "$dir" ls-files -- .env sniper.env data logs/sniper.log 2>/dev/null | head -n1)"
  if [[ -n "$tracked" ]]; then REPO_SAFETY_REASON="$tracked is tracked by git"; return 1; fi
  return 0
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
