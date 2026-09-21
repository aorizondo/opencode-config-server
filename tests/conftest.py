# Test configuration and fixtures
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx

# Añadir el directorio raíz al path para importar main
sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from main import (
    app,
    calcular_hash_directorio,
    verificar_token_oauth2,
)


class AsyncTestClient:
    """Async test client usando httpx.AsyncClient con ASGITransport."""
    
    def __init__(self, app, base_url="http://testserver"):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=base_url
        )
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        await self.close()
    
    async def close(self):
        await self._client.aclose()
    
    async def get(self, url, **kwargs):
        return await self._client.get(url, **kwargs)
    
    async def post(self, url, **kwargs):
        return await self._client.post(url, **kwargs)
    
    async def put(self, url, **kwargs):
        return await self._client.put(url, **kwargs)
    
    async def delete(self, url, **kwargs):
        return await self._client.delete(url, **kwargs)
    
    async def options(self, url, **kwargs):
        return await self._client.options(url, **kwargs)
    
    async def patch(self, url, **kwargs):
        return await self._client.patch(url, **kwargs)


@pytest.fixture(scope="session")
def test_config_root():
    """Crear estructura temporal de config_root para tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_root = Path(tmpdir) / "config_root"
        config_root.mkdir()
        
        # Crear config.json completo (copia del real)
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


@pytest.fixture(scope="session")
def test_out_of_band_root():
    """Crear estructura temporal de out_of_band para tests."""
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


@pytest.fixture
async def client(test_config_root, test_out_of_band_root):
    """Cliente de test async con config_root y out_of_band mockeados."""
    with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
         patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
         patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
         patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
         patch.object(main, 'AUDIENCE', 'test-audience'), \
         patch.object(main, 'ISSUER', 'https://auth.test'), \
         patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
        
        # Override la dependencia de autenticación para tests
        def mock_verify_token():
            return "test-token-12345"
        
        app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
        
        async with AsyncTestClient(app) as test_client:
            yield test_client
        
        app.dependency_overrides.clear()


@pytest.fixture
async def client_no_auth(test_config_root, test_out_of_band_root):
    """Cliente de test async SIN override de autenticación (para testear 401)."""
    with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
         patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
         patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
         patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
         patch.object(main, 'AUDIENCE', 'test-audience'), \
         patch.object(main, 'ISSUER', 'https://auth.test'), \
         patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
        
        async with AsyncTestClient(app) as test_client:
            yield test_client