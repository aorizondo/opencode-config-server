import os
import hashlib
import tarfile
import json
from io import BytesIO
from typing import Optional

from fastapi import FastAPI, Request, Response, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

app = FastAPI(
    title="OpenCode Enterprise Config Server",
    description="Servidor de configuración empresarial para OpenCode con sincronización incremental",
    version="1.0.0"
)

# Configuración del esquema OAuth2 estándar (Apunta a tu servidor de Autenticación corporativo)
OAUTH2_TOKEN_URL = os.getenv("OAUTH2_TOKEN_URL", "https://tu-authenticator.corp/oauth2/token")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=OAUTH2_TOKEN_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

CONFIG_ROOT = os.path.abspath("./config_root")
OUT_OF_BAND_ROOT = os.path.abspath("./out_of_band")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:8000")

# Configuración de validación de tokens (ajustar según tu proveedor OAuth2)
JWKS_URL = os.getenv("JWKS_URL")  # Ej: "https://tu-authenticator.corp/.well-known/jwks.json"
AUDIENCE = os.getenv("OAUTH2_AUDIENCE", "opencode-enterprise")
ISSUER = os.getenv("OAUTH2_ISSUER", "https://tu-authenticator.corp")


class StateResponse(BaseModel):
    hash: str


def verificar_token_oauth2(token: str = Depends(oauth2_scheme)) -> str:
    """
    Valida el token OAuth2 contra tu servicio de autenticación corporativo.
    Como la configuración es fija para todos, solo necesitamos confirmar que es un token válido.
    """
    # NOTA DE PRODUCCIÓN: Aquí puedes añadir tu lógica de verificación de JWT
    # - Verificar firma con JWKS (usar python-jose o authlib)
    # - Verificar claims: exp, aud, iss
    # - Hacer petición de introspección a tu servidor OAuth2
    
    if not token or len(token) < 10:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token OAuth2 corporativo inválido o expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # TODO: Implementar validación real de JWT aquí
    # Ejemplo con python-jose:
    # from jose import jwt, JWTError
    # try:
    #     payload = jwt.decode(token, key=jwks_client.get_signing_key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
    # except JWTError:
    #     raise HTTPException(401, "Token inválido")
    
    return token


def calcular_hash_directorio(directory: str) -> str:
    """Genera un hash SHA256 único combinando la estructura y contenido del directorio base."""
    hasher = hashlib.sha256()
    for root, _, files in sorted(os.walk(directory)):
        for file in sorted(files):
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, directory)
            hasher.update(rel_path.encode('utf-8'))
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    hasher.update(chunk)
    return hasher.hexdigest()


@app.get("/.well-known/opencode")
async def obtener_configuracion_descubrimiento(request: Request, token: str = Depends(verificar_token_oauth2)):
    """Endpoint de descubrimiento OpenCode - devuelve config.json con auth.command inyectado."""
    base_url = SERVER_BASE_URL
    
    # Intentar leer el archivo complejo config.json fijo para toda la empresa
    config_file_path = os.path.join(CONFIG_ROOT, "config.json")
    if os.path.exists(config_file_path):
        with open(config_file_path, 'r', encoding='utf-8') as f:
            base_json = json.load(f)
    else:
        base_json = {"$schema": "https://opencode.ai/config.json"}
    
    # Adjuntar el script de Bootstrap automatizado inyectando el Token OAuth2 de forma segura
    base_json["auth"] = {
        "type": "command",
        "command": f"curl -fsSL -H 'Authorization: Bearer {token}' {base_url}/cdn/bootstrap.sh | bash -s -- {base_url} {token}"
    }
    return JSONResponse(content=base_json)


@app.get("/cdn/state", response_model=StateResponse)
async def obtener_estado_actual(token: str = Depends(verificar_token_oauth2)):
    """Retorna la huella digital actual de la carpeta config_root."""
    current_hash = calcular_hash_directorio(CONFIG_ROOT)
    return {"hash": current_hash}


@app.get("/cdn/bundle.tar.gz")
async def obtener_paquete_configuracion(token: str = Depends(verificar_token_oauth2)):
    """Empaqueta dinámicamente config_root para la máquina cliente."""
    io_stream = BytesIO()
    with tarfile.open(fileobj=io_stream, mode="w:gz") as tar:
        tar.add(CONFIG_ROOT, arcname=".")
    io_stream.seek(0)
    return StreamingResponse(io_stream, media_type="application/gzip")


@app.get("/cdn/out_of_band/omo/config.yaml")
async def obtener_configuracion_omo(token: str = Depends(verificar_token_oauth2)):
    """Sirve de forma fija la configuración externa para Oh-My-Opencode (~/.omo)."""
    omo_path = os.path.join(OUT_OF_BAND_ROOT, "omo", "config.yaml")
    if os.path.exists(omo_path):
        with open(omo_path, 'r', encoding='utf-8') as f:
            return Response(content=f.read(), media_type="text/yaml")
    raise HTTPException(status_code=404, detail="Archivo de configuración OMO no encontrado en el servidor.")


@app.get("/cdn/bootstrap.sh")
async def obtener_script_bootstrap(token: str = Depends(verificar_token_oauth2)):
    """Genera y sirve el script de bootstrap incremental."""
    script = f"""#!/usr/bin/env bash
set -e

# Recuperar los parámetros pasados de forma segura por el motor de OpenCode
SERVER_URL="${{1}}"
AUTH_TOKEN="${{2}}"

if [ -z "$SERVER_URL" ] || [ -z "$AUTH_TOKEN" ]; then
    echo "Error interno: Faltan credenciales o URL de sincronización corporativa." >&2
    exit 1
fi

TARGET_DIR="$HOME/.config/opencode"
STATE_FILE="$TARGET_DIR/.enterprise_hash"
mkdir -p "$TARGET_DIR"

# 1. Validar huella de versión remota vs versión local
REMOTE_STATE=$(curl -fsSL -H "Authorization: Bearer $AUTH_TOKEN" "$SERVER_URL/cdn/state" 2>/dev/null)
REMOTE_HASH=$(echo "$REMOTE_STATE" | grep -o '"hash":"[^"]*' | grep -o '[^"]*$')

LOCAL_HASH=""
if [ -f "$STATE_FILE" ]; then
    LOCAL_HASH=$(cat "$STATE_FILE")
fi

# Si los hashes coinciden, no se descarga nada y se asume éxito de sesión inmediato
if [ "$LOCAL_HASH" = "$REMOTE_HASH" ] && [ -n "$LOCAL_HASH" ]; then
    echo "$AUTH_TOKEN"
    exit 0
fi

echo "[Corp OS] Sincronizando cambios detectados en las reglas y plugins..." >&2

# 2. Descarga incremental y extracción controlada en la ruta de OpenCode
TMP_TAR=$(mktemp)
curl -fsSL -H "Authorization: Bearer $AUTH_TOKEN" "$SERVER_URL/cdn/bundle.tar.gz" -o "$TMP_TAR"
tar -xzf "$TMP_TAR" -C "$TARGET_DIR"
rm -f "$TMP_TAR"

# =========================================================================
# 3. CONFIGURACIÓN FUERA DE LA CARPETA BASE: Oh-My-Opencode (~/.omo)
# =========================================================================
OMO_DIR="$HOME/.omo"
mkdir -p "$OMO_DIR"

# Traer de forma segura el archivo config.yaml compartido de OMO
curl -fsSL -H "Authorization: Bearer $AUTH_TOKEN" "$SERVER_URL/cdn/out_of_band/omo/config.yaml" -o "$OMO_DIR/config.yaml" 2>/dev/null || true


# =========================================================================
# 4. VARIABLES DE ENTORNO FIJAS Y COMPARTIDAS (API Keys Empresariales)
# =========================================================================
ENV_FILE="$HOME/.opencode.env"
cat << 'EOF' > "$ENV_FILE"
# CONFIGURACIÓN DE ENTORNO CORPORATIVO COMPARTIDO
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export OPENCODE_JIRA_TOKEN="${OPENCODE_JIRA_TOKEN:-}"
export OMO_ENGINE_ROUTE="https://internal.corp"
export OPENCODE_RULES_STRICT_MODE="true"
EOF

# Inyectar el puente de lectura automática en los perfiles de la shell del sistema local
vincular_entorno_a_shell() {{
    local shell_config="$1"
    if [ -f "$shell_config" ]; then
        if ! grep -q ".opencode.env" "$shell_config"; then
            echo -e "\\n# OpenCode Environmental Initialization\\nif [ -f \\$HOME/.opencode.env ]; then . \\$HOME/.opencode.env; fi" >> "$shell_config"
        fi
    fi
}}

vincular_entorno_a_shell "$HOME/.bashrc"
vincular_entorno_a_shell "$HOME/.zshrc"

# =========================================================================

# 5. Guardar la huella digital para evitar ejecuciones duplicadas innecesarias
echo "$REMOTE_HASH" > "$STATE_FILE"

echo "[Corp OS] Actualización incremental completada con éxito." >&2

# Salida obligatoria para que el CLI de OpenCode mantenga la sesión viva
echo "$AUTH_TOKEN"
"""
    return Response(content=script, media_type="text/x-shellscript")


@app.get("/health")
async def health_check():
    """Health check endpoint para load balancers."""
    return {"status": "healthy", "service": "opencode-enterprise-config"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)