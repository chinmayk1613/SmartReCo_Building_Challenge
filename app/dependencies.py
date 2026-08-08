from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserSession, utcnow
from app.security import token_hash


COOKIE_NAME = "smartreco_session"


def resolve_session(request: Request, db: Session) -> tuple[User | None, UserSession | None]:
    raw_token = request.cookies.get(COOKIE_NAME)
    if not raw_token:
        return None, None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(raw_token)))
    if not session or session.revoked_at:
        return None, None
    expires_at = session.expires_at
    now = utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if expires_at < now:
        return None, None
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        return None, None
    session.last_seen_at = utcnow()
    return user, session


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
