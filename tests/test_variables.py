"""Per-user and global variables filled into {env:NAME} when the config is served."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from opencode_config_server import configdoc, variables
from opencode_config_server.app import create_app

from .conftest import API_KEY, PASSWORD
from .test_admin import ADMIN_TOKEN, BASE, BEARER, basic

PROVIDERS = {
    "$schema": "https://opencode.ai/config.json",
    "providers": {
        "openai": {"settings": {"apiKey": "{env:OPENAI_API_KEY}"}},
        "bifrost": {
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": "{env:BIFROST_URL}/v1", "apiKey": "{env:BIFROST_API_KEY}"},
        },
    },
    "instructions": ["{env:LEFT_FOR_THE_MACHINE}"],
}


@pytest.fixture
async def server(settings, config_dir) -> httpx.AsyncClient:
    (config_dir / "opencode.jsonc").write_text(json.dumps(PROVIDERS), encoding="utf-8")
    app = create_app(replace(settings, admin_token=ADMIN_TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield client


async def served(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    token = (await client.post("/auth/token", headers=headers)).json()["token"]
    response = await client.get("/config/opencode.json", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


async def define(client, path: str, value: str | None, *, secret: bool = True) -> httpx.Response:
    return await client.put(f"/admin/api/variables/{path}", json={"value": value, "secret": secret}, headers=BEARER)


class TestServing:
    async def test_each_employee_gets_their_own_values(self, server):
        await define(server, "global/BIFROST_URL", "https://bifrost.corp", secret=False)
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        await define(server, "users/ana.ruiz/OPENAI_API_KEY", "sk-openai-for-ana")

        ci = await served(server, {"X-Api-Key": API_KEY})
        assert ci["providers"]["openai"]["settings"]["apiKey"] == "sk-openai-for-ci"
        assert ci["providers"]["bifrost"]["settings"]["baseURL"] == "https://bifrost.corp/v1"

        ana = await served(server, basic("ana.ruiz", PASSWORD))
        assert ana["providers"]["openai"]["settings"]["apiKey"] == "sk-openai-for-ana"

    async def test_undefined_names_are_left_for_the_machine(self, server):
        document = await served(server, {"X-Api-Key": API_KEY})
        assert document["providers"]["bifrost"]["settings"]["apiKey"] == "{env:BIFROST_API_KEY}"
        assert document["instructions"] == ["{env:LEFT_FOR_THE_MACHINE}"]

    async def test_a_user_value_wins_over_the_global_one(self, server):
        await define(server, "global/BIFROST_API_KEY", "sk-bf-shared")
        await define(server, "users/ci-runner/BIFROST_API_KEY", "sk-bf-ci-own")
        ci = await served(server, {"X-Api-Key": API_KEY})
        ana = await served(server, basic("ana.ruiz", PASSWORD))
        assert ci["providers"]["bifrost"]["settings"]["apiKey"] == "sk-bf-ci-own"
        assert ana["providers"]["bifrost"]["settings"]["apiKey"] == "sk-bf-shared"

    async def test_the_file_on_disk_never_holds_the_key(self, server, config_dir):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        assert "sk-openai-for-ci" not in (config_dir / "opencode.jsonc").read_text()
        assert oct((config_dir / "variables.json").stat().st_mode & 0o777) == "0o600"

    async def test_removing_a_value_stops_serving_it(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        assert (await server.delete("/admin/api/variables/users/ci-runner/OPENAI_API_KEY", headers=BEARER)).status_code == 200
        document = await served(server, {"X-Api-Key": API_KEY})
        assert document["providers"]["openai"]["settings"]["apiKey"] == "{env:OPENAI_API_KEY}"

    async def test_a_broken_file_keeps_the_last_good_values(self, server, config_dir):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        assert (await served(server, {"X-Api-Key": API_KEY}))["providers"]["openai"]["settings"]["apiKey"] == "sk-openai-for-ci"
        path = config_dir / "variables.json"
        path.write_text("{ broken", encoding="utf-8")
        import os

        stat = path.stat()
        os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))
        document = await served(server, {"X-Api-Key": API_KEY})
        assert document["providers"]["openai"]["settings"]["apiKey"] == "sk-openai-for-ci"
        health = (await server.get("/health")).json()
        assert health["status"] == "degraded" and any("variables.json" in problem for problem in health["problems"])


class TestAdminApi:
    async def test_secret_values_never_come_back(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        await define(server, "global/BIFROST_URL", "https://bifrost.corp", secret=False)
        listing = (await server.get("/admin/api/variables", headers=BEARER)).json()
        assert "sk-openai-for-ci" not in json.dumps(listing)
        assert listing["users"]["ci-runner"] == [{"name": "OPENAI_API_KEY", "secret": True, "machine": False, "value": None}]
        assert listing["global"] == [{"name": "BIFROST_URL", "secret": False, "machine": False, "value": "https://bifrost.corp"}]
        assert "ci-runner" in listing["known_users"]

    async def test_flipping_secret_keeps_the_value(self, server):
        await define(server, "global/BIFROST_URL", "https://bifrost.corp", secret=False)
        assert (await define(server, "global/BIFROST_URL", None, secret=True)).status_code == 200
        document = await served(server, {"X-Api-Key": API_KEY})
        assert document["providers"]["bifrost"]["settings"]["baseURL"] == "https://bifrost.corp/v1"

    @pytest.mark.parametrize(
        ("path", "value", "status"),
        [
            ("global/OPENCODE_CORP_SESSION", "x", 422),  # reserved: the session token
            ("global/BAD-NAME", "x", 422),
            ("global/LEAK", "{file:~/.ssh/id_rsa}", 422),
            ("global/NESTED", "{env:HOME}", 422),
            ("global/NEW_ONE", None, 422),  # nothing to keep
            ("users/nobody/OPENAI_API_KEY", "x", 404),
        ],
    )
    async def test_rejected_variables(self, server, path, value, status):
        assert (await define(server, path, value)).status_code == status

    async def test_values_stay_out_of_the_log(self, server, caplog):
        caplog.set_level("INFO")
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        assert "sk-openai-for-ci" not in caplog.text
        assert "OPENAI_API_KEY" in caplog.text

    async def test_history_restores_without_showing(self, server):
        await define(server, "global/BIFROST_URL", "https://one.example", secret=False)
        await define(server, "global/BIFROST_URL", "https://two.example", secret=False)
        history = (await server.get("/admin/api/history", params={"doc": "variables"}, headers=BEARER)).json()
        assert history["viewable"] is False
        version = history["versions"][0]["id"]
        assert (await server.get(f"/admin/api/history/{version}", params={"doc": "variables"}, headers=BEARER)).status_code == 403
        await server.post(f"/admin/api/history/{version}/restore", params={"doc": "variables"}, json={}, headers=BEARER)
        document = await served(server, {"X-Api-Key": API_KEY})
        assert document["providers"]["bifrost"]["settings"]["baseURL"] == "https://one.example/v1"

    async def test_deleting_a_user_drops_their_variables(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        await server.delete("/admin/api/users/ci-runner", headers=BEARER)
        assert "ci-runner" not in (await server.get("/admin/api/variables", headers=BEARER)).json()["users"]

    async def test_preview_as_a_user_masks_secrets(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-openai-for-ci")
        await define(server, "global/BIFROST_URL", "https://bifrost.corp", secret=False)
        preview = (await server.post("/admin/api/config/check", json={"text": json.dumps(PROVIDERS), "as_user": "ci-runner"}, headers=BEARER)).json()
        assert preview["rendered"]["providers"]["openai"]["settings"]["apiKey"] == "<secret OPENAI_API_KEY>"
        assert preview["rendered"]["providers"]["bifrost"]["settings"]["baseURL"] == "https://bifrost.corp/v1"
        assert preview["filled"] == ["BIFROST_URL", "OPENAI_API_KEY"]
        assert preview["unresolved"] == ["BIFROST_API_KEY", "LEFT_FOR_THE_MACHINE"]
        assert "sk-openai-for-ci" not in json.dumps(preview)


class TestUnits:
    def test_virtual_key_sessions_only_get_global_values(self):
        document = {"global": {"A": {"value": "g"}}, "users": {"vk:team": {"A": {"value": "u"}}}}
        assert variables.resolve(document, "vk:team") == {"A": "g"}

    def test_apply_only_touches_defined_names(self):
        assert variables.apply({"x": ["{env:A}-{env:B}"], "n": 1}, {"A": "1"}) == {"x": ["1-{env:B}"], "n": 1}

    def test_parse_rejects_bad_shapes(self):
        with pytest.raises(variables.VariableError):
            variables.parse(b'{"global": {"A": "plain string"}}')
        assert variables.parse(None) == {"global": {}, "users": {}}

    def test_literal_provider_keys_are_flagged(self):
        _, warnings = configdoc.check({"providers": {"openai": {"settings": {"apiKey": "sk-live-123"}}}})
        assert any("define NAME under Variables" in warning for warning in warnings)
        _, clean = configdoc.check({"providers": {"openai": {"settings": {"apiKey": "{env:OPENAI_API_KEY}"}}}})
        assert clean == []


# ------------------------------------------------------------------ machine environment


class TestMachineEndpoint:
    async def test_only_machine_variables_are_listed(self, server):
        await server.put("/admin/api/variables/global/HINDSIGHT_URL", json={"value": "https://h.corp", "machine": True}, headers=BEARER)
        await server.put("/admin/api/variables/global/BIFROST_URL", json={"value": "https://b.corp"}, headers=BEARER)
        await server.put(
            "/admin/api/variables/users/ci-runner/HINDSIGHT_API_KEY",
            json={"value": "hs key with spaces", "secret": True, "machine": True}, headers=BEARER,
        )
        token = (await server.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        listing = await server.get("/bootstrap/env", headers={"Authorization": f"Bearer {token}"})
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        lines = dict(line.split(" ") for line in listing.text.splitlines())
        assert set(lines) == {"HINDSIGHT_URL", "HINDSIGHT_API_KEY"}
        import base64

        assert base64.b64decode(lines["HINDSIGHT_API_KEY"]).decode() == "hs key with spaces"

    async def test_requires_a_session(self, server):
        assert (await server.get("/bootstrap/env")).status_code == 401

    async def test_listing_shows_the_flag(self, server):
        await server.put("/admin/api/variables/global/HINDSIGHT_URL", json={"value": "x", "machine": True}, headers=BEARER)
        listing = (await server.get("/admin/api/variables", headers=BEARER)).json()
        assert listing["global"][0]["machine"] is True


class TestOwnValuesWin:
    """The employee's own environment wins over the company's values."""

    async def test_names_reported_at_login_are_left_for_the_machine(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-company-for-ci")
        await define(server, "global/BIFROST_URL", "https://bifrost.corp", secret=False)
        token = (await server.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        names = (await server.get("/bootstrap/names", headers=auth)).text.split()
        assert names == ["BIFROST_URL", "OPENAI_API_KEY"], "names only, never values"

        reported = await server.post("/bootstrap/own-env", json={"names": ["OPENAI_API_KEY", "NOT_A_COMPANY_VAR"]}, headers=auth)
        assert reported.json()["own"] == ["OPENAI_API_KEY"]

        document = (await server.get("/config/opencode.json", headers=auth)).json()
        assert document["providers"]["openai"]["settings"]["apiKey"] == "{env:OPENAI_API_KEY}", "theirs is used"
        assert document["providers"]["bifrost"]["settings"]["baseURL"] == "https://bifrost.corp/v1"

        ana = await served(server, basic("ana.ruiz", PASSWORD))
        assert ana["providers"]["bifrost"]["settings"]["baseURL"] == "https://bifrost.corp/v1", "others unaffected"

        await server.post("/bootstrap/own-env", json={"names": []}, headers=auth)
        document = (await server.get("/config/opencode.json", headers=auth)).json()
        assert document["providers"]["openai"]["settings"]["apiKey"] == "sk-company-for-ci", "a later login can hand it back"

    async def test_the_panel_shows_whose_value_is_used(self, server):
        await define(server, "users/ci-runner/OPENAI_API_KEY", "sk-company-for-ci")
        token = (await server.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        await server.post("/bootstrap/own-env", json={"names": ["OPENAI_API_KEY"]}, headers={"Authorization": f"Bearer {token}"})
        listing = (await server.get("/admin/api/variables", headers=BEARER)).json()
        assert listing["own"] == {"ci-runner": ["OPENAI_API_KEY"]}
        preview = (await server.post("/admin/api/config/check", json={"text": json.dumps(PROVIDERS), "as_user": "ci-runner"}, headers=BEARER)).json()
        assert preview["kept"] == ["OPENAI_API_KEY"]
        assert preview["rendered"]["providers"]["openai"]["settings"]["apiKey"] == "{env:OPENAI_API_KEY}"

    async def test_survives_a_server_restart(self, settings, config_dir):
        from opencode_config_server import variables as module

        names = module.EmployeeNames(config_dir / ".state" / "employee-env.json")
        names.set("ci-runner", {"OPENAI_API_KEY"})
        assert module.EmployeeNames(config_dir / ".state" / "employee-env.json").get("ci-runner") == {"OPENAI_API_KEY"}
        assert oct((config_dir / ".state" / "employee-env.json").stat().st_mode & 0o777) == "0o600"


class TestMachineSync:
    """The login command, against a fake `opencode` that keeps a service env."""

    @pytest.fixture
    def fake_opencode(self, tmp_path):
        log = tmp_path / "opencode-calls.log"
        store = tmp_path / "service-env"
        store.mkdir()
        fake = tmp_path / "bin" / "opencode"
        fake.parent.mkdir()
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'log="{log}"; store="{store}"\n'
            'case "$1 $2" in\n'
            '  "service status") echo "http://127.0.0.1:4096" ;;\n'
            '  "service get") [ -f "$store/$4" ] && cat "$store/$4" ;;\n'
            '  "service set") printf "%s" "$5" > "$store/$4"; printf "set %s=%s\\n" "$4" "$5" >> "$log" ;;\n'
            '  "service unset") rm -f "$store/$4"; printf "unset %s\\n" "$4" >> "$log" ;;\n'
            '  "service start") echo start >> "$log" ;;\n'
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        return fake, log, store

    def wait_for(self, log, text, timeout=10.0):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if log.exists() and text in log.read_text():
                return log.read_text()
            time.sleep(0.1)
        raise AssertionError(f"{text!r} never appeared in {log.read_text() if log.exists() else '(no log)'}")

    @pytest.fixture
    def live(self, settings, config_dir):
        from .test_login_e2e import serve

        (config_dir / "opencode.jsonc").write_text(json.dumps(PROVIDERS), encoding="utf-8")
        with serve(replace(settings, admin_token=ADMIN_TOKEN)) as base_url:
            yield base_url, httpx.Client(base_url=base_url, headers=BEARER)

    def test_variables_reach_the_service_without_holding_the_login(self, live, tmp_path, fake_opencode):
        import time

        from .test_login_e2e import run_login

        base_url, admin = live
        fake, log, store = fake_opencode
        env = {"OPENCODE_CORP_TOKEN": API_KEY, "OPENCODE_CORP_OPENCODE_BIN": str(fake), "OPENCODE_CORP_ENV_DELAY": "2"}
        home = tmp_path / "home"
        home.mkdir()
        admin.put("/admin/api/variables/users/ci-runner/HINDSIGHT_API_KEY", json={"value": "hs key, with spaces", "secret": True, "machine": True}).raise_for_status()
        admin.put("/admin/api/variables/global/NOT_FOR_THE_MACHINE", json={"value": "x"}).raise_for_status()

        started = time.monotonic()
        result = run_login(base_url, env, home=home)
        assert result.returncode == 0, result.stderr
        assert time.monotonic() - started < 2, "the login must not wait for the detached job"
        assert "1 change(s) to apply" in result.stderr

        calls = self.wait_for(log, "start")
        assert "set HINDSIGHT_API_KEY=hs key, with spaces" in calls
        assert "NOT_FOR_THE_MACHINE" not in calls
        managed = home / ".config" / "opencode-corp" / "env.managed"
        assert [line.split()[0] for line in managed.read_text().splitlines()] == ["HINDSIGHT_API_KEY"]

        # Unchanged: no job, no restart.
        log.write_text("")
        assert run_login(base_url, env, home=home).returncode == 0
        time.sleep(2.5)
        assert log.read_text() == ""

        # Removed on the server, still the value we set: unset on the machine.
        admin.delete("/admin/api/variables/users/ci-runner/HINDSIGHT_API_KEY").raise_for_status()
        assert run_login(base_url, env, home=home).returncode == 0
        assert "unset HINDSIGHT_API_KEY" in self.wait_for(log, "start")
        assert managed.read_text() == ""

    def test_company_values_never_overwrite_the_employees(self, live, tmp_path, fake_opencode):
        from .test_login_e2e import run_login

        base_url, admin = live
        fake, log, store = fake_opencode
        home = tmp_path / "home"
        home.mkdir()
        # The employee already has their own: one in the environment OpenCode
        # runs with, one in the service configuration.
        (store / "HINDSIGHT_API_KEY").write_text("employee-hs")
        env = {
            "OPENCODE_CORP_TOKEN": API_KEY,
            "OPENCODE_CORP_OPENCODE_BIN": str(fake),
            "OPENCODE_CORP_ENV_DELAY": "0",
            "OPENAI_API_KEY": "employee-openai",
        }
        admin.put("/admin/api/variables/users/ci-runner/OPENAI_API_KEY", json={"value": "company-openai"}).raise_for_status()
        admin.put("/admin/api/variables/users/ci-runner/HINDSIGHT_API_KEY", json={"value": "company-hs", "machine": True}).raise_for_status()
        admin.put("/admin/api/variables/global/CORP_MACHINE_VAR", json={"value": "v1", "machine": True}).raise_for_status()

        result = run_login(base_url, env, home=home)
        assert result.returncode == 0, result.stderr
        assert "OPENAI_API_KEY: you already define it" in result.stderr
        assert "HINDSIGHT_API_KEY: you already define it" in result.stderr
        self.wait_for(log, "start")
        assert (store / "HINDSIGHT_API_KEY").read_text() == "employee-hs", "theirs, untouched"
        assert (store / "CORP_MACHINE_VAR").read_text() == "v1"

        # The served configuration leaves OPENAI_API_KEY to their machine.
        token = httpx.post(f"{base_url}/auth/token", headers={"X-Api-Key": API_KEY}).json()["token"]
        document = httpx.get(f"{base_url}/config/opencode.json", headers={"Authorization": f"Bearer {token}"}).json()
        assert document["providers"]["openai"]["settings"]["apiKey"] == "{env:OPENAI_API_KEY}"

        # The employee edits a value the login had set: from now on it is theirs.
        (store / "CORP_MACHINE_VAR").write_text("employee-edited")
        log.write_text("")
        admin.put("/admin/api/variables/global/CORP_MACHINE_VAR", json={"value": "v2", "machine": True}).raise_for_status()
        result = run_login(base_url, env, home=home)
        assert "CORP_MACHINE_VAR: you already define it" in result.stderr
        admin.delete("/admin/api/variables/global/CORP_MACHINE_VAR").raise_for_status()
        assert run_login(base_url, env, home=home).returncode == 0
        import time

        time.sleep(1)
        assert (store / "CORP_MACHINE_VAR").read_text() == "employee-edited", "never overwritten, never unset"
        assert "CORP_MACHINE_VAR" not in log.read_text()


class TestOwnSkills:
    """OpenCode loads remote skills after the employee's own folder, so the
    server leaves out the corporate skills they have their own version of."""

    async def test_reported_skills_leave_their_catalogue(self, server):
        token = (await server.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        reported = await server.post("/bootstrap/own-skills", json={"names": ["corp-standards", "not-a-corporate-skill"]}, headers=auth)
        assert reported.json()["own"] == ["corp-standards"], "only names the company publishes are kept"

        mine = (await server.get(f"/skills/{token}/index.json")).json()
        assert "corp-standards" not in {skill["name"] for skill in mine["skills"]}
        ana_token = (await server.post("/auth/token", headers=basic("ana.ruiz", PASSWORD))).json()["token"]
        theirs = (await server.get(f"/skills/{ana_token}/index.json")).json()
        assert "corp-standards" in {skill["name"] for skill in theirs["skills"]}, "others unaffected"

        listing = (await server.get("/admin/api/skills", headers=BEARER)).json()
        skill = next(item for item in listing["skills"] if item["name"] == "corp-standards")
        assert skill["kept_by"] == ["ci-runner"]

        await server.post("/bootstrap/own-skills", json={"names": []}, headers=auth)
        mine = (await server.get(f"/skills/{token}/index.json")).json()
        assert "corp-standards" in {skill["name"] for skill in mine["skills"]}, "a later login hands it back"

    def test_the_login_finds_them_like_opencode_does(self, settings, config_dir, tmp_path):
        from .test_login_e2e import run_login, serve

        home = tmp_path / "home"
        skills_dir = home / ".config" / "opencode" / "skills"
        (skills_dir / "team" / "corp-standards").mkdir(parents=True)
        (skills_dir / "team" / "corp-standards" / "SKILL.md").write_text("---\nname: corp-standards\ndescription: mine\n---\n")
        (skills_dir / ".hidden" / "ignored").mkdir(parents=True)
        (skills_dir / ".hidden" / "ignored" / "SKILL.md").write_text("x")
        (skills_dir / "my-notes.md").write_text("---\ndescription: personal\n---\n")
        with serve(replace(settings, admin_token=ADMIN_TOKEN)) as base_url:
            result = run_login(base_url, {"OPENCODE_CORP_TOKEN": API_KEY, "OPENCODE_CORP_SKIP_ENV": "1"}, home=home)
            assert result.returncode == 0, result.stderr
            assert "skill corp-standards: you have your own" in result.stderr
            assert "my-notes" not in result.stderr, "not a corporate skill: never reported"
            index = httpx.get(f"{base_url}/skills/{result.stdout}/index.json").json()
            assert "corp-standards" not in {skill["name"] for skill in index["skills"]}

            # Logging in again must keep it out, not flip it back in.
            again = run_login(base_url, {"OPENCODE_CORP_TOKEN": API_KEY, "OPENCODE_CORP_SKIP_ENV": "1"}, home=home)
            assert "skill corp-standards: you have your own" in again.stderr
            index = httpx.get(f"{base_url}/skills/{again.stdout}/index.json").json()
            assert "corp-standards" not in {skill["name"] for skill in index["skills"]}
