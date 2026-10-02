"""Shared fixtures: an isolated config tree and an ASGI client per test."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from opencode_config_server import auth, settings as settings_module  # noqa: E402
from opencode_config_server.app import create_app  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
PASSWORD = "corp-password-1234"
API_KEY = "ock_test_key_for_ci_runner"
SECRET = "test-session-secret-not-for-production"

# Real PBKDF2 at 600k iterations costs ~0.3s per call; tests only need the format.
TEST_ITERATIONS = 1_000


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A self-contained corporate config tree, seeded from the shipped defaults."""
    root = tmp_path / "config"
    root.mkdir()
    shutil.copy(REPO_ROOT / "config" / "opencode.jsonc", root / "opencode.jsonc")

    (root / "bootstrap").mkdir()
    (root / "bootstrap" / "install.sh").write_text(
        "#!/usr/bin/env bash\nset -u\necho 'test bootstrap ran'\ntouch \"${BOOTSTRAP_MARKER:-/dev/null}\"\n",
        encoding="utf-8",
    )

    skill = root / "skills" / "corp-standards"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: corp-standards\ndescription: Corporate rules\n---\n# Rules\n")
    (skill / "references" / "coding-standards.md").write_text("# Coding\nUse types.\n")
    (skill / "private.key").write_text("must-never-be-published")

    (root / "users.json").write_text(
        json.dumps(
            {
                "users": [
                    {
                        "name": "ana.ruiz",
                        "password": auth.hash_password(PASSWORD, iterations=TEST_ITERATIONS),
                        "version": 1,
                    },
                    {"name": "ci-runner", "api_keys": [auth.hash_api_key(API_KEY)], "version": 1},
                    {
                        "name": "ex.employee",
                        "password": auth.hash_password(PASSWORD, iterations=TEST_ITERATIONS),
                        "disabled": True,
                        "version": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def settings(config_dir: Path) -> settings_module.Settings:
    return settings_module.load(
        base_url="https://cfg.corp.example",
        config_dir=config_dir,
        users_file=config_dir / "users.json",
        session_secret=SECRET,
        environment="development",
    )


@pytest.fixture
def app(settings: settings_module.Settings):
    return create_app(settings)


@pytest.fixture
async def client(app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://cfg.corp.example"
    ) as instance:
        yield instance


@pytest.fixture
async def token(client: httpx.AsyncClient) -> str:
    response = await client.post("/auth/token", headers={"X-Api-Key": API_KEY})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def write_users(config_dir: Path, users: list[dict]) -> None:
    """Rewrite users.json and make sure the mtime check notices."""
    path = config_dir / "users.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    stat = path.stat()
    import os

    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
