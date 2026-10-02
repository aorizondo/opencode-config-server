# Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

cp config/users.json.example config/users.json
python -m opencode_config_server.passwd dev        # paste the hash in
OCS_BASE_URL=http://localhost:8000 python main.py
```

`OCS_ENV` defaults to `development`, which generates a throwaway signing secret
on each start. Tokens stop working on restart; `/health` says so.

## Layout

```
opencode_config_server/
  app.py            routes, auth dependencies, error mapping
  auth.py           password hashing, API keys, signed session tokens, rate limit
  manifest.py       the /.well-known/opencode document
  login_script.sh   what the harness executes on login (inlined into the manifest)
  configdoc.py      reads config/opencode.jsonc, injects server-side placeholders
  skills.py         index.json and file serving for remote skills
  jsonc.py          JSON with comments and trailing commas
  settings.py       environment-driven settings, with production guards
  passwd.py         `python -m opencode_config_server.passwd`
  secret.py         `python -m opencode_config_server.secret`
config/             what IT edits; mounted read-only in production
legacy/             the 1.x implementation, kept for reference only
```

## Tests

```bash
pytest                              # everything, ~4s
pytest tests/test_login_e2e.py -v   # the real login script against a real server
pytest -k manifest                  # the harness contract
```

`tests/test_login_e2e.py` boots uvicorn on a loopback port and runs the exact
argv from the manifest with a temporary `HOME`. It needs `bash` and `curl`, and
skips itself when either is missing.

### Where to add a test

| Change | Test to extend |
| --- | --- |
| the manifest shape | `test_manifest.py` — `assert_decodes_as_v2_manifest` mirrors the harness schema |
| the login script | `test_login_e2e.py`, and the string assertions in `test_manifest.py` |
| credentials or tokens | `test_auth_token.py`, `test_session_tokens.py` |
| the corporate document | `test_config_endpoint.py` |
| skills | `test_skills.py`, plus `TestSkillPathSafety` in `test_health_and_settings.py` |
| anything read at runtime from a new file | `test_packaging.py` — it proves the file reaches the image |

## Conventions

- No `from __future__ import annotations` in `app.py`: FastAPI resolves string
  annotations against module globals, and the dependencies there are closures
  over `create_app` locals. With deferred annotations FastAPI stops seeing the
  `Depends(...)` markers and treats them as query parameters — every
  authenticated endpoint starts answering `422`.
- Dependencies stay minimal: `fastapi` and `uvicorn`. Tokens are stdlib HMAC.
- Anything asserted about the harness belongs in `docs/PROTOCOL.md` with a
  `file:line` reference to the opencode sources, not in a comment.

## Linting

```bash
ruff check . && ruff format --check .
```
