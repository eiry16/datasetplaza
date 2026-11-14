"""Control-plane authentication for the local admin console.

Zero dependencies. The console is loopback-only, but "reachable from a browser
on this machine" is still a real attack surface: any local page could POST to
it. So mutating endpoints require a session and a double-submit CSRF token.

Password handling
-----------------
* First start generates a random password. It is printed once to the console
  and stored in runtime/admin_password.txt so the operator can recover it.
* runtime/admin_auth.json stores ONLY the salt and a PBKDF2 hash, never the
  password itself. It is a local file; protect it like any other credential.
* DATASETPLAZZ_ADMIN_PASSWORD (or --password) overrides it; in that case no
  plaintext recovery file is written.
* --no-auth disables all of this for offline scripted use only.

Bearer tokens are accepted for non-browser clients (curl / agent) and are not
subject to CSRF checks, because a cross-site page cannot set that header.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path

PASSWORD_ENV = "DATASETPLAZZ_ADMIN_PASSWORD"
SESSION_TTL = 8 * 3600
PBKDF2_ROUNDS = 120_000
MAX_FAILURES = 5
LOCK_SECONDS = 60
SESSION_COOKIE = "dp_session"
CSRF_COOKIE = "dp_csrf"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pbkdf2(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()


def _cookies(headers) -> dict[str, str]:
    raw = headers.get("Cookie") or ""
    try:
        jar = SimpleCookie()
        jar.load(raw)
        return {k: v.value for k, v in jar.items()}
    except Exception:  # malformed cookie header is simply unauthenticated
        return {}


def _bearer(headers) -> str | None:
    value = headers.get("Authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


class AuthManager:
    def __init__(self, runtime, enabled: bool = True, password: str | None = None):
        self.enabled = bool(enabled)
        self.runtime = Path(runtime)
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime / "admin_auth.json"
        self.recovery_path = self.runtime / "admin_password.txt"
        self.sessions: dict[str, dict] = {}
        self.failures: dict[str, tuple[int, float]] = {}
        self.lock = threading.RLock()
        self.salt = ""
        self._hash = ""
        self.source = "disabled"
        self.password_hint = ""
        if not self.enabled:
            return

        supplied = password or os.environ.get(PASSWORD_ENV)
        if supplied:
            self.source = "DATASETPLAZZ_ADMIN_PASSWORD / --password"
            self.password_hint = "由环境变量或启动参数指定，未落盘"
            # A stale recovery file would now point at the wrong password.
            self.recovery_path.unlink(missing_ok=True)
            plain = supplied
        else:
            plain = ""
            if self.recovery_path.is_file():
                plain = self.recovery_path.read_text(encoding="utf-8").strip()
            if not plain:
                plain = secrets.token_urlsafe(18)
            self.recovery_path.write_text(plain + "\n", encoding="utf-8")
            self.source = "runtime/admin_password.txt"
            self.password_hint = plain

        state = {}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        self.salt = state.get("salt") or secrets.token_hex(16)
        self._hash = _pbkdf2(plain, self.salt)
        self.state_path.write_text(
            json.dumps(
                {
                    "salt": self.salt,
                    "password_hash": self._hash,
                    "created_at": state.get("created_at") or _now(),
                    "source": self.source,
                    "note": "PBKDF2-SHA256 hash only; the password is never stored here",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # ---------------- session ----------------

    def _session(self, token: str | None) -> dict | None:
        if not token:
            return None
        rec = self.sessions.get(token)
        if not rec:
            return None
        if rec["expires"] <= time.time():
            self.sessions.pop(token, None)
            return None
        return rec

    def cookie_headers(
        self, token: str = "", csrf: str = "", secure: bool = False, revoke: bool = False
    ) -> list[str]:
        age = 0 if revoke else SESSION_TTL
        suffix = "; Secure" if secure else ""
        return [
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={age}{suffix}",
            f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Strict; "
            f"Max-Age={age}{suffix}",
        ]

    def status(self, headers, secure: bool = False) -> dict:
        token = _bearer(headers) or _cookies(headers).get(SESSION_COOKIE)
        logged_in = (not self.enabled) or self._session(token) is not None
        return {
            "ok": True,
            "enabled": self.enabled,
            "logged_in": logged_in,
            "https": bool(secure),
            "source": self.source,
            "session_ttl_seconds": SESSION_TTL if self.enabled else 0,
        }

    def login(self, password: str, client_ip: str, secure: bool = False) -> tuple[int, dict]:
        if not self.enabled:
            return 200, {"ok": True, "enabled": False, "token": "", "csrf_token": ""}
        with self.lock:
            now = time.time()
            fails, until = self.failures.get(client_ip, (0, 0.0))
            if now < until:
                return 429, {
                    "ok": False,
                    "reason": "rate_limited",
                    "detail": f"尝试过多，{int(until - now)} 秒后重试",
                    "retry_after": int(until - now),
                }
            guess = _pbkdf2(str(password or ""), self.salt)
            if not hmac.compare_digest(guess, self._hash):
                fails += 1
                lock_until = 0.0
                if fails >= MAX_FAILURES:
                    lock_until = now + LOCK_SECONDS
                    fails = 0
                self.failures[client_ip] = (fails, lock_until)
                return 401, {
                    "ok": False,
                    "reason": "invalid_password",
                    "detail": "管理员密码不正确",
                    "remaining_attempts": max(0, MAX_FAILURES - fails),
                }
            self.failures.pop(client_ip, None)
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            expires = now + SESSION_TTL
            self.sessions[token] = {"csrf": csrf, "expires": expires}
            return 200, {
                "ok": True,
                "enabled": True,
                "token": token,
                "csrf_token": csrf,
                "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "cookie_headers": self.cookie_headers(token, csrf, secure),
                "hint": "浏览器用 Cookie；脚本用 Authorization: Bearer <token>",
            }

    def logout(self, headers, secure: bool = False) -> tuple[int, dict]:
        token = _bearer(headers) or _cookies(headers).get(SESSION_COOKIE)
        with self.lock:
            if token:
                self.sessions.pop(token, None)
        return 200, {
            "ok": True,
            "logged_in": False,
            "cookie_headers": self.cookie_headers(secure=secure, revoke=True),
        }

    def check(self, headers, method: str) -> tuple[bool, str, str | None]:
        """Return (allowed, reason, token). Public routes are handled outside."""
        if not self.enabled:
            return True, "disabled", None
        bearer = _bearer(headers)
        jar = _cookies(headers)
        token = bearer or jar.get(SESSION_COOKIE)
        rec = self._session(token)
        if rec is None:
            return False, "unauthorized", None
        if method.upper() in WRITE_METHODS and not bearer:
            sent = headers.get("X-DP-CSRF") or ""
            cookie_csrf = jar.get(CSRF_COOKIE) or ""
            if (
                not sent
                or not hmac.compare_digest(sent, cookie_csrf)
                or not hmac.compare_digest(cookie_csrf, rec["csrf"])
            ):
                return False, "csrf_failed", token
        return True, "bearer" if bearer else "session", token
