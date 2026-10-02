"""Loads the corporate configuration document and injects server-side values.

What IT writes in `config/opencode.jsonc` is served almost verbatim. Two
conveniences are resolved here so the document stays free of deployment detail:

    {server_url}     -> the public base URL
    {session_token}  -> `{env:<session env>}`, which the harness substitutes
                        locally with the employee's own token

`{env:NAME}` and `{file:path}` are left untouched: the harness resolves both on
the employee's machine (packages/core/src/config/variable.ts).
"""

from __future__ import annotations

from typing import Any

from . import jsonc
from .settings import Settings

#: Rejected outright: the harness would read these from the employee's disk and
#: they are a classic way to turn a config feed into an exfiltration channel.
FORBIDDEN_FILE_REFERENCES = ("{file:~/.ssh", "{file:/etc/shadow", "{file:~/.aws", "{file:~/.gnupg")


class ConfigError(RuntimeError):
    """The corporate document is missing or cannot be served."""


def _substitute(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_substitute(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, replacements) for key, item in value.items()}
    return value


def load(settings: Settings) -> dict[str, Any]:
    path = settings.opencode_config_file
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"corporate configuration not readable: {path}") from error

    for marker in FORBIDDEN_FILE_REFERENCES:
        if marker in text:
            raise ConfigError(f"refusing to serve a configuration containing {marker!r} ({path})")

    try:
        document = jsonc.loads(text)
    except ValueError as error:
        raise ConfigError(f"{path} is not valid JSON/JSONC: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a JSON object")

    document = _substitute(
        document,
        {
            "{server_url}": settings.base_url,
            "{session_token}": f"{{env:{settings.session_env}}}",
        },
    )
    document.setdefault("$schema", "https://opencode.ai/config.json")
    return document
