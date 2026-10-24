"""Variables the server fills into the corporate document, per employee.

The corporate document references a value as `{env:NAME}`, exactly like an
environment variable. When it is served to an employee, this server replaces
the reference with the value defined for that employee, or failing that the
value defined for everyone; a name it does not define is left alone, and
OpenCode resolves it on the employee's machine as usual (from the stored
credential under `auth.env`, then the process environment --
packages/core/src/config.ts loadWellknownEntry, config/variable.ts).

The employee's own values win. At each login the login command reports which
of these names the employee already defines in their own environment -- names
only, never values -- and the server stops filling those in for them, so
OpenCode resolves them from the machine. (Their local opencode.json wins over
the whole corporate document anyway.)

That is how each employee gets their own provider key without it ever being
written in opencode.jsonc:

    "providers": {"openai": {"settings": {"apiKey": "{env:OPENAI_API_KEY}"}}}

`variables.json` (next to users.json, 0600):

    {
      "global": {"BIFROST_URL": {"value": "https://bifrost.corp", "secret": false}},
      "users":  {"ana.ruiz": {"OPENAI_API_KEY": {"value": "sk-...", "secret": true}}}
    }

Values are stored as written -- protect the file like users.json. "Secret" only
means the admin panel never shows the value again once saved.

A variable marked `"machine": true` is also handed to the login command
(GET /bootstrap/env), which sets it in the environment of the employee's
OpenCode background service -- for plugins, local MCP servers and shell tools,
which read the real environment, not the configuration document.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .auth import VIRTUAL_KEY_SUBJECT

#: Environment-variable style, so the same name works if a machine defines it.
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_VALUE_LENGTH = 8192
_REFERENCE = re.compile(r"\{env:([^}]+)\}")


class VariableError(ValueError):
    """A variable name, value or file that cannot be used."""


def check_name(name: str, *, reserved: str) -> None:
    if not NAME.match(name):
        raise VariableError("variable names use letters, digits and _ (like an environment variable)")
    if name == reserved:
        raise VariableError(f"{reserved} is reserved: OpenCode fills it with the employee's session token")


def check_value(value: str) -> None:
    if len(value) > MAX_VALUE_LENGTH:
        raise VariableError(f"values are limited to {MAX_VALUE_LENGTH} characters")
    if "{env:" in value or "{file:" in value:
        # OpenCode substitutes again after us: a value could otherwise make
        # every employee's machine read a local file into the document.
        raise VariableError("values cannot contain {env:…} or {file:…}")


def parse(data: bytes | None) -> dict[str, Any]:
    """The variables document; an absent file is an empty one."""
    if data is None:
        return {"global": {}, "users": {}}
    try:
        document = json.loads(data)
    except ValueError as error:
        raise VariableError(f"variables.json is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise VariableError("variables.json must contain an object")
    document.setdefault("global", {})
    document.setdefault("users", {})
    if not isinstance(document["global"], dict) or not isinstance(document["users"], dict):
        raise VariableError("variables.json needs 'global' and 'users' objects")
    for scope in [document["global"], *document["users"].values()]:
        if not isinstance(scope, dict):
            raise VariableError("each user's variables must be an object")
        for name, entry in scope.items():
            if not NAME.match(name):
                raise VariableError(f"invalid variable name {name!r}")
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                raise VariableError(f"variable {name!r} needs a string value")
    return document


def resolve_entries(
    document: dict[str, Any], subject: str, *, own: set[str] | frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """name -> {value, secret, machine} for one employee: their values over the
    global ones, minus the names in `own` (defined in the employee's environment).

    Sessions opened with an unregistered Bifrost key (`vk:<name>`) have no
    account, so they only get the global values.
    """
    entries = dict(document.get("global", {}))
    if not subject.startswith(VIRTUAL_KEY_SUBJECT):
        entries.update(document.get("users", {}).get(subject, {}))
    return {name: entry for name, entry in entries.items() if name not in own}


def resolve(document: dict[str, Any], subject: str, *, own: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    return {name: entry["value"] for name, entry in resolve_entries(document, subject, own=own).items()}


def apply(value: Any, values: dict[str, str]) -> Any:
    """Replace `{env:NAME}` for every NAME in `values`, inside string values only."""
    if isinstance(value, str):
        return _REFERENCE.sub(lambda match: values.get(match.group(1), match.group(0)), value)
    if isinstance(value, list):
        return [apply(item, values) for item in value]
    if isinstance(value, dict):
        return {key: apply(item, values) for key, item in value.items()}
    return value


def machine_lines(entries: dict[str, dict[str, Any]]) -> str:
    """`NAME base64(value)` per line for the variables marked for the machine.

    Base64 keeps any value safe to read with a plain `read` loop in bash.
    """
    lines = [
        f"{name} {base64.b64encode(entry['value'].encode('utf-8')).decode('ascii')}"
        for name, entry in sorted(entries.items())
        if entry.get("machine")
    ]
    return "".join(f"{line}\n" for line in lines)


def references(value: Any) -> set[str]:
    """Every NAME referenced as `{env:NAME}` in a document."""
    if isinstance(value, str):
        return set(_REFERENCE.findall(value))
    if isinstance(value, list):
        return set().union(*(references(item) for item in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(references(item) for item in value.values())) if value else set()
    return set()


class EmployeeNames:
    """Per employee, names of things they define themselves: variables in their
    environment, or skills in their own skills folder.

    Reported by the login command (names only) and persisted next to the
    configuration, in `.state/`, so a restart of this server does not start
    overriding employees again. Falls back to memory when the directory is not
    writable (e.g. a read-only config mount).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._names: dict[str, list[str]] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._names = {
                    str(subject): [name for name in names if isinstance(name, str) and NAME.match(name)]
                    for subject, names in loaded.items()
                    if isinstance(names, list)
                }
        except (OSError, ValueError):
            pass

    def get(self, subject: str) -> frozenset[str]:
        return frozenset(self._names.get(subject, ()))

    def all(self) -> dict[str, list[str]]:
        return {subject: list(names) for subject, names in self._names.items()}

    def set(self, subject: str, names: set[str]) -> None:
        if names:
            self._names[subject] = sorted(names)
        else:
            self._names.pop(subject, None)
        self._save()

    def forget(self, subject: str) -> None:
        if self._names.pop(subject, None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(json.dumps(self._names, indent=2), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.path)
        except OSError:
            pass  # memory only: still honoured until this server restarts


class VariableStore:
    """Reads variables.json, re-reading it when it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._document: dict[str, Any] = {"global": {}, "users": {}}
        self._mtime: float | None = None
        self.error: str | None = None

    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._document, self._mtime, self.error = {"global": {}, "users": {}}, None, None
            return self._document
        if mtime != self._mtime:
            try:
                self._document = parse(self.path.read_bytes())
                self.error = None
            except (OSError, VariableError) as error:
                # Keep serving the last good values: a typo must not strip
                # every employee's provider keys mid-day.
                self.error = str(error)
            self._mtime = mtime
        return self._document

    def reload(self) -> dict[str, Any]:
        self._mtime = None
        return self.load()

    def for_subject(self, subject: str) -> dict[str, str]:
        return resolve(self.load(), subject)
