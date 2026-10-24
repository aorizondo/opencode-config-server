"""HTTP surface of the enterprise configuration server.

    GET  /.well-known/opencode          public    discovery manifest
    POST /auth/token                    static    credentials -> session token
    POST /auth/device                   public    start a browser login (code pair)
    POST /auth/device/token             device    browser-approved code -> session token
    GET  /login                         public    browser login page
    POST /login/approve                 static    credentials + code -> approve the login
    GET  /config/opencode.json          bearer    corporate configuration
    GET  /skills/{token}/index.json     in-path   remote skill index
    GET  /skills/{token}/{name}/{file}  in-path   remote skill file
    GET  /bootstrap/install.sh          bearer    one-off installer
    GET  /health                        public    liveness, no secrets

Only the manifest, /health, the login page and the start of a browser login are
unauthenticated. None of them returns a secret: the manifest holds the login
command and where the configuration lives, and a browser login only yields a
token once someone has approved its code with valid credentials.
"""

# No `from __future__ import annotations` here on purpose: FastAPI resolves
# string annotations against module globals only, and the dependencies below are
# closures over create_app's locals. Deferred annotations make it fail to see
# the `Depends(...)` markers and treat them as query parameters instead.
import binascii
import base64
import html
import logging
import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import configdoc, manifest, skills
from .auth import (
    VIRTUAL_KEY_PREFIX,
    VIRTUAL_KEY_SUBJECT,
    AuthError,
    BifrostKeys,
    BifrostUnavailable,
    Claims,
    RateLimiter,
    SessionTokens,
    User,
    UserStore,
)
from .device import DeviceError, DeviceFlow, format_user_code, normalize_user_code
from .settings import Settings, load as load_settings

logger = logging.getLogger("opencode_config_server")

NO_STORE = {"Cache-Control": "no-store"}

_LOGIN_PAGE = Path(__file__).with_name("login.html")


class DeviceTokenRequest(BaseModel):
    device_code: str


class ApproveRequest(BaseModel):
    user_code: str
    api_key: str | None = None
    username: str | None = None
    password: str | None = None


def _device_error(error: DeviceError) -> JSONResponse:
    """RFC 8628 error shape; `detail` keeps the login script's parser working."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": error.code, "detail": str(error)},
        headers=NO_STORE,
    )


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
    devices = DeviceFlow(ttl_seconds=config.device_ttl_seconds)
    bifrost = BifrostKeys(config.bifrost_url) if config.bifrost_url else None

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
    app.state.devices = devices

    def client_key(request: Request) -> str:
        if config.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def verify_session(token: str) -> Claims:
        """Check signature, expiry, and that the account is still entitled."""
        claims = tokens.verify(token)
        if claims.subject.startswith(VIRTUAL_KEY_SUBJECT):
            # Opened with a Bifrost virtual key: there is no users.json entry to
            # consult, so these sessions are short lived (OCS_VK_TOKEN_TTL_DAYS).
            if bifrost is None:
                raise AuthError("virtual-key sessions are no longer accepted; run `opencode auth login` again")
            return claims
        user = users.get(claims.subject)
        if user is None:
            raise AuthError(f"account {claims.subject!r} no longer exists")
        if user.disabled:
            raise AuthError(f"account {claims.subject!r} is disabled")
        if user.version != claims.version:
            raise AuthError("session token has been revoked; run `opencode auth login` again")
        return claims

    def check_database() -> None:
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

    def throttle(bucket: str) -> None:
        try:
            limiter.check(bucket)
        except AuthError as error:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)) from error

    async def authenticate_key(key: str) -> User:
        """A local API key, or a Bifrost virtual key checked live against Bifrost."""
        if not key.startswith(VIRTUAL_KEY_PREFIX):
            return users.authenticate_api_key(key)
        if bifrost is None:
            raise AuthError("Bifrost virtual keys are not accepted by this server")
        try:
            return await run_in_threadpool(bifrost.authenticate, key)
        except BifrostUnavailable as error:
            # Bifrost being down is not the employee's fault: no rate-limit strike.
            logger.error("cannot validate a virtual key: %s", error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="cannot validate the virtual key right now (Bifrost unavailable); retry later",
            ) from error

    def token_response(user: User) -> JSONResponse:
        ttl = config.vk_token_ttl_days * 86_400 if user.name.startswith(VIRTUAL_KEY_SUBJECT) else None
        token, claims = tokens.issue(user, ttl_seconds=ttl)
        logger.info("issued session token for %s (expires %s)", claims.subject, claims.expires_at)
        return JSONResponse(
            content={"token": token, "subject": claims.subject, "expires_at": claims.expires_at},
            headers=NO_STORE,
        )

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
        throttle(bucket)
        check_database()

        try:
            if x_api_key:
                user = await authenticate_key(x_api_key.strip())
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
        return token_response(user)

    # -- browser login (RFC 8628 shape) -------------------------------------
    # The login command cannot prompt (stdin is /dev/null) and runs with the
    # OpenCode server's environment, not the employee's shell. So it asks for a
    # code pair, sends the employee to /login, and polls until they approve.

    @app.post("/auth/device", tags=["auth"])
    async def device_start(request: Request) -> JSONResponse:
        """Start a browser login. Returns the code pair the login script needs."""
        throttle(client_key(request))
        try:
            grant = devices.start()
        except DeviceError as error:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)) from error
        verification = f"{config.base_url}/login"
        return JSONResponse(
            content={
                "device_code": grant.device_code,
                "user_code": format_user_code(grant.user_code),
                "verification_uri": verification,
                "verification_uri_complete": f"{verification}?code={grant.user_code}",
                "expires_in": devices.ttl_seconds,
                "interval": grant.interval,
            },
            headers=NO_STORE,
        )

    @app.post("/auth/device/token", tags=["auth"])
    async def device_token(body: DeviceTokenRequest) -> JSONResponse:
        """Polled by the login script; yields the session token exactly once."""
        try:
            subject = devices.poll(body.device_code)
        except DeviceError as error:
            return _device_error(error)
        if subject.startswith(VIRTUAL_KEY_SUBJECT):
            # Bifrost vouched for the key a moment ago in /login/approve.
            return token_response(User(name=subject))
        user = users.get(subject)
        if user is None or user.disabled:
            return _device_error(DeviceError("access_denied", f"account {subject!r} cannot sign in"))
        return token_response(user)

    @app.get("/login", tags=["auth"], response_class=HTMLResponse)
    async def login_page(code: str = "") -> HTMLResponse:
        """The page the employee opens; it posts to /login/approve."""
        nonce = secrets.token_urlsafe(16)
        prefill = normalize_user_code(code)[:16]
        page = (
            _LOGIN_PAGE.read_text(encoding="utf-8")
            .replace("__NONCE__", nonce)
            .replace("__HOST__", html.escape(config.base_url))
            .replace("__CODE__", html.escape(format_user_code(prefill) if prefill else ""))
        )
        return HTMLResponse(
            content=page,
            headers={
                **NO_STORE,
                # Everything is inline and nonce'd; the page only talks to us.
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/login/approve", tags=["auth"])
    async def login_approve(request: Request, body: ApproveRequest) -> JSONResponse:
        """Check the employee's credentials, then approve the code they typed.

        Credentials travel in the JSON body, never in a cookie, so there is no
        session for a cross-site request to ride on.
        """
        bucket = client_key(request)
        throttle(bucket)
        check_database()
        try:
            devices.pending(body.user_code)
        except DeviceError as error:
            limiter.record_failure(bucket)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        try:
            if body.api_key:
                user = await authenticate_key(body.api_key.strip())
            elif body.username:
                user = users.authenticate_password(body.username.strip(), body.password or "")
            else:
                raise AuthError("enter an API key, or a username and password")
        except AuthError as error:
            limiter.record_failure(bucket)
            logger.warning("browser login rejected from %s: %s", bucket, error)
            raise _unauthorized(str(error)) from error
        try:
            devices.approve(body.user_code, user.name)
        except DeviceError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
        limiter.reset(bucket)
        logger.info("browser login approved for %s", user.name)
        return JSONResponse(content={"status": "approved", "subject": user.name}, headers=NO_STORE)

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
