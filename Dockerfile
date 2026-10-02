# syntax=docker/dockerfile:1
# =============================================================================
# OpenCode Enterprise Config Server
# =============================================================================
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# =============================================================================
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="OpenCode Enterprise Config Server" \
      org.opencontainers.image.description="Serves the corporate OpenCode configuration below each employee's own settings" \
      org.opencontainers.image.version="2.0.0" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    OCS_ENV="production" \
    OCS_CONFIG_DIR="/app/config" \
    OCS_HOST="0.0.0.0" \
    OCS_PORT="8000"

# curl is needed by HEALTHCHECK only; the app shells out to nothing.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* && apt-get clean \
    && groupadd --gid 1000 opencode \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin opencode

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app

# The whole package, so login_script.sh ships with it. The previous version of
# this server copied individual files and left the script the login command
# downloads out of the image, which broke authentication in every container.
COPY --chown=opencode:opencode opencode_config_server/ ./opencode_config_server/
COPY --chown=opencode:opencode main.py ./
# Mount your own config over this in production: see docker-compose.yml.
COPY --chown=opencode:opencode config/ ./config/

USER opencode
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
