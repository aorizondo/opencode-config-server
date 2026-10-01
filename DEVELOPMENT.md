# Guía de Desarrollo - OpenCode Enterprise Config Server

## Resumen del Proyecto

Servidor de configuración empresarial para [OpenCode](https://opencode.ai) que proporciona:

- **Autenticación OAuth2** - Integración con proveedores corporativos (Keycloak, Auth0, Azure AD)
- **Sincronización incremental** - Hash SHA-256 + descarga diferencial via tar.gz
- **Despliegue completo** - `~/.config/opencode` + `~/.omo` + variables de entorno
- **Políticas de seguridad** - Reglas obligatorias via `opencode-rules`

---

## Estructura del Proyecto

```
opencode-config-server/
├── main.py                      # Servidor FastAPI principal
├── config_root/                 # → ~/.config/opencode/
│   ├── config.json              # Configuración principal OpenCode
│   └── rules/                   # Reglas opencode-rules
│       ├── coding-standards.md
│       ├── security-policies.md
│       └── git-workflow.md
├── out_of_band/                 # Archivos fuera de ~/.config/opencode
│   └── omo/                     # → ~/.omo/
│       └── config.yaml          # Config Oh-My-Opencode
├── tests/                       # Suite de tests (68 tests)
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_hash.py
│   ├── test_endpoints.py
│   ├── test_config_validation.py
│   └── test_bootstrap_integration.py
├── Dockerfile                   # Multi-stage build
├── docker-compose.yml           # Producción + monitoring opcional
├── requirements.txt
├── pyproject.toml
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

---

## Desarrollo Local

### Prerrequisitos

- Python 3.11+
- Docker (opcional, para containerización)
- Proveedor OAuth2 (Keycloak, Auth0, Azure AD) o usa mock para desarrollo

### Setup Inicial

```bash
# Clonar repo
git clone https://github.com/aorizondo/opencode-config-server.git
cd opencode-config-server

# Crear entorno virtual
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Instalar dependencias
pip install -e ".[dev]"

# Configurar variables de entorno
cp .env.example .env
# Editar .env con tus valores
```

### Variables de Entorno (.env)

```bash
# Servidor (DEBE ser HTTPS en producción)
SERVER_BASE_URL=https://opencode-config.tu-empresa.corp

# OAuth2 - Configurar según tu proveedor
OAUTH2_TOKEN_URL=https://auth.tu-empresa.corp/oauth2/token
OAUTH2_AUDIENCE=opencode-enterprise
OAUTH2_ISSUER=https://auth.tu-empresa.corp
JWKS_URL=https://auth.tu-empresa.corp/.well-known/jwks.json

# Secrets para inyección en clientes
ANTHROPIC_API_KEY=sk-ant-...
OPENCODE_JIRA_TOKEN=...
```

### Ejecutar en Desarrollo

```bash
# Opción 1: Python directo
python main.py
# Servidor en http://localhost:8000

# Opción 2: Con uvicorn (hot reload)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Opción 3: Docker
docker-compose up -d --build
```

### Probar Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Descubrimiento OpenCode (requiere token)
curl -H "Authorization: Bearer TU_TOKEN" \
  http://localhost:8000/.well-known/opencode

# Hash del estado
curl -H "Authorization: Bearer TU_TOKEN" \
  http://localhost:8000/cdn/state

# Script bootstrap
curl -H "Authorization: Bearer TU_TOKEN" \
  http://localhost:8000/cdn/bootstrap.sh
```

---

## Tests

### Ejecutar Suite Completa

```bash
# Todos los tests
python -m pytest tests/ -v

# Con coverage
python -m pytest tests/ --cov=. --cov-report=html

# Solo tests unitarios rápidos
python -m pytest tests/test_hash.py tests/test_auth.py -v

# Tests de integración (async)
python -m pytest tests/test_endpoints.py tests/test_bootstrap_integration.py -v
```

### Estructura de Tests

| Archivo | Descripción |
|---------|-------------|
| `test_hash.py` | Unit tests para `calcular_hash_directorio()` |
| `test_auth.py` | Validación de tokens OAuth2 |
| `test_endpoints.py` | Tests de todos los endpoints FastAPI |
| `test_config_validation.py` | Validación de configs JSON/YAML |
| `test_bootstrap_integration.py` | Tests de generación y seguridad del script |

---

## Agregar/Modificar Reglas

### 1. Crear regla en `config_root/rules/`

```bash
# Ejemplo: nueva regla de arquitectura
cat > config_root/rules/architecture.md << 'EOF'
# Arquitectura Corporativa

## Principios
- Clean Architecture / Hexagonal
- Dependency Inversion
- Domain-driven design
EOF
```

### 2. Referenciar en `config.json` (si es obligatoria)

```json
{
  "experimental": {
    "policies": [...]
  },
  "rules": {
    "mandatory_rules": [
      "coding-standards",
      "security-policies",
      "git-workflow",
      "architecture"  // ← agregar aquí
    ]
  }
}
```

### 3. Test automático

El test `test_rules_have_content` verifica que todas las reglas tengan contenido sustancial (>100 chars).

---

## Modificar Configuración Oh-My-Opencode

Editar `out_of_band/omo/config.yaml`:

```yaml
# Agregar nuevo perfil
profiles:
  qa:
    model: "anthropic/claude-sonnet-4-5"
    temperature: 0.15
    permissions:
      bash: "ask"
      edit: "ask"

# Agregar nuevo agente
agents:
  security-auditor:
    model: "anthropic/claude-sonnet-4-5"
    system_prompt: |
      Eres un auditor de seguridad...
    permissions:
      read: "allow"
      glob: "allow"
      grep: "allow"

# Agregar integración
integrations:
  sonarqube:
    enabled: true
    url: "https://sonar.corp"
    token_env: "SONAR_TOKEN"
```

---

## Implementar Validación JWT Real

En `main.py`, función `verificar_token_oauth2()`:

```python
# Descomentar e instalar: pip install python-jose[cryptography] httpx
from jose import jwt, JWTError
from jose.utils import base64url_decode
import httpx

# Cache de JWKS
_jwks_cache = None
_jwks_cache_time = 0

async def obtener_jwks():
    global _jwks_cache, _jwks_cache_time
    import time
    if _jwks_cache and time.time() - _jwks_cache_time < 3600:  # 1 hora
        return _jwks_cache
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(JWKS_URL)
        resp.raise_for_status()
        _jwks_cache = resp.json()["keys"]
        _jwks_cache_time = time.time()
        return _jwks_cache

async def verificar_token_oauth2(token: str = Depends(oauth2_scheme)) -> str:
    # 1. Obtener header para kid
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    
    # 2. Buscar key en JWKS
    keys = await obtener_jwks()
    key = next((k for k in keys if k["kid"] == kid), None)
    if not key:
        raise HTTPException(401, "Key no encontrada en JWKS")
    
    # 3. Verificar token
    try:
        payload = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=OAUTH2_AUDIENCE,
            issuer=OAUTH2_ISSUER
        )
    except JWTError as e:
        raise HTTPException(401, f"Token inválido: {e}")
    
    # 4. Verificar claims adicionales si necesario
    # if payload.get("scope", "") != "opencode:config":
    #     raise HTTPException(403, "Scope insuficiente")
    
    return token
```

---

## Despliegue en Producción

### Docker (Recomendado)

```bash
# Build
docker build -t opencode-config-server .

# Run con variables de entorno
docker run -d \
  --name opencode-config \
  -p 8000:8000 \
  -e SERVER_BASE_URL=https://config.tu-empresa.corp \
  -e OAUTH2_TOKEN_URL=https://auth.tu-empresa.corp/oauth2/token \
  -e OAUTH2_AUDIENCE=opencode-enterprise \
  -e OAUTH2_ISSUER=https://auth.tu-empresa.corp \
  -e JWKS_URL=https://auth.tu-empresa.corp/.well-known/jwks.json \
  -e ANTHROPIC_API_KEY=... \
  -e OPENCODE_JIRA_TOKEN=... \
  opencode-config-server
```

### Docker Compose (Producción)

```bash
# Con Traefik (TLS automático via Let's Encrypt)
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Ver logs
docker-compose logs -f opencode-config
```

### Kubernetes (Helm/Kustomize)

```yaml
# kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - service.yaml
  - ingress.yaml
  - secret.yaml
```

### Requisitos de Producción Obligatorios

| Requisito | Descripción |
|-----------|-------------|
| **HTTPS** | OpenCode rechaza HTTP para `.well-known/opencode` |
| **Certificados válidos** | Let's Encrypt o CA corporativa |
| **OAuth2 real** | Validación JWT con JWKS/introspección |
| **Secrets management** | HashiCorp Vault, AWS Secrets Manager, etc. |
| **Monitoring** | Prometheus + Grafana (métricas en `/metrics` si se añade) |
| **Logs estructurados** | JSON logging a stdout/stderr |

---

## Arquitectura del Bootstrap Script

### Flujo de Ejecución

```
1. OpenCode CLI → opencode auth login <URL> --token <TOKEN>
2. OpenCode GET /.well-known/opencode → recibe config.json + auth.command
3. OpenCode ejecuta: curl -H "Auth: Bearer <TOKEN>" <URL>/cdn/bootstrap.sh | bash -s -- <URL> <TOKEN>
4. Script bootstrap.sh:
   a. GET /cdn/state → remote_hash
   b. Lee ~/.config/opencode/.enterprise_hash → local_hash
   c. Si coinciden: exit 0 (skip)
   d. Si difieren: GET /cdn/bundle.tar.gz → tar -xzf en ~/.config/opencode/
   e. GET /cdn/out_of_band/omo/config.yaml → ~/.omo/config.yaml
   f. Escribe ~/.opencode.env con vars de entorno
   g. Inyecta source en ~/.bashrc y ~/.zshrc
   h. Guarda remote_hash en .enterprise_hash
   i. Echo <TOKEN> para mantener sesión OpenCode
```

### Seguridad del Script

- `set -e` - Falla rápido en errores
- Variables quotadas: `"$VAR"` o `${VAR}`
- Validación de parámetros de entrada
- `curl -fsSL` - Silent, fail on error, follow redirects
- Timeout implícito via curl defaults
- No ejecuta código arbitrario del servidor (solo descarga archivos estáticos)

---

## Merge Inteligente (User-Wins)

### Filosofía

El bootstrap script ahora implementa un **merge inteligente** que preserva la configuración del usuario. La organización provee valores por defecto, pero el usuario siempre gana: su configuración existente nunca se sobrescribe.

### Estrategias de Merge por Tipo

| Archivo/Tipo | Estrategia | Comportamiento |
|--------------|------------|----------------|
| `config.json` | Deep merge | Merge recursivo de objetos. Valores del usuario prevalecen. Nuevas keys de la org se añaden. |
| `~/.omo/config.yaml` | Deep merge | Igual que JSON. Perfiles, agentes, integraciones del usuario se preservan. |
| `rules/*.md` | Add-only | Solo copia archivos `.md` que NO existan localmente. Reglas del usuario intactas. |
| `plugins/*.js` | Add-only | Solo añade plugins nuevos. |
| `~/.opencode.env` | Merge vars | Lee vars existentes. Solo añade vars de la org que falten. Usuario gana siempre. |

### Flujo de Merge

```
1. Descarga bundle.tar.gz → directorio temporal
2. Extrae a /tmp/merge/remote/
3. Ejecuta merge_script.py(target_dir, remote_dir)
   ├── deep_merge_json(~/.config/opencode/config.json, remote/config.json)
   ├── merge_rules(~/.config/opencode/rules/, remote/rules/)  # solo add
   ├── merge_rules(~/.config/opencode/plugins/, remote/plugins/)  # solo add
   ├── deep_merge_yaml(~/.omo/config.yaml, remote/config.yaml)
   └── merge_env(~/.opencode.env, org_env_vars)  # solo vars faltantes
4. Limpia directorio temporal
5. Actualiza .enterprise_hash
```

### Ejemplos de Comportamiento

#### config.json - Usuario añade proveedor personalizado
```json
// Usuario tiene:
{ "model": "mi-modelo", "provider": { "mi-proxy": { "url": "https://proxy" } } }

// Org actualiza:
{ "model": "org-model", "provider": { "org-gateway": { "url": "https://org" } } }

// Resultado (merge):
{ 
  "model": "mi-modelo",           // ← Usuario gana
  "provider": { 
    "mi-proxy": { "url": "https://proxy" },  // ← Usuario gana
    "org-gateway": { "url": "https://org" }  // ← Org se añade
  }
}
```

#### Rules - Usuario personaliza regla existente
```bash
# Usuario tiene: ~/.config/opencode/rules/mi-estilo.md
# Org añade: rules/nueva-regla.md

# Resultado: Ambas existen. mi-estilo.md NO se sobrescribe.
```

#### Variables de entorno - Usuario tiene API key propia
```bash
# Usuario tiene en ~/.opencode.env:
export ANTHROPIC_API_KEY="sk-user-key"

# Org provee:
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"

# Resultado: sk-user-key se preserva (usuario gana)
```

### Forzar Re-sync Completo (si es necesario)

```bash
# Eliminar hash para forzar re-merge completo
rm ~/.config/opencode/.enterprise_hash
opencode auth login https://config.tu-empresa.corp --token TU_TOKEN
```

### Verificar Qué Se Mergeó

```bash
# Ver diff de config.json
diff -u <(curl -s -H "Auth: Bearer $TOKEN" $URL/cdn/bundle.tar.gz | tar -xzO config.json) ~/.config/opencode/config.json

# Ver variables de entorno finales
cat ~/.opencode.env
```

### Personalizar Comportamiento de Merge

Editar `merge_script.py` para ajustar estrategias:

```python
# Cambiar a "org wins" para una key específica
def merge_dict(tgt, src):
    for key, value in src.items():
        if key in tgt:
            if isinstance(tgt[key], dict) and isinstance(value, dict):
                merge_dict(tgt[key], value)
            # Org wins para claves específicas
            if key in ["model", "theme"]:  
                tgt[key] = value
        else:
            tgt[key] = value
```

---

## Contribuir

### Flujo de Trabajo

```bash
# 1. Crear branch
git checkout -b feature/mi-feature

# 2. Commits atómicos (Conventional Commits)
git commit -m "feat(rules): add architecture standards"
git commit -m "fix(bootstrap): handle missing .bashrc gracefully"

# 3. Tests pasando
python -m pytest tests/ -v

# 4. Lint
ruff check .
ruff format .
mypy .

# 5. PR con descripción clara
# Título: feat: add architecture standards rule
# Body: qué, por qué, testing realizado
```

### Convenciones de Código

- **Python**: Ruff + MyPy (config en `pyproject.toml`)
- **Commits**: Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`)
- **PRs**: Requieren review + tests passing

---

## Troubleshooting

### Bootstrap script no se ejecuta

```bash
# 1. Verificar auth.command en discovery
curl -H "Auth: Bearer $TOKEN" $URL/.well-known/opencode | jq .auth.command

# 2. Probar script manualmente
curl -H "Auth: Bearer $TOKEN" $URL/cdn/bootstrap.sh | bash -s -- $URL $TOKEN

# 3. Logs de OpenCode
tail -f ~/.config/opencode/logs/opencode.log
```

### Hash no coincide / sync falla

```bash
# Forzar re-sync completo
rm ~/.config/opencode/.enterprise_hash
opencode auth login $URL --token $TOKEN
```

### Variables de entorno no cargan

```bash
# Verificar archivo
cat ~/.opencode.env

# Verificar en shell rc
grep opencode.env ~/.bashrc

# Recargar
source ~/.bashrc
```

### Token OAuth2 rechazado

```bash
# Verificar token válido
curl -H "Authorization: Bearer $TOKEN" $URL/cdn/state

# Verificar JWKS accesible
curl $JWKS_URL | jq .

# Verificar claims del token
echo $TOKEN | cut -d. -f2 | base64 -d | jq .
```

---

## Referencias

- [OpenCode Config Docs](https://opencode.ai/docs/config)
- [OpenCode Plugins](https://opencode.ai/docs/plugins)
- [FastAPI Docs](https://fastapi.tiangolo.com/)
- [OAuth2 RFC 6749](https://tools.ietf.org/html/rfc6749)
- [JWT RFC 7519](https://tools.ietf.org/html/rfc7519)

---

## Licencia

MIT License - Ver [LICENSE](LICENSE)