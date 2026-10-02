"""GET /bootstrap/install.sh: the one-off installer for third-party tooling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).parent.parent


class TestAccess:
    async def test_requires_a_token(self, client: httpx.AsyncClient):
        assert (await client.get("/bootstrap/install.sh")).status_code == 401

    async def test_serves_an_authenticated_client(self, client: httpx.AsyncClient, token: str):
        response = await client.get("/bootstrap/install.sh", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/x-shellscript")

    async def test_absence_is_a_404_not_a_500(self, client, token, config_dir):
        (config_dir / "bootstrap" / "install.sh").unlink()
        response = await client.get("/bootstrap/install.sh", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404

    async def test_can_be_switched_off(self, settings, token):
        from dataclasses import replace

        from opencode_config_server.app import create_app

        disabled = create_app(replace(settings, bootstrap_enabled=False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled), base_url="http://t") as client:
            response = await client.get("/bootstrap/install.sh", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 404


class TestShippedInstaller:
    """The default installer is what IT edits, so it has to stay sound."""

    script = REPO_ROOT / "config" / "bootstrap" / "install.sh"

    def test_is_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(self.script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_installs_hindsight(self):
        assert "@vectorize-io/hindsight-coding-agents install opencode2" in self.script.read_text()

    def test_is_idempotent_through_a_version_stamp(self):
        text = self.script.read_text()
        assert "bootstrap.version" in text
        assert "already applied" in text

    def test_does_not_mark_success_when_a_step_fails(self):
        text = self.script.read_text()
        assert "not marking it as applied" in text

    def test_runs_without_errexit_so_one_failure_is_not_fatal(self):
        """A failed installer must cost a warning, not the employee's session."""
        text = self.script.read_text()
        assert "set -uo pipefail" in text
        assert "set -euo pipefail" not in text
