"""The web admin panel: access control, and every write it can make.

The panel edits the same files IT edits by hand, so the assertions look at the
files on disk and at what employees are then served, not only at the API.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from opencode_config_server import admin, configdoc, settings as settings_module, store
from opencode_config_server.app import create_app

from .conftest import API_KEY, PASSWORD

ADMIN_TOKEN = "admin-token-for-tests-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BASE = "https://cfg.corp.example"


@pytest.fixture
def admin_settings(settings):
    return replace(settings, admin_token=ADMIN_TOKEN)


@pytest.fixture
async def panel(admin_settings) -> httpx.AsyncClient:
    app = create_app(admin_settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield client


@pytest.fixture
async def browser(panel) -> httpx.AsyncClient:
    """The panel as the page uses it: a session cookie plus the CSRF header."""
    response = await panel.post("/admin/api/session", json={"token": ADMIN_TOKEN})
    assert response.status_code == 200, response.text
    panel.headers["X-OCS-Admin"] = "1"
    return panel


def basic(name: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()}


# ------------------------------------------------------------------ access


class TestAccess:
    async def test_the_panel_does_not_exist_without_a_token(self, client):
        assert (await client.get("/admin")).status_code == 404
        assert (await client.get("/admin/api/overview", headers=BEARER)).status_code == 404

    async def test_api_requires_credentials(self, panel):
        response = await panel.get("/admin/api/overview")
        assert response.status_code == 401

    async def test_bearer_token_for_scripts(self, panel):
        assert (await panel.get("/admin/api/overview", headers=BEARER)).status_code == 200
        wrong = await panel.get("/admin/api/overview", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

    async def test_wrong_tokens_are_rate_limited(self, admin_settings):
        app = create_app(replace(admin_settings, rate_limit_attempts=3))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
            for _ in range(3):
                await client.post("/admin/api/session", json={"token": "wrong"})
            blocked = await client.post("/admin/api/session", json={"token": ADMIN_TOKEN})
            assert blocked.status_code == 429

    async def test_session_cookie_is_locked_down(self, panel):
        response = await panel.post("/admin/api/session", json={"token": ADMIN_TOKEN})
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/admin" in cookie
        assert "Secure" in cookie, "the base URL is https"
        assert ADMIN_TOKEN not in cookie

    async def test_cookie_requests_need_the_csrf_header(self, panel):
        await panel.post("/admin/api/session", json={"token": ADMIN_TOKEN})
        without = await panel.get("/admin/api/overview")
        assert without.status_code == 403
        assert (await panel.get("/admin/api/overview", headers={"X-OCS-Admin": "1"})).status_code == 200

    async def test_a_tampered_or_expired_cookie_is_refused(self, panel, admin_settings):
        sessions = admin.AdminSessions(admin_settings.session_secret, ADMIN_TOKEN, 3600)
        cookie, _ = sessions.issue()
        assert sessions.valid(cookie)
        expires, _, signature = cookie.partition(".")
        assert not sessions.valid(f"{int(expires) + 999}.{signature}")
        old, _ = sessions.issue(now=time.time() - 7200)
        assert not sessions.valid(old)
        panel.cookies.set(admin.COOKIE, f"{int(expires) + 999}.{signature}", domain="cfg.corp.example", path="/admin")
        assert (await panel.get("/admin/api/overview", headers={"X-OCS-Admin": "1"})).status_code == 401

    async def test_rotating_the_admin_token_ends_sessions(self, admin_settings):
        before = admin.AdminSessions(admin_settings.session_secret, ADMIN_TOKEN, 3600)
        after = admin.AdminSessions(admin_settings.session_secret, ADMIN_TOKEN + "-rotated", 3600)
        cookie, _ = before.issue()
        assert not after.valid(cookie)

    async def test_sign_out_clears_the_cookie(self, browser):
        response = await browser.delete("/admin/api/session")
        assert response.status_code == 200
        assert 'ocs_admin=""' in response.headers["set-cookie"] or "Max-Age=0" in response.headers["set-cookie"]

    async def test_employee_session_tokens_do_not_open_the_panel(self, panel):
        token = (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        assert (await panel.get("/admin/api/overview", headers={"Authorization": f"Bearer {token}"})).status_code == 401


class TestPage:
    async def test_is_served_with_a_strict_csp(self, panel):
        page = await panel.get("/admin")
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"
        assert "/admin/static/admin.js" in page.text
        assert "<script>" not in page.text, "no inline script: the CSP would block it"

    async def test_only_the_known_assets_are_served(self, panel):
        script = await panel.get("/admin/static/admin.js")
        assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
        assert (await panel.get("/admin/static/admin.css")).status_code == 200
        assert (await panel.get("/admin/static/index.html")).status_code == 404
        assert (await panel.get("/admin/static/..%2Fadmin.py")).status_code == 404

    def test_the_script_never_writes_html(self):
        source = (Path(admin.__file__).with_name("panel") / "admin.js").read_text(encoding="utf-8")
        assert "innerHTML" not in source and "insertAdjacentHTML" not in source and "outerHTML" not in source

    async def test_hidden_from_the_public_openapi_document(self, panel):
        schema = (await panel.get("/openapi.json")).json()
        assert not any(path.startswith("/admin") for path in schema["paths"])


# ------------------------------------------------------------------ configuration


class TestConfiguration:
    async def test_reports_the_file_as_stored(self, browser, config_dir):
        state = (await browser.get("/admin/api/config")).json()
        assert state["file"] == "opencode.jsonc"
        assert state["has_comments"] is True
        assert state["text"] == (config_dir / "opencode.jsonc").read_text(encoding="utf-8")
        assert state["etag"] == store.etag((config_dir / "opencode.jsonc").read_bytes())
        assert state["errors"] == [] and state["document"]["share"] == "disabled"

    async def test_text_saves_keep_comments_and_reach_employees(self, browser, config_dir, client):
        state = (await browser.get("/admin/api/config")).json()
        text = state["text"].replace('"share": "disabled"', '"share": "manual"')
        saved = await browser.put("/admin/api/config", json={"text": text, "etag": state["etag"]})
        assert saved.status_code == 200, saved.text
        on_disk = (config_dir / "opencode.jsonc").read_text(encoding="utf-8")
        assert "// " in on_disk and '"share": "manual"' in on_disk

        token = (await client.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        served = (await client.get("/config/opencode.json", headers={"Authorization": f"Bearer {token}"})).json()
        assert served["share"] == "manual"

    async def test_form_saves_write_plain_json(self, browser, config_dir):
        state = (await browser.get("/admin/api/config")).json()
        document = state["document"]
        document["agents"]["helper"] = {"description": "Helps", "mode": "subagent"}
        saved = await browser.put("/admin/api/config", json={"document": document, "etag": state["etag"]})
        assert saved.status_code == 200, saved.text
        on_disk = json.loads((config_dir / "opencode.jsonc").read_text(encoding="utf-8"))
        assert on_disk["agents"]["helper"]["mode"] == "subagent"
        assert saved.json()["has_comments"] is False

    async def test_a_stale_editor_cannot_overwrite_a_newer_file(self, browser, config_dir):
        state = (await browser.get("/admin/api/config")).json()
        (config_dir / "opencode.jsonc").write_text('{"share": "auto"}', encoding="utf-8")
        response = await browser.put("/admin/api/config", json={"text": state["text"], "etag": state["etag"]})
        assert response.status_code == 409
        assert json.loads((config_dir / "opencode.jsonc").read_text())["share"] == "auto"

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("{ not json", "not valid JSON"),
            ('{"agents": {"x": {"mode": "boss"}}}', "agents.x.mode"),
            ('{"commands": {"c": {"description": "no template"}}}', "commands.c.template"),
            ('{"instructions": ["{file:~/.ssh/id_rsa}"]}', "refusing to serve"),
        ],
    )
    async def test_invalid_documents_are_not_written(self, browser, config_dir, text, message):
        before = (config_dir / "opencode.jsonc").read_bytes()
        state = (await browser.get("/admin/api/config")).json()
        response = await browser.put("/admin/api/config", json={"text": text, "etag": state["etag"]})
        assert response.status_code == 422
        body = response.json()
        assert message in body["detail"] + " ".join(body["errors"])
        assert (config_dir / "opencode.jsonc").read_bytes() == before

    async def test_check_previews_what_employees_receive(self, browser, settings):
        response = await browser.post("/admin/api/config/check", json={"text": '{"skills": ["{server_url}/skills/{session_token}/"], "x": 1}'})
        body = response.json()
        assert body["rendered"]["skills"] == [f"{settings.base_url}/skills/{{env:{settings.session_env}}}/"]
        assert any("'x'" in warning for warning in body["warnings"])

    async def test_every_save_can_be_undone(self, browser, config_dir):
        original = (config_dir / "opencode.jsonc").read_text(encoding="utf-8")
        state = (await browser.get("/admin/api/config")).json()
        await browser.put("/admin/api/config", json={"text": '{"share": "auto"}', "etag": state["etag"]})

        history = (await browser.get("/admin/api/history", params={"doc": "config"})).json()
        assert history["viewable"] is True and len(history["versions"]) == 1
        version = history["versions"][0]["id"]
        old = (await browser.get(f"/admin/api/history/{version}", params={"doc": "config"})).json()
        assert old["text"] == original

        restored = await browser.post(f"/admin/api/history/{version}/restore", params={"doc": "config"}, json={})
        assert restored.status_code == 200
        assert (config_dir / "opencode.jsonc").read_text(encoding="utf-8") == original

    async def test_history_ids_cannot_escape(self, browser):
        response = await browser.get("/admin/api/history/..%2F..%2Fusers.json", params={"doc": "config"})
        assert response.status_code == 404

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    async def test_a_read_only_config_directory_is_reported(self, browser, config_dir):
        state = (await browser.get("/admin/api/config")).json()
        config_dir.chmod(0o555)
        try:
            assert (await browser.get("/admin/api/config")).json()["writable"] is False
            response = await browser.put("/admin/api/config", json={"text": "{}", "etag": state["etag"]})
            assert response.status_code == 409 and "read-only" in response.json()["detail"]
        finally:
            config_dir.chmod(0o755)


# ------------------------------------------------------------------ users


def users_on_disk(config_dir: Path) -> dict:
    return {entry["name"]: entry for entry in json.loads((config_dir / "users.json").read_text())["users"]}


class TestUsers:
    async def test_listing_never_exposes_hashes(self, browser):
        listing = (await browser.get("/admin/api/users")).json()
        names = {user["name"]: user for user in listing["users"]}
        assert names["ana.ruiz"]["password"] is True
        assert len(names["ci-runner"]["api_keys"]) == 1
        assert names["ex.employee"]["disabled"] is True
        assert "pbkdf2" not in json.dumps(listing) and "sha256:" not in json.dumps(listing)

    async def test_created_accounts_can_sign_in(self, browser, panel, config_dir):
        created = await browser.post("/admin/api/users", json={"name": "new.hire", "password": "a-long-password-1", "api_key": True})
        assert created.status_code == 200, created.text
        key = created.json()["api_key"]
        assert key.startswith("ock_")
        assert (await panel.post("/auth/token", headers=basic("new.hire", "a-long-password-1"))).status_code == 200
        assert (await panel.post("/auth/token", headers={"X-Api-Key": key})).status_code == 200

    @pytest.mark.parametrize(
        ("body", "status"),
        [
            ({"name": "ana.ruiz"}, 409),
            ({"name": "has space"}, 422),
            ({"name": "vk:team"}, 422),
            ({"name": "short.pw", "password": "short"}, 422),
        ],
    )
    async def test_rejected_new_users(self, browser, body, status):
        assert (await browser.post("/admin/api/users", json=body)).status_code == status

    async def test_disabling_cuts_off_existing_sessions(self, browser, panel):
        token = (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert (await panel.get("/config/opencode.json", headers=auth)).status_code == 200
        await browser.patch("/admin/api/users/ci-runner", json={"disabled": True})
        assert (await panel.get("/config/opencode.json", headers=auth)).status_code == 401
        await browser.patch("/admin/api/users/ci-runner", json={"disabled": False})
        assert (await panel.get("/config/opencode.json", headers=auth)).status_code == 200

    async def test_sign_out_everywhere_revokes_tokens(self, browser, panel, config_dir):
        token = (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        revoked = await browser.post("/admin/api/users/ci-runner/revoke")
        assert revoked.json()["user"]["version"] == 2
        assert users_on_disk(config_dir)["ci-runner"]["version"] == 2
        response = await panel.get("/config/opencode.json", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    async def test_changing_a_password(self, browser, panel):
        changed = await browser.patch("/admin/api/users/ana.ruiz", json={"password": "the-new-password-9"})
        assert changed.status_code == 200
        assert (await panel.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).status_code == 401
        assert (await panel.post("/auth/token", headers=basic("ana.ruiz", "the-new-password-9"))).status_code == 200

    async def test_api_keys_are_added_and_removed_by_fingerprint(self, browser, panel):
        added = (await browser.post("/admin/api/users/ana.ruiz/api-keys")).json()
        assert (await panel.post("/auth/token", headers={"X-Api-Key": added["api_key"]})).status_code == 200
        removed = await browser.delete(f"/admin/api/users/ana.ruiz/api-keys/{added['fingerprint']}")
        assert removed.status_code == 200 and removed.json()["user"]["api_keys"] == []
        assert (await panel.post("/auth/token", headers={"X-Api-Key": added["api_key"]})).status_code == 401
        assert (await browser.delete("/admin/api/users/ana.ruiz/api-keys/000000000000")).status_code == 404

    async def test_deleting_a_user(self, browser, panel, config_dir):
        assert (await browser.delete("/admin/api/users/ci-runner")).status_code == 200
        assert "ci-runner" not in users_on_disk(config_dir)
        assert (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).status_code == 401
        assert (await browser.delete("/admin/api/users/ci-runner")).status_code == 404

    async def test_unknown_fields_in_users_json_survive_edits(self, browser, config_dir):
        path = config_dir / "users.json"
        document = json.loads(path.read_text())
        document["users"][0]["email"] = "ana@corp.example"
        document["note"] = "managed by IT"
        path.write_text(json.dumps(document))
        await browser.post("/admin/api/users/ana.ruiz/revoke")
        after = json.loads(path.read_text())
        assert after["note"] == "managed by IT"
        assert users_on_disk(config_dir)["ana.ruiz"]["email"] == "ana@corp.example"

    async def test_a_broken_users_json_is_never_overwritten(self, browser, config_dir):
        (config_dir / "users.json").write_text("{ broken", encoding="utf-8")
        response = await browser.post("/admin/api/users", json={"name": "someone"})
        assert response.status_code == 409 and "fix it by hand" in response.json()["detail"]
        assert (config_dir / "users.json").read_text() == "{ broken"

    async def test_old_versions_restore_but_never_display(self, browser, panel, config_dir):
        await browser.delete("/admin/api/users/ci-runner")
        history = (await browser.get("/admin/api/history", params={"doc": "users"})).json()
        assert history["viewable"] is False
        version = history["versions"][0]["id"]
        assert (await browser.get(f"/admin/api/history/{version}", params={"doc": "users"})).status_code == 403
        assert (await browser.post(f"/admin/api/history/{version}/restore", params={"doc": "users"}, json={})).status_code == 200
        assert (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).status_code == 200


# ------------------------------------------------------------------ skills


class TestSkills:
    async def test_lists_files_with_publication_status(self, browser):
        listing = (await browser.get("/admin/api/skills")).json()
        skill = next(item for item in listing["skills"] if item["name"] == "corp-standards")
        files = {item["path"]: item["published"] for item in skill["files"]}
        assert files["SKILL.md"] is True
        assert files["private.key"] is False
        assert skill["published"] is True and skill["version"]

    async def test_unpublished_files_are_not_readable(self, browser):
        response = await browser.get("/admin/api/skills/corp-standards/files/private.key")
        assert response.status_code == 422

    async def test_a_new_skill_is_published_to_employees(self, browser, panel, config_dir):
        created = await browser.post("/admin/api/skills", json={"name": "release-notes", "description": "Use when writing release notes"})
        assert created.status_code == 200, created.text
        text = (config_dir / "skills" / "release-notes" / "SKILL.md").read_text()
        assert "description: Use when writing release notes" in text

        token = (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        index = (await panel.get(f"/skills/{token}/index.json")).json()
        assert "release-notes" in {skill["name"] for skill in index["skills"]}

    async def test_editing_a_file_changes_the_published_version(self, browser):
        before = next(s for s in (await browser.get("/admin/api/skills")).json()["skills"] if s["name"] == "corp-standards")
        current = (await browser.get("/admin/api/skills/corp-standards/files/SKILL.md")).json()
        saved = await browser.put(
            "/admin/api/skills/corp-standards/files/SKILL.md",
            json={"text": current["text"] + "\nMore rules.\n", "etag": current["etag"]},
        )
        assert saved.status_code == 200
        after = next(s for s in (await browser.get("/admin/api/skills")).json()["skills"] if s["name"] == "corp-standards")
        assert after["version"] != before["version"], "employees only re-download on a new version"

        stale = await browser.put("/admin/api/skills/corp-standards/files/SKILL.md", json={"text": "x", "etag": current["etag"]})
        assert stale.status_code == 409

    async def test_new_files_and_deletion(self, browser, config_dir):
        created = await browser.put("/admin/api/skills/corp-standards/files/references/new.md", json={"text": "# New\n", "etag": "absent"})
        assert created.status_code == 200
        assert (config_dir / "skills/corp-standards/references/new.md").read_text() == "# New\n"
        again = await browser.put("/admin/api/skills/corp-standards/files/references/new.md", json={"text": "# Other\n", "etag": "absent"})
        assert again.status_code == 409, "creating must not clobber an existing file"
        assert (await browser.delete("/admin/api/skills/corp-standards/files/references/new.md")).status_code == 200
        assert not (config_dir / "skills/corp-standards/references/new.md").exists()

    @pytest.mark.parametrize(
        ("path", "status"),
        [
            # The client normalises plain dot segments away (404); encoded ones reach the check.
            ("..%2Fescape.md", 422),
            ("references%2F..%2F..%2Fescape.md", 422),
            (".hidden.md", 422),
            ("binary.exe", 422),
            ("notes", 422),
        ],
    )
    async def test_paths_are_confined(self, browser, path, status):
        response = await browser.put(f"/admin/api/skills/corp-standards/files/{path}", json={"text": "x", "etag": "absent"})
        assert response.status_code == status

    async def test_size_limit(self, browser):
        big = "x" * (2 * 1024 * 1024 + 1)
        response = await browser.put("/admin/api/skills/corp-standards/files/big.md", json={"text": big, "etag": "absent"})
        assert response.status_code == 413

    async def test_warnings_for_a_skill_agents_would_ignore(self, browser):
        await browser.post("/admin/api/skills", json={"name": "vague", "description": "x"})
        current = (await browser.get("/admin/api/skills/vague/files/SKILL.md")).json()
        saved = await browser.put("/admin/api/skills/vague/files/SKILL.md", json={"text": "# no front matter\n", "etag": current["etag"]})
        assert any("no description" in warning for warning in saved.json()["warnings"])

    async def test_deleting_a_skill_keeps_its_files_in_history(self, browser, config_dir):
        assert (await browser.delete("/admin/api/skills/corp-standards")).status_code == 200
        assert not (config_dir / "skills/corp-standards").exists()
        history = (await browser.get("/admin/api/history", params={"doc": "skill:corp-standards/SKILL.md"})).json()
        assert len(history["versions"]) == 1

    async def test_bad_skill_names(self, browser):
        assert (await browser.post("/admin/api/skills", json={"name": "../x", "description": "d"})).status_code == 422
        assert (await browser.post("/admin/api/skills", json={"name": "corp-standards", "description": "d"})).status_code == 409
        assert (await browser.post("/admin/api/skills", json={"name": "ok", "description": "  "})).status_code == 422


# ------------------------------------------------------------------ installer


class TestInstaller:
    async def test_syntax_errors_are_refused(self, browser, config_dir):
        before = (config_dir / "bootstrap/install.sh").read_text()
        state = (await browser.get("/admin/api/bootstrap")).json()
        response = await browser.put("/admin/api/bootstrap", json={"text": "if then fi\n", "etag": state["etag"]})
        assert response.status_code == 422
        assert any("syntax error" in error for error in response.json()["errors"])
        assert (config_dir / "bootstrap/install.sh").read_text() == before

    async def test_a_saved_installer_is_what_employees_download(self, browser, panel):
        state = (await browser.get("/admin/api/bootstrap")).json()
        script = "#!/usr/bin/env bash\necho installed\n"
        saved = await browser.put("/admin/api/bootstrap", json={"text": script, "etag": state["etag"]})
        assert saved.status_code == 200
        token = (await panel.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        served = await panel.get("/bootstrap/install.sh", headers={"Authorization": f"Bearer {token}"})
        assert served.text == script


# ------------------------------------------------------------------ store and checks


class TestStore:
    def test_writes_are_atomic_and_versioned(self, tmp_path):
        files = store.Store(tmp_path)
        target = tmp_path / "doc.txt"
        first = files.write("doc", target, b"one", expected="absent")
        files.write("doc", target, b"two", expected=first)
        assert target.read_bytes() == b"two"
        assert [files.snapshot("doc", item.id) for item in files.history("doc")] == [b"one"]
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]

    def test_conflicts(self, tmp_path):
        files = store.Store(tmp_path)
        target = tmp_path / "doc.txt"
        files.write("doc", target, b"one")
        with pytest.raises(store.Conflict):
            files.write("doc", target, b"two", expected=store.etag(b"something else"))
        with pytest.raises(store.Conflict):
            files.write("doc", target, b"two", expected="absent")

    def test_history_is_pruned_and_private(self, tmp_path):
        files = store.Store(tmp_path)
        target = tmp_path / "doc.txt"
        for number in range(store.KEEP + 5):
            files.write("doc", target, str(number).encode())
        versions = files.history("doc")
        assert len(versions) == store.KEEP
        assert files.snapshot("doc", versions[0].id) == str(store.KEEP + 3).encode()
        assert oct(files.root.stat().st_mode & 0o777) == "0o700"

    def test_file_mode_is_preserved(self, tmp_path):
        files = store.Store(tmp_path)
        target = tmp_path / "secret.json"
        files.write("users", target, b"{}", mode=0o600)
        files.write("users", target, b'{"a": 1}')
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_snapshot_ids_are_validated(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            store.Store(tmp_path).snapshot("doc", "../../etc/passwd")


class TestShapeChecks:
    def test_the_shipped_document_is_clean(self):
        text = (Path(__file__).parent.parent / "config" / "opencode.jsonc").read_text(encoding="utf-8")
        assert configdoc.check(configdoc.parse(text)) == ([], [])

    def test_literal_secrets_in_headers_are_flagged(self):
        _, warnings = configdoc.check(
            {"mcp": {"servers": {"docs": {"type": "remote", "url": "https://x", "headers": {"authorization": "Bearer abc"}}}}}
        )
        assert any("{env:NAME}" in warning for warning in warnings)

    def test_v1_style_mcp_is_flagged(self):
        _, warnings = configdoc.check({"mcp": {"docs": {"type": "remote", "url": "https://x"}}})
        assert any("mcp.servers" in warning for warning in warnings)

    def test_policies_use_their_own_vocabulary(self):
        errors, _ = configdoc.check({"policies": [{"action": "shell", "resource": "*", "effect": "ask"}]})
        assert len(errors) == 2


class TestSettings:
    def test_production_wants_a_long_admin_token(self, config_dir, monkeypatch):
        monkeypatch.setenv("OCS_ADMIN_TOKEN", "short")
        with pytest.raises(settings_module.SettingsError, match="OCS_ADMIN_TOKEN"):
            settings_module.load(
                environment="production", base_url="https://cfg.corp.example", config_dir=config_dir,
                session_secret="x" * 48,
            )

    def test_the_token_is_kept_out_of_repr(self, admin_settings):
        assert ADMIN_TOKEN not in repr(admin_settings)

    def test_the_panel_assets_ship_with_the_package(self):
        text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(r'"panel/\*"', text)
