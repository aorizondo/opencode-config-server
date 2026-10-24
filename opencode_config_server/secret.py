"""`python -m opencode_config_server.secret` -- generate OCS_SESSION_SECRET.

With `--admin`, generate OCS_ADMIN_TOKEN for the web admin panel instead.
"""

from __future__ import annotations

import secrets
import sys

if __name__ == "__main__":
    name = "OCS_ADMIN_TOKEN" if "--admin" in sys.argv[1:] else "OCS_SESSION_SECRET"
    print(f"{name}={secrets.token_urlsafe(48)}")
