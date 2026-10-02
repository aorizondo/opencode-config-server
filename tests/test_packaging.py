"""Regression guards for the image and the package.

The previous version of this server served a Python script from an endpoint but
never copied it into the Docker image, so authentication failed in every
container while every test stayed green. These tests make that class of mistake
visible again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PACKAGE = REPO_ROOT / "opencode_config_server"


def dockerfile_copy_targets() -> list[str]:
    sources: list[str] = []
    for line in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = [part for part in stripped.split() if not part.startswith("--")][1:]
        sources.extend(parts[:-1])
    return sources


class TestDockerImage:
    def test_copies_the_whole_package(self):
        """Anything the app reads at runtime must be inside the image."""
        targets = dockerfile_copy_targets()
        assert "opencode_config_server/" in targets or "opencode_config_server" in targets
        assert "main.py" in targets

    @pytest.mark.parametrize(
        "runtime_file",
        sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in PACKAGE.iterdir()
            if path.is_file() and path.suffix in {".py", ".sh"}
        ),
    )
    def test_every_runtime_file_is_covered_by_a_copy(self, runtime_file: str):
        covered = any(
            runtime_file == target or runtime_file.startswith(target.rstrip("/") + "/")
            for target in dockerfile_copy_targets()
        )
        assert covered, f"{runtime_file} would be missing from the image"

    def test_runs_as_a_non_root_user(self):
        assert "USER opencode" in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_healthcheck_hits_the_health_endpoint(self):
        text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert "/health" in text and "HEALTHCHECK" in text


class TestPythonPackage:
    def test_the_login_script_ships_as_package_data(self):
        """An sdist/wheel without it serves an empty login command."""
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "login_script.sh" in text

    def test_the_login_script_is_present_and_executable_bash(self):
        script = PACKAGE / "login_script.sh"
        assert script.is_file()
        assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")

    def test_requirements_stay_minimal(self):
        """Every dependency is attack surface on a server that holds credentials."""
        lines = [
            line.split("==")[0].split(">=")[0].strip()
            for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert sorted(lines) == ["fastapi", "uvicorn[standard]"]


class TestComposeFile:
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def test_requires_a_session_secret(self):
        assert "OCS_SESSION_SECRET: ${OCS_SESSION_SECRET:?" in self.text

    def test_mounts_the_config_read_only(self):
        assert "./config:/app/config:ro" in self.text

    def test_does_not_publish_on_every_interface(self):
        """TLS terminates in front of this; the app itself speaks plain HTTP."""
        assert '"127.0.0.1:8000:8000"' in self.text
