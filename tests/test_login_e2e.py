"""End-to-end: the real login script against a real HTTP server.

This is the test that matters most. It exercises the exact contract the harness
imposes on `auth.command` (packages/core/src/integration.ts:620-650):

    stdout -> the credential, trimmed
    stderr -> what the employee reads in the TUI
    exit 0 -> login succeeded
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from opencode_config_server import manifest
from opencode_config_server.app import create_app

from .conftest import API_KEY, PASSWORD

pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None or shutil.which("bash") is None,
    reason="the login script needs curl and bash",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(settings, config_dir):
    """Serve the app on a loopback port, like a real deployment."""
    from dataclasses import replace

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(replace(settings, base_url=base_url))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        pytest.fail("the test server never became healthy")

    yield base_url

    server.should_exit = True
    thread.join(timeout=10)


def run_login(base_url: str, env: dict[str, str], *, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the manifest's auth.command exactly as the harness would."""
    document = httpx.get(f"{base_url}/.well-known/opencode", timeout=5).json()
    command = document["auth"]["command"]
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        # The harness never attaches a terminal and ignores stdin.
        "OPENCODE_CORP_SKIP_BOOTSTRAP": "1",
        **env,
    }
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        stdin=subprocess.DEVNULL,
    )


class TestSuccessfulLogin:
    def test_api_key_yields_a_usable_credential(self, live_server, tmp_path):
        result = run_login(live_server, {"OPENCODE_CORP_TOKEN": API_KEY}, home=tmp_path)
        assert result.returncode == 0, result.stderr
        token = result.stdout
        assert token == token.strip(), "the harness trims, but a clean token proves the fd-3 channel works"
        assert token.startswith("v1.")

        response = httpx.get(f"{live_server}/config/opencode.json", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["$schema"] == "https://opencode.ai/config.json"

    def test_username_and_password_yield_a_usable_credential(self, live_server, tmp_path):
        result = run_login(
            live_server,
            {"OPENCODE_CORP_USER": "ana.ruiz", "OPENCODE_CORP_PASSWORD": PASSWORD},
            home=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert httpx.get(
            f"{live_server}/config/opencode.json",
            headers={"Authorization": f"Bearer {result.stdout}"},
        ).status_code == 200

    def test_progress_goes_to_stderr_only(self, live_server, tmp_path):
        result = run_login(live_server, {"OPENCODE_CORP_TOKEN": API_KEY}, home=tmp_path)
        assert "opencode-corp:" in result.stderr
        assert "opencode-corp:" not in result.stdout
        assert result.stdout.count("\n") == 0

    def test_the_skill_url_from_the_config_resolves_with_that_token(self, live_server, tmp_path):
        """What the harness does next: substitute {env:...} and fetch the index."""
        token = run_login(live_server, {"OPENCODE_CORP_TOKEN": API_KEY}, home=tmp_path).stdout
        document = httpx.get(
            f"{live_server}/config/opencode.json", headers={"Authorization": f"Bearer {token}"}
        ).json()
        skill_url = document["skills"][0].replace("{env:OPENCODE_CORP_SESSION}", token)
        index = httpx.get(f"{skill_url}index.json")
        assert index.status_code == 200
        first = index.json()["skills"][0]
        assert httpx.get(f"{skill_url}{first['name']}/SKILL.md").status_code == 200


class TestCredentialsFile:
    def test_reads_a_properly_protected_file(self, live_server, tmp_path):
        credentials = tmp_path / "credentials"
        credentials.write_text(f"# corporate\ntoken = {API_KEY}\n", encoding="utf-8")
        credentials.chmod(0o600)
        result = run_login(
            live_server, {"OPENCODE_CORP_CREDENTIALS": str(credentials)}, home=tmp_path
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("v1.")

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
    def test_refuses_a_world_or_group_readable_file(self, live_server, tmp_path, mode: int):
        credentials = tmp_path / "credentials"
        credentials.write_text(f"token = {API_KEY}\n", encoding="utf-8")
        credentials.chmod(mode)
        result = run_login(live_server, {"OPENCODE_CORP_CREDENTIALS": str(credentials)}, home=tmp_path)
        assert result.returncode == 4
        assert "readable by other users" in result.stderr
        assert result.stdout == ""

    def test_environment_wins_over_the_file(self, live_server, tmp_path):
        credentials = tmp_path / "credentials"
        credentials.write_text("token = ock_stale_key\n", encoding="utf-8")
        credentials.chmod(0o600)
        result = run_login(
            live_server,
            {"OPENCODE_CORP_TOKEN": API_KEY, "OPENCODE_CORP_CREDENTIALS": str(credentials)},
            home=tmp_path,
        )
        assert result.returncode == 0, result.stderr


class TestFailures:
    def test_explains_how_to_provide_credentials(self, live_server, tmp_path):
        result = run_login(live_server, {}, home=tmp_path)
        assert result.returncode == 4
        assert result.stdout == "", "a failed login must not store anything"
        assert "OPENCODE_CORP_TOKEN" in result.stderr
        assert "OPENCODE_CORP_USER" in result.stderr

    def test_relays_the_server_rejection(self, live_server, tmp_path):
        result = run_login(
            live_server,
            {"OPENCODE_CORP_USER": "ana.ruiz", "OPENCODE_CORP_PASSWORD": "wrong"},
            home=tmp_path,
        )
        assert result.returncode == 6
        assert "HTTP 401" in result.stderr
        assert "invalid username or password" in result.stderr
        assert result.stdout == ""

    def test_reports_an_unreachable_server(self, live_server, tmp_path):
        document = httpx.get(f"{live_server}/.well-known/opencode", timeout=5).json()
        command = list(document["auth"]["command"])
        command[4] = f"http://127.0.0.1:{free_port()}"
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "OPENCODE_CORP_TOKEN": API_KEY},
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 5
        assert "cannot reach" in result.stderr
        assert result.stdout == ""

    def test_a_missing_server_url_fails_fast(self, tmp_path):
        script = manifest.login_script()
        result = subprocess.run(
            ["bash", "-c", script, "label"],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        )
        assert result.returncode == 2
        assert "no server URL" in result.stderr


class TestBootstrap:
    def test_runs_the_installer_and_keeps_stdout_clean(self, live_server, tmp_path):
        """The test installer prints to stdout; the token must survive that."""
        marker = tmp_path / "bootstrap-ran"
        result = run_login(
            live_server,
            {
                "OPENCODE_CORP_TOKEN": API_KEY,
                "OPENCODE_CORP_SKIP_BOOTSTRAP": "0",
                "BOOTSTRAP_MARKER": str(marker),
            },
            home=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert marker.exists(), "the installer never ran"
        assert result.stdout.startswith("v1.") and "\n" not in result.stdout
        assert "test bootstrap ran" in result.stderr

    def test_a_failing_installer_does_not_cost_the_session(self, live_server, tmp_path, config_dir):
        (config_dir / "bootstrap" / "install.sh").write_text(
            "#!/usr/bin/env bash\necho 'broken step' >&2\nexit 1\n", encoding="utf-8"
        )
        result = run_login(
            live_server,
            {"OPENCODE_CORP_TOKEN": API_KEY, "OPENCODE_CORP_SKIP_BOOTSTRAP": "0"},
            home=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("v1.")
        assert "bootstrap failed" in result.stderr
