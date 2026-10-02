# Migrating from the 1.x design

The 1.x server implemented a protocol the harness does not have. It could not
work against OpenCode v2 in any configuration, and the test suite passed anyway
because every test asserted the invented protocol instead of the real one. The
original files are kept under `legacy/` for reference.

## What was wrong

| 1.x | Reality |
| --- | --- |
| `auth: {type: "command", command: "<string>"}` | `auth: {command: string[], env: string}` — a string fails the schema, so `opencode auth login` dies on "Failed to load wellknown manifest" |
| `/.well-known/opencode` behind `Authorization: Bearer` | the harness fetches it with no credentials; the endpoint that explains how to authenticate cannot require authentication |
| served `config.json` | v2 reads `opencode.json` / `opencode.jsonc` |
| token interpolated into a shell string and passed as `argv` | command injection through the `Authorization` header, and the token visible in `ps` |
| `len(token) >= 10` as the auth check | any ten characters read the whole configuration |
| `bundle.tar.gz` + SHA-256 state + `merge.py` writing into `~/.config/opencode` | unnecessary: the harness already ranks remote config *below* local files and re-fetches every 10 minutes |
| `merge.py` rewriting `~/.opencode.env` | dropped every line that was not `export …`, and wrote `export KEY=${KEY:-}` — self-referential, so the "injected" secrets were always empty |
| `merge.py` never copied into the Docker image | `/cdn/merge.py` answered 404 in every container, breaking login entirely |

## Conceptual change

1.x pushed files onto the employee's disk and tried to merge them with a
"user wins" strategy written by hand. 2.x pushes **data** and lets the harness do
the merging, which it already does correctly:

| 1.x delivery | 2.x delivery |
| --- | --- |
| `config.json` written into `~/.config/opencode` | `GET /config/opencode.json`, loaded as the lowest-priority document |
| `rules/*.md` copied if absent | a skill served over HTTP, cached and versioned by the harness |
| `~/.omo/config.yaml` deep-merged by a script | out of scope: third-party tool config belongs to the bootstrap installer |
| `~/.opencode.env` rewritten | out of scope: the server never had those secrets anyway |
| `bootstrap.sh` doing merge + sync + shell-rc injection | `bootstrap/install.sh`, only for tooling that must exist as a local binary |

Nothing in 2.x writes to `~/.config/opencode`, so there is nothing to merge and
nothing to overwrite.

## What carried over

- `config_root/rules/*.md` became `config/skills/corp-standards/references/*.md`,
  delivered as a skill.
- `config_root/opencode.jsonc`, the large real-world configuration, is kept
  verbatim at `examples/opencode.full.jsonc`. **Review it before serving it**: it
  contains absolute paths from one workstation (for example a local plugin path
  under `/home/…`) that will not exist on an employee's machine.
- The Dockerfile and compose file were rewritten but keep the same shape.

## If you deployed 1.x

Employees who ran the old bootstrap have files on disk that 2.x will not remove:

```
~/.config/opencode/.enterprise_hash     stale, safe to delete
~/.config/opencode/config.json          merged by the old script; v2 ignores the
                                        filename, but check it for corporate
                                        values you no longer want pinned locally
~/.opencode.env                         contains `export KEY=${KEY:-}` lines
~/.bashrc, ~/.zshrc                     one sourcing line for ~/.opencode.env
~/.omo/config.yaml                      merged by the old script
```

None of it breaks 2.x. The one that matters is `~/.config/opencode/config.json`:
anything the old merge wrote there is now *higher* priority than the corporate
document, so an employee can be pinned to an old corporate value without knowing
it. Tell them to delete the keys they did not choose themselves.
