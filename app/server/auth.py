"""Pairing code and session-token authentication."""

from __future__ import annotations

import hmac
import secrets
import threading
import time


def generate_pairing_code() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(6))


def format_code(code: str) -> str:
    digits = "".join(c for c in code if c.isdigit())
    return "-".join(digits[i : i + 2] for i in range(0, len(digits), 2))


class Authenticator:
    """Validates pairing codes and issues per-device session tokens.

    A token lets a device that paired once reconnect after a network drop or a
    page reload without re-entering the code. Tokens are invalidated when the
    pairing code is regenerated.
    """

    MAX_FAILURES = 10
    LOCKOUT_SECONDS = 60.0

    def __init__(self, code: str, enabled: bool = True):
        self._lock = threading.Lock()
        self._code = code or generate_pairing_code()
        self.enabled = enabled
        self._tokens: dict[str, str] = {}  # token -> client_id
        self._failures: dict[str, list[float]] = {}

    @property
    def code(self) -> str:
        return self._code

    def regenerate(self) -> str:
        with self._lock:
            self._code = generate_pairing_code()
            self._tokens.clear()
            return self._code

    def is_locked_out(self, remote: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._failures.get(remote, []) if now - t < self.LOCKOUT_SECONDS]
            self._failures[remote] = hits
            return len(hits) >= self.MAX_FAILURES

    def check(self, remote: str, client_id: str, code: str, token: str) -> tuple[bool, str]:
        """Returns (ok, reason). Reason is one of ok, auth_disabled, token, rate_limited, bad_code."""
        if not self.enabled:
            return True, "auth_disabled"
        if self.is_locked_out(remote):
            return False, "rate_limited"
        with self._lock:
            if token:
                owner = self._tokens.get(token)
                if owner is not None and hmac.compare_digest(owner, client_id):
                    return True, "token"
            if code and hmac.compare_digest(code.encode(), self._code.encode()):
                return True, "ok"
            self._failures.setdefault(remote, []).append(time.monotonic())
            return False, "bad_code"

    def issue_token(self, client_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            # one token per client id
            for t, cid in list(self._tokens.items()):
                if cid == client_id:
                    del self._tokens[t]
            self._tokens[token] = client_id
        return token
