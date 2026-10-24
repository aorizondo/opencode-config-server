"""Web admin panel: the page at /admin and the JSON API under /admin/api.

Mounted only when OCS_ADMIN_TOKEN is set. One shared token, presented two ways:

  * scripts send `Authorization: Bearer <token>` on every call;
  * the browser trades it once, at POST /admin/api/session, for a signed
    HttpOnly, SameSite=Strict cookie scoped to /admin that lasts
    OCS_ADMIN_SESSION_HOURS. Requests riding on that cookie must also send
    `X-OCS-Admin: 1`: another site cannot add a custom header without a CORS
    preflight this server never answers, so CSRF is closed even where SameSite
    is not honoured.

Rotating OCS_ADMIN_TOKEN, or OCS_SESSION_SECRET, ends every panel session.

The panel edits the same files IT would edit by hand -- the server already
re-reads them on every request -- through `store.Store`: validated first,
written atomically, refused when someone else changed the file meanwhile, and
the previous version kept in the history so any save can be undone.
"""

# No `from __future__ import annotations`: FastAPI needs real annotations to see
# the dependencies and request bodies declared below (see app.py).
import hashlib
import hmac
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import configdoc, jsonc, manifest, skills, store, variables
from .auth import AuthError, RateLimiter, User, UserStore, generate_api_key, hash_api_key, hash_password
from .settings import Settings

logger = logging.getLogger("opencode_config_server.admin")

COOKIE = "ocs_admin"
PANEL_DIR = Path(__file__).with_name("panel")
#: The only files the panel page loads. Anything else under /admin/static is 404.
PANEL_ASSETS = {"admin.js": "text/javascript; charset=utf-8", "admin.css": "text/css; charset=utf-8"}
PANEL_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

#: Usable in HTTP Basic (no ':') and safe in logs and URLs.
_USER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
#: A safe single path segment, as the harness requires for skill names.
_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MIN_PASSWORD_LENGTH = 12
#: Keys pasted from another service (a Bifrost virtual key, say). Only their
#: unsalted SHA-256 is stored, so short guessable values are refused.
MIN_CUSTOM_KEY_LENGTH = 20
MAX_CUSTOM_KEY_LENGTH = 512
#: Starlette renamed the constant (422 UNPROCESSABLE_CONTENT); the code is what matters.
UNPROCESSABLE = 422
SKILL_TEMPLATE = "---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n"


class AdminSessions:
    """Signed, expiring panel sessions bound to the current admin token."""

    def __init__(self, session_secret: str, admin_token: str, ttl_seconds: int) -> None:
        self._token_digest = hashlib.sha256(admin_token.encode("utf-8")).digest()
        self._key = hmac.new(
            session_secret.encode("utf-8"), b"ocs-admin\0" + self._token_digest, hashlib.sha256
        ).digest()
        self.ttl_seconds = ttl_seconds

    def matches(self, candidate: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).digest(), self._token_digest)

    def _sign(self, expires: int) -> str:
        return hmac.new(self._key, str(expires).encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, *, now: float | None = None) -> tuple[str, int]:
        expires = int(time.time() if now is None else now) + self.ttl_seconds
        return f"{expires}.{self._sign(expires)}", expires

    def valid(self, cookie: str, *, now: float | None = None) -> bool:
        raw, _, signature = cookie.partition(".")
        if not raw.isdigit():
            return False
        expires = int(raw)
        if expires <= (time.time() if now is None else now):
            return False
        return hmac.compare_digest(signature, self._sign(expires))


# -- request bodies -----------------------------------------------------------


class SessionRequest(BaseModel):
    token: str


class ConfigWrite(BaseModel):
    etag: str
    #: The raw editor sends text (comments survive); the forms send a document.
    text: str | None = None
    document: dict[str, Any] | None = None


class ConfigDraft(BaseModel):
    text: str | None = None
    document: dict[str, Any] | None = None
    #: Preview the document as this employee receives it, variables filled in.
    as_user: str | None = None


class VariableWrite(BaseModel):
    #: None keeps the stored value (e.g. to flip `secret` on an existing one).
    value: str | None = None
    secret: bool = False
    #: Also set it in the environment of OpenCode on the employee's machine.
    machine: bool = False


class NewUser(BaseModel):
    name: str
    password: str | None = None
    #: Generate an API key for the new account.
    api_key: bool = False
    #: Or register this existing key instead (e.g. the employee's Bifrost key).
    api_key_value: str | None = None


class NewApiKey(BaseModel):
    #: An existing key to register; omitted, a new one is generated.
    key: str | None = None


class UserChange(BaseModel):
    password: str | None = None
    disabled: bool | None = None


class NewSkill(BaseModel):
    name: str
    description: str


class FileWrite(BaseModel):
    text: str
    #: ETag of what the editor loaded; "absent" when creating the file.
    etag: str


class Restore(BaseModel):
    etag: str | None = None


# -- helpers --------------------------------------------------------------------


def _invalid(detail: str, errors: list[str] | None = None, warnings: list[str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=UNPROCESSABLE,
        content={"detail": detail, "errors": errors or [], "warnings": warnings or []},
    )


def _conflict(error: store.Conflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{error}; reload to see the current version",
    )


def _not_writable(path: Path) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{path} is read-only for the server (mounted read-only?); the panel cannot change it",
    )


def has_comments(text: str) -> bool:
    try:
        return jsonc.strip(text) != text
    except ValueError:
        return False


def key_fingerprint(stored: str) -> str:
    """Short, stable handle for one stored API key digest (never the key)."""
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:12]


def user_summary(entry: dict[str, Any]) -> dict[str, Any]:
    keys = entry.get("api_keys", [])
    if isinstance(keys, str):
        keys = [keys]
    return {
        "name": entry.get("name"),
        "password": bool(entry.get("password")),
        "api_keys": [key_fingerprint(key) for key in keys if isinstance(key, str)],
        "version": entry.get("version", 1),
        "disabled": bool(entry.get("disabled", False)),
    }


def frontmatter(text: str) -> dict[str, str]:
    """The `key: value` lines of a SKILL.md header; enough to sanity-check it."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip()] = value.strip().strip("'\"")
    return fields


def skill_warnings(directory: Path) -> list[str]:
    entry = directory / "SKILL.md"
    if not entry.is_file():
        if (directory / f"{directory.name}.md").is_file():
            return []
        return ["no SKILL.md: OpenCode skips this skill"]
    fields = frontmatter(entry.read_text(encoding="utf-8", errors="replace"))
    warnings = []
    if not fields.get("description"):
        warnings.append("SKILL.md has no description: agents are never told when to use it")
    if fields.get("name") and fields["name"] != directory.name:
        warnings.append(f"SKILL.md names itself {fields['name']!r} but the folder is {directory.name!r}")
    return warnings


def shell_syntax_error(text: str) -> str | None:
    """`bash -n` on the installer; None when it parses (or bash is unavailable)."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        result = subprocess.run(
            [bash, "-n"], input=text, capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return "bash -n timed out"
    if result.returncode == 0:
        return None
    return result.stderr.replace(f"{bash}: ", "").strip() or "bash -n reported a syntax error"


def powershell_syntax_error(text: str) -> str | None:
    """PowerShell's own parser on install.ps1; None when it parses, or when
    PowerShell (pwsh) is not installed on this server."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        script = handle.name
    try:
        # With -Command, trailing arguments are appended to the command text
        # rather than passed as $args, so the path goes inside it (mkstemp
        # paths never contain a quote).
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command",
             f"$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);"
             "$e | ForEach-Object { 'line {0}: {1}' -f $_.Extent.StartLineNumber, $_.Message }"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "the PowerShell syntax check timed out"
    finally:
        Path(script).unlink(missing_ok=True)
    return result.stdout.strip() or None


def install(
    app: FastAPI,
    *,
    config: Settings,
    users: UserStore,
    values: variables.VariableStore,
    own_names: variables.EmployeeNames,
    own_skills: variables.EmployeeNames,
    limiter: RateLimiter,
    client_key: Callable[[Request], str],
    report: Callable[[], dict[str, Any]],
) -> None:
    """Mount the panel on `app`. Call only when an admin token is configured."""
    sessions = AdminSessions(config.session_secret, config.admin_token, config.admin_session_hours * 3600)
    files = store.Store(config.config_dir)
    secure_cookie = config.base_url.startswith("https://")

    @app.middleware("http")
    async def admin_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            for name, value in SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    def audit(request: Request, action: str) -> None:
        logger.info("admin %s from %s", action, client_key(request))

    def require_admin(request: Request) -> None:
        bucket = "admin:" + client_key(request)
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            try:
                limiter.check(bucket)
            except AuthError as error:
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)) from error
            if sessions.matches(authorization[7:].strip()):
                return
            limiter.record_failure(bucket)
            logger.warning("admin token rejected from %s", bucket)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")
        cookie = request.cookies.get(COOKIE)
        if cookie and sessions.valid(cookie):
            if request.headers.get("x-ocs-admin") != "1":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing X-OCS-Admin header")
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in to the admin panel")

    router = APIRouter(prefix="/admin", include_in_schema=False)
    api = APIRouter(prefix="/admin/api", include_in_schema=False, dependencies=[Depends(require_admin)])

    # -- the page -------------------------------------------------------------

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def panel() -> HTMLResponse:
        page = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(content=page, headers={"Content-Security-Policy": PANEL_CSP})

    @router.get("/static/{name}")
    async def panel_asset(name: str) -> FileResponse:
        if name not in PANEL_ASSETS:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return FileResponse(PANEL_DIR / name, media_type=PANEL_ASSETS[name])

    # -- session --------------------------------------------------------------

    @router.post("/api/session")
    async def sign_in(request: Request, body: SessionRequest) -> JSONResponse:
        bucket = "admin:" + client_key(request)
        try:
            limiter.check(bucket)
        except AuthError as error:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(error)) from error
        if not sessions.matches(body.token.strip()):
            limiter.record_failure(bucket)
            logger.warning("admin sign-in rejected from %s", bucket)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")
        limiter.reset(bucket)
        cookie, expires = sessions.issue()
        response = JSONResponse(content={"expires_at": expires})
        response.set_cookie(
            COOKIE, cookie, max_age=sessions.ttl_seconds, path="/admin",
            httponly=True, samesite="strict", secure=secure_cookie,
        )
        audit(request, "signed in")
        return response

    @router.delete("/api/session")
    async def sign_out() -> JSONResponse:
        response = JSONResponse(content={"status": "signed out"})
        response.delete_cookie(COOKIE, path="/admin", httponly=True, samesite="strict", secure=secure_cookie)
        return response

    # -- overview -------------------------------------------------------------

    @api.get("/overview")
    async def overview() -> dict[str, Any]:
        return {
            "base_url": config.base_url,
            "login_command": f"opencode auth login {config.base_url}",
            "windows_login_command": f"opencode auth login {config.base_url}{manifest.WINDOWS_PATH}",
            "health": report(),
            "features": {
                "skills": config.skills_enabled,
                "bootstrap": config.bootstrap_enabled,
                "bifrost": bool(config.bifrost_url),
                "token_ttl_days": config.token_ttl_days,
                "vk_token_ttl_days": config.vk_token_ttl_days,
            },
            "files": {
                "config": {"path": str(config.opencode_config_file), "writable": store.writable(config.opencode_config_file)},
                "users": {"path": str(config.users_file), "writable": store.writable(config.users_file)},
                "skills": {"path": str(config.skills_dir), "writable": store.writable(config.skills_dir / "x")},
                "bootstrap": {"path": str(config.bootstrap_file), "writable": store.writable(config.bootstrap_file)},
                "windows_bootstrap": {
                    "path": str(config.windows_bootstrap_file), "writable": store.writable(config.windows_bootstrap_file),
                },
                "variables": {"path": str(config.variables_file), "writable": store.writable(config.variables_file)},
            },
        }

    # -- corporate configuration --------------------------------------------

    def config_state() -> dict[str, Any]:
        path = config.opencode_config_file
        data = store.read(path)
        text = data.decode("utf-8", errors="replace") if data is not None else ""
        state: dict[str, Any] = {
            "file": path.name,
            "text": text,
            "etag": store.etag(data),
            "exists": data is not None,
            "writable": store.writable(path),
            "has_comments": has_comments(text),
            "document": None,
            "error": None,
            "errors": [],
            "warnings": [],
        }
        try:
            document = configdoc.parse(text, source=path.name) if data is not None else {}
        except configdoc.ConfigError as error:
            state["error"] = str(error)
            return state
        state["document"] = document
        state["errors"], state["warnings"] = configdoc.check(document)
        return state

    def draft_document(text: str | None, document: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        """(text to write, parsed document) for either kind of draft."""
        if text is not None:
            return text, configdoc.parse(text)
        if document is not None:
            text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
            # Same refusal as the raw editor, e.g. for {file:~/.ssh/...}.
            return text, configdoc.parse(text)
        raise configdoc.ConfigError("send either text or document")

    @api.get("/config")
    async def get_config() -> dict[str, Any]:
        return config_state()

    @api.post("/config/check")
    async def check_config(body: ConfigDraft) -> JSONResponse:
        try:
            _, document = draft_document(body.text, body.document)
        except configdoc.ConfigError as error:
            return _invalid(str(error))
        errors, warnings = configdoc.check(document)
        rendered = configdoc.render(document, config)
        filled: list[str] = []
        kept: list[str] = []
        if body.as_user:
            own = own_names.get(body.as_user)
            entries = variables.resolve_entries(values.load(), body.as_user, own=own)
            kept = sorted(variables.references(rendered) & own)
            # Secret values never travel back to the browser, not even here.
            shown = {name: f"<secret {name}>" if entry.get("secret") else entry["value"] for name, entry in entries.items()}
            filled = sorted(variables.references(rendered) & set(shown))
            rendered = variables.apply(rendered, shown)
        return JSONResponse(
            content={
                "errors": errors,
                "warnings": warnings,
                "rendered": rendered,
                "filled": filled,
                # Defined in that employee's own environment: theirs is used.
                "kept": kept,
                # Left for each employee's machine to resolve from its environment.
                "unresolved": sorted(variables.references(rendered) - {config.session_env}),
            }
        )

    @api.put("/config")
    async def put_config(request: Request, body: ConfigWrite) -> JSONResponse:
        path = config.opencode_config_file
        if not store.writable(path):
            raise _not_writable(path)
        try:
            text, document = draft_document(body.text, body.document)
        except configdoc.ConfigError as error:
            return _invalid(str(error))
        errors, warnings = configdoc.check(document)
        if errors:
            return _invalid("the configuration has errors; nothing was saved", errors, warnings)
        try:
            files.write("config", path, text.encode("utf-8"), expected=body.etag)
        except store.Conflict as error:
            raise _conflict(error) from error
        audit(request, f"saved {path.name} ({'text' if body.text is not None else 'forms'})")
        return JSONResponse(content={**config_state(), "saved_warnings": warnings})

    # -- users ----------------------------------------------------------------

    def users_document(data: bytes | None) -> dict[str, Any]:
        if data is None:
            return {"users": []}
        try:
            document = json.loads(data)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{config.users_file.name} cannot be parsed ({error}); fix it by hand first",
            ) from error
        if not isinstance(document, dict) or not isinstance(document.get("users"), list):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{config.users_file.name} must contain a 'users' array; fix it by hand first",
            )
        return document

    def find(entries: list[Any], name: str) -> dict[str, Any]:
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no user named {name!r}")

    def change_users(request: Request, action: str, change: Callable[[list[Any]], Any]) -> Any:
        if not store.writable(config.users_file):
            raise _not_writable(config.users_file)
        outcome: dict[str, Any] = {}

        def transform(data: bytes | None) -> bytes:
            document = users_document(data)
            outcome["value"] = change(document["users"])
            for entry in document["users"]:
                try:
                    User.parse(entry)
                except AuthError as error:
                    raise HTTPException(status_code=UNPROCESSABLE, detail=str(error)) from error
            return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        files.modify("users", config.users_file, transform, mode=0o600)
        users.reload(force=True)
        audit(request, action)
        return outcome.get("value")

    def custom_key(raw: str) -> str:
        key = raw.strip()
        if not MIN_CUSTOM_KEY_LENGTH <= len(key) <= MAX_CUSTOM_KEY_LENGTH:
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail=f"API keys need {MIN_CUSTOM_KEY_LENGTH} to {MAX_CUSTOM_KEY_LENGTH} characters",
            )
        if any(char.isspace() or not char.isprintable() for char in key):
            raise HTTPException(status_code=UNPROCESSABLE, detail="API keys cannot contain spaces or control characters")
        return key

    def ensure_unassigned(entries: list[Any], stored: str) -> None:
        """One key, one account: sign-in looks the key up, it must not be ambiguous."""
        for entry in entries:
            keys = entry.get("api_keys", []) if isinstance(entry, dict) else []
            if stored in ([keys] if isinstance(keys, str) else keys):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"that key is already registered to {entry.get('name')!r}",
                )

    def check_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail=f"passwords need at least {MIN_PASSWORD_LENGTH} characters",
            )

    @api.get("/users")
    async def list_users() -> dict[str, Any]:
        data = store.read(config.users_file)
        state: dict[str, Any] = {
            "file": config.users_file.name,
            "writable": store.writable(config.users_file),
            "error": users.error,
            "bifrost": bool(config.bifrost_url),
            "users": [],
        }
        try:
            document = users_document(data)
        except HTTPException as error:
            state["error"] = error.detail
            return state
        state["users"] = [user_summary(entry) for entry in document["users"] if isinstance(entry, dict)]
        return state

    @api.post("/users")
    async def create_user(request: Request, body: NewUser) -> dict[str, Any]:
        name = body.name.strip()
        if not _USER_NAME.match(name):
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail="user names use letters, digits and . _ @ + - (up to 128, no spaces or ':')",
            )
        entry: dict[str, Any] = {"name": name, "version": 1}
        if body.password:
            check_password(body.password)
            entry["password"] = await run_in_threadpool(hash_password, body.password)
        # A pasted key is never echoed back: the admin already has it.
        generated = generate_api_key() if body.api_key and not body.api_key_value else None
        key = custom_key(body.api_key_value) if body.api_key_value else generated
        if key:
            entry["api_keys"] = [hash_api_key(key)]

        def add(entries: list[Any]) -> None:
            if any(isinstance(item, dict) and item.get("name") == name for item in entries):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{name!r} already exists")
            if key:
                ensure_unassigned(entries, entry["api_keys"][0])
            entries.append(entry)

        change_users(request, f"created user {name}", add)
        return {"user": user_summary(entry), "api_key": generated}

    @api.patch("/users/{name}")
    async def update_user(request: Request, name: str, body: UserChange) -> dict[str, Any]:
        password_hash = None
        if body.password is not None:
            check_password(body.password)
            password_hash = await run_in_threadpool(hash_password, body.password)

        def apply(entries: list[Any]) -> dict[str, Any]:
            entry = find(entries, name)
            if password_hash is not None:
                entry["password"] = password_hash
            if body.disabled is True:
                entry["disabled"] = True
            elif body.disabled is False:
                entry.pop("disabled", None)
            return user_summary(entry)

        changes = [label for label, changed in (("password", password_hash), ("disabled", body.disabled)) if changed is not None]
        return {"user": change_users(request, f"changed {', '.join(changes) or 'nothing'} for {name}", apply)}

    @api.delete("/users/{name}")
    async def delete_user(request: Request, name: str) -> dict[str, str]:
        def remove(entries: list[Any]) -> None:
            entries.remove(find(entries, name))

        change_users(request, f"deleted user {name}", remove)
        # Their variables (provider keys, typically) go with the account.
        own_names.forget(name)
        own_skills.forget(name)
        document = values.reload()
        if name in document.get("users", {}) and store.writable(config.variables_file):
            change_variables(request, f"dropped the variables of deleted user {name}", lambda doc: doc["users"].pop(name, None))
        return {"status": "deleted"}

    @api.post("/users/{name}/api-keys")
    async def add_api_key(request: Request, name: str, body: NewApiKey | None = None) -> dict[str, Any]:
        pasted = body is not None and body.key is not None
        key = custom_key(body.key) if pasted else generate_api_key()
        stored = hash_api_key(key)

        def add(entries: list[Any]) -> dict[str, Any]:
            entry = find(entries, name)
            ensure_unassigned(entries, stored)
            keys = entry.get("api_keys", [])
            entry["api_keys"] = ([keys] if isinstance(keys, str) else list(keys)) + [stored]
            return user_summary(entry)

        action = f"registered an existing API key for {name}" if pasted else f"added an API key for {name}"
        user = change_users(request, action, add)
        return {"user": user, "api_key": None if pasted else key, "fingerprint": key_fingerprint(stored)}

    @api.delete("/users/{name}/api-keys/{fingerprint}")
    async def remove_api_key(request: Request, name: str, fingerprint: str) -> dict[str, Any]:
        def remove(entries: list[Any]) -> dict[str, Any]:
            entry = find(entries, name)
            keys = entry.get("api_keys", [])
            keys = [keys] if isinstance(keys, str) else list(keys)
            kept = [key for key in keys if not (isinstance(key, str) and key_fingerprint(key) == fingerprint)]
            if len(kept) == len(keys):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such API key")
            if kept:
                entry["api_keys"] = kept
            else:
                entry.pop("api_keys", None)
            return user_summary(entry)

        return {"user": change_users(request, f"removed API key {fingerprint} from {name}", remove)}

    @api.post("/users/{name}/revoke")
    async def revoke_sessions(request: Request, name: str) -> dict[str, Any]:
        def bump(entries: list[Any]) -> dict[str, Any]:
            entry = find(entries, name)
            version = entry.get("version", 1)
            entry["version"] = (version if isinstance(version, int) and not isinstance(version, bool) else 1) + 1
            return user_summary(entry)

        return {"user": change_users(request, f"revoked every session of {name}", bump)}

    # -- variables ------------------------------------------------------------

    def variable_rows(scope: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "secret": bool(entry.get("secret")),
                "machine": bool(entry.get("machine")),
                "value": None if entry.get("secret") else entry["value"],
            }
            for name, entry in sorted(scope.items())
        ]

    def change_variables(request: Request, action: str, change: Callable[[dict[str, Any]], Any]) -> Any:
        if not store.writable(config.variables_file):
            raise _not_writable(config.variables_file)
        outcome: dict[str, Any] = {}

        def transform(data: bytes | None) -> bytes:
            try:
                document = variables.parse(data)
            except variables.VariableError as error:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{error}; fix it by hand first") from error
            outcome["value"] = change(document)
            return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        files.modify("variables", config.variables_file, transform, mode=0o600)
        values.reload()
        audit(request, action)
        return outcome.get("value")

    def known_users() -> list[str]:
        try:
            entries = users_document(store.read(config.users_file))["users"]
        except HTTPException:
            return []
        return sorted(entry["name"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("name"), str))

    def checked(name: str, value: str | None) -> None:
        try:
            variables.check_name(name, reserved=config.session_env)
            if value is not None:
                variables.check_value(value)
        except variables.VariableError as error:
            raise HTTPException(status_code=UNPROCESSABLE, detail=str(error)) from error

    def put(scope: dict[str, Any], name: str, body: VariableWrite) -> dict[str, Any]:
        existing = scope.get(name)
        if body.value is None and existing is None:
            raise HTTPException(status_code=UNPROCESSABLE, detail="enter a value")
        scope[name] = {
            "value": existing["value"] if body.value is None else body.value,
            "secret": body.secret,
            "machine": body.machine,
        }
        return {"name": name, "secret": body.secret, "machine": body.machine}

    @api.get("/variables")
    async def list_variables() -> dict[str, Any]:
        state: dict[str, Any] = {
            "file": config.variables_file.name,
            "writable": store.writable(config.variables_file),
            "reserved": config.session_env,
            "error": None,
            "global": [],
            "users": {},
            "known_users": known_users(),
            # Names each employee defines themselves (reported at their login).
            "own": own_names.all(),
        }
        try:
            document = variables.parse(store.read(config.variables_file))
        except variables.VariableError as error:
            state["error"] = str(error)
            return state
        state["global"] = variable_rows(document["global"])
        state["users"] = {name: variable_rows(scope) for name, scope in sorted(document["users"].items())}
        return state

    @api.put("/variables/global/{name}")
    async def put_global_variable(request: Request, name: str, body: VariableWrite) -> dict[str, Any]:
        checked(name, body.value)
        # The value never reaches the log, only its name.
        return change_variables(request, f"set global variable {name}", lambda doc: put(doc["global"], name, body))

    @api.put("/variables/users/{user}/{name}")
    async def put_user_variable(request: Request, user: str, name: str, body: VariableWrite) -> dict[str, Any]:
        checked(name, body.value)
        if user not in known_users():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no user named {user!r}")
        return change_variables(
            request, f"set variable {name} for {user}", lambda doc: put(doc["users"].setdefault(user, {}), name, body)
        )

    def drop(scope: dict[str, Any] | None, name: str) -> None:
        if not scope or name not in scope:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no variable named {name!r}")
        del scope[name]

    @api.delete("/variables/global/{name}")
    async def delete_global_variable(request: Request, name: str) -> dict[str, str]:
        change_variables(request, f"deleted global variable {name}", lambda doc: drop(doc["global"], name))
        return {"status": "deleted"}

    @api.delete("/variables/users/{user}/{name}")
    async def delete_user_variable(request: Request, user: str, name: str) -> dict[str, str]:
        def remove(document: dict[str, Any]) -> None:
            drop(document["users"].get(user), name)
            if not document["users"][user]:
                del document["users"][user]

        change_variables(request, f"deleted variable {name} for {user}", remove)
        return {"status": "deleted"}

    # -- skills ---------------------------------------------------------------

    def skill_dir(name: str) -> Path:
        if not _SKILL_NAME.match(name):
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail="skill names use letters, digits and . _ - (up to 64), starting with a letter or digit",
            )
        return config.skills_dir / name

    def skill_file(name: str, relative: str) -> Path:
        base = skill_dir(name)
        parts = PurePosixPath(relative).parts
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or "\0" in relative
            or len(parts) > 5
            or any(part in {".", ".."} or part.startswith(".") for part in parts)
        ):
            raise HTTPException(status_code=UNPROCESSABLE, detail="invalid file path")
        if PurePosixPath(relative).suffix.lower() not in skills.PUBLISHED_SUFFIXES:
            allowed = " ".join(sorted(skills.PUBLISHED_SUFFIXES))
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail=f"only these file types are published to employees: {allowed}",
            )
        target = base.joinpath(*parts)
        root = base.resolve()
        if root not in target.resolve().parents:
            raise HTTPException(status_code=UNPROCESSABLE, detail="invalid file path")
        return target

    def check_skills_writable() -> None:
        if not store.writable(config.skills_dir / "x"):
            raise _not_writable(config.skills_dir)

    @api.get("/skills")
    async def list_skills() -> dict[str, Any]:
        root = config.skills_dir
        published = {skill.name: skill for skill in skills.discover(root)}
        # Who keeps their own version of each skill (reported at their login).
        kept_by: dict[str, list[str]] = {}
        for subject, names in own_skills.all().items():
            for skill_name in names:
                kept_by.setdefault(skill_name, []).append(subject)
        entries = []
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                items = []
                for item in sorted(directory.rglob("*")):
                    relative = item.relative_to(directory)
                    if not item.is_file() or any(part.startswith(".") for part in relative.parts):
                        continue
                    items.append(
                        {
                            "path": relative.as_posix(),
                            "size": item.stat().st_size,
                            "published": skills._is_publishable(item),
                        }
                    )
                skill = published.get(directory.name)
                entries.append(
                    {
                        "name": directory.name,
                        "published": skill is not None,
                        "version": skill.version if skill else None,
                        "files": items,
                        "warnings": skill_warnings(directory),
                        "kept_by": sorted(kept_by.get(directory.name, [])),
                    }
                )
        return {
            "enabled": config.skills_enabled,
            "writable": store.writable(root / "x"),
            "suffixes": sorted(skills.PUBLISHED_SUFFIXES),
            "max_file_bytes": skills.MAX_FILE_BYTES,
            "skills": entries,
        }

    @api.post("/skills")
    async def create_skill(request: Request, body: NewSkill) -> dict[str, Any]:
        check_skills_writable()
        name = body.name.strip()
        directory = skill_dir(name)
        description = " ".join(body.description.split())
        if not description:
            raise HTTPException(
                status_code=UNPROCESSABLE,
                detail="describe when agents should use the skill: that sentence is all they see",
            )
        if directory.exists():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"skill {name!r} already exists")
        text = SKILL_TEMPLATE.format(name=name, description=description)
        try:
            files.write(f"skill:{name}/SKILL.md", directory / "SKILL.md", text.encode("utf-8"), expected="absent")
        except store.Conflict as error:
            raise _conflict(error) from error
        audit(request, f"created skill {name}")
        return {"name": name, "file": "SKILL.md"}

    @api.delete("/skills/{name}")
    async def delete_skill(request: Request, name: str) -> dict[str, str]:
        check_skills_writable()
        directory = skill_dir(name)
        if not directory.is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no skill named {name!r}")
        # Every visible file goes to the history first, so a deleted skill can
        # be brought back file by file.
        for item in sorted(directory.rglob("*")):
            relative = item.relative_to(directory)
            if item.is_file() and not any(part.startswith(".") for part in relative.parts):
                files.delete(f"skill:{name}/{relative.as_posix()}", item)
        shutil.rmtree(directory)
        audit(request, f"deleted skill {name}")
        return {"status": "deleted"}

    @api.get("/skills/{name}/files/{relative:path}")
    async def read_skill_file(name: str, relative: str) -> dict[str, Any]:
        target = skill_file(name, relative)
        data = store.read(target)
        if data is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="not a UTF-8 text file"
            ) from error
        return {"path": relative, "text": text, "etag": store.etag(data)}

    @api.put("/skills/{name}/files/{relative:path}")
    async def write_skill_file(request: Request, name: str, relative: str, body: FileWrite) -> dict[str, Any]:
        check_skills_writable()
        target = skill_file(name, relative)
        data = body.text.encode("utf-8")
        if len(data) > skills.MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"skill files are limited to {skills.MAX_FILE_BYTES // (1024 * 1024)} MiB",
            )
        if not skill_dir(name).is_dir():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no skill named {name!r}")
        try:
            new_etag = files.write(f"skill:{name}/{relative}", target, data, expected=body.etag)
        except store.Conflict as error:
            raise _conflict(error) from error
        audit(request, f"saved skill file {name}/{relative}")
        return {"path": relative, "etag": new_etag, "warnings": skill_warnings(skill_dir(name))}

    @api.delete("/skills/{name}/files/{relative:path}")
    async def delete_skill_file(request: Request, name: str, relative: str) -> dict[str, str]:
        check_skills_writable()
        target = skill_file(name, relative)
        try:
            files.delete(f"skill:{name}/{relative}", target)
        except FileNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such file") from error
        audit(request, f"deleted skill file {name}/{relative}")
        return {"status": "deleted"}

    # -- bootstrap installer --------------------------------------------------

    def installer(platform: str) -> tuple[str, Path]:
        """(history key, file) of the installer for one platform."""
        if platform == "windows":
            return "bootstrap-windows", config.windows_bootstrap_file
        if platform in ("", "unix"):
            return "bootstrap", config.bootstrap_file
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown platform {platform!r}")

    def bootstrap_state(platform: str) -> dict[str, Any]:
        _, path = installer(platform)
        data = store.read(path)
        return {
            "platform": platform or "unix",
            "file": f"bootstrap/{path.name}",
            "text": data.decode("utf-8", errors="replace") if data is not None else "",
            "etag": store.etag(data),
            "exists": data is not None,
            "writable": store.writable(path),
            "enabled": config.bootstrap_enabled,
            # install.sh is always checked with bash -n; install.ps1 only when
            # PowerShell (pwsh) is installed on this server.
            "checked": platform != "windows" or shutil.which("pwsh") is not None,
        }

    @api.get("/bootstrap")
    async def get_bootstrap(platform: str = "") -> dict[str, Any]:
        return bootstrap_state(platform)

    @api.put("/bootstrap")
    async def put_bootstrap(request: Request, body: FileWrite, platform: str = "") -> Any:
        key, path = installer(platform)
        if not store.writable(path):
            raise _not_writable(path)
        check = powershell_syntax_error if key == "bootstrap-windows" else shell_syntax_error
        error = await run_in_threadpool(check, body.text)
        if error:
            return _invalid("the installer does not parse; nothing was saved", error.splitlines())
        try:
            files.write(key, path, body.text.encode("utf-8"), expected=body.etag, mode=0o755)
        except store.Conflict as conflict:
            raise _conflict(conflict) from conflict
        audit(request, f"saved bootstrap/{path.name}")
        return bootstrap_state(platform)

    # -- history --------------------------------------------------------------

    def document(doc: str) -> tuple[Path, bool]:
        """(file, whether its old versions may be shown) for a history key."""
        if doc == "config":
            return config.opencode_config_file, True
        if doc == "bootstrap":
            return config.bootstrap_file, True
        if doc == "bootstrap-windows":
            return config.windows_bootstrap_file, True
        if doc == "users":
            # Old versions hold password hashes: restorable, never displayed.
            return config.users_file, False
        if doc == "variables":
            # Same for secret values.
            return config.variables_file, False
        if doc.startswith("skill:"):
            name, _, relative = doc[len("skill:"):].partition("/")
            return skill_file(name, relative), True
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown document {doc!r}")

    @api.get("/history")
    async def history(doc: str) -> dict[str, Any]:
        _, viewable = document(doc)
        return {
            "doc": doc,
            "viewable": viewable,
            "versions": [{"id": item.id, "time": item.time, "size": item.size} for item in files.history(doc)],
        }

    @api.get("/history/{snapshot_id}")
    async def history_version(doc: str, snapshot_id: str) -> dict[str, Any]:
        _, viewable = document(doc)
        if not viewable:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="old versions of users.json are not shown")
        try:
            data = files.snapshot(doc, snapshot_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such version") from error
        return {"id": snapshot_id, "text": data.decode("utf-8", errors="replace")}

    @api.post("/history/{snapshot_id}/restore")
    async def restore(request: Request, doc: str, snapshot_id: str, body: Restore) -> Any:
        path, _ = document(doc)
        if not store.writable(path):
            raise _not_writable(path)
        try:
            data = files.snapshot(doc, snapshot_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such version") from error
        text = data.decode("utf-8", errors="replace")
        # A restored version passes the same checks as a fresh save.
        if doc == "config":
            try:
                errors, _ = configdoc.check(configdoc.parse(text))
            except configdoc.ConfigError as error:
                errors = [str(error)]
            if errors:
                return _invalid("that version has errors and was not restored", errors)
        elif doc == "users":
            try:
                for entry in users_document(data)["users"]:
                    User.parse(entry)
            except (AuthError, HTTPException) as error:
                return _invalid("that version is not a valid users.json", [str(getattr(error, "detail", error))])
        elif doc == "variables":
            try:
                variables.parse(data)
            except variables.VariableError as error:
                return _invalid("that version is not a valid variables.json", [str(error)])
        elif doc in ("bootstrap", "bootstrap-windows"):
            check = powershell_syntax_error if doc == "bootstrap-windows" else shell_syntax_error
            error = await run_in_threadpool(check, text)
            if error:
                return _invalid("that version does not parse and was not restored", error.splitlines())
        mode = 0o600 if doc in ("users", "variables") else 0o755 if doc.startswith("bootstrap") else 0o644
        try:
            files.write(doc, path, data, expected=body.etag, mode=mode)
        except store.Conflict as error:
            raise _conflict(error) from error
        if doc == "users":
            users.reload(force=True)
        if doc == "variables":
            values.reload()
        audit(request, f"restored {doc} to {snapshot_id}")
        return {"status": "restored", "etag": store.etag(data)}

    app.include_router(router)
    app.include_router(api)
