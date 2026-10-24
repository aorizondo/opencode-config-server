"""Static credentials and signed session tokens.

Three ways in, all static by design (no IdP, no OAuth round trip):

  * username + password   -> PBKDF2-HMAC-SHA256 hashes in `users.json`
  * API key               -> SHA-256 digests in `users.json`, for CI and headless
  * Bifrost virtual key   -> `sk-bf-*`, checked live against Bifrost (optional)

Both exchange into the same artefact: an opaque, HMAC-signed session token the
harness stores as a credential and replays on every later request. The harness
has no refresh mechanism for command-issued credentials, so these are long
lived and revoked server side by bumping a user's `version`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PBKDF2_ITERATIONS = 600_000
PBKDF2_PREFIX = "pbkdf2_sha256"
API_KEY_PREFIX = "sha256"
TOKEN_VERSION = "v1"
#: Bifrost virtual keys carry this prefix; anything else is a local API key.
VIRTUAL_KEY_PREFIX = "sk-bf-"
#: Subjects of sessions opened with a Bifrost virtual key. Reserved: no entry in
#: users.json may use it, so a local account can never impersonate one.
VIRTUAL_KEY_SUBJECT = "vk:"

# Verified against this when the user does not exist, so a wrong username and a
# wrong password cost the same.
_DUMMY_HASH = (
    f"{PBKDF2_PREFIX}${PBKDF2_ITERATIONS}$"
    "AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


class AuthError(Exception):
    """Credentials were rejected. The message is safe to show a user."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(plain: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return f"{PBKDF2_PREFIX}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(stored: str, plain: str) -> bool:
    try:
        algorithm, iterations, salt, digest = stored.split("$")
        if algorithm != PBKDF2_PREFIX:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), _b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64encode(candidate), digest)


def hash_api_key(plain: str) -> str:
    return f"{API_KEY_PREFIX}:{hashlib.sha256(plain.encode('utf-8')).hexdigest()}"


def generate_api_key() -> str:
    return f"ock_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True)
class User:
    name: str
    password_hash: str | None = None
    api_key_hashes: tuple[str, ...] = ()
    #: Bumped to invalidate every token already issued to this user.
    version: int = 1
    disabled: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "User":
        if not isinstance(raw, dict):
            raise AuthError("each entry in users must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AuthError("each user needs a non-empty name")
        if name.startswith(VIRTUAL_KEY_SUBJECT):
            raise AuthError(f"user names may not start with {VIRTUAL_KEY_SUBJECT!r}: {name!r}")
        keys = raw.get("api_keys", [])
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list) or any(not isinstance(item, str) for item in keys):
            raise AuthError(f"api_keys for {name!r} must be a list of strings")
        password = raw.get("password")
        if password is not None and not isinstance(password, str):
            raise AuthError(f"password for {name!r} must be a string hash")
        if password is not None and not password.startswith(f"{PBKDF2_PREFIX}$"):
            raise AuthError(
                f"password for {name!r} must be a PBKDF2 hash, not a plaintext value; "
                "generate one with: python -m opencode_config_server.passwd"
            )
        version = raw.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise AuthError(f"version for {name!r} must be a positive integer")
        return cls(
            name=name,
            password_hash=password,
            api_key_hashes=tuple(keys),
            version=version,
            disabled=bool(raw.get("disabled", False)),
        )


class UserStore:
    """Reads `users.json` from disk, picking up edits without a restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._users: dict[str, User] = {}
        self._mtime: float | None = None
        self._error: str | None = None
        self._missing = False

    def reload(self, *, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
            self._missing = False
        except OSError:
            self._users = {}
            self._mtime = None
            self._missing = True
            self._error = (
                f"user database not found: {self.path} -- add the first user in the admin panel (/admin), "
                "which creates it, or start from config/users.json.example"
            )
            return
        if not force and self._mtime == mtime:
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            # Keep serving the previous snapshot: a typo in the file must not
            # lock every employee out mid-day.
            self._error = f"failed to read {self.path}: {error}"
            self._mtime = mtime
            return
        entries = document.get("users") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            self._error = f"{self.path} must contain a 'users' array"
            self._mtime = mtime
            return
        users: dict[str, User] = {}
        try:
            for entry in entries:
                user = User.parse(entry)
                users[user.name] = user
        except AuthError as error:
            # A malformed entry must never authenticate and must never be
            # reported as a bad password: keep serving the previous snapshot.
            self._error = f"invalid entry in {self.path}: {error}"
            self._mtime = mtime
            return
        self._users = users
        self._mtime = mtime
        self._error = None

    @property
    def error(self) -> str | None:
        self.reload()
        return self._error

    @property
    def missing(self) -> bool:
        """No users.json at all: a fresh server, or one that only takes Bifrost
        virtual keys. Unlike a broken file, nothing is unknown -- there are
        simply no local accounts."""
        self.reload()
        return self._missing

    def count(self) -> int:
        self.reload()
        return len(self._users)

    def get(self, name: str) -> User | None:
        self.reload()
        return self._users.get(name)

    def authenticate_password(self, name: str, password: str) -> User:
        user = self.get(name)
        stored = user.password_hash if user and user.password_hash else _DUMMY_HASH
        matched = verify_password(stored, password)
        if user is None or not user.password_hash or not matched:
            raise AuthError("invalid username or password")
        if user.disabled:
            raise AuthError(f"account {name!r} is disabled")
        return user

    def find_api_key(self, key: str) -> User | None:
        """The account `key` is registered to, disabled or not; None if nobody's."""
        self.reload()
        candidate = hash_api_key(key)
        for user in self._users.values():
            for stored in user.api_key_hashes:
                if hmac.compare_digest(stored, candidate):
                    return user
        return None

    def authenticate_api_key(self, key: str) -> User:
        user = self.find_api_key(key)
        if user is None:
            raise AuthError("invalid API key")
        if user.disabled:
            raise AuthError(f"account {user.name!r} is disabled")
        return user


class BifrostUnavailable(Exception):
    """Bifrost could not answer. Not a credential failure: never count it as one."""


class BifrostKeys:
    """Checks Bifrost virtual keys with Bifrost's self-service quota endpoint.

    `GET /api/governance/virtual-keys/quota` authenticates with the key itself, so
    no Bifrost admin credential lives here (transports/bifrost-http/handlers/
    governance.go). Its answers:

        401  missing or unknown key
        403  expired key
        200  the key exists -- *including inactive keys*, which Bifrost serves on
             purpose with `is_active: false`. So 200 alone does not mean "valid".
    """

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self._url = f"{base_url.rstrip('/')}/api/governance/virtual-keys/quota"
        self._timeout = timeout

    def authenticate(self, key: str) -> User:
        request = urllib.request.Request(
            self._url,
            # x-bf-vk takes any key value; the other headers insist on the sk-bf- prefix.
            headers={"x-bf-vk": key, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise AuthError("invalid virtual key") from error
            if error.code == 403:
                raise AuthError("this virtual key has expired") from error
            raise BifrostUnavailable(f"Bifrost answered HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise BifrostUnavailable(f"cannot reach Bifrost: {error}") from error
        if not isinstance(body, dict) or not isinstance(body.get("virtual_key_name"), str):
            raise BifrostUnavailable("unexpected answer from Bifrost's quota endpoint")
        if body.get("is_active") is not True:
            raise AuthError("this virtual key is inactive")
        return User(name=f"{VIRTUAL_KEY_SUBJECT}{body['virtual_key_name']}")


@dataclass(frozen=True)
class Claims:
    subject: str
    version: int
    issued_at: int
    expires_at: int


class SessionTokens:
    """Issues and verifies `v1.<payload>.<signature>` tokens.

    URL-safe on purpose: skill downloads carry no headers, so the token has to
    survive inside a path segment.
    """

    def __init__(self, secret: str, *, ttl_days: int = 90) -> None:
        if not secret:
            raise ValueError("session secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl_days * 86_400

    def _sign(self, payload: str) -> str:
        return _b64encode(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, user: User, *, now: int | None = None, ttl_seconds: int | None = None) -> tuple[str, Claims]:
        issued = int(time.time() if now is None else now)
        lifetime = self._ttl if ttl_seconds is None else ttl_seconds
        claims = Claims(subject=user.name, version=user.version, issued_at=issued, expires_at=issued + lifetime)
        body = {"sub": claims.subject, "ver": claims.version, "iat": claims.issued_at, "exp": claims.expires_at}
        payload = _b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{TOKEN_VERSION}.{payload}.{self._sign(payload)}", claims

    def verify(self, token: str, *, now: int | None = None) -> Claims:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_VERSION:
            raise AuthError("malformed session token")
        _, payload, signature = parts
        if not hmac.compare_digest(self._sign(payload), signature):
            raise AuthError("session token signature is not valid")
        try:
            body = json.loads(_b64decode(payload))
            claims = Claims(
                subject=str(body["sub"]),
                version=int(body["ver"]),
                issued_at=int(body["iat"]),
                expires_at=int(body["exp"]),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise AuthError("malformed session token") from error
        if claims.expires_at <= int(time.time() if now is None else now):
            raise AuthError("session token has expired; run `opencode auth login` again")
        return claims


class RateLimiter:
    """Fixed-window counter per key. In-memory: one process, one window.

    Deliberately not shared across replicas. It blunts credential stuffing
    against a single instance; a real lockout policy belongs at the edge.
    """

    def __init__(self, *, attempts: int, window_seconds: int) -> None:
        self._attempts = attempts
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        recent = [hit for hit in self._hits.get(key, []) if current - hit < self._window]
        self._hits[key] = recent
        if len(recent) >= self._attempts:
            raise AuthError("too many failed attempts; wait a few minutes and retry")

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self._hits.setdefault(key, []).append(current)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
