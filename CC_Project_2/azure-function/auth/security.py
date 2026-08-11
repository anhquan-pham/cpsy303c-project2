"""
Password hashing + JWT helpers for the Nutritional Insights Dashboard.

Env vars required (set these in Azure Function App > Settings, not in code):
    JWT_SECRET          - long random string, used to sign session tokens
"""

import os
import time
import jwt  # PyJWT
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

JWT_SECRET = os.environ.get("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 60 * 60 * 8  # 8 hour session


def hash_password(plain_password: str) -> str:
    """Return an argon2 hash to store in the DB. Never store plain_password."""
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Check a login attempt against the stored hash."""
    try:
        return _hasher.verify(stored_hash, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        # malformed hash, etc. - treat as failed login, don't leak details
        return False


def create_session_token(user_id: str, name: str, email: str) -> str:
    """Issue a JWT for a logged-in user (email/password OR OAuth)."""
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is not set in Function App settings")

    now = int(time.time())
    payload = {
        "sub": user_id,
        "name": name,
        "email": email,
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_session_token(token: str) -> dict | None:
    """Return the decoded payload if valid, or None if invalid/expired."""
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is not set in Function App settings")

    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_bearer_token(headers: dict) -> str | None:
    """Pull the raw token out of an 'Authorization: Bearer <token>' header."""
    auth_header = headers.get("Authorization") or headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()