"""Runtime settings, resolved from the environment once at import of the app."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

#: Name of the environment variable the harness fills with the session token.
#: It appears in the manifest as `auth.env` and inside the corporate config as
#: `{env:<name>}`; the two must match or substitution silently yields "".
DEFAULT_SESSION_ENV = "OPENCODE_CORP_SESSION"


class SettingsError(RuntimeError):
    """Raised when the deployment is configured in a way that cannot work."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from error


@dataclass(frozen=True)
class Settings:
    #: Public origin clients reach, without trailing slash. Must be https in
    #: production: the session token travels in headers and in skill URLs.
    base_url: str
    config_dir: Path
    users_file: Path
    session_secret: str
    session_env: str = DEFAULT_SESSION_ENV
    token_ttl_days: int = 90
    environment: str = "development"
    rate_limit_attempts: int = 10
    rate_limit_window_seconds: int = 300
    bootstrap_enabled: bool = True
    skills_enabled: bool = True
    #: Honour X-Forwarded-For when rate limiting. Only enable behind a proxy
    #: you control, otherwise clients can forge their own rate-limit bucket.
    trust_proxy: bool = False
    ephemeral_secret: bool = field(default=False, compare=False)

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @property
    def opencode_config_file(self) -> Path:
        """The corporate document. `.jsonc` wins so comments are the default."""
        jsonc = self.config_dir / "opencode.jsonc"
        return jsonc if jsonc.exists() else self.config_dir / "opencode.json"

    @property
    def skills_dir(self) -> Path:
        return self.config_dir / "skills"

    @property
    def bootstrap_file(self) -> Path:
        return self.config_dir / "bootstrap" / "install.sh"


def load(**overrides: object) -> Settings:
    """Build settings from the environment, with explicit overrides for tests."""
    environment = str(overrides.pop("environment", os.getenv("OCS_ENV", "development")))
    base_url = str(overrides.pop("base_url", os.getenv("OCS_BASE_URL", "http://localhost:8000"))).rstrip("/")
    config_dir = Path(str(overrides.pop("config_dir", os.getenv("OCS_CONFIG_DIR", "./config")))).resolve()
    users_file = overrides.pop("users_file", os.getenv("OCS_USERS_FILE")) or config_dir / "users.json"
    secret = overrides.pop("session_secret", os.getenv("OCS_SESSION_SECRET"))

    ephemeral = False
    if not secret:
        if environment == "production":
            raise SettingsError(
                "OCS_SESSION_SECRET is required when OCS_ENV=production; "
                "generate one with: python -m opencode_config_server.secret"
            )
        # Development convenience: tokens stop working on restart, which is
        # noisy but never silently insecure.
        secret = secrets.token_urlsafe(48)
        ephemeral = True

    if environment == "production" and not base_url.startswith("https://"):
        raise SettingsError(f"OCS_BASE_URL must be https in production, got {base_url!r}")

    settings = Settings(
        base_url=base_url,
        config_dir=config_dir,
        users_file=Path(str(users_file)).resolve(),
        session_secret=str(secret),
        session_env=str(overrides.pop("session_env", os.getenv("OCS_SESSION_ENV", DEFAULT_SESSION_ENV))),
        token_ttl_days=int(overrides.pop("token_ttl_days", _env_int("OCS_TOKEN_TTL_DAYS", 90))),
        environment=environment,
        rate_limit_attempts=int(
            overrides.pop("rate_limit_attempts", _env_int("OCS_RATE_LIMIT_ATTEMPTS", 10)),
        ),
        rate_limit_window_seconds=int(
            overrides.pop("rate_limit_window_seconds", _env_int("OCS_RATE_LIMIT_WINDOW", 300)),
        ),
        bootstrap_enabled=bool(overrides.pop("bootstrap_enabled", _env_bool("OCS_BOOTSTRAP_ENABLED", True))),
        skills_enabled=bool(overrides.pop("skills_enabled", _env_bool("OCS_SKILLS_ENABLED", True))),
        trust_proxy=bool(overrides.pop("trust_proxy", _env_bool("OCS_TRUST_PROXY", False))),
        ephemeral_secret=ephemeral,
    )
    if overrides:
        raise SettingsError(f"unknown settings: {', '.join(sorted(map(str, overrides)))}")
    if not settings.session_env.isidentifier():
        raise SettingsError(f"OCS_SESSION_ENV must be a valid variable name, got {settings.session_env!r}")
    return settings
