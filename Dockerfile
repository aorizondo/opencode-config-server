# Dockerfile para OpenCode Enterprise Config Server
# Multi-stage build para imagen mínima y segura

# =============================================================================
# Stage 1: Builder - Instala dependencias y compila si es necesario
# =============================================================================
FROM python:3.12-slim AS builder

# Configuración de build
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instalar dependencias del sistema necesarias para compilar
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Crear usuario no-root para build
RUN useradd --no-create-home --shell /bin/false builder

# Directorio de trabajo
WORKDIR /app

# Copiar archivos de dependencias primero (para cache de Docker)
COPY pyproject.toml requirements.txt ./

# Instalar dependencias en directorio virtual
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# =============================================================================
# Stage 2: Runtime - Imagen final mínima
# =============================================================================
FROM python:3.12-slim AS runtime

# Metadatos
LABEL org.opencontainers.image.title="OpenCode Enterprise Config Server" \
      org.opencontainers.image.description="Servidor de configuración empresarial para OpenCode con sincronización incremental" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.authors="Enterprise IT" \
      org.opencontainers.image.source="https://github.com/tu-org/opencode-enterprise-config" \
      org.opencontainers.image.licenses="MIT"

# Configuración de runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Configuración por defecto (sobrescribir via docker-compose/.env)
    SERVER_BASE_URL="http://localhost:8000" \
    OAUTH2_TOKEN_URL="https://tu-authenticator.corp/oauth2/token" \
    OAUTH2_AUDIENCE="opencode-enterprise" \
    OAUTH2_ISSUER="https://tu-authenticator.corp" \
    JWKS_URL="" \
    CONFIG_ROOT="/app/config_root" \
    OUT_OF_BAND_ROOT="/app/out_of_band"

# Instalar solo dependencias de runtime necesarias
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    tar \
    gzip \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Crear usuario no-root para ejecución
RUN groupadd --gid 1000 opencode \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/false opencode

# Copiar virtualenv desde builder
COPY --from=builder /opt/venv /opt/venv

# Directorio de trabajo
WORKDIR /app

# Copiar código de la aplicación
COPY --chown=opencode:opencode main.py ./
COPY --chown=opencode:opencode config_root/ ./config_root/
COPY --chown=opencode:opencode out_of_band/ ./out_of_band/

# Cambiar a usuario no-root
USER opencode

# Exponer puerto
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Comando de inicio
CMD ["python", "main.py"]