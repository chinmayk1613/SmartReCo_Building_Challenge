import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import User
from app.routes import router
from app.scheduler import start_scheduler, stop_scheduler
from app.security import hash_password


def bootstrap_admin() -> None:
    settings = get_settings()
    if not settings.demo_admin_password:
        return
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.email == settings.demo_admin_email.lower()))
        if not existing:
            db.add(
                User(
                    email=settings.demo_admin_email.lower(),
                    display_name="SmartReco Admin",
                    password_hash=hash_password(settings.demo_admin_password),
                    role="admin",
                )
            )
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.langsmith_tracing:
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        if settings.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_HIDE_INPUTS"] = "true"
        os.environ["LANGSMITH_HIDE_OUTPUTS"] = "true"
    init_db()
    bootstrap_admin()
    if settings.scheduler_enabled:
        start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="SmartReco", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().allowed_host_list)


@app.exception_handler(HTTPException)
async def browser_auth_handler(request: Request, exc: HTTPException):
    """Redirect browser navigation to sign-in while preserving JSON API errors."""
    wants_html = "text/html" in request.headers.get("accept", "")
    is_admin_page = request.url.path.startswith("/admin") and not request.url.path.startswith("/api/")
    if exc.status_code == 401 and wants_html and is_admin_page:
        requested = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(requested, safe='/?=&')}", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.cookies.get("smartreco_session") and response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, private"
    if get_settings().cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
