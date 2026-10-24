# Operations

## Deploying

```bash
cp .env.example .env
python -m opencode_config_server.secret >> .env     # OCS_SESSION_SECRET
$EDITOR .env                                        # OCS_BASE_URL must be https
cp config/users.json.example config/users.json
docker compose up -d
curl -fsS https://cfg.corp/health | jq
```

`/health` is the deployment self-check. `status: "degraded"` lists exactly what
is wrong — a missing user database, an unparseable corporate document, an
ephemeral signing secret — without revealing any of it to an attacker.

TLS terminates in front of the container; the app speaks plain HTTP on
`127.0.0.1:8000`. The server refuses to start with `OCS_ENV=production` unless
`OCS_BASE_URL` is `https://` and `OCS_SESSION_SECRET` is set.

## The admin panel

`/admin` is a web panel over the same files this document describes. It is
mounted only when `OCS_ADMIN_TOKEN` is set:

```bash
python -m opencode_config_server.secret --admin >> .env   # OCS_ADMIN_TOKEN
sudo chown -R 1000:1000 config                            # the container's user
docker compose -f docker-compose.yml -f docker-compose.admin.yml up -d
```

The override remounts `config/` read-write; the default compose file keeps it
read-only, which is still the right choice when the panel is off. Without write
access the panel opens in read-only mode and says which file it cannot change.

Sign in with the token. The browser gets a session cookie that lasts
`OCS_ADMIN_SESSION_HOURS` (8 by default, 1–24); scripts send the token as
`Authorization: Bearer …` to the same JSON API under `/admin/api/`.

| Section | Notes |
| --- | --- |
| Configuration | Forms for model and defaults, agents, commands, MCP servers, permissions and policies, plugins. Saving from a form rewrites `opencode.jsonc` as plain JSON, so its comments go (the panel asks first); the **Advanced** tab edits the file as text and keeps them. **Check & preview** shows the document exactly as employees receive it. |
| Users | API keys are shown once, when created; only their digest is stored and the panel identifies them by a short fingerprint. **Sign out everywhere** bumps `version`. Unknown fields in `users.json` are preserved. A `users.json` that does not parse is never overwritten: fix it by hand first. |
| Skills | Only small text files with a published extension can be created or edited; other files in a skill folder are listed but never shown. Each save changes the skill's version, so employees re-download it. |
| Installer | Refused if `bash -n` reports a syntax error. |

**Every save** is validated, written atomically (temp file + rename), refused
with "changed since it was loaded" when the file changed after the editor opened
it, and the replaced version goes to `config/.history/` — the last 30 per file,
restorable from each section's **History** button. Old versions of `users.json`
can be restored but are never displayed, since they hold password hashes.

Every change is logged as `admin <action> from <ip>`. There is one shared admin
token, so the log tells you what changed and from where, not who: put the panel
behind your SSO proxy if you need per-person attribution.

## Accounts

`config/users.json`:

```json
{
  "users": [
    { "name": "ana.ruiz", "password": "pbkdf2_sha256$600000$…$…", "version": 1 },
    { "name": "ci-runner", "api_keys": ["sha256:…"], "version": 1 }
  ]
}
```

| Task | How |
| --- | --- |
| Add a password account | `python -m opencode_config_server.passwd ana.ruiz`, paste the hash |
| Add an API key | `python -m opencode_config_server.passwd --api-key`, paste the digest, hand the key over once |
| Revoke one employee's tokens | bump their `version` |
| Suspend an account | `"disabled": true` |
| Remove an account | delete the entry |

All of these take effect on the **next request** — no restart. A revoked or
deleted account loses access to the configuration and to the skill feed
immediately; the employee's next login gets a fresh token.

Plaintext in the `password` field is rejected, loudly: the server answers `503`
and logs the offending user rather than authenticating anyone.

A broken `users.json` keeps the last good snapshot in memory and logs an error,
so a typo during an edit does not lock the company out. If there is no usable
snapshot at all, `/auth/token` answers `503` — never `401`, which would send
employees chasing their own passwords.

## The corporate configuration

`config/opencode.jsonc` is JSON with comments and trailing commas, like the
harness's own config files. Two placeholders are resolved server side:

| Placeholder | Becomes |
| --- | --- |
| `{server_url}` | `OCS_BASE_URL` |
| `{session_token}` | `{env:OPENCODE_CORP_SESSION}`, filled in on the employee's machine |

`{env:NAME}` and `{file:path}` are passed through untouched for the harness to
resolve locally.

Remember the priority rules from [PROTOCOL.md](PROTOCOL.md#4-priority-local-always-wins):
scalar keys like `model` are all-or-nothing, while `agents`, `commands`,
`permissions`, `mcp.servers`, `skills` and `plugins` merge. Set a corporate
`model` and any employee who has their own `model` ignores yours entirely —
usually what you want, occasionally a surprise.

Changes reach running sessions within 10 minutes. Nothing to restart, nothing to
re-login.

## Publishing a skill

```
config/skills/<name>/SKILL.md          required (or <name>.md)
config/skills/<name>/references/*.md   optional, any depth
```

`SKILL.md` needs YAML frontmatter with `name` and `description`; the description
is what makes the harness load it, so describe *when* to use it, not just what
it is.

Published suffixes: `.md .txt .json .yaml .yml .toml .csv .py .sh .js .ts`, up
to 2 MB each. Anything else in the directory — a `.key`, a `.png`, a dotfile —
stays private.

Versions come from a content hash, so an edit invalidates the employee's cache
automatically. A skill directory without an entry file is skipped rather than
served broken.

Verify what clients will see:

```bash
TOKEN=$(curl -fsS -X POST -H "X-Api-Key: $KEY" https://cfg.corp/auth/token | jq -r .token)
curl -fsS "https://cfg.corp/skills/$TOKEN/index.json" | jq
```

## The bootstrap installer

`config/bootstrap/install.sh` runs once per employee, on login, for things that
must exist as local binaries. The shipped version installs `hindsight`.

Three rules, enforced by tests:

- **Idempotent.** It runs again on every fresh login; the version stamp in
  `~/.local/state/opencode-corp/bootstrap.version` short-circuits it.
- **Never fatal.** It runs without `set -e`, and the login survives a failed
  step with a warning. An employee must never lose their session to a broken
  installer.
- **Bump `VERSION`** when you change the steps, or clients that already ran it
  will skip the new ones.

Its stdout is already redirected to stderr by the caller, so it can print
freely; the session token travels on a separate descriptor and cannot be
corrupted by it.

Set `OCS_BOOTSTRAP_ENABLED=false` to stop serving it entirely.

## Rotating the signing secret

Rotating `OCS_SESSION_SECRET` invalidates **every** issued token at once. Each
employee's next config refresh fails with a logged warning and they must run
`opencode auth login` again. Do it on compromise, not on a schedule.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `opencode auth login` fails with "Failed to load wellknown manifest" | `/.well-known/opencode` unreachable, or behind auth — it must be public |
| "No authentication method found at …" | the manifest has no `auth` block |
| Login fails with "Authentication command returned no credential" | the login script printed nothing on stdout; check its stderr in the TUI |
| "the login code expired before it was approved" | nobody signed in at the printed address in time; raise `OCS_DEVICE_TTL` (max 540 s) if employees need longer |
| The browser does not open | the OpenCode server has no `DISPLAY`/`WAYLAND_DISPLAY` (a headless service); the address is printed, open it by hand |
| "Bifrost virtual keys are not accepted by this server" | `OCS_BIFROST_URL` is not set |
| "cannot validate the virtual key right now (Bifrost unavailable)" (`503`) | `OCS_BIFROST_URL` unreachable, or Bifrost answered something other than 200/401/403 |
| The panel says a file is read-only | `config/` is mounted `:ro`, or not owned by uid 1000 inside the container; use `docker-compose.admin.yml` and `chown` |
| "changed since it was loaded" when saving in the panel | another admin, or a hand edit, saved the file after you opened it; reload and redo the change |
| `/admin` answers 404 | `OCS_ADMIN_TOKEN` is not set |
| A CI login waits for a browser instead of using its credential | the credential variables are not in the OpenCode server's environment; use `opencode auth login --standalone` |
| Login succeeds but no corporate settings appear | the token is rejected by `/config/opencode.json`; check `OCS_SESSION_ENV` matches the `{env:…}` in the document |
| Skills never update | the index is served without `version`, or the employee's cache under `~/.cache/opencode/skills` predates it |
| Everything works for one employee and not another | the other employee has the same keys set locally — local wins, by design |

Server logs record the subject of every issued token, every rejection and every
served document. They never record a token, a password or a hash.

## Operational limits

- The rate limiter is per process and in memory. Behind more than one replica,
  or for a real lockout policy, do it at the edge.
- The admin panel serialises writes per process. Two replicas editing the same
  file are still caught by the ETag check, but run the panel on one replica.
- Pending browser logins are in memory too. A restart, or a second replica
  answering the poll, costs the employee a retry of `opencode auth login`; run
  one replica, or pin `/auth/device*` and `/login*` to one.
- `OCS_TRUST_PROXY=true` makes the limiter believe `X-Forwarded-For`. Enable it
  only when the proxy in front sets that header itself, otherwise any client can
  forge its own bucket.
