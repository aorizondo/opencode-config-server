"""Browser login (RFC 8628 shape) and Bifrost virtual keys.

The login command cannot prompt -- the harness gives it /dev/null as stdin and
the OpenCode server's environment -- so the employee signs in at /login with the
code the command prints, and the command polls for the session token.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from opencode_config_server.app import create_app
from opencode_config_server.device import DeviceError, DeviceFlow, normalize_user_code

from .conftest import API_KEY, PASSWORD


# --------------------------------------------------------------------------- unit


class TestDeviceFlow:
    def test_a_code_pair_yields_the_subject_exactly_once(self):
        flow = DeviceFlow(ttl_seconds=300, interval=2)
        grant = flow.start(now=1000)
        flow.approve(grant.user_code, "ana.ruiz", now=1001)
        assert flow.poll(grant.device_code, now=1010) == "ana.ruiz"
        with pytest.raises(DeviceError) as again:
            flow.poll(grant.device_code, now=1012)
        assert again.value.code == "expired_token"

    def test_pending_then_slow_down_when_polling_too_fast(self):
        flow = DeviceFlow(ttl_seconds=300, interval=2)
        grant = flow.start(now=1000)
        with pytest.raises(DeviceError) as first:
            flow.poll(grant.device_code, now=1002)
        assert first.value.code == "authorization_pending"
        with pytest.raises(DeviceError) as second:
            flow.poll(grant.device_code, now=1002.2)
        assert second.value.code == "slow_down"

    def test_codes_expire(self):
        flow = DeviceFlow(ttl_seconds=60)
        grant = flow.start(now=1000)
        with pytest.raises(DeviceError) as late:
            flow.approve(grant.user_code, "ana.ruiz", now=1061)
        assert late.value.code == "expired_token"
        with pytest.raises(DeviceError):
            flow.poll(grant.device_code, now=1061)

    def test_a_code_cannot_be_approved_twice(self):
        flow = DeviceFlow()
        grant = flow.start(now=1000)
        flow.approve(grant.user_code, "ana.ruiz", now=1001)
        with pytest.raises(DeviceError) as twice:
            flow.approve(grant.user_code, "mallory", now=1002)
        assert twice.value.code == "invalid_grant"

    def test_user_codes_are_typed_loosely(self):
        assert normalize_user_code("abcd-efgh") == normalize_user_code("ABCD EFGH") == "ABCDEFGH"
        flow = DeviceFlow()
        grant = flow.start(now=1000)
        typed = f"{grant.user_code[:4].lower()}-{grant.user_code[4:].lower()}"
        flow.approve(typed, "ana.ruiz", now=1001)
        assert flow.poll(grant.device_code, now=1010) == "ana.ruiz"

    def test_bounded_number_of_pending_logins(self):
        flow = DeviceFlow(max_pending=2)
        flow.start(now=1000)
        flow.start(now=1000)
        with pytest.raises(DeviceError) as full:
            flow.start(now=1000)
        assert full.value.code == "slow_down"


# ----------------------------------------------------------------------- endpoints


async def start(client: httpx.AsyncClient) -> dict:
    response = await client.post("/auth/device")
    assert response.status_code == 200, response.text
    return response.json()


class TestEndpoints:
    async def test_start_returns_what_the_script_needs(self, client):
        grant = await start(client)
        assert set(grant) >= {"device_code", "user_code", "verification_uri", "verification_uri_complete",
                              "expires_in", "interval"}
        assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", grant["user_code"])
        assert grant["verification_uri"] == "https://cfg.corp.example/login"
        assert grant["verification_uri_complete"].endswith(normalize_user_code(grant["user_code"]))
        assert grant["expires_in"] == 300

    async def test_full_flow_with_an_api_key(self, client):
        grant = await start(client)
        pending = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
        assert pending.status_code == 400 and pending.json()["error"] == "authorization_pending"

        approved = await client.post("/login/approve", json={"user_code": grant["user_code"], "api_key": API_KEY})
        assert approved.status_code == 200, approved.text
        assert approved.json() == {"status": "approved", "subject": "ci-runner"}

        issued = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
        assert issued.status_code == 200
        token = issued.json()["token"]
        config = await client.get("/config/opencode.json", headers={"Authorization": f"Bearer {token}"})
        assert config.status_code == 200

        reused = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
        assert reused.json()["error"] == "expired_token", "the token is handed out once"

    async def test_full_flow_with_username_and_password(self, client):
        grant = await start(client)
        approved = await client.post(
            "/login/approve", json={"user_code": grant["user_code"], "username": "ana.ruiz", "password": PASSWORD}
        )
        assert approved.status_code == 200
        issued = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
        assert issued.json()["subject"] == "ana.ruiz"

    async def test_wrong_credentials_leave_the_code_pending(self, client):
        grant = await start(client)
        rejected = await client.post(
            "/login/approve", json={"user_code": grant["user_code"], "username": "ana.ruiz", "password": "nope"}
        )
        assert rejected.status_code == 401
        assert "invalid username or password" in rejected.json()["detail"]
        pending = await client.post("/auth/device/token", json={"device_code": grant["device_code"]})
        assert pending.json()["error"] == "authorization_pending"

    async def test_a_disabled_account_cannot_approve(self, client):
        grant = await start(client)
        rejected = await client.post(
            "/login/approve", json={"user_code": grant["user_code"], "username": "ex.employee", "password": PASSWORD}
        )
        assert rejected.status_code == 401
        assert "disabled" in rejected.json()["detail"]

    async def test_unknown_code(self, client):
        rejected = await client.post("/login/approve", json={"user_code": "ZZZZ-ZZZZ", "api_key": API_KEY})
        assert rejected.status_code == 400
        assert "unknown or has expired" in rejected.json()["detail"]

    async def test_no_credentials_at_all(self, client):
        grant = await start(client)
        rejected = await client.post("/login/approve", json={"user_code": grant["user_code"]})
        assert rejected.status_code == 401

    async def test_approvals_are_rate_limited(self, settings):
        app = create_app(replace(settings, rate_limit_attempts=3))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cfg.corp.example") as c:
            for _ in range(3):
                await c.post("/login/approve", json={"user_code": "ZZZZ-ZZZZ", "api_key": "wrong"})
            blocked = await c.post("/login/approve", json={"user_code": "ZZZZ-ZZZZ", "api_key": API_KEY})
            assert blocked.status_code == 429


class TestLoginPage:
    async def test_is_self_contained_and_locked_down(self, client):
        page = await client.get("/login?code=abcd-efgh")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        csp = page.headers["content-security-policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        assert f'nonce="{nonce}"' in page.text
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"
        assert 'value="ABCD-EFGH"' in page.text
        assert "http://" not in page.text.replace("https://cfg.corp.example", ""), "no external resources"

    async def test_the_code_parameter_cannot_inject_markup(self, client):
        page = await client.get('/login?code="><script>alert(1)</script>')
        assert "<script>alert" not in page.text
        assert "alert(1)" not in page.text

    async def test_each_request_gets_a_fresh_nonce(self, client):
        first = (await client.get("/login")).headers["content-security-policy"]
        second = (await client.get("/login")).headers["content-security-policy"]
        assert first != second


# ------------------------------------------------------------------ Bifrost keys


class FakeBifrost(BaseHTTPRequestHandler):
    """Answers like transports/bifrost-http/handlers/governance.go:getVirtualKeyQuota."""

    KEYS = {
        "sk-bf-active": (200, {"virtual_key_name": "team-ana", "is_active": True, "budgets": []}),
        "sk-bf-inactive": (200, {"virtual_key_name": "team-old", "is_active": False, "budgets": []}),
        "sk-bf-expired": (403, {"error": {"message": "Virtual key has expired"}}),
        "sk-bf-broken": (500, {"error": {"message": "Failed to retrieve virtual key"}}),
    }

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path != "/api/governance/virtual-keys/quota":
            self.send_response(404)
            self.end_headers()
            return
        status, body = self.KEYS.get(self.headers.get("x-bf-vk", ""), (401, {"error": {"message": "nope"}}))
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def bifrost_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBifrost)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
async def vk_client(settings, bifrost_url):
    app = create_app(replace(settings, bifrost_url=bifrost_url))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://cfg.corp.example") as c:
        yield c


class TestBifrostVirtualKeys:
    async def test_an_active_key_signs_in_through_the_browser(self, vk_client):
        grant = await start(vk_client)
        approved = await vk_client.post(
            "/login/approve", json={"user_code": grant["user_code"], "api_key": "sk-bf-active"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["subject"] == "vk:team-ana"
        issued = (await vk_client.post("/auth/device/token", json={"device_code": grant["device_code"]})).json()
        config = await vk_client.get("/config/opencode.json", headers={"Authorization": f"Bearer {issued['token']}"})
        assert config.status_code == 200

    async def test_an_active_key_works_on_the_direct_exchange_too(self, vk_client, settings):
        issued = await vk_client.post("/auth/token", headers={"X-Api-Key": "sk-bf-active"})
        assert issued.status_code == 200
        body = issued.json()
        assert body["subject"] == "vk:team-ana"
        lifetime = body["expires_at"] - int(__import__("time").time())
        assert lifetime <= settings.vk_token_ttl_days * 86_400 + 5, "virtual-key sessions are short lived"

    @pytest.mark.parametrize(
        ("key", "status", "detail"),
        [
            ("sk-bf-inactive", 401, "inactive"),  # Bifrost answers 200: is_active decides
            ("sk-bf-expired", 401, "expired"),
            ("sk-bf-unknown", 401, "invalid virtual key"),
        ],
    )
    async def test_rejections(self, vk_client, key, status, detail):
        grant = await start(vk_client)
        rejected = await vk_client.post("/login/approve", json={"user_code": grant["user_code"], "api_key": key})
        assert rejected.status_code == status
        assert detail in rejected.json()["detail"]

    async def test_bifrost_failing_is_a_503_not_a_bad_credential(self, vk_client):
        grant = await start(vk_client)
        failed = await vk_client.post("/login/approve", json={"user_code": grant["user_code"], "api_key": "sk-bf-broken"})
        assert failed.status_code == 503
        assert "Bifrost unavailable" in failed.json()["detail"]

    async def test_virtual_keys_are_refused_when_bifrost_is_not_configured(self, client):
        grant = await start(client)
        rejected = await client.post("/login/approve", json={"user_code": grant["user_code"], "api_key": "sk-bf-active"})
        assert rejected.status_code == 401
        assert "not accepted" in rejected.json()["detail"]

    async def test_local_api_keys_keep_working_alongside(self, vk_client):
        issued = await vk_client.post("/auth/token", headers={"X-Api-Key": API_KEY})
        assert issued.status_code == 200 and issued.json()["subject"] == "ci-runner"

    async def test_a_vk_session_dies_if_bifrost_is_switched_off(self, settings, bifrost_url):
        on = create_app(replace(settings, bifrost_url=bifrost_url))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=on), base_url="https://cfg.corp.example") as c:
            token = (await c.post("/auth/token", headers={"X-Api-Key": "sk-bf-active"})).json()["token"]
        off = create_app(settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=off), base_url="https://cfg.corp.example") as c:
            denied = await c.get("/config/opencode.json", headers={"Authorization": f"Bearer {token}"})
            assert denied.status_code == 401
