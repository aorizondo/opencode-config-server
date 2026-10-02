"""Remote skills: the index contract, cache invalidation and path safety.

The harness downloads these with no headers at all
(packages/core/src/skill/discovery.ts), so the token travels in the path.
"""

from __future__ import annotations

import httpx
import pytest

from opencode_config_server import auth, skills

from .conftest import API_KEY, write_users


class TestIndexContract:
    async def test_shape_matches_what_the_harness_decodes(self, client: httpx.AsyncClient, token: str):
        response = await client.get(f"/skills/{token}/index.json")
        assert response.status_code == 200
        document = response.json()
        assert list(document) == ["skills"]
        for skill in document["skills"]:
            assert set(skill) == {"name", "version", "files"}
            assert isinstance(skill["name"], str) and "/" not in skill["name"]
            assert isinstance(skill["version"], str) and skill["version"]
            assert isinstance(skill["files"], list) and skill["files"]

    async def test_every_skill_carries_an_entry_file(self, client, token):
        """Without SKILL.md (or <name>.md) the harness drops the skill silently."""
        for skill in (await client.get(f"/skills/{token}/index.json")).json()["skills"]:
            assert "SKILL.md" in skill["files"] or f"{skill['name']}.md" in skill["files"]

    async def test_a_version_is_always_published(self, client, token):
        """A missing version freezes the employee's cached copy forever."""
        for skill in (await client.get(f"/skills/{token}/index.json")).json()["skills"]:
            assert skill["version"]

    def test_version_tracks_content(self, config_dir):
        root = config_dir / "skills"
        before = skills.index(root)["skills"][0]["version"]
        (root / "corp-standards" / "SKILL.md").write_text(
            "---\nname: corp-standards\ndescription: Updated\n---\n# New\n", encoding="utf-8"
        )
        after = skills.index(root)["skills"][0]["version"]
        assert before != after, "an edit must change the version or clients never refresh"

    def test_version_is_stable_without_edits(self, config_dir):
        root = config_dir / "skills"
        assert skills.index(root) == skills.index(root)

    def test_private_files_are_not_published(self, config_dir):
        files = skills.index(config_dir / "skills")["skills"][0]["files"]
        assert "private.key" not in files

    def test_a_directory_without_an_entry_file_is_skipped(self, config_dir):
        incomplete = config_dir / "skills" / "half-written"
        incomplete.mkdir()
        (incomplete / "notes.md").write_text("draft", encoding="utf-8")
        assert [skill["name"] for skill in skills.index(config_dir / "skills")["skills"]] == ["corp-standards"]


class TestFileDownloads:
    async def test_serves_a_published_file(self, client: httpx.AsyncClient, token: str):
        response = await client.get(f"/skills/{token}/corp-standards/SKILL.md")
        assert response.status_code == 200
        assert "corp-standards" in response.text
        assert response.headers["content-type"].startswith("text/markdown")

    async def test_serves_a_nested_file(self, client, token):
        response = await client.get(f"/skills/{token}/corp-standards/references/coding-standards.md")
        assert response.status_code == 200
        assert "Use types." in response.text

    async def test_refuses_an_unpublished_file(self, client, token):
        assert (await client.get(f"/skills/{token}/corp-standards/private.key")).status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "../../../etc/passwd",
            "references/../../../../etc/passwd",
            "..%2f..%2fusers.json",
            "references/%2e%2e/%2e%2e/users.json",
        ],
    )
    async def test_refuses_traversal(self, client, token, path: str):
        response = await client.get(f"/skills/{token}/corp-standards/{path}")
        assert response.status_code == 404
        assert "root:" not in response.text

    async def test_refuses_to_escape_the_skills_directory(self, client, token):
        response = await client.get(f"/skills/{token}/../users.json")
        assert response.status_code == 404
        assert "pbkdf2" not in response.text


class TestPathAuth:
    async def test_rejects_a_missing_or_forged_token(self, client: httpx.AsyncClient):
        for value in ["not-a-token", "v1.a.b", ""]:
            assert (await client.get(f"/skills/{value}/index.json")).status_code == 404

    async def test_rejects_a_revoked_token(self, client, config_dir, token):
        write_users(config_dir, [{"name": "ci-runner", "api_keys": [auth.hash_api_key(API_KEY)], "version": 9}])
        assert (await client.get(f"/skills/{token}/index.json")).status_code == 404
        assert (await client.get(f"/skills/{token}/corp-standards/SKILL.md")).status_code == 404

    async def test_can_be_switched_off(self, config_dir, settings):
        from dataclasses import replace

        from opencode_config_server.app import create_app

        disabled = create_app(replace(settings, skills_enabled=False))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=disabled), base_url="http://t") as client:
            issued = (await client.post("/auth/token", headers={"X-Api-Key": API_KEY})).json()["token"]
            assert (await client.get(f"/skills/{issued}/index.json")).status_code == 404
