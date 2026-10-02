"""POST /auth/token: the only place static credentials are accepted."""

from __future__ import annotations

import base64

import httpx

from opencode_config_server import auth

from .conftest import API_KEY, PASSWORD, TEST_ITERATIONS, write_users


def basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


class TestPasswordLogin:
    async def test_accepts_valid_credentials(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))
        assert response.status_code == 200
        body = response.json()
        assert body["subject"] == "ana.ruiz"
        assert body["token"].startswith("v1.")
        assert body["expires_at"] > 0
        assert response.headers["cache-control"] == "no-store"

    async def test_rejects_a_wrong_password(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers=basic("ana.ruiz", "wrong"))
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid username or password"

    async def test_rejects_an_unknown_user_identically(self, client: httpx.AsyncClient):
        """Same message as a wrong password: no account-existence oracle."""
        response = await client.post("/auth/token", headers=basic("nobody", PASSWORD))
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid username or password"

    async def test_rejects_a_disabled_account(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers=basic("ex.employee", PASSWORD))
        assert response.status_code == 401
        assert "disabled" in response.json()["detail"]

    async def test_rejects_malformed_basic_credentials(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers={"Authorization": "Basic not-base64!!"})
        assert response.status_code == 401

    async def test_advertises_basic_when_nothing_is_sent(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token")
        assert response.status_code == 401
        assert "Basic" in response.headers["www-authenticate"]


class TestApiKeyLogin:
    async def test_accepts_a_valid_key(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers={"X-Api-Key": API_KEY})
        assert response.status_code == 200
        assert response.json()["subject"] == "ci-runner"

    async def test_rejects_an_unknown_key(self, client: httpx.AsyncClient):
        response = await client.post("/auth/token", headers={"X-Api-Key": "ock_nope"})
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid API key"

    async def test_rejects_a_key_belonging_to_a_disabled_account(self, client, config_dir):
        write_users(
            config_dir,
            [{"name": "ci-runner", "api_keys": [auth.hash_api_key(API_KEY)], "disabled": True, "version": 1}],
        )
        response = await client.post("/auth/token", headers={"X-Api-Key": API_KEY})
        assert response.status_code == 401
        assert "disabled" in response.json()["detail"]


class TestRateLimit:
    async def test_locks_out_after_repeated_failures(self, client: httpx.AsyncClient, settings):
        for _ in range(settings.rate_limit_attempts):
            assert (await client.post("/auth/token", headers=basic("ana.ruiz", "wrong"))).status_code == 401
        blocked = await client.post("/auth/token", headers=basic("ana.ruiz", "wrong"))
        assert blocked.status_code == 429
        # The lockout must not be bypassable by then sending the right password.
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 429

    async def test_a_success_clears_the_counter(self, client: httpx.AsyncClient, settings):
        for _ in range(settings.rate_limit_attempts - 1):
            await client.post("/auth/token", headers=basic("ana.ruiz", "wrong"))
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 200
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", "wrong"))).status_code == 401


class TestUserDatabase:
    async def test_edits_are_picked_up_without_a_restart(self, client, config_dir):
        write_users(
            config_dir,
            [{"name": "nuevo", "password": auth.hash_password(PASSWORD, iterations=TEST_ITERATIONS), "version": 1}],
        )
        assert (await client.post("/auth/token", headers=basic("nuevo", PASSWORD))).status_code == 200
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 401

    async def test_a_missing_database_is_a_server_fault_not_a_credential_fault(self, client, config_dir):
        (config_dir / "users.json").unlink()
        response = await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))
        assert response.status_code == 503
        assert "contact IT" in response.json()["detail"]

    async def test_a_corrupt_database_keeps_serving_the_last_good_snapshot(self, client, config_dir):
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 200
        path = config_dir / "users.json"
        path.write_text("{ not json", encoding="utf-8")
        import os

        stat = path.stat()
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
        # A typo during an edit must not lock the whole company out.
        assert (await client.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 200

    async def test_a_plaintext_password_is_refused_loudly(self, config_dir, client):
        write_users(config_dir, [{"name": "lazy", "password": "hunter2", "version": 1}])
        response = await client.post("/auth/token", headers=basic("lazy", "hunter2"))
        # A deployment fault, not a credential fault: never 401, never a token.
        assert response.status_code == 503
        assert "contact IT" in response.json()["detail"]
