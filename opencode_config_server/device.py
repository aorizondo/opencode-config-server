"""Browser login for the harness's `auth.command`, shaped after RFC 8628.

The harness runs the login command with stdin on /dev/null and the *server's*
environment (packages/core/src/integration.ts), so a script cannot prompt for a
password and does not see variables exported in the employee's shell. Instead:

    script                      server                         browser
    POST /auth/device     ->    device_code + user_code
    prints the URL + code                                ->    GET /login?code=…
                                                         <-    POST /login/approve
    POST /auth/device/token ->  session token (once)           (API key or user/pass)

The device code is the script's secret and is never shown; the user code is what
the employee checks in the browser. Both are single use and short lived: the
harness kills the command after 10 minutes, so grants never outlive that.

In-memory, like the rate limiter: one process, one table. A restart only costs
the employee a retry.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

#: No 0/O, 1/I/L, 5/S, 2/Z: the code is read off a terminal and typed by hand.
USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRTUVWXY346789"
USER_CODE_LENGTH = 8


class DeviceError(Exception):
    """A device grant cannot proceed. `code` follows RFC 8628 section 3.5."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DeviceGrant:
    device_code: str
    user_code: str
    expires_at: float
    interval: int
    #: Name of the account that approved it, once approved.
    subject: str | None = None
    last_poll: float = 0.0


def normalize_user_code(raw: str) -> str:
    """`abcd-efgh`, `ABCD EFGH` and `abcdefgh` all mean the same code."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_user_code(code: str) -> str:
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


class DeviceFlow:
    def __init__(self, *, ttl_seconds: int = 300, interval: int = 2, max_pending: int = 1000) -> None:
        self._ttl = ttl_seconds
        self._interval = interval
        self._max_pending = max_pending
        self._by_device: dict[str, DeviceGrant] = {}
        self._by_user: dict[str, str] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def _prune(self, now: float) -> None:
        for device_code, grant in list(self._by_device.items()):
            if grant.expires_at <= now:
                self._forget(device_code)

    def _forget(self, device_code: str) -> None:
        grant = self._by_device.pop(device_code, None)
        if grant is not None:
            self._by_user.pop(grant.user_code, None)

    def start(self, *, now: float | None = None) -> DeviceGrant:
        current = time.time() if now is None else now
        self._prune(current)
        if len(self._by_device) >= self._max_pending:
            raise DeviceError("slow_down", "too many logins in progress; retry in a minute")
        user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
        while user_code in self._by_user:
            user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
        grant = DeviceGrant(
            device_code=secrets.token_urlsafe(32),
            user_code=user_code,
            expires_at=current + self._ttl,
            interval=self._interval,
        )
        self._by_device[grant.device_code] = grant
        self._by_user[user_code] = grant.device_code
        return grant

    def pending(self, user_code: str, *, now: float | None = None) -> DeviceGrant:
        """The grant a browser is about to approve; raises if it is not approvable."""
        current = time.time() if now is None else now
        self._prune(current)
        device_code = self._by_user.get(normalize_user_code(user_code))
        grant = self._by_device.get(device_code) if device_code else None
        if grant is None:
            raise DeviceError("expired_token", "this code is unknown or has expired; run the login again")
        if grant.subject is not None:
            raise DeviceError("invalid_grant", "this code has already been used")
        return grant

    def approve(self, user_code: str, subject: str, *, now: float | None = None) -> None:
        self.pending(user_code, now=now).subject = subject

    def poll(self, device_code: str, *, now: float | None = None) -> str:
        """The approving account's name, exactly once; otherwise a DeviceError."""
        current = time.time() if now is None else now
        grant = self._by_device.get(device_code)
        if grant is None or grant.expires_at <= current:
            self._forget(device_code)
            raise DeviceError("expired_token", "the login code expired before it was approved")
        if grant.subject is None:
            # RFC 8628: polling faster than the advertised interval earns slow_down.
            too_fast = current - grant.last_poll < grant.interval * 0.5
            grant.last_poll = current
            if too_fast:
                raise DeviceError("slow_down", "polling too fast")
            raise DeviceError("authorization_pending", "waiting for the browser login")
        subject = grant.subject
        self._forget(device_code)
        return subject
