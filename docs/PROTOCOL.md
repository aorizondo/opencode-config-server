# The harness contract

Everything here was read off the OpenCode v2 sources (v2.0.14) rather than
inferred from documentation. Paths are relative to the opencode repository.

## 1. Discovery

`packages/core/src/wellknown.ts:12-32`

```ts
Auth         = Schema.Struct({ command: Schema.Array(Schema.String), env: Schema.String })
RemoteConfig = Schema.Struct({ url: Schema.String, headers: Schema.optional(Record(String,String)) })
Manifest     = Schema.Struct({ auth: optional(Auth), config: optional(NullOr(Config)), remote_config: optional(RemoteConfig) })
```

`inspect()` (`wellknown.ts:60-67`) fetches `<origin>/.well-known/opencode` **with
no credentials**. The endpoint must therefore be public: it is what tells the
client how to authenticate in the first place.

`command` is an argv array, not a command line, and `env` is the *name* of the
variable the token will be exposed as. `add()` (`wellknown.ts:147-158`) refuses
an origin whose manifest has no `auth`, so `opencode auth login` fails outright
on a manifest that misses it.

Our implementation: `opencode_config_server/manifest.py`.

## 2. The login command

`packages/core/src/integration.ts:595-650`

```ts
processes.spawn(ChildProcess.make(method.command[0], method.command.slice(1), {
  extendEnv: true,
  stdin: "ignore",
}))
```

- **execve, no shell.** `["bash", "-c", script, argv0, …args]` is how shell
  logic gets in. `-l` is avoided on purpose: a login shell sources the
  employee's profile, and anything it prints lands in the credential.
- **stdout, trimmed, becomes the credential.** Empty stdout is an error
  ("Authentication command returned no credential").
- **stderr is streamed to the user** as the attempt's message, and is what they
  see when it fails.
- **Non-zero exit fails the login**, with the collected stderr as the message.
- **stdin is `/dev/null`**: the command cannot prompt.
- **The environment is the OpenCode server's**, not the employee's shell
  (`extendEnv: true` on the server process). With the background service,
  variables exported before `opencode auth login` never reach the command;
  `--standalone` starts a private server from the calling shell
  (`packages/cli/src/services/standalone.ts`, also `extendEnv: true`), so there
  they do.
- **Both the CLI and the TUI show stderr live**, one completed line at a time
  (`packages/cli/src/commands/handlers/auth/login.ts`,
  `packages/tui/src/component/dialog-integration.tsx`). That is what makes a
  browser login possible: the command prints an address and a code, then waits.
- **The attempt is abandoned after 10 minutes**, so a login code has to expire
  well before that (`OCS_DEVICE_TTL`, at most 540 s).

Our command therefore signs in through the browser, shaped after RFC 8628:

```
script                         server                          browser
POST /auth/device        ->    device_code + user_code
prints /login?code=…                                     ->    GET /login?code=…
                                                         <-    POST /login/approve
POST /auth/device/token  ->    400 authorization_pending        (password, API key
  … every `interval` s   ->    200 {"token": …}, exactly once    or Bifrost key)
```

The device code is the script's secret and never leaves it; the user code is
what the employee compares in the browser. Polling errors follow RFC 8628 §3.5
(`authorization_pending`, `slow_down`, `expired_token`, `access_denied`). A
credential in the environment or in `~/.config/opencode-corp/credentials`
skips the browser and goes straight to `POST /auth/token` — that is the CI path.

The command runs **only on an explicit login**, never on a refresh. Anything
that must stay current cannot live here.

Our implementation: `opencode_config_server/login_script.sh`, exercised
end-to-end in `tests/test_login_e2e.py`.

## 3. Loading the remote configuration

`packages/core/src/config.ts:145-170`

```ts
const credential = (yield* credentials.list(entry.integrationID)).at(-1)
if (!credential || credential.value.type !== "key") return []
const variables = { [auth.env]: credential.value.key }
const configs = yield* wellknown.resolve(entry, variables)
```

`resolve()` (`wellknown.ts:77-96`) substitutes `{env:NAME}` in the
`remote_config` URL and headers, then fetches it. The fetched document is then
run through `ConfigVariable.substitute` again with the same variables, so
`{env:<auth.env>}` also works *inside* the configuration — that is how the skill
URL gets the employee's token.

Only credentials of type `key` are accepted. There is no refresh path for a
command-issued credential: when the token expires the corporate config silently
stops loading (a logged warning, nothing user-facing). Hence long-lived tokens
plus server-side revocation.

## 4. Priority: local always wins

`packages/core/src/config.ts:229-236`, documented at `config.ts:29`
("*from lowest to highest priority*"):

```
[ wellknown, global(~/.config/opencode), explicit(--config), direct, project, content ]
```

Two different merge regimes apply on top of that ordering:

| Key | Mechanism | Effect |
| --- | --- | --- |
| `agents.<id>` | per-field, per document, in order (`config/plugin/agent.ts:95-125`) | corporate defaults, local overrides survive field by field; `update()` creates ids that do not exist |
| `commands.<name>` | `add()` per name (`config/plugin/command.ts:74`) | inline `template`, no files needed |
| `skills[]` | accumulated; http(s) entries become URL sources (`config/plugin/skill.ts:96-99`) | remote skills |
| `mcp.servers.<name>` | by name, later document wins (`config/plugin/mcp.ts:47-55`) | overridable |
| `plugins[]` | concatenated across documents (`plugin/skill.ts:72-78`) | `"-<pkg>"` removes one (`config/plugin/source.ts:117-119`) |
| `permissions[]` | accumulated with `push`, evaluated `findLast` | corporate rules are a floor, not a lock |
| `model`, `share`, `experimental`, `tool_output`, … | `Config.latest` → `findLast` | **all or nothing**: if the employee sets the key, the corporate value is ignored entirely |

That last row is the one to design around: scalar keys cannot be
*partially* imposed.

## 5. Remote skills

`packages/core/src/skill/discovery.ts:92-200`

```
GET <base>/index.json     -> { skills: [ { name, version?, files: [...] } ] }
GET <base>/<name>/<file>  -> file contents
```

- Cached under `<global.cache>/skills/<hash(base)>/<name>/`.
- **No headers are sent.** The token must ride in the path; a query string is
  dropped by `new URL("index.json", base)`.
- Files whose origin differs from the index origin are refused
  (`discovery.ts:131`), as are unsafe names and paths (`discovery.ts:16-46`).
- A skill without `SKILL.md` or `<name>.md` is dropped.
- **`version` is effectively mandatory.** Without it the branch at
  `discovery.ts:152` only downloads files that are missing, so an employee keeps
  the first copy they ever fetched. We derive it from a content hash
  (`opencode_config_server/skills.py`).

## 6. Refresh cadence

`packages/core/src/config.ts:308-325` re-inspects every manifest every 10
minutes and reloads the configuration. A reload is also triggered by
`credential.switched` and by `wellknown.updated`. No hashes, no state files, no
tarballs: the client simply re-fetches.

## 7. What `{file:…}` does and does not do

`packages/core/src/config/variable.ts:56-58` resolves `{file:…}` against the
local filesystem only — there is no remote fetch. A corporate document cannot
ship file *contents* this way; it can only point at paths that already exist on
the employee's machine. This is also why `configdoc.py` refuses to serve a
document referencing `~/.ssh`, `~/.aws` and friends: the harness would happily
read them and a later `mcp` header could carry them back out.

## 8. OpenCode v1

v1 implements the same discovery with a different auth entry
(`packages/opencode/src/config/config.ts:355-395`): an auth record of type
`wellknown` holding `{key, token}`, and `mergeConfigConcatArrays` instead of the
per-domain merge above. The same manifest works for both: v1 ignores `auth` and
reads `config`/`remote_config`.

## 9. Not usable for this

`enterprise.url` in the config schema (`packages/schema/src/config.ts:44`) is
session-sharing configuration, unrelated to configuration distribution.
