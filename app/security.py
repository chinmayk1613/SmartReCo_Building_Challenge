import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher

from app.config import get_settings
from app.models import User, UserSession, utcnow


password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user: User) -> tuple[UserSession, str]:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(48)
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
    )
    return session, raw_token
