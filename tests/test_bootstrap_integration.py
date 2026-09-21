# Integration tests for bootstrap script behavior (async)
import os
import tempfile
import subprocess
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


class TestBootstrapScriptGeneration:
    """Tests para generación y sintaxis del bootstrap script."""

    @pytest.fixture
    async def script_client(self, test_config_root, test_out_of_band_root):
        """Cliente para obtener el script generado."""
        import main
        from main import app, verificar_token_oauth2
        
        with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
             patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token"
            
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            async with AsyncTestClient(app) as client:
                yield client
            
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_script_generated_correctly(self, script_client):
        """Test que el script se genera correctamente."""
        response = await script_client.get("/cdn/bootstrap.sh")
        assert response.status_code == 200
        script = response.text
        
        # Verificar elementos clave
        assert script.startswith("#!/usr/bin/env bash")
        assert "set -e" in script
        assert 'SERVER_URL="${1}"' in script
        assert 'AUTH_TOKEN="${2}"' in script
        assert "TARGET_DIR=\"$HOME/.config/opencode\"" in script
        assert 'STATE_FILE="$TARGET_DIR/.enterprise_hash"' in script
        assert 'OMO_DIR="$HOME/.omo"' in script
        assert "REMOTE_STATE=" in script
        assert "REMOTE_HASH=" in script
        assert "LOCAL_HASH=" in script
        assert 'if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]' in script
        assert "bundle.tar.gz" in script
        assert "tar -xzf" in script
        assert "out_of_band/omo/config.yaml" in script
        assert ".opencode.env" in script
        assert "ANTHROPIC_API_KEY" in script
        assert "OPENCODE_JIRA_TOKEN" in script
        assert "OMO_ENGINE_ROUTE" in script
        assert ".bashrc" in script
        assert ".zshrc" in script
        assert ".opencode.env" in script
        assert 'echo "$REMOTE_HASH" > "$STATE_FILE"' in script
        assert 'echo "$AUTH_TOKEN"' in script

    @pytest.mark.asyncio
    async def test_script_syntax_valid(self, script_client):
        """Test que el script tiene sintaxis bash válida."""
        response = await script_client.get("/cdn/bootstrap.sh")
        script = response.text
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
            f.write(script)
            f.flush()
            result = subprocess.run(['bash', '-n', f.name], capture_output=True)
            assert result.returncode == 0, f"Bash syntax error: {result.stderr.decode()}"

    @pytest.mark.asyncio
    async def test_discovery_uses_https_when_configured(self, test_config_root, test_out_of_band_root):
        """Endpoint de descubrimiento usa HTTPS cuando SERVER_BASE_URL es HTTPS."""
        import main
        from main import app, verificar_token_oauth2
        
        with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
             patch.object(main, 'SERVER_BASE_URL', 'https://config.prod.corp'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.prod.corp/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token"
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            async with AsyncTestClient(app) as client:
                response = await client.get("/.well-known/opencode")
                data = response.json()
                command = data["auth"]["command"]
                assert "https://config.prod.corp" in command
            
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_script_quotes_variables(self, script_client):
        """Variables en script están correctamente quotadas."""
        response = await script_client.get("/cdn/bootstrap.sh")
        script = response.text
        
        assert '"$SERVER_URL"' in script or '${SERVER_URL}' in script
        assert '"$AUTH_TOKEN"' in script or '${AUTH_TOKEN}' in script
        assert '"$TARGET_DIR"' in script or '${TARGET_DIR}' in script
        assert '"$REMOTE_HASH"' in script or '${REMOTE_HASH}' in script

    @pytest.mark.asyncio
    async def test_script_sets_errexit(self, script_client):
        """Script tiene set -e para fallar rápido en errores."""
        response = await script_client.get("/cdn/bootstrap.sh")
        script = response.text
        lines = script.split('\n')
        assert "set -e" in lines[1]


class TestEnvironmentVariableInjection:
    """Tests para inyección de variables de entorno en el script."""

    @staticmethod
    async def _get_script(test_config_root, test_out_of_band_root):
        """Helper para obtener script base."""
        import main
        from main import app, verificar_token_oauth2
        
        with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
             patch.object(main, 'SERVER_BASE_URL', 'http://testserver'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token"
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            async with AsyncTestClient(app) as client:
                response = await client.get("/cdn/bootstrap.sh")
                script = response.text
            
            app.dependency_overrides.clear()
            return script

    @pytest.mark.asyncio
    async def test_env_file_template_contains_vars(self, test_config_root, test_out_of_band_root):
        """Template del archivo .opencode.env contiene las variables requeridas."""
        script = await self._get_script(test_config_root, test_out_of_band_root)
        
        # Verificar que el template cat << 'EOF' contiene las variables
        assert "ANTHROPIC_API_KEY" in script
        assert "OPENCODE_JIRA_TOKEN" in script
        assert "OMO_ENGINE_ROUTE" in script
        assert "OPENCODE_RULES_STRICT_MODE" in script
        
        # Verificar que usa bash parameter expansion (${VAR:-})
        assert "${ANTHROPIC_API_KEY:-}" in script or "${{ANTHROPIC_API_KEY:-}}" in script
        assert "${OPENCODE_JIRA_TOKEN:-}" in script or "${{OPENCODE_JIRA_TOKEN:-}}" in script

    @pytest.mark.asyncio
    async def test_shell_rc_injection_logic(self, test_config_root, test_out_of_band_root):
        """Lógica de inyección en .bashrc/.zshrc está presente."""
        script = await self._get_script(test_config_root, test_out_of_band_root)
        
        assert "vincular_entorno_a_shell" in script
        assert ".bashrc" in script
        assert ".zshrc" in script
        assert ".opencode.env" in script
        assert "grep -q" in script  # Check if already exists
        assert "OpenCode Environmental Initialization" in script

    @pytest.mark.asyncio
    async def test_state_file_logic(self, test_config_root, test_out_of_band_root):
        """Lógica de archivo de estado (.enterprise_hash) está presente."""
        script = await self._get_script(test_config_root, test_out_of_band_root)
        
        assert "STATE_FILE" in script
        assert ".enterprise_hash" in script
        assert "REMOTE_HASH" in script
        assert "LOCAL_HASH" in script
        assert 'echo "$REMOTE_HASH" > "$STATE_FILE"' in script


class TestBootstrapScriptSecurity:
    """Tests de seguridad del bootstrap script."""

    @staticmethod
    async def _create_client(test_config_root, test_out_of_band_root, server_base_url):
        """Helper para crear cliente con configuración específica."""
        import main
        from main import app, verificar_token_oauth2
        
        with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
             patch.object(main, 'SERVER_BASE_URL', server_base_url), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.test/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token"
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            client = AsyncTestClient(app, base_url=server_base_url)
            return client

    @pytest.mark.asyncio
    async def test_discovery_uses_https_in_production(self, test_config_root, test_out_of_band_root):
        """Endpoint de descubrimiento usa HTTPS en producción."""
        import main
        from main import app, verificar_token_oauth2
        
        with patch.object(main, 'CONFIG_ROOT', str(test_config_root)), \
             patch.object(main, 'OUT_OF_BAND_ROOT', str(test_out_of_band_root)), \
             patch.object(main, 'SERVER_BASE_URL', 'https://config.prod.corp'), \
             patch.object(main, 'OAUTH2_TOKEN_URL', 'https://auth.prod.corp/oauth2/token'), \
             patch.object(main, 'AUDIENCE', 'test-audience'), \
             patch.object(main, 'ISSUER', 'https://auth.test'), \
             patch.object(main, 'JWKS_URL', 'https://auth.test/.well-known/jwks.json'):
            
            def mock_verify_token():
                return "test-token"
            app.dependency_overrides[verificar_token_oauth2] = mock_verify_token
            
            async with AsyncTestClient(app) as client:
                response = await client.get("/.well-known/opencode")
                data = response.json()
                command = data["auth"]["command"]
                assert "https://config.prod.corp" in command
            
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_script_quotes_variables(self, test_config_root, test_out_of_band_root):
        """Variables en script están correctamente quotadas."""
        client = await self._create_client(test_config_root, test_out_of_band_root, 'http://testserver')
        try:
            response = await client.get("/cdn/bootstrap.sh")
            script = response.text
            
            assert '"$SERVER_URL"' in script or '${SERVER_URL}' in script
            assert '"$AUTH_TOKEN"' in script or '${AUTH_TOKEN}' in script
            assert '"$TARGET_DIR"' in script or '${TARGET_DIR}' in script
            assert '"$REMOTE_HASH"' in script or '${REMOTE_HASH}' in script
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_script_sets_errexit(self, test_config_root, test_out_of_band_root):
        """Script tiene set -e para fallar rápido en errores."""
        client = await self._create_client(test_config_root, test_out_of_band_root, 'http://testserver')
        try:
            response = await client.get("/cdn/bootstrap.sh")
            script = response.text
            lines = script.split('\n')
            assert "set -e" in lines[1]
        finally:
            await client.close()


class TestAsyncTestClient:
    """Tests para el cliente de test async."""

    @pytest.mark.asyncio
    async def test_client_creation(self):
        from main import app
        client = AsyncTestClient(app)
        await client.close()
        assert True
    
    @pytest.mark.asyncio
    async def test_client_get(self):
        from main import app
        async with AsyncTestClient(app) as client:
            # Health endpoint no requiere auth
            import main
            with patch.object(main, 'CONFIG_ROOT', '/tmp'), \
                 patch.object(main, 'OUT_OF_BAND_ROOT', '/tmp'):
                response = await client.get("/health")
                assert response.status_code == 200