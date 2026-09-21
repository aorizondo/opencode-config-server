# Políticas de Seguridad Corporativas

## Gestión de Secrets

### Prohibido
- Hardcodear passwords, API keys, tokens en código
- Commitear archivos `.env`, `.env.local`, `secrets.yaml`
- Compartir credenciales por Slack, email, chat
- Usar secrets de producción en desarrollo

### Obligatorio
- Usar gestor de secrets corporativo (HashiCorp Vault, AWS Secrets Manager, 1Password CLI)
- Rotar credenciales cada 90 días máximo
- Revocar acceso inmediato al detectar compromiso
- Auditoría trimestral de accesos

## Dependencias

### Análisis de Vulnerabilidades
- Escaneo automático en CI/CD (OWASP Dependency Check, Snyk, Trivy)
- Bloquear merge si hay vulnerabilidades `CRITICAL` o `HIGH` sin mitigar
- Actualizar dependencias mensualmente (Dependabot/Renovate)
- Revisar licencias - solo licencias aprobadas (MIT, Apache-2.0, BSD-3)

### Supply Chain
- Verificar firmas de paquetes (npm provenance, sigstore)
- Usar lockfiles commitidos (`package-lock.json`, `go.sum`, `Cargo.lock`)
- Pinnear versiones en Dockerfiles (`FROM node:20.12.0@sha256:...`)
- Base images oficiales y actualizadas

## Red y Comunicaciones

### TLS/SSL
- TLS 1.3 obligatorio en producción
- Certificados válidos y renovados automáticamente (Let's Encrypt / cert-manager)
- HSTS activado en todos los endpoints HTTPS
- Certificate Transparency monitoring

### APIs
- Autenticación obligatoria (OAuth2/OIDC, mTLS, API Keys con rotación)
- Rate limiting por cliente/IP
- Validación estricta de entrada (schema validation)
- Logging de accesos anómalos (sin loggear datos sensibles)

## Datos

### Encriptación
- **En tránsito**: TLS 1.3 en todas las comunicaciones
- **En reposo**: AES-256 para bases de datos, discos, backups
- **En uso**: Confidential Computing donde aplique (AWS Nitro, Azure Confidential VMs)

### Clasificación
- **Pública**: Documentación, marketing
- **Interna**: Código, configs, logs anonimizados
- **Confidencial**: PII, credenciales, claves criptográficas
- **Restringida**: Secretos de producción, claves raíz

### Retención
- Logs de acceso: 1 año
- Logs de auditoría seguridad: 7 años
- Backups: 30 días (diarios), 1 año (mensuales)
- Datos de clientes: Según contrato/regulación (GDPR, CCPA)

## Acceso y Autenticación

### Principio de Mínimo Privilegio
- Acceso just-in-time (JIT) para producción
- Roles definidos por equipo/proyecto
- Revocación automática al cambio de rol/salida
- Revisiones de acceso trimestrales

### MFA
- Obligatorio para: VPN, consolas cloud, GitHub/GitLab, gestores de secrets
- Hardware keys (YubiKey) para admins de infraestructura

## Incidentes

### Respuesta
- Plan de respuesta a incidentes documentado y probado
- Runbooks para escenarios comunes
- Comunicación clara: stakeholders, clientes, reguladores
- Post-mortem blameless en < 48h para incidentes SEV-1/2

### Reporte
- Canal dedicado para reporte de vulnerabilidades (security@empresa.com)
- Bug bounty program para investigadores externos
- SLA de respuesta: 24h (ack), 72h (triage), 30d (fix crítico)

## Cumplimiento

- SOC 2 Type II anual
- ISO 27001 certificación
- GDPR/CCPA compliance para datos personales
- PCI DSS si procesa pagos
- Auditorías internas trimestrales