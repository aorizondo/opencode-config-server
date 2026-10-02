#!/usr/bin/env bash
# Login command run by the OpenCode harness (packages/core/src/integration.ts).
#
# Contract the harness imposes:
#   * argv comes from the manifest's auth.command; stdin is /dev/null
#   * stdout (trimmed) becomes the stored credential  -> only the token goes there
#   * stderr is what the employee sees in the TUI     -> all progress goes there
#   * exit != 0 fails the login and shows stderr as the error
set -euo pipefail

# Stdout is the credential channel, so nothing else may write to it: fd 3 keeps
# the token, fd 1 is rerouted to stderr for the whole run. A chatty shell
# profile or a noisy installer can no longer corrupt the stored credential.
exec 3>&1 1>&2

SERVER="${1:-}"
if [ -z "$SERVER" ]; then
  echo "opencode-corp: no server URL was passed to the login command" >&2
  exit 2
fi
SERVER="${SERVER%/}"

say() { printf 'opencode-corp: %s\n' "$1" >&2; }

for binary in curl; do
  command -v "$binary" >/dev/null 2>&1 || { say "'$binary' is required but not installed"; exit 3; }
done

CRED_FILE="${OPENCODE_CORP_CREDENTIALS:-$HOME/.config/opencode-corp/credentials}"
CORP_TOKEN="${OPENCODE_CORP_TOKEN:-}"
CORP_USER="${OPENCODE_CORP_USER:-}"
CORP_PASSWORD="${OPENCODE_CORP_PASSWORD:-}"

# Credentials file: `key = value` lines, comments with '#'. Only read when the
# environment did not already provide a credential.
if [ -z "$CORP_TOKEN" ] && [ -z "$CORP_USER" ] && [ -f "$CRED_FILE" ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    mode=$(stat -c '%a' "$CRED_FILE" 2>/dev/null || echo "")
  else
    mode=$(stat -f '%Lp' "$CRED_FILE" 2>/dev/null || echo "")
  fi
  case "$mode" in
    *[0-7][1-7]|*[1-7][0-7]) say "refusing to read $CRED_FILE: it is readable by other users (chmod 600 it)"; exit 4 ;;
  esac
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    key="${line%%=*}"; value="${line#*=}"
    [ "$key" = "$line" ] && continue
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/')"
    case "$key" in
      token|api_key) CORP_TOKEN="$value" ;;
      user|username) CORP_USER="$value" ;;
      password) CORP_PASSWORD="$value" ;;
    esac
  done < "$CRED_FILE"
fi

response_file="$(mktemp)"
trap 'rm -f "$response_file"' EXIT

if [ -n "$CORP_TOKEN" ]; then
  say "authenticating with the corporate API key"
  status=$(curl -sS -o "$response_file" -w '%{http_code}' -X POST \
    -H "X-Api-Key: $CORP_TOKEN" -H 'Accept: application/json' \
    "$SERVER/auth/token" || true)
elif [ -n "$CORP_USER" ]; then
  if [ -z "$CORP_PASSWORD" ]; then
    say "OPENCODE_CORP_USER is set but no password was provided"
    exit 4
  fi
  say "authenticating as $CORP_USER"
  status=$(curl -sS -o "$response_file" -w '%{http_code}' -X POST \
    -u "$CORP_USER:$CORP_PASSWORD" -H 'Accept: application/json' \
    "$SERVER/auth/token" || true)
else
  cat >&2 <<MSG
opencode-corp: no corporate credentials found.

Provide one of these, then run the login again:

  export OPENCODE_CORP_TOKEN=<api key>
  export OPENCODE_CORP_USER=<user> OPENCODE_CORP_PASSWORD=<password>

  or write $CRED_FILE (chmod 600):
    token = <api key>
MSG
  exit 4
fi

# curl writes 000 into -w when it never got a response at all.
case "$status" in
  2[0-9][0-9] | 3[0-9][0-9] | 4[0-9][0-9] | 5[0-9][0-9]) ;;
  *)
    say "cannot reach $SERVER (network, VPN or TLS problem)"
    exit 5
    ;;
esac
if [ "$status" != "200" ]; then
  detail=$(sed -n 's/.*"detail":"\([^"]*\)".*/\1/p' "$response_file" | head -n 1)
  say "login rejected by $SERVER (HTTP $status)${detail:+: $detail}"
  exit 6
fi

TOKEN=$(sed -n 's/.*"token":"\([^"]*\)".*/\1/p' "$response_file" | head -n 1)
if [ -z "$TOKEN" ]; then
  say "the server answered without a token"
  exit 7
fi

# Bootstrap: third-party tooling that has to exist on disk. Authenticated, and
# never fatal -- a failed installer must not cost the employee their session.
if [ "${OPENCODE_CORP_SKIP_BOOTSTRAP:-0}" != "1" ]; then
  installer="$(mktemp)"
  if curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVER/bootstrap/install.sh" -o "$installer" 2>/dev/null; then
    say "running the corporate bootstrap"
    if bash "$installer" "$SERVER"; then
      say "bootstrap finished"
    else
      say "bootstrap failed; continuing with the login (re-run it later)"
    fi
  fi
  rm -f "$installer"
fi

say "logged in; the corporate configuration loads below your own settings"
printf '%s' "$TOKEN" >&3
