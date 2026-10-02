"""HTTP surface of the enterprise configuration server.

    GET  /.well-known/opencode          public    discovery manifest
    POST /auth/token                    static    credentials -> session token
    GET  /config/opencode.json          bearer    corporate configuration
    GET  /skills/{token}/index.json     in-path   remote skill index
    GET  /skills/{token}/{name}/{file}  in-path   remote skill file
    GET  /bootstrap/install.sh          bearer    one-off installer
    GET  /health                        public    liveness, no secrets

Only the manifest and /health are unauthenticated, and the manifest contains no
secret: just the login command and where the configuration lives.
"""

# No `from __future__ import annotations` here on purpose: FastAPI resolves
# string annotations against module globals only, and the dependencies below are
# closures over create_app's locals. Deferred annotations make it fail to see
# the `Depends(...)` markers and treat them as query parameters instead.
import binascii
import base64
import logging
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import configdoc, manifest, skills
from .auth import AuthError, Claims, RateLimiter, SessionTokens, UserStore
from .settings import Settings, load as load_settings

logger = logging.getLogger("opencode_config_server")

NO_STORE = {"Cache-Control": "no-store"}


def _unauthorized(detail: str, *, scheme: str = "Bearer") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": scheme},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    users = UserStore(config.users_file)
    tokens = SessionTokens(config.session_secret, ttl_days=config.token_ttl_days)
    limiter = RateLimiter(
        attempts=config.rate_limit_attempts,
        window_seconds=config.rate_limit_window_seconds,
    )

    app = FastAPI(
        title="OpenCode Enterprise Config Server",
        description="Serves the corporate OpenCode configuration below each employee's own settings.",
        version="2.0.0",
        # No CORS: every client is the harness or curl, never a browser.
    )
    app.state.settings = config
    app.state.users = users
    app.state.tokens = tokens
    app.state.limiter = limiter

    def client_key(request: Request) -> str:
        if config.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def verify_session(token: str) -> Claims:
        """Check signature, expiry, and that the account is still entitled."""
        claims = tokens.verify(token)
        user = users.get(claims.subject)
        if user is None:
            raise AuthError(f"account {claims.subject!r} no longer exists")
        if user.disabled:
            raise AuthError(f"account {claims.subject!r} is disabled")
        if user.version != claims.version:
            raise AuthError("session token has been revoked; run `opencode auth login` again")
        return claims

    def bearer_claims(authorization: Annotated[str | None, Header()] = None) -> Claims:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise _unauthorized("a corporate session token is required")
        try:
            return verify_session(authorization[7:].strip())
        except AuthError as error:
            raise _unauthorized(str(error)) from error

    def path_claims(token: str) -> Claims:
        """Skill downloads carry no headers, so the token arrives in the path."""
        try:
            return verify_session(token)
        except AuthError as error:
            # 404 rather than 401: nothing here should hint at a valid namespace,
            # and the harness only logs a warning either way.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.get("/.well-known/opencode", tags=["discovery"])
    async def discovery() -> JSONResponse:
        """Public manifest. `opencode auth login <origin>` starts here."""
        return JSONResponse(content=manifest.build(config))

    @app.post("/auth/token", tags=["auth"])
    async def issue_token(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        """Exchange static credentials for a signed session token."""
        bucket = client_key(request)
        try:
            limiter.check(bucket)
        except AuthError as error:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)) from error

        database_error = users.error
        if database_error:
            logger.error("user database problem: %s", database_error)
            if users.count() == 0:
                # Nothing usable in memory either: this is a server fault and
                # must not be reported as a bad password. With a usable snapshot
                # we keep authenticating, so one typo during an edit does not
                # lock the whole company out.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="the corporate user database is unavailable; contact IT",
                )

        try:
            if x_api_key:
                user = users.authenticate_api_key(x_api_key.strip())
            elif authorization and authorization.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
                    name, _, password = decoded.partition(":")
                except (binascii.Error, UnicodeDecodeError) as error:
                    raise AuthError("malformed Basic credentials") from error
                user = users.authenticate_password(name, password)
            else:
                raise _unauthorized(
                    "send either an X-Api-Key header or HTTP Basic credentials",
                    scheme='Basic realm="opencode-corp"',
                )
        except AuthError as error:
            limiter.record_failure(bucket)
            logger.warning("login rejected from %s: %s", bucket, error)
            raise _unauthorized(str(error), scheme='Basic realm="opencode-corp"') from error

        limiter.reset(bucket)
        token, claims = tokens.issue(user)
        logger.info("issued session token for %s (expires %s)", claims.subject, claims.expires_at)
        return JSONResponse(
            content={"token": token, "subject": claims.subject, "expires_at": claims.expires_at},
            headers=NO_STORE,
        )

    @app.get("/config/opencode.json", tags=["config"])
    async def corporate_config(claims: Annotated[Claims, Depends(bearer_claims)]) -> JSONResponse:
        """The document the harness ranks *below* the employee's own files."""
        try:
            document = configdoc.load(config)
        except configdoc.ConfigError as error:
            logger.error("cannot serve corporate config: %s", error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the corporate configuration is unavailable; contact IT",
            ) from error
        logger.info("served corporate config to %s", claims.subject)
        return JSONResponse(content=document, headers=NO_STORE)

    @app.get("/skills/{token}/index.json", tags=["skills"])
    async def skill_index(token: str) -> JSONResponse:
        if not config.skills_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="skills are disabled")
        path_claims(token)
        return JSONResponse(content=skills.index(config.skills_dir), headers=NO_STORE)

    @app.get("/skills/{token}/{name}/{file_path:path}", tags=["skills"])
    async def skill_file(token: str, name: str, file_path: str) -> FileResponse:
        if not config.skills_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="skills are disabled")
        path_claims(token)
        try:
            target = skills.resolve(config.skills_dir, name, file_path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such skill file") from error
        media_type = "text/markdown; charset=utf-8" if target.suffix == ".md" else None
        return FileResponse(target, media_type=media_type, headers=NO_STORE)

    @app.get("/bootstrap/install.sh", tags=["bootstrap"])
    async def bootstrap(claims: Annotated[Claims, Depends(bearer_claims)]) -> Response:
        if not config.bootstrap_enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bootstrap is disabled")
        try:
            script = config.bootstrap_file.read_text(encoding="utf-8")
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no bootstrap installer is published",
            ) from error
        logger.info("served bootstrap to %s", claims.subject)
        return PlainTextResponse(content=script, media_type="text/x-shellscript", headers=NO_STORE)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, Any]:
        """Deployment self-check. Reports readiness without leaking anything."""
        problems: list[str] = []
        users.reload(force=True)
        if users.error:
            problems.append(users.error)
        elif users.count() == 0:
            problems.append("no users are configured")
        try:
            configdoc.load(config)
        except configdoc.ConfigError as error:
            problems.append(str(error))
        if config.ephemeral_secret:
            problems.append("OCS_SESSION_SECRET is unset: tokens will break on restart")
        return {
            "status": "healthy" if not problems else "degraded",
            "service": "opencode-enterprise-config",
            "users": users.count(),
            "skills": len(skills.discover(config.skills_dir)),
            "problems": problems,
        }

    return app


#: For `uvicorn opencode_config_server.app:app --factory`. The plain
#: instance lives in main.py so settings errors surface at startup.
app = create_app
