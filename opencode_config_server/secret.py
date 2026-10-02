"""`python -m opencode_config_server.secret` -- generate OCS_SESSION_SECRET."""

from __future__ import annotations

import secrets

if __name__ == "__main__":
    print(f"OCS_SESSION_SECRET={secrets.token_urlsafe(48)}")
