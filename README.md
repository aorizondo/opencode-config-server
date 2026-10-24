# OpenCode Enterprise Config Server

One corporate OpenCode configuration, served to every employee's harness over the
mechanism OpenCode v2 already ships: the `/.well-known/opencode` manifest.

- **Nothing local is ever overwritten.** The harness ranks a well-known document
  as its *lowest* priority source, below `~/.config/opencode` and below any
  project config. Nothing is written to the employee's disk to deliver settings.
- **Agents, commands, skills, MCP servers, permissions and plugins** all travel as
  data. Only tooling that must exist as a local binary needs the installer.
- **Static credentials**: a username and password, or an API key. No identity
  provider, no OAuth round trip.
- **Changes land within 10 minutes**, in running sessions, with no restart and no
  re-login.

---

## Run it locally in five minutes

```bash
git clone <this repo> && cd opencode-config-server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# 1. Create an account. The command prints a hash; paste it into users.json.
cp config/users.json.example config/users.json
python -m opencode_config_server.passwd ana.ruiz

# 2. Start the server.
OCS_BASE_URL=http://localhost:8000 python main.py
```

Check it in another terminal:

```bash
curl -fsS http://localhost:8000/health
# {"status":"healthy","service":"opencode-enterprise-config","users":1,"skills":1,"problems":[]}
```

`status: "degraded"` is not a failure — `problems` tells you exactly what to fix
(no accounts yet, an unparseable config, a missing signing secret).

Now connect a machine that has OpenCode v2:

```bash
opencode auth login http://localhost:8000
```

OpenCode prints a sign-in address and a code, and opens the browser when it can.
The employee signs in there — username and password, a corporate API key, or a
Bifrost virtual key — and OpenCode finishes on its own:

```
│  opencode-corp: sign in from your browser to finish the login:
│  opencode-corp:     http://localhost:8000/login?code=U86XN7R8
│  opencode-corp:     code: U86X-N7R8
◆  Connected to localhost
```

That is the whole employee experience. Their own settings keep winning; the
corporate ones fill in everything they have not set.

**Machines without a browser** (CI, build agents) skip the browser by
providing a credential up front, in this order of precedence:
`OPENCODE_CORP_TOKEN` (an API key), then `OPENCODE_CORP_USER` +
`OPENCODE_CORP_PASSWORD`, then a `~/.config/opencode-corp/credentials` file
(mode `600`). Mind *where* those variables live: the login command is started by
the OpenCode **server**, with the server's environment. Exported in your shell
they only reach it with `opencode auth login --standalone`, which starts a
private server from that shell. `OPENCODE_CORP_NO_BROWSER=1` prints the address
without trying to open it.

<details>
<summary><b>If the login fails</b> — the four things it is, almost always</summary>

| What you see | What it is |
| --- | --- |
| `Failed to load wellknown manifest` | the server is unreachable, or you put `/.well-known/opencode` behind auth — it must be public, it is what explains *how* to authenticate |
| `No authentication method found at …` | the manifest has no `auth` block |
| `Authentication command returned no credential` | the login script printed nothing on stdout. Its stderr is in the TUI and says why: a world-readable credentials file, a rejected password |
| `the login code expired before it was approved` | nobody signed in at the printed address within `OCS_DEVICE_TTL` (5 minutes by default); run the login again |
| login works, no corporate settings appear | the token is being rejected by `/config/opencode.json`. Check `OCS_SESSION_ENV` matches the `{env:…}` placeholder in `config/opencode.jsonc` |

</details>

## Going to production

```bash
cp .env.example .env
python -m opencode_config_server.secret >> .env   # OCS_SESSION_SECRET
$EDITOR .env                                      # OCS_BASE_URL must be https://
docker compose up -d
curl -fsS https://cfg.corp.example/health
```

The server refuses to start with `OCS_ENV=production` unless the base URL is
`https` and a signing secret is set — session tokens travel in headers and inside
skill URLs, so plain HTTP would hand them to anyone on the path.

TLS terminates in front of the container; the app itself listens on
`127.0.0.1:8000`. Full checklist in [`docs/SECURITY.md`](docs/SECURITY.md#hardening-checklist).

## The admin panel

Set `OCS_ADMIN_TOKEN` and open `/admin`: a web panel for everything below,
without touching the files by hand.

```bash
python -m opencode_config_server.secret --admin >> .env   # OCS_ADMIN_TOKEN
docker compose -f docker-compose.yml -f docker-compose.admin.yml up -d
```

| Section | What it edits |
| --- | --- |
| Configuration | `opencode.jsonc` through forms — model and defaults, agents, slash commands, MCP servers, permissions and policies, plugins — or as raw text in the Advanced tab, where comments survive |
| Users | create accounts, set passwords, issue API keys (shown once), disable, sign out everywhere, delete |
| Skills | create skills, edit their files, see what is published and at which version |
| Installer | `bootstrap/install.sh`, checked with `bash -n` before it is saved |

It writes the same files you would edit by hand, so both keep working. Every
save is checked first (the shapes OpenCode v2 accepts, `{file:~/.ssh…}` refused,
literal secrets in MCP headers flagged), written atomically, refused if someone
else changed the file meanwhile, and the previous version is kept in
`config/.history` with a one-click restore. Without `OCS_ADMIN_TOKEN` the panel
is not mounted at all. Details in [`docs/OPERATIONS.md`](docs/OPERATIONS.md#the-admin-panel)
and [`docs/SECURITY.md`](docs/SECURITY.md#the-admin-panel).

## Day-to-day: I want X, I do Y

| I want to… | Do this |
| --- | --- |
| add an employee | `python -m opencode_config_server.passwd <name>`, paste the hash into `config/users.json` |
| add a CI or headless account | `python -m opencode_config_server.passwd --api-key`, paste the digest, hand the key over once |
| revoke one person's tokens | bump their `version` in `users.json` |
| suspend someone | `"disabled": true` |
| revoke absolutely everyone | rotate `OCS_SESSION_SECRET` |
| change a corporate default | edit `config/opencode.jsonc` |
| publish engineering standards | drop a `SKILL.md` in `config/skills/<name>/` |
| install a tool on every machine | add a step to `config/bootstrap/install.sh` and bump its `VERSION` |

Everything above takes effect without a restart: `users.json` on the next
request, the configuration and skills on each client's next refresh. Each row
also has a button in the [admin panel](#the-admin-panel).

## What can be delivered, and who wins

| Need | Where it goes | If the employee also sets it |
| --- | --- | --- |
| Agents (prompt, model, mode) | `agents.<id>` | they win, field by field |
| Slash commands | `commands.<name>.template` | theirs wins by name |
| Skills (standards, docs, runbooks) | `config/skills/`, served over HTTP | theirs wins by name |
| MCP servers | `mcp.servers.<name>` | theirs wins by name |
| Permission rules | `permissions[]`, accumulated | their rule is evaluated last |
| Providers and models | `providers.<id>` | they win, field by field |
| Plugins | `plugins[]`, npm specs, auto-installed | they can remove yours with `"-<package>"` |
| `model`, `share`, `experimental`, … | top-level keys | **all or nothing** — if they set the key, your value is ignored entirely |

That last row is the one to design around: scalar keys cannot be partially
imposed. And because local always wins, an employee *can* opt out of anything,
including your plugin. If a setting has to be mandatory, enforce it outside
OpenCode — MDM, a managed image, CI. The two properties cannot both hold.

## How it works

```
  employee                                      this server
     │  opencode auth login https://cfg.corp
     ├─── GET /.well-known/opencode ──────────▶  public manifest, no secrets
     │◀── {auth:{command,env}, remote_config} ─
     │
     │    runs auth.command: bash, no shell profile, stdin closed
     ├─── POST /auth/device ──────────────────▶  login code, nobody signed in yet
     │◀── {device_code, user_code, …} ────────
     │    prints /login?code=… — the employee signs in in a browser,
     │    which posts the code + credentials to /login/approve
     ├─── POST /auth/device/token (polls) ────▶  answered once the code is approved
     │◀── {"token":"v1.…"} ───────────────────   signed, stateless, 90 days
     │    (CI: POST /auth/token with the credential instead of the browser)
     │    runs the bootstrap installer (hindsight, …)
     │    prints the token on stdout → stored by the harness as a credential
     │
     │    on startup, every 10 minutes, and on every config reload:
     ├─── GET /config/opencode.json ──────────▶  Bearer <token>
     │◀── the corporate configuration ─────────
     ├─── GET /skills/<token>/index.json ─────▶  token in the path, see below
     │◀── skills, cached and versioned ────────
```

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/.well-known/opencode` | none | discovery manifest |
| `POST` | `/auth/token` | Basic or `X-Api-Key` | static credentials → session token |
| `POST` | `/auth/device` | none | start a browser login: device code + user code |
| `POST` | `/auth/device/token` | device code | polled; the session token once the code is approved |
| `GET` | `/login` | none | the browser sign-in page |
| `POST` | `/login/approve` | credentials in body | approve a user code with a password, API key or Bifrost key |
| `GET` | `/config/opencode.json` | `Bearer` | the corporate configuration |
| `GET` | `/skills/{token}/index.json` | token in path | remote skill index |
| `GET` | `/skills/{token}/{name}/{file}` | token in path | remote skill file |
| `GET` | `/bootstrap/install.sh` | `Bearer` | one-off installer |
| `GET` | `/health` | none | self-check, no secrets |

### Three constraints the harness imposes

Worth knowing before you debug anything, and all three are verified by tests:

1. **The manifest must be public.** `inspect()` fetches it with no credentials
   ([`wellknown.ts:60`][wellknown]) — it is the thing that tells a client how to
   authenticate.
2. **The login command's stdout *is* the credential**, trimmed
   ([`integration.ts:620-650`][integration]). It runs through execve with no
   shell and with stdin closed, so it cannot prompt, and `bash -l` is out: a
   chatty shell profile would end up inside the token. The script routes
   everything to stderr and keeps the token on a separate descriptor.
3. **Skill downloads carry no headers at all** ([`skill/discovery.ts`][skills]),
   so the token rides in the URL path. A query string would be dropped. It will
   therefore appear in proxy access logs — redact `/skills/`, or set
   `OCS_SKILLS_ENABLED=false` and ship standards through the installer instead.

[`docs/PROTOCOL.md`](docs/PROTOCOL.md) has the rest, read off the v2 sources with
file and line references: merge regimes per key, why `version` is mandatory in
the skill index, what `{file:…}` does and does not do, and how v1 differs.

## What IT edits

```
config/
  opencode.jsonc          the corporate configuration (JSON with comments)
  users.json              accounts: password hashes, API key digests, revocation
  skills/<name>/SKILL.md  skills delivered over HTTP, never installed on disk
  bootstrap/install.sh    third-party tooling that must exist as a local binary
```

Two placeholders are resolved server side: `{server_url}` and `{session_token}`.
`{env:NAME}` and `{file:path}` are passed through for the harness to resolve on
the employee's machine.

> Write access to `config/` is equivalent to code execution on every employee's
> machine — `plugins[]` runs in their harness, `install.sh` runs on their shell.
> Review changes to it like you review code.

## Tests

```bash
pytest                              # 268 tests
pytest tests/test_login_e2e.py      # the real login script against a real server
pytest tests/test_harness_schema.py # the served document against OpenCode's own schema
```

Two suites check more than our own assumptions:

- `test_login_e2e.py` boots the app on a loopback port and runs the exact
  `auth.command` from the manifest with a temporary `HOME`: only the token on
  stdout, progress on stderr, the right exit code per failure, and a broken
  installer that does not cost the employee their session.
- `test_harness_schema.py` feeds the served document to OpenCode's own
  `ConfigNormalize.normalize` and `Schema.decodeUnknownOption(Info)` and demands
  zero diagnostics — proof the harness is not silently dropping half of it. Needs
  `bun` and an OpenCode checkout (`OPENCODE_SOURCE=…`); skips itself otherwise.

## Documentation

| | |
| --- | --- |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | the harness contract, verified against the v2 sources, with `file:line` references |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | the admin panel, accounts, revocation, publishing skills, rotating secrets, troubleshooting |
| [`docs/SECURITY.md`](docs/SECURITY.md) | trust model, what a leaked token buys, hardening checklist |
| [`docs/MIGRATION.md`](docs/MIGRATION.md) | what changed from 1.x and why that design could not work |
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | layout, conventions, where to add a test |

`legacy/` holds the 1.x implementation for reference. `examples/opencode.full.jsonc`
is a large real-world configuration to crib from — review it before serving it, it
carries absolute paths from one workstation.

[wellknown]: https://github.com/sst/opencode/blob/dev/packages/core/src/wellknown.ts
[integration]: https://github.com/sst/opencode/blob/dev/packages/core/src/integration.ts
[skills]: https://github.com/sst/opencode/blob/dev/packages/core/src/skill/discovery.ts
