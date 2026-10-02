"""Deployment guardrails: /health self-checks and settings validation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from opencode_config_server import settings as settings_module
from opencode_config_server.app import create_app
from opencode_config_server.skills import resolve


class TestHealth:
    async def test_reports_a_healthy_deployment(self, client: httpx.AsyncClient):
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "status": "healthy",
            "service": "opencode-enterprise-config",
            "users": 3,
            "skills": 1,
            "problems": [],
        }

    async def test_needs_no_credentials(self, client: httpx.AsyncClient):
        assert (await client.get("/health")).status_code == 200

    async def test_flags_a_missing_user_database(self, client, config_dir):
        (config_dir / "users.json").unlink()
        body = (await client.get("/health")).json()
        assert body["status"] == "degraded"
        assert any("user database not found" in problem for problem in body["problems"])

    async def test_flags_an_empty_user_database(self, client, config_dir):
        (config_dir / "users.json").write_text('{"users": []}', encoding="utf-8")
        body = (await client.get("/health")).json()
        assert body["status"] == "degraded"
        assert "no users are configured" in body["problems"]

    async def test_flags_a_broken_corporate_document(self, client, config_dir):
        (config_dir / "opencode.jsonc").write_text("{ nope", encoding="utf-8")
        body = (await client.get("/health")).json()
        assert body["status"] == "degraded"
        assert any("not valid JSON" in problem for problem in body["problems"])

    async def test_flags_an_ephemeral_session_secret(self, settings):
        from dataclasses import replace

        app = create_app(replace(settings, ephemeral_secret=True))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            body = (await client.get("/health")).json()
            assert any("OCS_SESSION_SECRET" in problem for problem in body["problems"])

    async def test_never_leaks_a_secret(self, client):
        body = (await client.get("/health")).text
        assert "pbkdf2" not in body and "test-session-secret" not in body


class TestSettings:
    def test_production_demands_a_session_secret(self, monkeypatch):
        monkeypatch.delenv("OCS_SESSION_SECRET", raising=False)
        with pytest.raises(settings_module.SettingsError, match="OCS_SESSION_SECRET"):
            settings_module.load(environment="production")

    def test_production_demands_https(self):
        with pytest.raises(settings_module.SettingsError, match="https"):
            settings_module.load(
                environment="production", session_secret="x", base_url="http://cfg.corp"
            )

    def test_development_tolerates_a_generated_secret(self):
        resolved = settings_module.load(environment="development")
        assert resolved.ephemeral_secret is True
        assert resolved.session_secret

    def test_rejects_an_unusable_session_variable_name(self):
        with pytest.raises(settings_module.SettingsError, match="variable name"):
            settings_module.load(session_secret="x", session_env="not a name")

    def test_trailing_slashes_never_reach_a_url(self):
        resolved = settings_module.load(session_secret="x", base_url="https://cfg.corp/")
        assert resolved.base_url == "https://cfg.corp"

    def test_jsonc_wins_over_json(self, tmp_path: Path):
        (tmp_path / "opencode.json").write_text("{}", encoding="utf-8")
        resolved = settings_module.load(session_secret="x", config_dir=tmp_path)
        assert resolved.opencode_config_file.name == "opencode.json"
        (tmp_path / "opencode.jsonc").write_text("{}", encoding="utf-8")
        assert settings_module.load(
            session_secret="x", config_dir=tmp_path
        ).opencode_config_file.name == "opencode.jsonc"


class TestSkillPathSafety:
    """Direct unit coverage, so the defence does not depend on URL routing."""

    @pytest.mark.parametrize(
        "relative",
        [
            "../../users.json",
            "references/../../../users.json",
            "/etc/passwd",
            "references/../../opencode.jsonc",
            "",
        ],
    )
    def test_refuses_to_leave_the_skill_directory(self, config_dir: Path, relative: str):
        with pytest.raises(FileNotFoundError):
            resolve(config_dir / "skills", "corp-standards", relative)

    @pytest.mark.parametrize("name", ["..", ".", "", "../skills", "corp-standards/../..", "nope"])
    def test_refuses_an_unsafe_skill_name(self, config_dir: Path, name: str):
        with pytest.raises(FileNotFoundError):
            resolve(config_dir / "skills", name, "SKILL.md")

    def test_accepts_a_published_file(self, config_dir: Path):
        assert resolve(config_dir / "skills", "corp-standards", "SKILL.md").is_file()
