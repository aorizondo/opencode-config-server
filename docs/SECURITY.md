# Security model

## What this protects

The corporate configuration document, the skill feed and the bootstrap
installer, all behind static credentials. Everything an authenticated employee
can read, every authenticated employee can read: there is one configuration for
the whole company.

## What is public

Also `/windows/.well-known/opencode` and `/login.ps1`: the Windows manifest and
the PowerShell login script it downloads. The manifest pins the script's
SHA-256, so a script that does not match is never run.

`/.well-known/opencode`, `/health`, the sign-in page `/login`, and the start of
a browser login, `POST /auth/device`.

The manifest has to be public — the harness fetches it with no credentials
(`wellknown.ts:60`), because it is what explains how to authenticate. It
contains the login script and the config URL, and no secret. Treat the script as
published: it is, to anyone who can reach the server.

`/health` reports counts and problem descriptions, never names, hashes or
tokens.

`POST /auth/device` hands out a pair of codes that are worth nothing until
someone approves the user code with valid credentials. The number of pending
logins is capped (1000) and each expires after `OCS_DEVICE_TTL`.

### The browser login

- The **device code** (256 bits) only ever travels between the login command and
  the server; it is what redeems the token, exactly once. The **user code**
  (8 characters from a 27-letter alphabet, ~38 bits) is only good for approving,
  and approving is rate limited like `/auth/token`: a wrong code counts as a
  failed attempt.
- `/login` is a single self-contained page: no external resources, a per-request
  CSP nonce (`default-src 'none'`, `connect-src 'self'`, `frame-ancestors
  'none'`), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and the
  `code` parameter is escaped. Credentials go in a JSON body to
  `/login/approve`, never in a URL.
- The page tells the employee to continue only if they just ran
  `opencode auth login` and the codes match. That is the defence against the
  classic device-flow phish — an attacker starting a login and sending someone
  else the link — and it is a human check, as in every RFC 8628 deployment.

## Credentials

| Artefact | At rest | In transit |
| --- | --- | --- |
| Password | PBKDF2-HMAC-SHA256, 600 000 iterations, 16-byte salt | HTTP Basic over TLS |
| API key | SHA-256 digest | `X-Api-Key` over TLS |
| Session token | not stored — stateless HMAC-SHA256 | `Authorization: Bearer`, and a URL path segment for skills |
| Bifrost virtual key | not stored here — checked against Bifrost on each sign-in | JSON body to `/login/approve`, or `X-Api-Key`; forwarded to Bifrost as `x-bf-vk` |

Password and API key comparisons use `hmac.compare_digest`. An unknown username
is verified against a dummy hash so it costs the same as a wrong password, and
both return the identical message — there is no account-existence oracle.

### The session token in a URL path

Skill downloads carry no headers, so the token appears in the path. The
consequences are real and bounded:

- It will appear in reverse-proxy access logs. Strip or redact the path for
  `/skills/`, or accept that your logs hold valid credentials.
- It cannot be in a query string instead: the harness drops those when resolving
  the index URL.
- A leaked token reads the configuration and the skills. It cannot write
  anything, cannot escalate, and dies when you bump that user's `version`.

If this is unacceptable, set `OCS_SKILLS_ENABLED=false` and distribute standards
through the bootstrap installer instead.

### Revocation

The harness has no refresh mechanism for a command-issued credential
(`config.ts:148` accepts only `type: "key"`), so tokens are long-lived — 90 days
by default. Revocation is therefore server side and checked on **every** request:

1. signature and expiry (`SessionTokens.verify`)
2. the account still exists
3. the account is not disabled
4. the token's `ver` matches the user's current `version`

Bump `version` to kill one user's tokens; rotate `OCS_SESSION_SECRET` to kill
everyone's.

### Variables

`config/variables.json` (0600) holds the values the server fills into
`{env:NAME}` — typically provider API keys — in clear, like `users.json` holds
password hashes. Each value is sent only to the employee it belongs to, or to
everyone for shared ones. Secret values are write-only in the panel, history
versions of the file are restorable but not viewable, and the log names the
variable, never the value. Values cannot contain `{env:…}` or `{file:…}`.
An employee's own environment always wins: the login reports which of these
names they define themselves (names only), and the server leaves those to their
machine; "machine" variables never overwrite or remove a value the employee set.
Details in [VARIABLES.md](VARIABLES.md#security).

### Bifrost virtual keys

With `OCS_BIFROST_URL` set, a key starting with `sk-bf-` is checked with
Bifrost's self-service `GET /api/governance/virtual-keys/quota` (no admin
credential involved; the key is the credential). It is accepted only on `200`
**and** `"is_active": true` — Bifrost also answers `200` for a key that has been
switched off. `401` (unknown) and `403` (expired) are rejections; anything else,
or Bifrost being unreachable, is a `503`, not a bad credential.

The session belongs to `vk:<virtual key name>`. Those names cannot be created in
`users.json`, so a Bifrost key can never pose as a local account.

**A virtual key registered to an account** (pasted in the admin panel, or its
digest added to `api_keys` by hand) signs in as that account instead: the
session is revocable from `users.json` like any other, and lasts
`OCS_TOKEN_TTL_DAYS`. When `OCS_BIFROST_URL` is set, Bifrost must still report
the key active as well, so switching it off in Bifrost stops the next sign-in;
without it, the key is just an API key. A disabled account is refused whatever
Bifrost says.

Reusing a key from another service ties the two together: whoever obtains it
gets both. Only its unsalted SHA-256 is stored here, which is why pasted keys
need at least 20 characters — a real virtual key has 42.

There is no `users.json` entry to revoke, which is the trade-off: **disabling a
virtual key in Bifrost stops new sign-ins, not sessions already issued.** Those
live until `OCS_VK_TOKEN_TTL_DAYS` (7 by default), which is why they are short.
Unsetting `OCS_BIFROST_URL` invalidates all of them at once; rotating
`OCS_SESSION_SECRET` does too, along with everyone else's. The key itself keeps
being enforced by Bifrost for inference, so what lingers is read access to the
corporate configuration.

## The admin panel

Mounted only when `OCS_ADMIN_TOKEN` is set; otherwise `/admin` does not exist.
Treat the token like the `config/` directory it unlocks: it can change what runs
on every employee's machine (plugins, the installer).

- **One shared token**, compared in constant time and rate limited like the
  employee logins (`OCS_RATE_LIMIT_*`, bucket per client). Production refuses to
  start with a token shorter than 32 characters.
- **Browser sessions** are an HMAC-signed expiry in a cookie that is `HttpOnly`,
  `SameSite=Strict`, `Path=/admin`, and `Secure` whenever the base URL is https.
  The signing key is derived from `OCS_SESSION_SECRET` *and* the admin token, so
  rotating either ends every panel session. The cookie never contains the token.
- **CSRF**: requests riding on the cookie must also send `X-OCS-Admin: 1`.
  Another origin cannot add that header without a CORS preflight, and this
  server answers none.
- **The page** is static HTML with an external script and stylesheet under
  `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
  frame-ancestors 'none'`, plus `X-Frame-Options: DENY`, `no-store` and
  `nosniff`. The script builds the DOM with `textContent` only — a test fails if
  `innerHTML` ever appears.
- **Secrets stay out of the browser**: password hashes and API key digests are
  never returned; a new API key is shown exactly once. Old versions of
  `users.json` in the history can be restored but not read. The history
  directory is `0700` and its files `0600`.
- **Session tokens of employees do not open the panel**, and the panel does not
  appear in `/openapi.json`.
- **Audit**: every change is logged as `admin <action> from <ip>`.

What the panel does not give you is per-person accountability: there is one
token. If several people administer the server, put `/admin` behind your SSO
proxy and keep the token as the second factor.

## The server is trusted by every client

An employee who logs in grants this server the ability to set their agents,
permissions, MCP servers, providers and plugin list. That is the point, and it is
also the blast radius. Specifically:

- **A `{file:…}` reference in the configuration is read from the employee's
  disk** by the harness. Combined with an `mcp` header or a provider `baseURL`,
  that is an exfiltration path. `configdoc.py` refuses to serve a document
  mentioning `~/.ssh`, `~/.aws`, `~/.gnupg` or `/etc/shadow`, but that is a
  tripwire against accidents, not a sandbox. Review changes to
  `config/opencode.jsonc` like you review code.
- **`plugins[]` entries execute in the harness process.** An npm spec served
  from here runs on every employee machine. Pin versions.
- **`bootstrap/install.sh` runs arbitrary shell** on first login. Same review
  bar as any deployment script.

Write access to `config/` is equivalent to code execution on every employee's
machine. Protect it accordingly: review, and no shared credentials to the host.

## Employees can opt out

Because the corporate document is the lowest-priority source, an employee can
override any scalar key and can drop the corporate plugin with
`"plugins": ["-<package>"]`. This is inherent to "never overwrite local
settings" — the two cannot both be true. If a setting must be mandatory, enforce
it outside OpenCode: MDM, a managed image, or CI checks.

## Hardening checklist

- [ ] `OCS_ENV=production`, `OCS_BASE_URL` on https, TLS terminated in front
- [ ] `OCS_SESSION_SECRET` from `python -m opencode_config_server.secret`, held
      in a secret store and not in the image
- [ ] `config/users.json` outside the image (`OCS_USERS_FILE`) or mounted
      read-only, `0600`
- [ ] `config/` mounted read-only — unless the admin panel is enabled, which
      needs it writable (`docker-compose.admin.yml`)
- [ ] `OCS_ADMIN_TOKEN` unset, or 32+ characters from
      `python -m opencode_config_server.secret --admin` and kept in a secret
      store; `/admin` reachable only from your network or behind SSO
- [ ] container as non-root, `cap_drop: ALL`, `read_only: true`
- [ ] access logs for `/skills/` redacted
- [ ] `OCS_TRUST_PROXY` true only behind a proxy that sets `X-Forwarded-For`
- [ ] rate limiting or lockout at the edge if you run more than one replica
- [ ] employees told to keep `~/.config/opencode-corp/credentials` at `0600`
      (the login script refuses to read it otherwise)

## Reporting

Security issues in this server go to the platform team, not to a public issue
tracker.
