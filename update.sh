#!/usr/bin/env bash
# Pull the newest commit, update deps, migrate, test; restart the service ONLY if everything passes.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
cd "$REPO_DIR"
[[ -x "$(venv_python)" ]] || fail "not installed; run ./install-macos.sh first"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  fail "working tree has local modifications; commit or stash them before updating"
fi
branch="$(git rev-parse --abbrev-ref HEAD)"
before="$(git rev-parse HEAD)"
info "fetching origin/$branch"
git fetch origin "$branch"
git merge --ff-only "origin/$branch" || fail "cannot fast-forward $branch; resolve manually"
after="$(git rev-parse HEAD)"
if [[ "$before" == "$after" ]]; then
  info "already up to date ($after)"
else
  info "updated $before → $after"
fi
info "updating dependencies"
pip_install -e "${REPO_DIR}[dev]"
info "validating configuration"
"$(venv_bin solana-sniper)" config-check || fail "configuration invalid after update; service NOT restarted"
info "running migrations"
"$(venv_bin solana-sniper)" migrate || fail "migration failed; service NOT restarted"
info "running tests"
"$(venv_python)" -m pytest -q -p no:cacheprovider || fail "tests failed; service NOT restarted"
if is_macos && [[ -n "$(launchctl_bin)" ]] && service_loaded; then
  info "checks passed; restarting service"
  "$REPO_DIR/restart.sh"
else
  info "checks passed; service not running, nothing to restart"
fi
