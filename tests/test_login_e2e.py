"""End-to-end: the real login script against a real HTTP server.

This is the test that matters most. It exercises the exact contract the harness
imposes on `auth.command` (packages/core/src/integration.ts:620-650):

    stdout -> the credential, trimmed
    stderr -> what the employee reads in the TUI
    exit 0 -> login succeeded
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import replace
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


@contextlib.contextmanager
def serve(settings):
    """Serve the app on a loopback port, like a real deployment."""
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

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def live_server(settings, config_dir):
    with serve(settings) as base_url:
        yield base_url


def login_command(base_url: str, env: dict[str, str], *, home: Path) -> tuple[list[str], dict[str, str]]:
    """The manifest's auth.command and the environment the harness gives it."""
    document = httpx.get(f"{base_url}/.well-known/opencode", timeout=5).json()
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        # The harness never attaches a terminal and ignores stdin.
        "OPENCODE_CORP_SKIP_BOOTSTRAP": "1",
        **env,
    }
    return document["auth"]["command"], environment


def run_login(base_url: str, env: dict[str, str], *, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the manifest's auth.command exactly as the harness would."""
    command, environment = login_command(base_url, env, home=home)
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


class BrowserLogin:
    """The login command left waiting for someone to approve it in a browser."""

    def __init__(self, base_url: str, env: dict[str, str], *, home: Path) -> None:
        command, environment = login_command(base_url, env, home=home)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.printed: list[str] = []
        self.url = self.code = ""
        for line in self.process.stderr:
            self.printed.append(line)
            if found := re.search(r"(https?://\S+/login\?code=\S+)", line):
                self.url = found.group(1)
            if found := re.search(r"code: (\S+)", line):
                self.code = found.group(1)
                break

    def finish(self, timeout: float = 30) -> tuple[int, str, str]:
        stdout, stderr = self.process.communicate(timeout=timeout)
        return self.process.returncode, stdout, "".join(self.printed) + stderr


class TestBrowserLogin:
    """No credentials in the environment or on disk: sign in from a browser."""

    def test_browser_approval_yields_a_usable_credential(self, live_server, tmp_path):
        login = BrowserLogin(live_server, {"OPENCODE_CORP_NO_BROWSER": "1"}, home=tmp_path)
        assert login.code, "".join(login.printed)
        assert login.url.startswith(f"{live_server}/login?code=")

        approved = httpx.post(
            f"{live_server}/login/approve",
            json={"user_code": login.code, "username": "ana.ruiz", "password": PASSWORD},
            timeout=5,
        )
        assert approved.status_code == 200, approved.text

        returncode, token, printed = login.finish()
        assert returncode == 0, printed
        config = httpx.get(
            f"{live_server}/config/opencode.json", headers={"Authorization": f"Bearer {token}"}, timeout=5
        )
        assert config.status_code == 200

    def test_the_browser_does_not_keep_the_login_waiting(self, live_server, tmp_path):
        """The harness waits for the command's output to close; a browser left
        holding it would make the login hang until the browser exits."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        opened = tmp_path / "opened"
        fake = bin_dir / "xdg-open"
        fake.write_text(f'#!/bin/sh\necho $$ > {opened}.pid\nprintf %s "$1" > {opened}\nexec sleep 30\n')
        fake.chmod(0o755)

        login = BrowserLogin(
            live_server,
            {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DISPLAY": ":0"},
            home=tmp_path,
        )
        httpx.post(
            f"{live_server}/login/approve",
            json={"user_code": login.code, "api_key": API_KEY},
            timeout=5,
        ).raise_for_status()

        try:
            returncode, _, printed = login.finish(timeout=15)
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int((tmp_path / "opened.pid").read_text()), 15)
        assert returncode == 0, printed
        assert opened.read_text() == login.url

    def test_an_unapproved_code_runs_out(self, settings, config_dir, tmp_path):
        with serve(replace(settings, device_ttl_seconds=3)) as base_url:
            login = BrowserLogin(base_url, {"OPENCODE_CORP_NO_BROWSER": "1"}, home=tmp_path)
            returncode, stdout, printed = login.finish()
        assert returncode == 8
        assert stdout == "", "a failed login must not store anything"
        assert "expired" in printed


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
