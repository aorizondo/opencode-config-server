"""Validate what we serve against the harness's real schema.

Everything else in this suite asserts our own reading of the contract. This one
hands documents to OpenCode's own `ConfigNormalize.normalize` plus
`Schema.decodeUnknownOption(Info)` and checks the harness accepts them with no
diagnostics -- the only way to know a corporate document is not silently dropped
or partially rewritten on an employee's machine.

Needs `bun` and an OpenCode v2 checkout with installed dependencies. Point
`OPENCODE_SOURCE` at it, or keep it as a sibling directory; the whole module
skips itself otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from opencode_config_server import configdoc, jsonc, settings as settings_module

REPO_ROOT = Path(__file__).parent.parent
VALIDATOR_SOURCE = Path(__file__).parent / "harness" / "validate_config.ts"
# Starting bun against the opencode workspace costs minutes on a cold cache.
VALIDATOR_TIMEOUT = 600


def harness_core() -> Path | None:
    candidates = [
        Path(os.environ["OPENCODE_SOURCE"]) if os.environ.get("OPENCODE_SOURCE") else None,
        REPO_ROOT.parent / "opencode-v2",
        REPO_ROOT.parent / "opencode",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        core = candidate / "packages" / "core"
        if (core / "src" / "config" / "normalize.ts").is_file() and (core / "node_modules").is_dir():
            return core
    return None


CORE = harness_core()

pytestmark = pytest.mark.skipif(
    shutil.which("bun") is None or CORE is None,
    reason="needs bun and an OpenCode v2 checkout with installed dependencies (set OPENCODE_SOURCE)",
)


def documents_under_test(tmp_path: Path) -> dict[str, dict]:
    """Every document this repository can put in front of the harness."""
    shipped = jsonc.loads((REPO_ROOT / "config" / "opencode.jsonc").read_text(encoding="utf-8"))

    # What a client actually receives, placeholders resolved.
    served = configdoc.load(
        settings_module.load(
            base_url="https://cfg.corp.example",
            config_dir=REPO_ROOT / "config",
            users_file=tmp_path / "unused-users.json",
            session_secret="validation-only",
        )
    )

    documents = {"shipped": shipped, "served": served}
    example = REPO_ROOT / "examples" / "opencode.full.jsonc"
    if example.is_file():
        documents["example"] = jsonc.loads(example.read_text(encoding="utf-8"))
    return documents


@pytest.fixture(scope="session")
def reports(tmp_path_factory) -> dict[str, dict]:
    """Validate every document in a single bun run."""
    assert CORE is not None
    tmp_path = tmp_path_factory.mktemp("harness")
    documents = documents_under_test(tmp_path)

    paths: list[str] = []
    for name, document in documents.items():
        target = tmp_path / f"{name}.json"
        target.write_text(json.dumps(document), encoding="utf-8")
        paths.append(str(target))

    # Must run from packages/core for bun to resolve the workspace imports.
    validator = CORE / "ocs-validate-config.generated.ts"
    validator.write_text(VALIDATOR_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        result = subprocess.run(
            ["bun", validator.name, *paths],
            capture_output=True,
            text=True,
            cwd=CORE,
            timeout=VALIDATOR_TIMEOUT,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - environment dependent
        pytest.skip(f"the harness validator did not finish within {VALIDATOR_TIMEOUT}s")
    finally:
        validator.unlink(missing_ok=True)

    assert result.stdout, f"the validator printed nothing (exit {result.returncode}): {result.stderr[-2000:]}"
    parsed = [json.loads(line) for line in result.stdout.strip().splitlines() if line.startswith("{")]
    return {Path(report["file"]).stem: report for report in parsed}


class TestShippedDocument:
    def test_the_harness_normalizes_it_without_diagnostics(self, reports):
        report = reports["shipped"]
        assert "error" not in report, report.get("error")
        assert report["normalize"] == "normalized"
        # A diagnostic means the harness drops or rewrites part of the document.
        assert report["diagnostics"] == []

    def test_the_harness_decodes_it(self, reports):
        assert reports["shipped"]["decode"] == "ok"


class TestServedDocument:
    def test_substitution_keeps_the_document_valid(self, reports):
        report = reports["served"]
        assert report["normalize"] == "normalized"
        assert report["diagnostics"] == []
        assert report["decode"] == "ok"

    def test_every_delivery_channel_survives_the_decode(self, reports):
        """The keys the whole design relies on must still be there afterwards."""
        report = reports["served"]
        assert "corp-reviewer" in report["agents"], "inline corporate agents were dropped"
        assert "corp-review" in report["commands"], "inline corporate commands were dropped"
        assert report["permissions"] >= 1, "corporate permission rules were dropped"
        assert report["skills"], "the remote skill source was dropped"

    def test_the_skill_url_keeps_its_token_placeholder(self, reports):
        """The harness substitutes {env:...} locally; it must reach it intact."""
        assert "{env:OPENCODE_CORP_SESSION}" in reports["served"]["skills"][0]


class TestExampleDocument:
    def test_the_full_reference_example_is_not_rejected(self, reports):
        if "example" not in reports:
            pytest.skip("no full example is shipped")
        report = reports["example"]
        assert report["normalize"] != "rejected", report.get("diagnostics")
