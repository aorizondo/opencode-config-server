# Security model

## What this protects

The corporate configuration document, the skill feed and the bootstrap
installer, all behind static credentials. Everything an authenticated employee
can read, every authenticated employee can read: there is one configuration for
the whole company.

## What is public

Only `/.well-known/opencode` and `/health`.

The manifest has to be public — the harness fetches it with no credentials
(`wellknown.ts:60`), because it is what explains how to authenticate. It
contains the login script and the config URL, and no secret. Treat the script as
published: it is, to anyone who can reach the server.

`/health` reports counts and problem descriptions, never names, hashes or
tokens.

## Credentials

| Artefact | At rest | In transit |
| --- | --- | --- |
| Password | PBKDF2-HMAC-SHA256, 600 000 iterations, 16-byte salt | HTTP Basic over TLS |
| API key | SHA-256 digest | `X-Api-Key` over TLS |
| Session token | not stored — stateless HMAC-SHA256 | `Authorization: Bearer`, and a URL path segment for skills |

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
- [ ] `config/` mounted read-only — the server never writes to it
- [ ] container as non-root, `cap_drop: ALL`, `read_only: true`
- [ ] access logs for `/skills/` redacted
- [ ] `OCS_TRUST_PROXY` true only behind a proxy that sets `X-Forwarded-For`
- [ ] rate limiting or lockout at the edge if you run more than one replica
- [ ] employees told to keep `~/.config/opencode-corp/credentials` at `0600`
      (the login script refuses to read it otherwise)

## Reporting

Security issues in this server go to the platform team, not to a public issue
tracker.
