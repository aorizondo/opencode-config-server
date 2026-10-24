"""Windows: a PowerShell login behind <server>/windows, and install.ps1.

OpenCode reads the manifest at `<origin>/.well-known/opencode` and the origin may
carry a path, so Windows machines run `opencode auth login <server>/windows`.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import replace

import httpx
import pytest

from opencode_config_server import manifest
from opencode_config_server.app import create_app

from .conftest import API_KEY
from .test_admin import ADMIN_TOKEN, BASE, BEARER


class TestManifest:
    async def test_windows_logs_in_with_powershell(self, client, settings):
        document = (await client.get("/windows/.well-known/opencode")).json()
        command = document["auth"]["command"]
        assert command[:6] == ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
        assert f"'{settings.base_url}/login.ps1'" in command[6]
        assert len(command[6]) < 8191, "fits even cmd.exe's limit, far under CreateProcess's 32K"
        assert '"' not in command[6], "no double quotes: nothing for Windows argument quoting to mangle"
        assert document["auth"]["env"] == settings.session_env
        unix = (await client.get("/.well-known/opencode")).json()
        assert document["remote_config"] == unix["remote_config"], "one configuration for every platform"

    async def test_the_manifest_pins_the_script_it_runs(self, client):
        launcher = (await client.get("/windows/.well-known/opencode")).json()["auth"]["command"][6]
        script = await client.get("/login.ps1")
        assert script.status_code == 200
        assert hashlib.sha256(script.content).hexdigest() in launcher

    def test_the_script_is_ascii(self):
        """The launcher hashes the downloaded text re-encoded as UTF-8: no BOM, no surprises."""
        assert manifest.powershell_script().isascii()

    def test_the_script_ships_as_package_data(self):
        from pathlib import Path

        assert "login_script.ps1" in (Path(__file__).parent.parent / "pyproject.toml").read_text()


class TestInstaller:
    async def test_install_ps1_needs_a_session(self, client, config_dir):
        (config_dir / "bootstrap" / "install.ps1").write_text("Write-Output 'hi'\n", encoding="utf-8")
        assert (await client.get("/bootstrap/install.ps1")).status_code == 401
        token = (await client.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        served = await client.get("/bootstrap/install.ps1", headers={"Authorization": f"Bearer {token}"})
        assert served.status_code == 200 and served.text == "Write-Output 'hi'\n"

    async def test_the_panel_edits_it_separately(self, settings, config_dir):
        app = create_app(replace(settings, admin_token=ADMIN_TOKEN))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            state = (await client.get("/admin/api/bootstrap", params={"platform": "windows"}, headers=BEARER)).json()
            assert state["file"] == "bootstrap/install.ps1" and state["exists"] is False
            saved = await client.put(
                "/admin/api/bootstrap", params={"platform": "windows"}, headers=BEARER,
                json={"text": "Write-Output 'corp'\n", "etag": state["etag"]},
            )
            assert saved.status_code == 200, saved.text
            assert (config_dir / "bootstrap" / "install.ps1").read_text() == "Write-Output 'corp'\n"
            assert (config_dir / "bootstrap" / "install.sh").exists(), "the bash one is untouched"
            history = (await client.get("/admin/api/history", params={"doc": "bootstrap-windows"}, headers=BEARER)).json()
            assert history["viewable"] is True
            overview = (await client.get("/admin/api/overview", headers=BEARER)).json()
            assert overview["windows_login_command"].endswith("/windows")

    @pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed here")
    async def test_syntax_errors_are_refused_when_pwsh_is_available(self, settings):
        app = create_app(replace(settings, admin_token=ADMIN_TOKEN))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            state = (await client.get("/admin/api/bootstrap", params={"platform": "windows"}, headers=BEARER)).json()
            response = await client.put(
                "/admin/api/bootstrap", params={"platform": "windows"}, headers=BEARER,
                json={"text": "if ( {\n", "etag": state["etag"]},
            )
            assert response.status_code == 422


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed here")
class TestScript:
    def test_the_login_script_parses(self, tmp_path):
        from pathlib import Path

        script = Path(manifest.__file__).with_name("login_script.ps1")
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
             f"$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);$e.Count"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "0"
