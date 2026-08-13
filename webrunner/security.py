"""Password hashing, session handling and API tokens.

PBKDF2-HMAC-SHA256 from the standard library keeps the dependency list short;
310k iterations matches the OWASP guidance for this construction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

PBKDF2_ITERATIONS = 310_000
SALT_BYTES = 16
TOKEN_BYTES = 32


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_api_token() -> str:
    return "wr_" + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """API tokens are stored only as a digest; the plaintext is shown once."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    return token[:11]


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def password_strength_warning(password: str) -> str:
    """A short warning if the password is weak, otherwise an empty string."""
    weak = {"admin", "password", "changeme", "change-me", "webrunner", "12345678", "letmein"}
    if len(password) < 8:
        return "Password is shorter than 8 characters."
    if password.lower() in weak:
        return "That password is one of the first things an attacker will try."
    return ""


def csrf_token(secret: str, session_token: str) -> str:
    """A CSRF token bound to the session, so it cannot be forged or replayed."""
    digest = hmac.new(secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(digest.digest()).decode("ascii").rstrip("=")


def verify_csrf(secret: str, session_token: str, presented: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(csrf_token(secret, session_token), presented)


def session_expiry(days: int) -> float:
    return time.time() + max(1, days) * 86400
