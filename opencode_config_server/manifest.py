"""The `/.well-known/opencode` manifest.

Shape is fixed by the harness (packages/core/src/wellknown.ts):

    Auth         = { command: string[], env: string }
    RemoteConfig = { url: string, headers?: Record<string,string> }
    Manifest     = { auth?, config?, remote_config? }

`command` is an argv array executed with execve -- there is no shell -- so the
login logic is handed to `bash -c` explicitly. The script is inlined rather than
piped from the network: the manifest is public and auditable, and IT can read
exactly what runs on an employee's machine before approving the rollout.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .settings import Settings

#: argv[0] for `bash -c <script> <argv0> <args...>`; shows up in process lists.
COMMAND_LABEL = "opencode-corp-login"

_SCRIPT_PATH = Path(__file__).with_name("login_script.sh")


@lru_cache(maxsize=1)
def login_script() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


def build(settings: Settings) -> dict[str, Any]:
    """Build the manifest. Public endpoint: never embed a secret here."""
    return {
        "auth": {
            "command": ["bash", "-c", login_script(), COMMAND_LABEL, settings.base_url],
            "env": settings.session_env,
        },
        "remote_config": {
            "url": f"{settings.base_url}/config/opencode.json",
            "headers": {"authorization": f"Bearer {{env:{settings.session_env}}}"},
        },
    }
