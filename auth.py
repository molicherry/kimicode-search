import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone


PBKDF2_ITERATIONS = 100_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return f"{salt}:{password_hash.hex()}"


def verify_password(password: str, stored_value: str) -> bool:
    if ":" not in stored_value:
        return False

    salt, expected_hash = stored_value.split(":", 1)
    candidate_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return hmac.compare_digest(candidate_hash, expected_hash)


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def mask_visible_ends(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - (visible * 2))}{value[-visible:]}"


def generate_session_id() -> str:
    return secrets.token_hex(32)


def generate_user_api_key() -> str:
    return f"kimu_{secrets.token_hex(16)}"


def session_expiry(ttl_seconds: int) -> datetime:
    return utc_now() + timedelta(seconds=ttl_seconds)
