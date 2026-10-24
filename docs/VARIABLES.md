# Variables

How to put values that differ per employee — provider API keys above all — into
the corporate configuration without ever writing them in `opencode.jsonc`.

Verified against OpenCode v2.1.1: `packages/core/src/config.ts`
(`loadWellknownEntry`), `packages/core/src/config/variable.ts`,
`packages/cli/src/services/service-config.ts`.

## The four placeholders

| Write this | Filled in by | With |
| --- | --- | --- |
| `{server_url}` | this server, before sending | this server's public address (`OCS_BASE_URL`) |
| `{session_token}` | this server, as `{env:OPENCODE_CORP_SESSION}` | the employee's session token. Only useful to reach this server (the skills URL uses it) |
| `{env:NAME}` | this server if it defines `NAME`, otherwise OpenCode on the employee's machine | see [Where `{env:NAME}` comes from](#where-envname-comes-from) |
| `{file:~/path}` | OpenCode on the employee's machine | the contents of that file, trimmed. Use `~/` or absolute paths; a relative path resolves against nothing useful in a remote document. A missing file gives an empty string |

They work inside any string of the document: whole values or parts of them
(`"{env:BIFROST_URL}/v1"`). They are never resolved inside comments.

## Where `{env:NAME}` comes from

The rule everywhere: **the employee's own values win over the company's.**

```
{env:NAME}
   │
   ├─ 0. the employee defines NAME themselves        → left as is: theirs is used
   │     (reported by their last `opencode auth login`)
   ├─ 1. a value for this employee on the Variables page    ─┐ filled in by this
   ├─ 2. a value for everyone on the Variables page          ─┘ server, per request
   │
   │     (otherwise the placeholder travels as is)
   │
   ├─ 3. OPENCODE_CORP_SESSION only: the employee's session token
   ├─ 4. the environment of the OpenCode process on the machine
   └─ 5. empty string
```

Steps 0–2 happen here, every time an employee's OpenCode fetches the
configuration (at start-up and every 10 minutes). Steps 3–5 are OpenCode's own
rules for remote documents.

Step 0: at each login, the login command asks for the names of the variables
that apply to the employee (`GET /bootstrap/names`), checks which ones they
already define, and reports those names — never their values — back
(`POST /bootstrap/own-env`). "Already define" means present in the environment
OpenCode runs with (the same one step 4 reads), or in its service configuration,
with a value the login did not put there itself. A change on the machine counts
from the next login.

The file on disk keeps the placeholder; only the copy sent to that employee
carries the value.

## When the employee and the company define the same thing

| Same name or field in… | Wins | How |
| --- | --- | --- |
| the employee's local `opencode.json` and the corporate document | the employee | OpenCode ranks the corporate document below every local file (checked: a local `providers.<id>.settings.apiKey` replaces the corporate one) |
| the employee's environment and a variable on the Variables page | the employee | step 0 above |
| the employee's OpenCode service environment and a "machine" variable | the employee | the login never sets or unsets a variable it did not set itself |
| a "machine" variable the login set, later edited by the employee | the employee | from then on it is theirs: not overwritten, not unset |
| a skill in the employee's own skills folder and a corporate skill with the same name | the employee | the login reports the names it finds in `~/.config/opencode/skill(s)/`, and the server leaves those out of their catalogue (OpenCode would otherwise load the corporate one last and use it) |
| a skill folder inside one project (`.opencode/skills/`) and a corporate skill with the same name | **the company** | the login cannot see every project; give corporate skills names nobody else would use |
| `skills` and `instructions` lists | both | OpenCode adds every document's entries together; an employee can add their own, not remove the company's |

The panel shows, per user, which names they define themselves, and **Preview**
lists them for the chosen user.

## Defining values: the Variables page

Admin panel → **Variables**:

- **Everyone**: shared values, e.g. `BIFROST_URL`.
- **Per user**: pick an employee, add their own values, e.g. their
  `OPENAI_API_KEY`. A user's value replaces the shared one with the same name.
- **Secret**: once saved, the panel never shows the value again — only that it
  is set. To change it, type a new one; to keep it, leave the field empty.
- Names look like environment variables: letters, digits and `_`.
  `OPENCODE_CORP_SESSION` is reserved (the session token).

Changes reach employees on their next refresh, within 10 minutes, without
anyone signing in again. **Preview** on the Configuration page shows the
document exactly as a chosen employee receives it (secret values masked), plus
the names left for their machine to resolve.

Deleting a user deletes their variables. The file is `config/variables.json`;
see [Security](#security).

## Example: a personal OpenAI key per employee

Configuration (Advanced tab, or the file):

```jsonc
"providers": {
  "openai": {
    "settings": { "apiKey": "{env:OPENAI_API_KEY}" }
  }
}
```

Variables → Per user → `ana.ruiz` → `OPENAI_API_KEY` = her key, secret.

Ana receives her key; an employee without a value gets `{env:OPENAI_API_KEY}`
untouched and OpenCode falls back to `OPENAI_API_KEY` on their machine, if any.

## Example: Bifrost as the LLM gateway

Each employee calls Bifrost with their own virtual key, so Bifrost's budgets and
rate limits apply per person. Bifrost's OpenAI-compatible endpoint is
`<bifrost>/v1` and takes the virtual key as `Authorization: Bearer sk-bf-…`
(checked against a real Bifrost: `GET /v1/models` answers 200 with the key, 401
without).

```jsonc
"providers": {
  "bifrost": {
    "name": "Bifrost",
    "package": "@opencode/ai/providers/openai-compatible",
    "settings": {
      "baseURL": "{env:BIFROST_URL}/v1",
      "apiKey": "{env:BIFROST_API_KEY}"
    },
    "models": {
      "claude-sonnet": { "modelID": "anthropic/claude-sonnet-4-5", "name": "Claude Sonnet (Bifrost)" },
      "gpt": { "modelID": "openai/gpt-5", "name": "GPT-5 (Bifrost)" }
    }
  }
},
"model": "bifrost/claude-sonnet"
```

- The key of `models` (`claude-sonnet`) is how OpenCode names the model
  (`bifrost/claude-sonnet`); `modelID` is what Bifrost receives. List what your
  Bifrost serves with `curl -H "Authorization: Bearer $VK" <bifrost>/v1/models`.
- Variables → Everyone → `BIFROST_URL` = `https://bifrost.corp` (not secret).
- Variables → Per user → each employee → `BIFROST_API_KEY` = their virtual key
  (secret). Registering the same key under Users → API keys lets them sign in
  to this server with it too.

## Example: an MCP server with a personal token

```jsonc
"mcp": {
  "servers": {
    "corp-docs": {
      "type": "remote",
      "url": "https://docs.corp/mcp",
      "headers": { "authorization": "Bearer {env:CORP_DOCS_TOKEN}" }
    }
  }
}
```

## Values that must exist on the machine itself

`{env:…}` filled in here only reaches the configuration document. A plugin, a
local MCP server or a shell command that reads `process.env.NAME` needs the
variable in the environment of the OpenCode process itself. OpenCode's
background service keeps its own environment, which you can change by hand:

```bash
opencode service set env HINDSIGHT_API_KEY 'value'   # stored in ~/.config/opencode/service.json, 0600
opencode service get env                              # list them
opencode service unset env HINDSIGHT_API_KEY
```

Or tick **Also set it in the environment of OpenCode on the machine** on the
variable, and the login does it for each employee:

1. After signing in, the login command asks `GET /bootstrap/env` for that
   employee's machine variables (user values over shared ones, as above).
2. It works out, read-only, what to change: set the ones whose value differs,
   skip the ones the employee defines themselves, unset the ones it set before
   that the server no longer sends — only if they still hold the value it put
   there. Nothing to change means no restart.
3. If there is something to change it starts a detached job and returns.
   `opencode service set env` stops the service, and the login runs inside
   that service, so the job waits a few seconds for the login to finish.
4. The job runs `opencode service set env` / `unset env` and starts the service
   again if it was running.

What that means in practice:

- It is applied at **`opencode auth login`**, not every 10 minutes: re-run the
  login on a machine to pick up a change.
- Applying a change **restarts OpenCode** on that machine, interrupting sessions
  in progress there.
- **The employee's values are never touched**: one they set themselves is kept
  (and the login says so), and one the login set and they later changed becomes
  theirs.
- It needs the `opencode` binary of the running service (found from the process
  that runs the login, else `opencode` on `PATH`; set
  `OPENCODE_CORP_OPENCODE_BIN` to force one). `OPENCODE_CORP_SKIP_ENV=1` skips
  both this and step 0.
- `opencode auth login --standalone` uses a private server, not the service:
  the values land in the service's environment for its next start.
- Values pass as arguments to `opencode service set env`, so another account on
  the same machine could glimpse them in the process list for a moment.
- The login's bookkeeping lives in `~/.config/opencode-corp/env.managed`: the
  names it set, with a digest of each value, which is how it recognises its own.

The variable then also satisfies `{env:NAME}` in the configuration, at step 4 of
the order above — but there is no need to tick it for that.

On Windows the PowerShell login does the same, with a hidden PowerShell process
as the detached job. Windows PowerShell 5.1 mangles arguments that contain
double quotes when calling programs, so keep `"` out of machine values.

Mind paths that differ between platforms: the corporate document is the same
for every machine, and `HOME` is not set on Windows. Use a per-user variable
(`{env:PROJECTS_DIR}`) rather than `{env:HOME}/…`.

Do not call `opencode service set env` from `bootstrap/install.sh` (or
`install.ps1`): the
installer runs inside the login, inside that same service, so stopping it aborts
the login.

## Security

- Never write a key literally in the configuration: every employee who signs in
  can read the whole document. The panel warns about literal `apiKey`,
  `authToken`, `accessToken`, `bearerToken` and secret-looking header values.
- `config/variables.json` holds the values in clear, `0600`, like `users.json`
  holds password hashes: keep `config/` out of git (`.gitignore` already
  excludes both) and out of backups you would not trust with the keys.
- A value is sent only to the employee it belongs to (or to everyone, for shared
  ones), over TLS, with `Cache-Control: no-store`. OpenCode keeps the last
  configuration it received on that machine.
- Values cannot contain `{env:…}` or `{file:…}`: OpenCode substitutes again
  after this server, and a value must not be able to make machines read local
  files.
- Old versions of `variables.json` in the history can be restored, never
  displayed. The log records which variable changed, never its value.
- Sessions opened with an unregistered Bifrost key (`vk:<name>`) belong to no
  account, so they only get the values for everyone.
- What an employee defines themselves reaches this server as **names only**
  (`config/.state/employee-env.json`, 0600), never values.

## Where values end up on the employee's machine

| What | Where | When it changes |
| --- | --- | --- |
| The session token | OpenCode's database, `~/.local/share/opencode/opencode.db`, table `credential` (file 0600) | at each `opencode auth login` |
| The configuration with the values filled in | the same database, table `kv`, key `wellknown:config:<server>`; also in memory while OpenCode runs | at start-up and every 10 minutes; kept as is if the server is unreachable |
| Machine variables | `~/.config/opencode/service.json`, `env` (0600), in the service's environment once it restarts | at `opencode auth login`, only if they changed |

These are OpenCode's defaults on every platform, under the user's home;
`XDG_DATA_HOME` and `XDG_CONFIG_HOME` move them
(`packages/util/src/global-roots.ts`).
