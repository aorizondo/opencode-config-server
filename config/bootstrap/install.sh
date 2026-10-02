#!/usr/bin/env bash
# Corporate bootstrap, served by GET /bootstrap/install.sh and run once by the
# login command. $1 is the server base URL.
#
# Rules for anything added here:
#   * idempotent: it runs again on every fresh login
#   * never fatal: the login survives a failed step, so fail loudly, not hard
#   * stdout is already redirected to stderr by the caller -- print freely
set -uo pipefail

SERVER="${1:-}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/opencode-corp"
STAMP="$STATE_DIR/bootstrap.version"
# Bump this when the steps below change so clients re-run them.
VERSION="1"

mkdir -p "$STATE_DIR"

if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$VERSION" ]; then
  echo "bootstrap $VERSION already applied"
  exit 0
fi

# Node toolchains usually land in the shell profile, which a non-login shell
# never reads. Load the usual suspects, quietly.
if [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null 2>&1 || true
fi
[ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH"
export PATH

failures=0

step() {
  local label="$1"; shift
  echo "-> $label"
  if "$@"; then
    return 0
  fi
  echo "   FAILED: $label" >&2
  failures=$((failures + 1))
  return 1
}

# --- Third-party tooling -----------------------------------------------------
# hindsight: persistent memory banks for coding agents.
install_hindsight() {
  command -v npx >/dev/null 2>&1 || { echo "   npx not found, skipping hindsight"; return 1; }
  npx --yes @vectorize-io/hindsight-coding-agents install opencode2
}
step "install hindsight (opencode2)" install_hindsight

# Add further steps here, e.g.:
# step "install the corporate CLI" bash -c 'curl -fsS "$0/cdn/corp-cli" | sh' "$SERVER"

if [ "$failures" -eq 0 ]; then
  printf '%s' "$VERSION" > "$STAMP"
  echo "bootstrap $VERSION complete"
  exit 0
fi

echo "bootstrap finished with $failures failed step(s); not marking it as applied" >&2
exit 1
