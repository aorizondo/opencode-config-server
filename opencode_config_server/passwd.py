"""`python -m opencode_config_server.passwd` -- hash a password for users.json."""

from __future__ import annotations

import getpass
import json
import sys

from .auth import generate_api_key, hash_api_key, hash_password


def main(argv: list[str]) -> int:
    if "--api-key" in argv:
        key = generate_api_key()
        print("Add this to the user's api_keys in users.json:")
        print(f"  {json.dumps(hash_api_key(key))}")
        print("\nGive this to the employee (it is shown once):")
        print(f"  {key}")
        return 0

    name = argv[0] if argv and not argv[0].startswith("-") else "the user"
    first = getpass.getpass(f"Password for {name}: ")
    if not first:
        print("empty password", file=sys.stderr)
        return 1
    if first != getpass.getpass("Repeat: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    print(f"\nAdd this as the user's password in users.json:\n  {json.dumps(hash_password(first))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
