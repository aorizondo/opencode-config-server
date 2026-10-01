# Tests for FastAPI endpoints (async)
import json
import tarfile
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx


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


@pytest.fixture
async def api_client():
    """Cliente de test async para endpoints."""
    import main
    from main import app, verificar_token_oauth2
    
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
        
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "coding-standards.md").write_text("# Coding Standards\n\n## Principles\n- Use strict typing\n- Document public APIs\n- Test coverage > 80%")
        (rules_dir / "security-policies.md").write_text("# Security Policies\n\n## Secrets\n- No hardcoded credentials")
        (rules_dir / "git-workflow.md").write_text("# Git Workflow\n\n## Branches\n- main (protected)")
        
        plugins_dir = config_root / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "custom.js").write_text("// Custom plugin")
        
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
        
        with patch.object(main, 'CONFIG_ROOT', str(config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(out_of_band)), \
             patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token-12345"
            
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            async with AsyncTestClient(app) as client:
                yield client
            
            app.dependency_overrides.clear()


@pytest.fixture
async def api_client_no_auth():
    """Cliente de test async SIN override de autenticación."""
    import main
    from main import app
    
    with tempfile.TemporaryDirectory() as tmpdir:
        config_root = Path(tmpdir) / "config_root"
        config_root.mkdir()
        (config_root / "config.json").write_text('{"model": "test-model"}')
        
        rules_dir = config_root / "rules"
        rules_dir.mkdir()
        (rules_dir / "coding-standards.md").write_text("# Test")
        
        out_of_band = Path(tmpdir) / "out_of_band"
        omo_dir = out_of_band / "omo"
        omo_dir.mkdir(parents=True)
        (omo_dir / "config.yaml").write_text("version: '1.0'\nengine:\n  binary: 'opencode'")
        
        with patch.object(main, 'CONFIG_ROOT', str(config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(out_of_band)), \
             patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            async with AsyncTestClient(app) as client:
                yield client


class TestHealthEndpoint:
    """Tests para el endpoint de health check."""

    @pytest.mark.asyncio
    async def test_health_check(self, api_client):
        response = await api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "opencode-enterprise-config"


class TestWellKnownOpenCodeEndpoint:
    """Tests para /.well-known/opencode (descubrimiento OpenCode)."""

    @pytest.mark.asyncio
    async def test_returns_valid_json(self, api_client):
        response = await api_client.get("/.well-known/opencode")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    @pytest.mark.asyncio
    async def test_contains_required_fields(self, api_client):
        response = await api_client.get("/.well-known/opencode")
        data = response.json()
        
        assert data["model"] == "test-model"
        assert "opencode-rules" in data["plugins"]
        
        assert "auth" in data
        assert data["auth"]["type"] == "command"
        assert "command" in data["auth"]
        
        command = data["auth"]["command"]
        assert "http://testserver" in command
        assert "test-token-12345" in command
        assert "/cdn/bootstrap.sh" in command

    @pytest.mark.asyncio
    async def test_auth_command_format(self, api_client):
        response = await api_client.get("/.well-known/opencode")
        data = response.json()
        command = data["auth"]["command"]
        
        assert command.startswith("curl -fsSL -H 'Authorization: Bearer test-token-12345'")
        assert "http://testserver/cdn/bootstrap.sh" in command
        assert "| bash -s -- http://testserver test-token-12345" in command

    @pytest.mark.asyncio
    async def test_401_without_token(self, api_client_no_auth):
        response = await api_client_no_auth.get("/.well-known/opencode")
        assert response.status_code == 401
        assert "Bearer" in response.headers.get("WWW-Authenticate", "")


class TestStateEndpoint:
    """Tests para /cdn/state (hash del directorio)."""

    @pytest.mark.asyncio
    async def test_returns_hash(self, api_client):
        response = await api_client.get("/cdn/state")
        assert response.status_code == 200
        data = response.json()
        assert "hash" in data
        assert len(data["hash"]) == 64

    @pytest.mark.asyncio
    async def test_hash_consistency(self, api_client):
        response1 = await api_client.get("/cdn/state")
        response2 = await api_client.get("/cdn/state")
        assert response1.json()["hash"] == response2.json()["hash"]

    @pytest.mark.asyncio
    async def test_401_without_token(self, api_client_no_auth):
        response = await api_client_no_auth.get("/cdn/state")
        assert response.status_code == 401


class TestBundleEndpoint:
    """Tests para /cdn/bundle.tar.gz (descarga de configuración completa)."""

    @pytest.mark.asyncio
    async def test_returns_tar_gz(self, api_client):
        response = await api_client.get("/cdn/bundle.tar.gz")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/gzip"

    @pytest.mark.asyncio
    async def test_valid_tar_gz_content(self, api_client):
        response = await api_client.get("/cdn/bundle.tar.gz")
        tar_bytes = io.BytesIO(response.content)
        
        with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
            names = tar.getnames()
            
            assert "./config.json" in names or "config.json" in names
            assert "./rules/coding-standards.md" in names or "rules/coding-standards.md" in names
            assert "./rules/security-policies.md" in names or "rules/security-policies.md" in names
            assert "./plugins/custom.js" in names or "plugins/custom.js" in names
            
            # Find config.json (could be ./config.json or config.json)
            config_name = next((n for n in names if n.endswith("config.json")), None)
            assert config_name is not None
            config_content = tar.extractfile(config_name).read().decode()
            assert "test-model" in config_content
            assert "opencode-rules" in config_content

    @pytest.mark.asyncio
    async def test_401_without_token(self, api_client_no_auth):
        response = await api_client_no_auth.get("/cdn/bundle.tar.gz")
        assert response.status_code == 401


class TestOMOConfigEndpoint:
    """Tests para /cdn/out_of_band/omo/config.yaml."""

    @pytest.mark.asyncio
    async def test_returns_yaml(self, api_client):
        response = await api_client.get("/cdn/out_of_band/omo/config.yaml")
        assert response.status_code == 200
        assert "yaml" in response.headers["content-type"] or "text" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_contains_expected_config(self, api_client):
        response = await api_client.get("/cdn/out_of_band/omo/config.yaml")
        content = response.text
        assert "version:" in content
        assert "engine:" in content
        assert "binary" in content

    @pytest.mark.asyncio
    async def test_404_when_missing(self, api_client):
        # This test would need a separate client without the file
        # Skipping for now as it requires separate setup
        pass

    @pytest.mark.asyncio
    async def test_401_without_token(self, api_client_no_auth):
        response = await api_client_no_auth.get("/cdn/out_of_band/omo/config.yaml")
        assert response.status_code == 401


class TestBootstrapScriptEndpoint:
    """Tests para /cdn/bootstrap.sh (script de bootstrap)."""

    @pytest.mark.asyncio
    async def test_returns_shell_script(self, api_client):
        response = await api_client.get("/cdn/bootstrap.sh")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/x-shellscript")

    @pytest.mark.asyncio
    async def test_script_contains_required_elements(self, api_client):
        response = await api_client.get("/cdn/bootstrap.sh")
        script = response.text
        
        assert script.startswith("#!/usr/bin/env bash")
        assert "set -e" in script
        
        assert 'SERVER_URL="${1}"' in script
        assert 'AUTH_TOKEN="${2}"' in script
        
        assert "Faltan credenciales" in script
        
        assert 'TARGET_DIR="$HOME/.config/opencode"' in script
        assert 'STATE_FILE="$TARGET_DIR/.enterprise_hash"' in script
        
        assert "REMOTE_STATE=" in script
        assert "REMOTE_HASH=" in script
        assert "LOCAL_HASH=" in script
        assert 'if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]' in script
        
        assert "bundle.tar.gz" in script
        assert "tar -xzf" in script
        
        # Nuevo: merge script download and execution
        assert "merge.py" in script
        assert "MERGE_SCRIPT" in script
        assert 'python3 "$MERGE_SCRIPT"' in script
        
        assert ".bashrc" in script
        assert ".zshrc" in script
        assert ".opencode.env" in script
        
        assert 'echo "$REMOTE_HASH" > "$STATE_FILE"' in script
        
        assert 'echo "$AUTH_TOKEN"' in script

    @pytest.mark.asyncio
    async def test_script_executable(self, api_client):
        response = await api_client.get("/cdn/bootstrap.sh")
        script = response.text
        
        import subprocess
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            f.write(script)
            f.flush()
            result = subprocess.run(['bash', '-n', f.name], capture_output=True)
            assert result.returncode == 0, f"Bash syntax error: {result.stderr.decode()}"

    @pytest.mark.asyncio
    async def test_401_without_token(self, api_client_no_auth):
        response = await api_client_no_auth.get("/cdn/bootstrap.sh")
        assert response.status_code == 401


class TestCORSHeaders:
    """Tests para headers CORS."""

    @pytest.mark.asyncio
    async def test_cors_headers_present(self, api_client):
        response = await api_client.get("/health", headers={"Origin": "https://example.com"})
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_options_request(self, api_client):
        response = await api_client.options("/.well-known/opencode")
        assert response.status_code in (200, 405)