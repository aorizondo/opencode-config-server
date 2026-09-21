# OpenCode Enterprise Config Server

Servidor de configuración empresarial para [OpenCode](https://opencode.ai) que proporciona:

- 🔐 **Autenticación OAuth2** - Integración con proveedores corporativos (Keycloak, Auth0, Azure AD, etc.)
- 🔄 **Sincronización incremental** - Solo descarga cambios usando hash SHA-256
- 📦 **Despliegue completo** - Configuración en `~/.config/opencode` + externas (`~/.omo`, variables de entorno)
- 🛡️ **Políticas de seguridad** - Reglas corporativas obligatorias via `opencode-rules`
- 🐳 **Container-ready** - Docker multi-stage + docker-compose para producción

## Arquitectura

```
┌─────────────────┐     HTTPS + OAuth2      ┌──────────────────────┐
│  Desarrollador  │ ──────────────────────▶ │  Config Server       │
│  (OpenCode CLI) │ ◀────────────────────── │  (FastAPI + OAuth2)  │
└─────────────────┘   config.json + auth    └──────────┬───────────┘
                                                      │
                        ┌─────────────────────────────┼─────────────────────────────┐
                        ▼                             ▼                             ▼
               ┌─────────────────┐           ┌─────────────────┐           ┌─────────────────┐
               │ ~/.config/      │           │ ~/.omo/         │           │ ~/.opencode.env │
               │ opencode/       │           │ config.yaml     │           │ (API keys, etc) │
               │ config.json     │           │                 │           │                 │
               │ rules/*.md      │           │                 │           │                 │
               └─────────────────┘           └─────────────────┘           └─────────────────┘
```

## Inicio Rápido

### 1. Clonar y configurar

```bash
git clone https://github.com/tu-org/opencode-enterprise-config.git
cd opencode-enterprise-config

# Copiar y editar variables de entorno
cp .env.example .env
# Editar .env con tus valores reales
```

### 2. Desarrollo local

```bash
# Crear entorno virtual
python -m venv .venv
source .venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar servidor
python main.py
# Servidor en http://localhost:8000
```

### 3. Con Docker

```bash
# Construir y ejecutar
docker-compose up -d --build

# Ver logs
docker-compose logs -f opencode-config
```

### 4. Probar endpoint de descubrimiento

```bash
# Sin autenticación (fallará con 401)
curl -i http://localhost:8000/.well-known/opencode

# Con token OAuth2 válido
curl -H "Authorization: Bearer TU_TOKEN_OAUTH2" \
     http://localhost:8000/.well-known/opencode
```

## Configuración

### Variables de Entorno (.env)

```bash
# Servidor
SERVER_BASE_URL=https://opencode-config.tu-empresa.corp

# OAuth2 (configurar según tu proveedor)
OAUTH2_TOKEN_URL=https://auth.tu-empresa.corp/oauth2/token
OAUTH2_AUDIENCE=opencode-enterprise
OAUTH2_ISSUER=https://auth.tu-empresa.corp
JWKS_URL=https://auth.tu-empresa.corp/.well-known/jwks.json

# Secrets para inyección en clientes (bootstrap.sh)
ANTHROPIC_API_KEY=sk-ant-...
OPENCODE_JIRA_TOKEN=atlassian-token-...
```

### Estructura de Configuración

```
opencode-enterprise-config/
├── main.py                    # Servidor FastAPI
├── config_root/               # → ~/.config/opencode/
│   ├── config.json            # Config principal OpenCode
│   └── rules/                 # Reglas opencode-rules
│       ├── coding-standards.md
│       ├── security-policies.md
│       └── git-workflow.md
└── out_of_band/               # Archivos fuera de ~/.config/opencode
    └── omo/                   # → ~/.omo/
        └── config.yaml        # Config Oh-My-Opencode
```

### Personalizar Reglas

Edita los archivos en `config_root/rules/` - se sincronizan automáticamente:

- `coding-standards.md` - Estándares de código por lenguaje
- `security-policies.md` - Políticas de seguridad obligatorias
- `git-workflow.md` - Flujo de trabajo Git corporativo

### Personalizar Oh-My-Opencode

Edita `out_of_band/omo/config.yaml` para:

- Perfiles por entorno (dev/staging/prod)
- Agentes predefinidos (code-reviewer, test-generator, docs-writer)
- Integraciones (Jira, GitLab, Slack, PagerDuty)
- Hooks de seguridad y auditoría

## Uso por Desarrolladores

### Login único

```bash
# Autenticarse contra el servidor corporativo
opencode auth login https://opencode-config.tu-empresa.corp \
  --token TU_TOKEN_DE_AUTHENTICATOR
```

Esto ejecuta automáticamente el bootstrap script que:
1. Verifica hash local vs remoto
2. Descarga solo cambios (tar.gz incremental)
3. Despliega `~/.config/opencode/` + reglas
4. Despliega `~/.omo/config.yaml`
5. Inyecta variables en `~/.opencode.env` y `.bashrc`/`.zshrc`
6. Guarda hash para próxima vez (skip si sin cambios)

### Verificar estado

```bash
# Ver hash actual del servidor
curl -H "Authorization: Bearer $TOKEN" \
  https://opencode-config.tu-empresa.corp/cdn/state

# Ver hash local guardado
cat ~/.config/opencode/.enterprise_hash
```

## Despliegue en Producción

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

### Docker Swarm

```bash
docker stack deploy -c docker-compose.yml opencode-config
```

### Requisitos de Producción

- **HTTPS obligatorio** - OpenCode rechaza HTTP para `.well-known/opencode`
- **Certificados válidos** - Let's Encrypt o CA corporativa
- **OAuth2 configurado** - Validación real de JWT (JWKS, introspección)
- **Secrets management** - HashiCorp Vault, AWS Secrets Manager, etc.
- **Monitoring** - Prometheus + Grafana (métricas en `/metrics` si se añade)

## Endpoints API

| Endpoint | Método | Auth | Descripción |
|----------|--------|------|-------------|
| `/.well-known/opencode` | GET | OAuth2 | Descubrimiento OpenCode + auth.command |
| `/cdn/state` | GET | OAuth2 | Hash SHA-256 actual de config_root |
| `/cdn/bundle.tar.gz` | GET | OAuth2 | Archivo tar.gz con config_root completo |
| `/cdn/out_of_band/omo/config.yaml` | GET | OAuth2 | Config YAML para ~/.omo |
| `/cdn/bootstrap.sh` | GET | OAuth2 | Script de bootstrap incremental |
| `/health` | GET | Público | Health check para load balancers |

## Seguridad

### Validación de Tokens (Producción)

En `main.py`, la función `verificar_token_oauth2()` debe implementar validación real:

```python
from jose import jwt, JWTError
from jose.utils import base64url_decode
import httpx

async def verificar_token_oauth2(token: str = Depends(oauth2_scheme)) -> str:
    # 1. Obtener JWKS
    async with httpx.AsyncClient() as client:
        jwks = await client.get(JWKS_URL)
        keys = jwks.json()["keys"]
    
    # 2. Encontrar key por kid
    header = jwt.get_unverified_header(token)
    key = next((k for k in keys if k["kid"] == header["kid"]), None)
    
    # 3. Verificar firma y claims
    payload = jwt.decode(
        token,
        key=key,
        algorithms=["RS256"],
        audience=OAUTH2_AUDIENCE,
        issuer=OAUTH2_ISSUER
    )
    return token
```

### Políticas de Seguridad Aplicadas

- Denegar proveedores no autorizados (OpenAI, Gemini, Groq)
- Bloquear comandos peligrosos (`rm -rf /`, `curl | bash`, fork bombs)
- Restringir paths de archivo (bloquear `/etc`, `~/.ssh`, `~/.aws`, etc.)
- Requerir MFA para acceso a producción
- Auditoría de todas las ejecuciones de herramientas

## Desarrollo

### Añadir nuevas reglas

```bash
# 1. Crear archivo en config_root/rules/
echo "# Nueva Regla" > config_root/rules/nueva-regla.md

# 2. Referenciar en config.json si es obligatoria
# "mandatory_rules": [..., "nueva-regla"]

# 3. Commit y push - se sincroniza automáticamente en próximo login
git add .
git commit -m "feat(rules): add nueva-regla"
git push
```

### Tests

```bash
# Ejecutar tests
pytest -v

# Con coverage
pytest --cov=. --cov-report=html
```

### Linting

```bash
# Ruff (rápido)
ruff check .
ruff format .

# MyPy (tipado)
mypy .
```

## Troubleshooting

### El bootstrap script no se ejecuta

1. Verificar que `auth.command` esté en el JSON de `/.well-known/opencode`
2. Verificar logs de OpenCode: `~/.config/opencode/logs/opencode.log`
3. Probar script manualmente: `curl -H "Auth: Bearer $TOKEN" $URL/cdn/bootstrap.sh | bash -s -- $URL $TOKEN`

### Hash no coincide / sincronización falla

```bash
# Forzar re-sincronización completa
rm ~/.config/opencode/.enterprise_hash
opencode auth login $URL --token $TOKEN
```

### Variables de entorno no cargan

```bash
# Verificar archivo
cat ~/.opencode.env

# Verificar que está en .bashrc/.zshrc
grep opencode.env ~/.bashrc

# Recargar shell
source ~/.bashrc
```

## Contribuir

1. Fork del repo
2. Crear branch: `git checkout -b feature/mi-feature`
3. Commits siguiendo [Conventional Commits](https://www.conventionalcommits.org/)
4. Tests pasando: `pytest && ruff check . && mypy .`
5. PR con descripción clara

## Licencia

MIT License - Ver [LICENSE](LICENSE) para detalles.

## Soporte

- 📧 Email: it@tu-empresa.corp
- 🐛 Issues: [GitHub Issues](https://github.com/tu-org/opencode-enterprise-config/issues)
- 📖 Wiki: [Documentación completa](https://github.com/tu-org/opencode-enterprise-config/wiki)