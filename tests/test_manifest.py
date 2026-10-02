"""The manifest must match the shape the harness decodes, exactly.

Reference: packages/core/src/wellknown.ts:12-32 in opencode v2.

    Auth         = { command: Schema.Array(Schema.String), env: Schema.String }
    RemoteConfig = { url: Schema.String, headers?: Record<String,String> }
    Manifest     = { auth?: Auth, config?: Config, remote_config?: RemoteConfig }

A manifest that fails this decode makes `opencode auth login` fail with
"Failed to load wellknown manifest", which is how the previous implementation
of this server broke.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest

from opencode_config_server import manifest


def assert_decodes_as_v2_manifest(document: object) -> None:
    assert isinstance(document, dict)
    assert set(document) <= {"auth", "config", "remote_config"}, "unknown top-level keys are a protocol error"

    auth = document.get("auth")
    assert isinstance(auth, dict), "auth is required for `opencode auth login` to accept the origin"
    assert set(auth) == {"command", "env"}, f"auth must be exactly command+env, got {sorted(auth)}"
    assert isinstance(auth["command"], list) and auth["command"], "command must be a non-empty argv array"
    assert all(isinstance(item, str) for item in auth["command"]), "every argv entry must be a string"
    assert isinstance(auth["env"], str) and auth["env"].isidentifier()

    remote = document.get("remote_config")
    assert isinstance(remote, dict)
    assert isinstance(remote["url"], str) and remote["url"].startswith(("http://", "https://"))
    headers = remote.get("headers", {})
    assert isinstance(headers, dict)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items())


class TestManifestShape:
    async def test_matches_the_v2_schema(self, client: httpx.AsyncClient):
        response = await client.get("/.well-known/opencode")
        assert response.status_code == 200
        assert_decodes_as_v2_manifest(response.json())

    async def test_is_public(self, client: httpx.AsyncClient):
        """Discovery has to work before any credential exists: no auth here."""
        response = await client.get("/.well-known/opencode")
        assert response.status_code == 200
        assert "authorization" not in {key.lower() for key in response.request.headers}

    async def test_carries_no_secret(self, client: httpx.AsyncClient):
        body = (await client.get("/.well-known/opencode")).text
        for secret in ("test-session-secret-not-for-production", "ock_test_key_for_ci_runner", "corp-password"):
            assert secret not in body

    async def test_token_placeholder_matches_auth_env(self, client: httpx.AsyncClient):
        document = (await client.get("/.well-known/opencode")).json()
        name = document["auth"]["env"]
        # The harness only substitutes the variable it was told about; a mismatch
        # silently yields an empty Authorization header.
        assert document["remote_config"]["headers"]["authorization"] == f"Bearer {{env:{name}}}"

    async def test_remote_config_points_at_this_server(self, client, settings):
        document = (await client.get("/.well-known/opencode")).json()
        assert document["remote_config"]["url"] == f"{settings.base_url}/config/opencode.json"


class TestLoginCommand:
    def test_runs_through_bash_without_a_login_shell(self, settings):
        """execve, not a shell: the argv has to spell out the interpreter.

        `-l` is deliberately absent -- a login shell sources the employee's
        profile, whose stdout would end up inside the stored credential.
        """
        command = manifest.build(settings)["auth"]["command"]
        assert command[0] == "bash"
        assert command[1] == "-c"
        assert "-l" not in command[1]
        assert command[3] == manifest.COMMAND_LABEL, "argv0 names the process in `ps`"
        assert command[4] == settings.base_url

    def test_inlined_script_is_valid_bash(self, settings):
        script = manifest.build(settings)["auth"]["command"][2]
        result = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_script_writes_only_the_token_to_stdout(self, settings):
        script = manifest.build(settings)["auth"]["command"][2]
        # fd 3 is the credential channel; stdout is rerouted to stderr.
        assert "exec 3>&1 1>&2" in script
        assert 'printf \'%s\' "$TOKEN" >&3' in script
        assert script.count(">&3") == 1

    @pytest.mark.parametrize(
        "needle",
        [
            "OPENCODE_CORP_TOKEN",
            "OPENCODE_CORP_USER",
            "OPENCODE_CORP_PASSWORD",
            "OPENCODE_CORP_CREDENTIALS",
            "/auth/token",
            "/bootstrap/install.sh",
        ],
    )
    def test_script_covers_every_credential_source(self, settings, needle: str):
        assert needle in manifest.build(settings)["auth"]["command"][2]
