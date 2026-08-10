from datetime import datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserSession, utcnow
from app.security import token_hash


COOKIE_NAME = "smartreco_session"
_AUTH_CACHE_ATTRIBUTE = "_smartreco_auth_resolution"


def _with_timezone(value: datetime, now: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=now.tzinfo)


def _cache_resolution(request: Request, resolution: tuple[User | None, UserSession | None]):
    setattr(request.state, _AUTH_CACHE_ATTRIBUTE, resolution)
    return resolution


def resolve_session(request: Request, db: Session) -> tuple[User | None, UserSession | None]:
    if hasattr(request.state, _AUTH_CACHE_ATTRIBUTE):
        return getattr(request.state, _AUTH_CACHE_ATTRIBUTE)
    raw_token = request.cookies.get(COOKIE_NAME)
    if not raw_token:
        return _cache_resolution(request, (None, None))
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw_token)))
    if not session or session.revoked_at:
        return _cache_resolution(request, (None, None))

    now = utcnow()
    settings = get_settings()
    expires_at = _with_timezone(session.expires_at, now)
    created_at = _with_timezone(session.created_at, now)
    last_seen_at = _with_timezone(session.last_seen_at, now)
    absolute_expires_at = created_at + timedelta(hours=settings.session_ttl_hours)
    idle_expires_at = last_seen_at + timedelta(minutes=settings.session_idle_minutes)
    if now >= min(expires_at, absolute_expires_at) or now >= idle_expires_at:
        session.revoked_at = now
        db.commit()
        return _cache_resolution(request, (None, None))

    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        return _cache_resolution(request, (None, None))
    session.last_seen_at = now
    db.commit()
    return _cache_resolution(request, (user, session))


def require_user(request: Request, db: Session) -> tuple[User, UserSession]:
    user, session = resolve_session(request, db)
    if not user or not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user, session


def require_admin(request: Request, db: Session) -> tuple[User, UserSession]:
    user, session = require_user(request, db)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user, session


def validate_csrf(request: Request, session: UserSession, submitted: str | None) -> None:
    header = request.headers.get("x-csrf-token")
    if (submitted or header) != session.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
