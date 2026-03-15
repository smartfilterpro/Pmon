"""Authentication: JWT tokens + TOTP 2FA + admin approval."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pyotp

from pmon import database as db

logger = logging.getLogger(__name__)

# JWT secret - set via env var on Railway, auto-generated otherwise
JWT_SECRET = os.environ.get("PMON_JWT_SECRET", "pmon-dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, username: str, is_admin: bool) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def register_user(username: str, password: str) -> dict:
    """Register a new user. First user auto-becomes approved admin."""
    existing = db.get_user(username)
    if existing:
        raise ValueError("Username already taken")

    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    pw_hash = hash_password(password)

    # First user is auto-admin + auto-approved
    is_first = db.get_user_count() == 0
    user_id = db.create_user(
        username, pw_hash,
        is_admin=is_first,
        approved=is_first,
    )

    status = "approved" if is_first else "pending"
    return {"user_id": user_id, "username": username, "status": status}


def login_user(username: str, password: str, totp_code: str | None = None) -> dict:
    """Authenticate user. Returns token dict or raises ValueError."""
    user = db.get_user(username)
    if not user:
        raise ValueError("Invalid username or password")

    if not verify_password(password, user["password_hash"]):
        raise ValueError("Invalid username or password")

    # Check if account is approved
    if not user["approved"]:
        raise ValueError("Account pending admin approval")

    # Check 2FA if enabled
    if user["totp_enabled"]:
        if not totp_code:
            raise ValueError("2FA code required")
        totp = pyotp.TOTP(user["totp_secret"])
        if not totp.verify(totp_code, valid_window=1):
            raise ValueError("Invalid 2FA code")

    db.update_last_login(user["id"])
    token = create_token(user["id"], user["username"], bool(user["is_admin"]))
    return {
        "token": token,
        "user_id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "totp_enabled": bool(user["totp_enabled"]),
    }


def setup_totp(user_id: int) -> dict:
    """Generate a TOTP secret for 2FA setup."""
    user = db.get_user_by_id(user_id)
    if not user:
        raise ValueError("User not found")

    secret = pyotp.random_base32()
    db.update_user_totp(user_id, secret, enabled=False)

    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=user["username"], issuer_name="Pmon")

    return {"secret": secret, "uri": uri}


def confirm_totp(user_id: int, code: str) -> bool:
    """Verify a TOTP code and enable 2FA if valid."""
    user = db.get_user_by_id(user_id)
    if not user or not user["totp_secret"]:
        raise ValueError("TOTP not set up")

    totp = pyotp.TOTP(user["totp_secret"])
    if totp.verify(code, valid_window=1):
        db.update_user_totp(user_id, user["totp_secret"], enabled=True)
        return True
    return False


def disable_totp(user_id: int):
    """Disable 2FA for a user."""
    db.update_user_totp(user_id, "", enabled=False)


def create_initial_admin():
    """Create admin user from env vars if no users exist."""
    if db.get_user_count() > 0:
        return

    admin_user = os.environ.get("PMON_ADMIN_USER")
    admin_pass = os.environ.get("PMON_ADMIN_PASSWORD")

    if admin_user and admin_pass:
        try:
            register_user(admin_user, admin_pass)
            logger.info(f"Created initial admin user: {admin_user}")
        except ValueError as e:
            logger.warning(f"Could not create admin user: {e}")
