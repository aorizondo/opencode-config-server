# Tests for config validation and edge cases
import json
import yaml
import tempfile
from pathlib import Path

import pytest

from main import calcular_hash_directorio


@pytest.fixture
def temp_config_root():
    """Crear estructura temporal de config_root para tests (function-scoped)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_root = Path(tmpdir) / "config_root"
        config_root.mkdir()
        
        # Crear config.json completo
        (config_root / "config.json").write_text('''{
  "$schema": "https://opencode.ai/config.json",
  "theme": "opencode",
  "model": "test-model",
  "autoupdate": "notify",
  "share": "disabled",
  "watcher": {
    "ignore": ["node_modules/**", ".git/**"]
  },
  "enabled_providers": ["anthropic"],
  "disabled_providers": ["openai", "gemini"],
  "permission": {
    "bash": "ask",
    "edit": "ask"
  },
  "experimental": {
    "policies": [
      {"effect": "deny", "action": "provider.use", "resource": "openai"}
    ]
  },
  "plugins": ["opencode-rules"],
  "provider": {"custom-gateway": {"options": {"baseURL": "https://internal.corp"}}},
  "rules": {"include": ["rules/**/*.md"]}
}''')
        
        # Crear reglas
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "coding-standards.md").write_text("# Coding Standards\n\n## Principles\n- Use strict typing\n- Document public APIs\n- Test coverage > 80%\n\n## TypeScript\n- Use const by default\n- Prefer interface over type\n- No any or @ts-ignore\n\n## Python\n- Type hints required\n- Use dataclasses/pydantic\n- Ruff for linting\n\n## Security\n- Never commit secrets\n- Validate all inputs\n- Use prepared statements")
        (rules_dir / "security-policies.md").write_text("# Security Policies\n\n## Secrets\n- No hardcoded credentials\n- Use vault/secrets manager\n- Rotate every 90 days\n\n## Dependencies\n- Scan in CI/CD\n- Block critical/high vulns\n- Update monthly\n\n## Network\n- TLS 1.3 required\n- Auth on all APIs\n- Rate limiting\n\n## Data\n- Encrypt at rest (AES-256)\n- Encrypt in transit (TLS 1.3)\n- Classify data")
        (rules_dir / "git-workflow.md").write_text("# Git Workflow\n\n## Branches\n- main (protected)\n- feature/*\n- fix/*\n- hotfix/*\n\n## Commits\n- Conventional Commits\n- Atomic commits\n\n## PRs\n- Require review\n- Tests passing\n- Lint clean\n\n## Versioning\n- SemVer\n- Tags: v1.2.3")
        
        # Crear plugins
        plugins_dir = config_root / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "custom.js").write_text("// Custom plugin")
        
        yield config_root


@pytest.fixture
def temp_out_of_band_root():
    """Crear estructura temporal de out_of_band para tests (function-scoped)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_of_band = Path(tmpdir) / "out_of_band"
        omo_dir = out_of_band / "omo"
        omo_dir.mkdir(parents=True)
        (omo_dir / "config.yaml").write_text("""version: "1.0"
engine:
  binary: "opencode"
  workdir: "~/workspace"
profiles:
  development:
    model: "anthropic/claude-sonnet-4-5"
    temperature: 0.2
  staging:
    model: "anthropic/claude-sonnet-4-5"
    temperature: 0.1
  production:
    model: "anthropic/claude-sonnet-4-5"
    temperature: 0.0
plugins:
  opencode-rules:
    enabled: true
    mandatory_rules:
      - "coding-standards"
      - "security-policies"
      - "git-workflow"
agents:
  code-reviewer:
    model: "anthropic/claude-sonnet-4-5"
  test-generator:
    model: "anthropic/claude-sonnet-4-5"
  docs-writer:
    model: "anthropic/claude-sonnet-4-5"
integrations:
  jira:
    enabled: true
  gitlab:
    enabled: true
  slack:
    enabled: true
security:
  dangerous_commands:
    block: ["rm -rf /", "curl * | bash"]
  allowed_paths: ["~/workspace/**"]
  blocked_paths: ["/etc/**", "~/.ssh/**"]
""")
        yield out_of_band


class TestConfigJsonStructure:
    """Tests para validar estructura de config.json."""

    def test_config_json_exists(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        assert config_file.exists()

    def test_config_json_valid_json(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        content = config_file.read_text()
        data = json.loads(content)
        assert isinstance(data, dict)

    def test_config_json_has_schema(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        assert "$schema" in data
        assert data["$schema"] == "https://opencode.ai/config.json"

    def test_config_json_has_model(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        assert "model" in data
        assert isinstance(data["model"], str)

    def test_config_json_has_plugins(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        assert "plugins" in data
        assert isinstance(data["plugins"], list)
        assert "opencode-rules" in data["plugins"]

    def test_config_json_has_permissions(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        assert "permission" in data
        perms = data["permission"]
        assert perms["bash"] == "ask"
        assert perms["edit"] == "ask"

    def test_config_json_has_experimental_policies(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        assert "experimental" in data
        assert "policies" in data["experimental"]
        policies = data["experimental"]["policies"]
        assert isinstance(policies, list)
        assert len(policies) > 0

    def test_config_json_denies_openai(self, temp_config_root):
        config_file = temp_config_root / "config.json"
        data = json.loads(config_file.read_text())
        policies = data["experimental"]["policies"]
        openai_denied = any(
            p.get("effect") == "deny" and p.get("resource") == "openai"
            for p in policies
        )
        assert openai_denied


class TestRulesFiles:
    """Tests para archivos de reglas."""

    def test_coding_standards_exists(self, temp_config_root):
        assert (temp_config_root / "rules" / "coding-standards.md").exists()

    def test_security_policies_exists(self, temp_config_root):
        assert (temp_config_root / "rules" / "security-policies.md").exists()

    def test_git_workflow_exists(self, temp_config_root):
        assert (temp_config_root / "rules" / "git-workflow.md").exists()

    def test_rules_have_content(self, temp_config_root):
        for rule_file in (temp_config_root / "rules").glob("*.md"):
            content = rule_file.read_text()
            assert len(content) > 100
            assert "#" in content


class TestOMOConfig:
    """Tests para configuración de Oh-My-Opencode."""

    def test_omo_config_exists(self, temp_out_of_band_root):
        assert (temp_out_of_band_root / "omo" / "config.yaml").exists()

    def test_omo_config_valid_yaml(self, temp_out_of_band_root):
        config_file = temp_out_of_band_root / "omo" / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert isinstance(data, dict)

    def test_omo_config_has_profiles(self, temp_out_of_band_root):
        config_file = temp_out_of_band_root / "omo" / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert "profiles" in data
        profiles = data["profiles"]
        assert "development" in profiles
        assert "staging" in profiles
        assert "production" in profiles

    def test_omo_config_has_agents(self, temp_out_of_band_root):
        config_file = temp_out_of_band_root / "omo" / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert "agents" in data
        agents = data["agents"]
        assert "code-reviewer" in agents
        assert "test-generator" in agents
        assert "docs-writer" in agents

    def test_omo_config_has_integrations(self, temp_out_of_band_root):
        config_file = temp_out_of_band_root / "omo" / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert "integrations" in data
        integrations = data["integrations"]
        assert "jira" in integrations
        assert "gitlab" in integrations
        assert "slack" in integrations

    def test_omo_config_has_security(self, temp_out_of_band_root):
        config_file = temp_out_of_band_root / "omo" / "config.yaml"
        data = yaml.safe_load(config_file.read_text())
        assert "security" in data
        security = data["security"]
        assert "dangerous_commands" in security
        assert "allowed_paths" in security
        assert "blocked_paths" in security


class TestEdgeCases:
    """Tests para casos edge."""

    def test_config_root_symlink_handling(self, temp_config_root):
        """Hash maneja symlinks correctamente (no los sigue)."""
        # Crear symlink
        target = temp_config_root / "rules" / "coding-standards.md"
        link = temp_config_root / "symlink_test.md"
        link.symlink_to(target)
        
        # Hash debe incluir el symlink como archivo
        hash_with_symlink = calcular_hash_directorio(str(temp_config_root))
        
        # Eliminar symlink y recalcular
        link.unlink()
        hash_without = calcular_hash_directorio(str(temp_config_root))
        
        # Deben ser diferentes (symlink es parte de la estructura)
        assert hash_with_symlink != hash_without

    def test_large_file_hashing(self, temp_config_root):
        """Hash maneja archivos grandes sin problemas de memoria."""
        # Crear archivo de 10MB
        large_file = temp_config_root / "large.dat"
        large_file.write_bytes(b"x" * (10 * 1024 * 1024))
        
        hash_val = calcular_hash_directorio(str(temp_config_root))
        assert len(hash_val) == 64
        
        large_file.unlink()

    def test_special_characters_in_filenames(self, temp_config_root):
        """Nombres de archivo con caracteres especiales."""
        special = temp_config_root / "file with spaces & symbols#.md"
        special.write_text("# Special\nContent")
        
        hash_val = calcular_hash_directorio(str(temp_config_root))
        assert len(hash_val) == 64
        
        special.unlink()

    def test_empty_files(self, temp_config_root):
        """Archivos vacíos se manejan correctamente."""
        empty = temp_config_root / "empty.md"
        empty.write_text("")
        
        hash_val = calcular_hash_directorio(str(temp_config_root))
        assert len(hash_val) == 64
        
        empty.unlink()

    def test_nested_directories(self, temp_config_root):
        """Directorios anidados profundos."""
        deep = temp_config_root / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("deep content")
        
        hash_val = calcular_hash_directorio(str(temp_config_root))
        assert len(hash_val) == 64