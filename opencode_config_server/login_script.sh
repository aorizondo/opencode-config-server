#!/usr/bin/env bash
# Login command run by the OpenCode harness (packages/core/src/integration.ts).
#
# Contract the harness imposes:
#   * argv comes from the manifest's auth.command; stdin is /dev/null
#   * stdout (trimmed) becomes the stored credential  -> only the token goes there
#   * stderr is what the employee sees in the TUI     -> all progress goes there
#   * exit != 0 fails the login and shows stderr as the error
#   * the command runs with the OpenCode *server's* environment and is killed
#     after 10 minutes
#
# Credentials, first match wins:
#   1. OPENCODE_CORP_TOKEN, or OPENCODE_CORP_USER + OPENCODE_CORP_PASSWORD
#   2. the credentials file ($OPENCODE_CORP_CREDENTIALS, ~/.config/opencode-corp/credentials)
#   3. browser login: the employee signs in at <server>/login with the code we print
#      (OPENCODE_CORP_NO_BROWSER=1 prints the address without trying to open it)
#
# After the token: the corporate bootstrap (OPENCODE_CORP_SKIP_BOOTSTRAP=1 skips
# it) and the machine environment (OPENCODE_CORP_SKIP_ENV=1 skips it).
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

# Flat JSON from our own server: no jq dependency on employee machines.
json_string() { sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" "$response_file" | head -n 1; }
json_number() { sed -n "s/.*\"$1\":\([0-9][0-9]*\).*/\1/p" "$response_file" | head -n 1; }

open_browser() {
  [ "${OPENCODE_CORP_NO_BROWSER:-0}" = "1" ] && return 1
  # `3>&-`: the browser must not inherit the token channel, or the harness would
  # keep reading our stdout until the browser exits.
  case "$(uname -s)" in
    Darwin)
      command -v open >/dev/null 2>&1 && { open "$1" >/dev/null 2>&1 3>&- & return 0; } ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null && command -v cmd.exe >/dev/null 2>&1; then
        cmd.exe /c start "" "$1" >/dev/null 2>&1 3>&- &
        return 0
      fi
      if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$1" >/dev/null 2>&1 3>&- &
        return 0
      fi ;;
    MINGW* | MSYS* | CYGWIN*)
      start "" "$1" >/dev/null 2>&1 3>&- &
      return 0 ;;
  esac
  return 1
}

# Browser login (RFC 8628 shape). Leaves the token response in $response_file
# and status=200, like the direct exchanges above.
browser_login() {
  status=$(curl -sS -o "$response_file" -w '%{http_code}' -X POST \
    -H 'Accept: application/json' "$SERVER/auth/device" || true)
  case "$status" in
    200) ;;
    000) say "cannot reach $SERVER (network, VPN or TLS problem)"; exit 5 ;;
    *)
      detail=$(json_string detail)
      say "the server refused to start a browser login (HTTP $status)${detail:+: $detail}"
      exit 6 ;;
  esac
  local device_code user_code url interval expires deadline error
  device_code=$(json_string device_code)
  user_code=$(json_string user_code)
  url=$(json_string verification_uri_complete)
  interval=$(json_number interval); interval=${interval:-2}
  expires=$(json_number expires_in); expires=${expires:-300}
  if [ -z "$device_code" ] || [ -z "$url" ]; then
    say "the server answered without a login code"
    exit 7
  fi

  say "sign in from your browser to finish the login:"
  say "    $url"
  say "    code: $user_code"
  if open_browser "$url"; then
    say "a browser window should open; if it does not, open the address above"
  fi

  deadline=$(( $(date +%s) + expires ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    sleep "$interval"
    status=$(curl -sS -o "$response_file" -w '%{http_code}' -X POST \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -d "{\"device_code\":\"$device_code\"}" "$SERVER/auth/device/token" || true)
    case "$status" in
      200) return 0 ;;
      000) continue ;;  # a network blip while waiting is not fatal
      400)
        error=$(json_string error)
        case "$error" in
          authorization_pending) continue ;;
          slow_down) interval=$(( interval + 2 )); continue ;;
          *)
            detail=$(json_string detail)
            say "browser login failed${detail:+: $detail}"
            exit 8 ;;
        esac ;;
      *)
        detail=$(json_string detail)
        say "browser login rejected by $SERVER (HTTP $status)${detail:+: $detail}"
        exit 6 ;;
    esac
  done
  say "the login code expired before it was approved; run the login again"
  exit 8
}

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
  browser_login
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

# Variables: the employee's own values always win over the company's.
#
#   * {env:NAME} in the configuration: we tell the server which of its
#     variables this employee already defines (names only, never values), and
#     it stops filling those in for them, so OpenCode uses theirs.
#   * "machine" variables, set in the environment of the OpenCode background
#     service: only ever set or unset here if this script set them before and
#     they still hold what it put there (env.managed keeps a digest per name).
#     Anything the employee set or changed is left alone.
#
# "Their own" means present in the environment this login runs with -- the
# OpenCode server's, so exactly what {env:NAME} would see -- or in the service
# configuration, with a value this script did not put there.
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode-corp"
MANAGED="$STATE_DIR/env.managed"

opencode_binary() {
  local candidate="${OPENCODE_CORP_OPENCODE_BIN:-}"
  if [ -z "$candidate" ] && [ -r "/proc/$PPID/exe" ]; then
    candidate=$(readlink "/proc/$PPID/exe" 2>/dev/null || true)
  elif [ -z "$candidate" ]; then
    candidate=$(ps -o comm= -p "$PPID" 2>/dev/null | head -n 1 || true)
  fi
  case "${candidate##*/}" in
    opencode*) ;;
    *) candidate=$(command -v opencode 2>/dev/null || true) ;;
  esac
  [ -n "$candidate" ] && [ -x "$candidate" ] && printf '%s' "$candidate"
}

checksum() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 | cut -d' ' -f1
  else cksum | cut -d' ' -f1
  fi
}

digest_of() { printf '%s' "$1" | checksum; }
# Digest of the value this script set for a name, if it set one.
ours_for() { [ -f "$MANAGED" ] && awk -v n="$1" '$1 == n { print $2 }' "$MANAGED" | head -n 1; }
service_value() { [ -n "$OPENCODE_BIN" ] && "$OPENCODE_BIN" service get env "$1" 2>/dev/null || true; }

employee_owns() {
  local name="$1" ours value
  ours=$(ours_for "$name")
  for value in "$(printenv "$name" 2>/dev/null || true)" "$(service_value "$name")"; do
    [ -n "$value" ] || continue
    if [ -z "$ours" ] || [ "$(digest_of "$value")" != "$ours" ]; then return 0; fi
  done
  return 1
}

report_own_env() {
  local names name own="" json=""
  names="$(mktemp)"
  if ! curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVER/bootstrap/names" -o "$names" 2>/dev/null; then
    rm -f "$names"
    return 0
  fi
  while read -r name; do
    case "$name" in "" | [0-9]* | *[!A-Za-z0-9_]*) continue ;; esac
    if employee_owns "$name"; then
      own="$own $name"
      json="$json${json:+,}\"$name\""
    fi
  done < "$names"
  rm -f "$names"
  curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"names\":[$json]}" "$SERVER/bootstrap/own-env" >/dev/null 2>&1 || true
  for name in $own; do
    say "$name: you already define it, so your own value is used instead of the company's"
  done
}

# Skills: OpenCode loads remote skill sources after the employee's own skills
# folder, so a corporate skill would replace one of theirs with the same name.
# Report which corporate skills they have their own of (names only); the server
# leaves those out of their catalogue. Same scan rules as OpenCode
# (config/plugin/skill-scan.ts): a folder holding SKILL.md, or a top-level .md.
report_own_skills() {
  local base="${XDG_CONFIG_HOME:-$HOME/.config}/opencode" index dir own="" json="" name
  index="$(mktemp)"
  # Every published name, not our catalogue: that one already leaves out what we
  # reported last time, and comparing against it would hand those back.
  if ! curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVER/bootstrap/skill-names" -o "$index" 2>/dev/null; then
    rm -f "$index"
    return 0
  fi
  for name in $(
    for dir in "$base/skill" "$base/skills"; do
      [ -d "$dir" ] || continue
      (cd "$dir" && find . -maxdepth 7 -name SKILL.md -not -path '*/.*' 2>/dev/null) |
        while read -r file; do basename "$(dirname "$file")"; done
      (cd "$dir" && find . -maxdepth 1 -type f -name '*.md' ! -name SKILL.md 2>/dev/null) |
        while read -r file; do basename "$file" .md; done
    done | sort -u
  ); do
    case "$name" in "" | . | *[!A-Za-z0-9._-]*) continue ;; esac
    if grep -qxF "$name" "$index"; then
      own="$own $name"
      json="$json${json:+,}\"$name\""
    fi
  done
  rm -f "$index"
  curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"names\":[$json]}" "$SERVER/bootstrap/own-skills" >/dev/null 2>&1 || true
  for name in $own; do
    say "skill $name: you have your own, so the company's version is not used"
  done
}

# "machine" variables: `opencode service set env` stops the service -- and this
# command runs inside it -- so the decisions are made here, read-only, and a
# detached job applies them a few seconds after the login has returned.
sync_machine_env() {
  local listing plan next job name encoded value current ours changes=0
  listing="$(mktemp)"
  if ! curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVER/bootstrap/env" -o "$listing" 2>/dev/null; then
    rm -f "$listing"
    return 0
  fi
  if [ ! -s "$listing" ] && [ ! -s "$MANAGED" ]; then
    rm -f "$listing"
    return 0
  fi
  if [ -z "$OPENCODE_BIN" ]; then
    say "cannot find the opencode binary, so the machine environment was not updated"
    rm -f "$listing"
    return 0
  fi
  plan="$(mktemp)"
  next="$(mktemp)"
  while read -r name encoded; do
    [ -n "$name" ] || continue
    employee_owns "$name" && continue   # already reported above
    value=$(printf '%s' "$encoded" | base64 --decode 2>/dev/null) || continue
    if [ "$(service_value "$name")" != "$value" ]; then
      printf 'set %s %s\n' "$name" "$encoded" >> "$plan"
      changes=$((changes + 1))
    fi
    printf '%s %s\n' "$name" "$(digest_of "$value")" >> "$next"
  done < "$listing"
  # Names this script set before and the server no longer sends: unset them,
  # unless the employee changed the value since (then it is theirs).
  if [ -f "$MANAGED" ]; then
    while read -r name ours; do
      [ -n "$name" ] || continue
      grep -q "^$name " "$listing" && continue
      current=$(service_value "$name")
      if [ -n "$current" ] && [ "$(digest_of "$current")" = "$ours" ]; then
        printf 'unset %s\n' "$name" >> "$plan"
        changes=$((changes + 1))
      fi
    done < "$MANAGED"
  fi
  rm -f "$listing"
  mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
  if [ "$changes" -eq 0 ]; then
    mv "$next" "$MANAGED"
    rm -f "$plan"
    return 0
  fi
  job="$(mktemp)"
  cat > "$job" <<'JOB'
#!/usr/bin/env bash
# Applies the machine environment plan once the login that spawned it is done.
set -u
bin="$1" plan="$2" next="$3" managed="$4"
sleep "${OPENCODE_CORP_ENV_DELAY:-3}"
running=0
[ "$("$bin" service status 2>/dev/null | head -n 1)" != "stopped" ] && running=1
while read -r action name encoded; do
  case "$action" in
    set) "$bin" service set env "$name" "$(printf '%s' "$encoded" | base64 --decode)" >/dev/null 2>&1 ;;
    unset) "$bin" service unset env "$name" >/dev/null 2>&1 ;;
  esac
done < "$plan"
mv "$next" "$managed"
rm -f "$plan" "$0"
[ "$running" = 1 ] && "$bin" service start >/dev/null 2>&1
exit 0
JOB
  # Its own session: stopping the service must not take the job down with it.
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$job" "$OPENCODE_BIN" "$plan" "$next" "$MANAGED" </dev/null >/dev/null 2>&1 3>&- &
  else
    nohup bash "$job" "$OPENCODE_BIN" "$plan" "$next" "$MANAGED" </dev/null >/dev/null 2>&1 3>&- &
  fi
  say "machine environment: $changes change(s) to apply; OpenCode restarts in a few seconds to pick them up"
}

report_own_skills || true

if [ "${OPENCODE_CORP_SKIP_ENV:-0}" != "1" ]; then
  OPENCODE_BIN=$(opencode_binary || true)
  report_own_env || true
  sync_machine_env || true
fi

say "logged in; the corporate configuration loads below your own settings"
printf '%s' "$TOKEN" >&3
