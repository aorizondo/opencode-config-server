"""The `/.well-known/opencode` manifest.

Shape is fixed by the harness (packages/core/src/wellknown.ts):

    Auth         = { command: string[], env: string }
    RemoteConfig = { url: string, headers?: Record<string,string> }
    Manifest     = { auth?, config?, remote_config? }

`command` is an argv array executed with execve -- there is no shell -- so the
login logic is handed to `bash -c` explicitly. The script is inlined rather than
piped from the network: the manifest is public and auditable, and IT can read
exactly what runs on an employee's machine before approving the rollout.

Windows has no bash, and OpenCode does not tell the server which OS asks, but
the manifest lives at `<origin>/.well-known/opencode` and the origin may carry a
path (wellknown.ts `inspect`). So Windows machines log in against
`<server>/windows`, whose manifest runs PowerShell instead. That script is too
long for a Windows command line (32K characters), so the command downloads
`/login.ps1` and runs it only if its SHA-256 matches the one pinned here: the
manifest stays the single thing to audit.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from .settings import Settings

#: argv[0] for `bash -c <script> <argv0> <args...>`; shows up in process lists.
COMMAND_LABEL = "opencode-corp-login"

_SCRIPT_PATH = Path(__file__).with_name("login_script.sh")
_POWERSHELL_PATH = Path(__file__).with_name("login_script.ps1")
#: Where Windows machines point `opencode auth login`, relative to the base URL.
WINDOWS_PATH = "/windows"


@lru_cache(maxsize=1)
def login_script() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def powershell_script() -> bytes:
    return _POWERSHELL_PATH.read_bytes()


def powershell_launcher(settings: Settings) -> str:
    """Windows PowerShell 5.1: fetch /login.ps1, check its SHA-256, run it."""
    if "'" in settings.base_url:
        raise ValueError("the base URL cannot contain a single quote")
    digest = hashlib.sha256(powershell_script()).hexdigest()
    return (
        "$ErrorActionPreference='Stop';"
        "[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor "
        "[Net.SecurityProtocolType]::Tls12;"
        "$c=New-Object Net.WebClient;$c.Encoding=[Text.Encoding]::UTF8;"
        f"$s=$c.DownloadString('{settings.base_url}/login.ps1');"
        "$h=([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash("
        "[Text.Encoding]::UTF8.GetBytes($s))) -replace '-','').ToLowerInvariant();"
        f"if($h -ne '{digest}'){{[Console]::Error.WriteLine("
        "'opencode-corp: the login script does not match this manifest; not running it');exit 9};"
        f"& ([ScriptBlock]::Create($s)) '{settings.base_url}'"
    )


def build(settings: Settings, *, windows: bool = False) -> dict[str, Any]:
    """Build the manifest. Public endpoint: never embed a secret here."""
    command = (
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
         powershell_launcher(settings)]
        if windows
        else ["bash", "-c", login_script(), COMMAND_LABEL, settings.base_url]
    )
    return {
        "auth": {
            "command": command,
            "env": settings.session_env,
        },
        "remote_config": {
            "url": f"{settings.base_url}/config/opencode.json",
            "headers": {"authorization": f"Bearer {{env:{settings.session_env}}}"},
        },
    }
