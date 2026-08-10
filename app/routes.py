import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dependencies import COOKIE_NAME, require_admin, require_user, resolve_session, validate_csrf
from app.models import (
    ActivityEvent,
    AuthAttempt,
    AuditLog,
    BehavioralSignal,
    CatalogOutbox,
    Delivery,
    DeliveryAttempt,
    Product,
    ProductVectorState,
    Recommendation,
    RecommendationItem,
    RecommendationExposure,
    RecommendationRun,
    ServiceInvocation,
    User,
    UserInterestProfile,
    UserSession,
    utcnow,
)
from app.schemas import EventBatchInput, ProductInput, RegisterInput
from app.security import create_session, hash_password, verify_password
from app.services.admin_overview import build_overview_detail
from app.services.catalog import archive_product, create_product, get_active_products, update_product
from app.services.delivery import (
    MAX_DAILY_DIGESTS,
    configured_digest_time_gmt,
    dispatch_due_deliveries,
    schedule_admin_digest_slot,
    schedule_due_digests,
)
from app.services.mesh import mesh_gateway
from app.services.recommendation import (
    execute_contextual_recommendation,
    process_activity_and_maybe_recommend,
    profile_to_dict,
    queue_contextual_recommendation,
    retrieve_contextual_courses,
    retrieve_and_rank,
    current_cart_product_ids,
    saved_or_purchased_product_ids,
    execute_traced_mesh_attempt,
)
from app.services.observability import begin_invocation, finish_invocation
from app.services.langsmith_reconciliation import reconciliation_summary
from app.services.signals import overall_interest_topics, signal_summary
from app.services.vector_store import get_vector_store, sync_pending_catalog


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
AUTH_CSRF_COOKIE = "smartreco_auth_csrf"
REPORTING_TIMEZONE = timezone.utc


def as_reporting_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(REPORTING_TIMEZONE)


def format_utc(value: datetime | None, value_format: str = "%Y-%m-%d %H:%M:%S") -> str:
    converted = as_reporting_time(value)
    return converted.strftime(value_format) if converted else "—"


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


templates.env.filters["utc_time"] = format_utc


def context(request: Request, db: Session, **extra) -> dict:
    user, session = resolve_session(request, db)
    cart_count = len(current_cart_product_ids(user.id)) if user and user.role == "user" else 0
    return {"request": request, "current_user": user, "current_session": session, "cart_count": cart_count, **extra}


def load_recommendation(db: Session, user_id: str) -> tuple[Recommendation | None, list[dict]]:
    recommendation = db.scalar(
        select(Recommendation)
        .where(
            Recommendation.user_id == user_id,
            Recommendation.status == "active",
            Recommendation.recommendation_type == "overall",
            Recommendation.context_product_id.is_(None),
            (Recommendation.expires_at.is_(None) | (Recommendation.expires_at > utcnow())),
        )
        .order_by(Recommendation.generated_at.desc())
    )
    if not recommendation:
        return None, []
    rows = db.execute(
        select(RecommendationItem, Product)
        .join(Product, Product.id == RecommendationItem.product_id)
        .where(RecommendationItem.recommendation_id == recommendation.id, Product.status == "active")
        .order_by(RecommendationItem.rank)
    ).all()
    expected_count = db.scalar(select(func.count(RecommendationItem.id)).where(RecommendationItem.recommendation_id == recommendation.id)) or 0
    if len(rows) != expected_count or any(item.product_version != product.version for item, product in rows):
        recommendation.status = "invalidated"
        db.commit()
        return None, []
    return recommendation, [{"item": item, "product": product} for item, product in rows]


def load_contextual_recommendation(
    db: Session, user_id: str, product_id: str
) -> tuple[Recommendation | None, list[dict]]:
    recommendation = db.scalar(
        select(Recommendation)
        .where(
            Recommendation.user_id == user_id,
            Recommendation.status == "active",
            Recommendation.recommendation_type == "contextual",
            Recommendation.context_product_id == product_id,
            (Recommendation.expires_at.is_(None) | (Recommendation.expires_at > utcnow())),
        )
        .order_by(Recommendation.generated_at.desc())
    )
    if not recommendation:
        return None, []
    rows = db.execute(
        select(RecommendationItem, Product)
        .join(Product, Product.id == RecommendationItem.product_id)
        .where(
            RecommendationItem.recommendation_id == recommendation.id,
            Product.status == "active",
        )
        .order_by(RecommendationItem.rank)
    ).all()
    expected_count = db.scalar(select(func.count(RecommendationItem.id)).where(RecommendationItem.recommendation_id == recommendation.id)) or 0
    if len(rows) != expected_count or any(item.product_version != product.version for item, product in rows):
        recommendation.status = "invalidated"
        db.commit()
        return None, []
    return recommendation, [
        {
            "item": item,
            "product": product,
            "rank": item.rank,
            "explanation": item.explanation,
            "recommendation_id": recommendation.id,
        }
        for item, product in rows
    ]


def contextual_lifecycle(db: Session, user_id: str, product_id: str) -> dict:
    recommendation, rows = load_contextual_recommendation(db, user_id, product_id)
    run = db.scalar(
        select(RecommendationRun)
        .where(
            RecommendationRun.user_id == user_id,
            RecommendationRun.scope_key == f"course:{product_id}",
        )
        .order_by(RecommendationRun.created_at.desc())
        .limit(1)
    )
    stale_active_run = False
    if run and run.status in {"queued", "running"}:
        now = utcnow()
        created_at = run.created_at
        lease_expires_at = run.lease_expires_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=now.tzinfo)
        if lease_expires_at and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=now.tzinfo)
        stale_active_run = bool(
            (lease_expires_at and lease_expires_at < now)
            or (run.status == "queued" and (now - created_at).total_seconds() > 60)
        )
        # A read must never leave the customer watching an endless spinner when
        # a worker disappeared. The next page visit durably closes and replaces
        # this stale run in queue_contextual_recommendation().
        state = "failed" if stale_active_run else "generating"
    elif run and run.status == "failed" and (
        not recommendation or run.created_at >= recommendation.generated_at
    ):
        state = "failed"
    elif recommendation:
        state = "current"
    else:
        state = "empty"
    return {
        "state": state,
        "run": run,
        "recommendation": recommendation,
        "rows": rows,
        "stale_active_run": stale_active_run,
    }


SIGNAL_LABELS = {
    "BROWSE": "Browsed",
    "TOPIC_INTEREST": "Explored topic",
    "EXPLICIT_INTENT": "Searched",
    "EXPOSURE": "Course shown",
    "PRODUCT_INTEREST": "Viewed course",
    "HIGH_ENGAGEMENT": "Course dwell time",
    "PURCHASE_INTENT": "Saved course",
    "CART_REVIEW": "Reviewed cart course",
    "CART_RELEASED": "Removed from cart",
    "NEGATIVE_FEEDBACK": "Not for me",
    "RECOMMENDATION_EXPOSURE": "Recommendation shown",
    "RECOMMENDATION_RESPONSE": "Opened recommendation",
    "CONVERSION": "Purchased course",
}

EVENT_LABELS = {
    "page_viewed": "Page viewed",
    "category_selected": "Category explored",
    "search_submitted": "Search submitted",
    "product_impression": "Course appeared on screen",
    "product_viewed": "Course detail opened",
    "product_clicked": "Course clicked",
    "active_dwell": "Course dwell time recorded",
    "added_to_cart": "Course saved",
    "cart_viewed": "Cart course reviewed",
    "removed_from_cart": "Course removed",
    "recommendation_impression": "Recommendation shown",
    "recommendation_clicked": "Recommendation opened",
    "recommendation_dismissed": "Recommendation dismissed",
    "purchase_started": "Purchase started",
    "purchase_completed": "Course purchased",
}


def activity_detail(event: ActivityEvent, product: Product | None = None) -> str:
    if event.event_type == "active_dwell":
        return f"{round((event.duration_ms or 0) / 1000)} seconds actively viewing {product.title if product else 'a course'}"
    return event.search_query or (product.title if product else None) or event.category or event.page_path or "Platform navigation"


def personalized_course_rows(db: Session, user_id: str, current_product_id: str | None = None, limit: int = 3) -> list[dict]:
    """Return up to three current, eligible recommendations and fill gaps from RAG ranking."""
    excluded = saved_or_purchased_product_ids(user_id)
    if current_product_id:
        excluded.add(current_product_id)
    recommendation, persisted_rows = load_recommendation(db, user_id)
    rows: list[dict] = []
    selected_ids: set[str] = set()
    for row in persisted_rows:
        if row["product"].id in excluded:
            continue
        rows.append(
            {
                "product": row["product"],
                "explanation": row["item"].explanation,
                "rank": len(rows) + 1,
                "recommendation_id": recommendation.id if recommendation else None,
            }
        )
        selected_ids.add(row["product"].id)
        if len(rows) >= limit:
            return rows
    profile = db.get(UserInterestProfile, user_id)
    if not profile:
        return rows
    profile_data = profile_to_dict(profile)
    profile_data["excluded_product_ids"] = sorted(excluded | selected_ids)
    ranked, _metrics = retrieve_and_rank(profile_data, limit=max(limit * 3, limit))
    for candidate in ranked:
        if candidate["id"] in excluded or candidate["id"] in selected_ids:
            continue
        product = db.get(Product, candidate["id"])
        if not product or product.status != "active":
            continue
        rows.append(
            {
                "product": product,
                "explanation": candidate["default_reason"],
                "rank": len(rows) + 1,
                "recommendation_id": None,
            }
        )
        selected_ids.add(product.id)
        if len(rows) >= limit:
            break
    return rows


def contextual_course_rows(db: Session, user_id: str, product: Product, limit: int = 3, *, record_invocation: bool = True) -> list[dict]:
    selected, _metrics = retrieve_contextual_courses(
        user_id, product.id, limit=limit, record_invocation=record_invocation
    )
    rows: list[dict] = []
    for rank, item in enumerate(selected, start=1):
        candidate = db.get(Product, item["id"])
        if candidate:
            rows.append({
                "product": candidate,
                "explanation": item["explanation"],
                "rank": rank,
                "recommendation_id": None,
                "context_score": item["context_score"],
            })
    return rows


def topic_course_rows(db: Session, user_id: str, topics: list[dict]) -> list[dict]:
    """Choose one eligible RAG result per whole-history interest topic."""
    excluded = saved_or_purchased_product_ids(user_id)
    user_profile = db.get(UserInterestProfile, user_id)
    if user_profile:
        excluded.update(user_profile.negative_product_ids or [])
    chosen: set[str] = set()
    result: list[dict] = []
    for topic in topics:
        profile = {
            "primary_intent": topic["topic"],
            "secondary_intents": [],
            "category_weights": {topic["topic"]: max(1.0, topic["score"])},
            "recent_searches": [topic["label"]],
            "positive_product_ids": [],
            "negative_product_ids": sorted(excluded),
            "excluded_product_ids": sorted(excluded | chosen),
            "journey_stage": "exploration",
        }
        ranked, _metrics = retrieve_and_rank(profile, limit=8)
        candidate = next((item for item in ranked if item["id"] not in excluded and item["id"] not in chosen), None)
        if not candidate:
            continue
        product = db.get(Product, candidate["id"])
        if not product:
            continue
        chosen.add(product.id)
        result.append({**topic, "course": product})
    return result


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.scalar(select(func.count(User.id)))
    vector_status = get_vector_store().verify_index()
    return {
        "status": "ok",
        "database": "ok",
        "mesh": "configured" if mesh_gateway.enabled else "deterministic-fallback",
        "model": get_settings().active_chat_model,
        "rag_embeddings": {
            "status": vector_status.status,
            "provider": vector_status.descriptor["embedding_model"],
            "compatible": vector_status.compatible,
            "rebuild_required": vector_status.rebuild_required,
            "error_code": vector_status.error_code,
        },
        "langsmith": "configured" if get_settings().langsmith_tracing and get_settings().langsmith_api_key else "awaiting-api-key",
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, q: str | None = None, category: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Product).where(Product.status == "active")
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Product.title.ilike(like), Product.description.ilike(like), Product.category.ilike(like)))
    if category:
        stmt = stmt.where(Product.category == category)
    products = list(db.scalars(stmt.order_by(Product.popularity.desc(), Product.created_at.desc())).all())
    categories = list(db.scalars(select(Product.category).where(Product.status == "active").distinct().order_by(Product.category)).all())
    user, _session = resolve_session(request, db)
    recommendation = items = None
    signals = []
    profile = None
    top_interests = []
    if user and user.personalization_enabled:
        recommendation, items = load_recommendation(db, user.id)
        signals = signal_summary(db, user.id, limit=8)
        profile = db.get(UserInterestProfile, user.id)
        top_interests = topic_course_rows(db, user.id, overall_interest_topics(db, user.id))
    return templates.TemplateResponse(
        request,
        "home.html",
        context(request, db, products=products, categories=categories, q=q or "", selected_category=category, recommendation=recommendation, recommendation_items=items or [], signals=signals, profile=profile, top_interests=top_interests),
    )


@router.get("/products/{slug}", response_class=HTMLResponse)
def product_detail(slug: str, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    product = db.scalar(select(Product).where(Product.slug == slug, Product.status == "active"))
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    user, _ = resolve_session(request, db)
    personalized = bool(user and user.role == "user" and user.personalization_enabled)
    signals = list(reversed(signal_summary(db, user.id, limit=10))) if personalized else []
    recommendation, items = load_recommendation(db, user.id) if personalized else (None, [])
    lifecycle = contextual_lifecycle(db, user.id, product.id) if personalized else None
    page_visit_id = secrets.token_urlsafe(18) if personalized else None
    if personalized:
        queued = queue_contextual_recommendation(user.id, product.id, visit_id=page_visit_id)
        if queued["created"]:
            background.add_task(execute_contextual_recommendation, queued["run_id"])
            lifecycle = contextual_lifecycle(db, user.id, product.id)
    recommended_next = lifecycle["rows"] if lifecycle else []
    is_in_cart = product.id in current_cart_product_ids(user.id) if user and user.role == "user" else False
    return templates.TemplateResponse(
        request,
        "product.html",
        context(request, db, product=product, signals=signals, signal_labels=SIGNAL_LABELS, recommendation=recommendation, recommendation_items=items, recommended_next=recommended_next, contextual_lifecycle=lifecycle, contextual_recommendation=(lifecycle["recommendation"] if lifecycle else None), page_visit_id=page_visit_id, is_in_cart=is_in_cart),
    )


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request, "register.html",
        context(request, db, error=None, form_csrf=token, email_domain=get_settings().registration_email_domain),
    )
    response.set_cookie(AUTH_CSRF_COOKIE, token, httponly=True, samesite="strict", secure=get_settings().cookie_secure, max_age=900)
    return response


@router.post("/register")
def register(
    request: Request,
    email_local: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    form_csrf: str = Form(...),
    db: Session = Depends(get_db),
):
    cookie_token = request.cookies.get(AUTH_CSRF_COOKIE, "")
    if not cookie_token or not secrets.compare_digest(cookie_token, form_csrf):
        raise HTTPException(403, "Invalid registration form token")
    try:
        local_part = email_local.strip().lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", local_part) or ".." in local_part:
            raise ValueError("Invalid account name")
        data = RegisterInput(
            email=f"{local_part}@{get_settings().registration_email_domain}",
            display_name=display_name,
            password=password,
        )
        user = User(email=str(data.email).lower(), display_name=data.display_name, password_hash=hash_password(data.password))
        db.add(user)
        db.flush()
        db.add(UserInterestProfile(user_id=user.id, profile_version=0, journey_stage="exploration"))
        db.add(AuditLog(actor_user_id=user.id, action="user.registered", object_type="user", object_id=user.id))
        session, raw_token = create_session(user)
        db.add(session)
        db.commit()
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        return templates.TemplateResponse(
            request, "register.html",
            context(request, db, error="That account name is unavailable or invalid.", form_csrf=form_csrf, email_domain=get_settings().registration_email_domain),
            status_code=400,
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE_NAME, raw_token, httponly=True, samesite="lax", secure=get_settings().cookie_secure, max_age=get_settings().session_ttl_hours * 3600)
    response.delete_cookie(AUTH_CSRF_COOKIE)
    return response


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", db: Session = Depends(get_db)):
    token = secrets.token_urlsafe(32)
    next_path = next if next.startswith("/admin") and not next.startswith("//") else ""
    response = templates.TemplateResponse(
        request, "login.html", context(request, db, error=None, form_csrf=token, next_path=next_path)
    )
    response.set_cookie(AUTH_CSRF_COOKIE, token, httponly=True, samesite="strict", secure=get_settings().cookie_secure, max_age=900)
    return response


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    form_csrf: str = Form(...),
    next_path: str = Form("", alias="next"),
    db: Session = Depends(get_db),
):
    cookie_token = request.cookies.get(AUTH_CSRF_COOKIE, "")
    if not cookie_token or not secrets.compare_digest(cookie_token, form_csrf):
        raise HTTPException(403, "Invalid sign-in form token")
    settings = get_settings()
    normalized_email = email.strip().lower()
    ip_address = request.client.host if request.client else "unknown"
    cutoff = utcnow() - timedelta(minutes=settings.login_window_minutes)
    last_success = db.scalar(
        select(func.max(AuthAttempt.attempted_at)).where(
            AuthAttempt.succeeded.is_(True), or_(AuthAttempt.email == normalized_email, AuthAttempt.ip_address == ip_address)
        )
    )
    if last_success:
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=timezone.utc)
        cutoff = max(cutoff, last_success)
    failures = db.scalar(
        select(func.count(AuthAttempt.id)).where(
            AuthAttempt.succeeded.is_(False), AuthAttempt.attempted_at >= cutoff,
            or_(AuthAttempt.email == normalized_email, AuthAttempt.ip_address == ip_address),
        )
    ) or 0
    if failures >= settings.login_max_attempts:
        return templates.TemplateResponse(
            request, "login.html",
            context(request, db, error="Too many sign-in attempts. Please wait and try again.", form_csrf=form_csrf, next_path=next_path),
            status_code=429,
        )
    user = db.scalar(select(User).where(User.email == normalized_email, User.is_active.is_(True)))
    if not user or not verify_password(user.password_hash, password):
        db.add(AuthAttempt(email=normalized_email, ip_address=ip_address, succeeded=False))
        db.commit()
        return templates.TemplateResponse(
            request, "login.html",
            context(request, db, error="Email or password is incorrect.", form_csrf=form_csrf, next_path=next_path),
            status_code=400,
        )
    db.add(AuthAttempt(email=normalized_email, ip_address=ip_address, succeeded=True))
    session, raw_token = create_session(user)
    db.add(session)
    db.commit()
    safe_next = next_path if next_path.startswith("/admin") and not next_path.startswith("//") else ""
    destination = safe_next if user.role == "admin" and safe_next else "/admin" if user.role == "admin" else "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(COOKIE_NAME, raw_token, httponly=True, samesite="lax", secure=get_settings().cookie_secure, max_age=get_settings().session_ttl_hours * 3600)
    response.delete_cookie(AUTH_CSRF_COOKIE)
    return response


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    user, session = require_user(request, db)
    validate_csrf(request, session, csrf_token)
    session.revoked_at = utcnow()
    db.commit()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


def _profile_template_context(request: Request, db: Session, user: User, **values):
    email_local, email_domain = user.email.rsplit("@", 1)
    return context(
        request,
        db,
        user=user,
        email_local=email_local,
        email_domain=email_domain,
        phone_display="Not collected by SmartReco",
        digest_time_gmt=configured_digest_time_gmt(db),
        **values,
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user, _session = require_user(request, db)
    return templates.TemplateResponse(
        request,
        "profile.html",
        _profile_template_context(request, db, user, saved=False, error=None),
    )


@router.post("/profile", response_class=HTMLResponse)
def profile_update_email(
    request: Request,
    csrf_token: str = Form(...),
    email_local: str = Form(...),
    current_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user, session = require_user(request, db)
    validate_csrf(request, session, csrf_token)
    if not verify_password(user.password_hash, current_password):
        return templates.TemplateResponse(
            request,
            "profile.html",
            _profile_template_context(request, db, user, saved=False, error="Current password is incorrect."),
            status_code=400,
        )
    local_part = email_local.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", local_part) or ".." in local_part:
        return templates.TemplateResponse(
            request,
            "profile.html",
            _profile_template_context(request, db, user, saved=False, error="Enter a valid email account name."),
            status_code=400,
        )
    current_email = user.email.lower()
    email_domain = current_email.rsplit("@", 1)[1]
    updated_email = f"{local_part}@{email_domain}"
    duplicate = db.scalar(select(User.id).where(User.email == updated_email, User.id != user.id))
    if duplicate:
        return templates.TemplateResponse(
            request,
            "profile.html",
            _profile_template_context(request, db, user, saved=False, error="That email account name is already in use."),
            status_code=400,
        )
    if updated_email != current_email:
        user.email = updated_email
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="user.email.updated",
                object_type="user",
                object_id=user.id,
                audit_metadata={
                    "old_email_hash": hashlib.sha256(current_email.encode()).hexdigest(),
                    "new_email_hash": hashlib.sha256(updated_email.encode()).hexdigest(),
                    "domain": email_domain,
                },
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return templates.TemplateResponse(
                request,
                "profile.html",
                _profile_template_context(request, db, user, saved=False, error="That email account name is already in use."),
                status_code=400,
            )
    return templates.TemplateResponse(
        request,
        "profile.html",
        _profile_template_context(request, db, user, saved=True, error=None),
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)):
    user, _session = require_user(request, db)
    return templates.TemplateResponse(request, "account.html", context(request, db, user=user, saved=False))


@router.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request, db: Session = Depends(get_db)):
    user, _session = require_user(request, db)
    if user.role == "admin":
        return RedirectResponse("/admin", status_code=303)
    cart_ids = current_cart_product_ids(user.id)
    products = list(
        db.scalars(select(Product).where(Product.id.in_(cart_ids), Product.status == "active").order_by(Product.title)).all()
    ) if cart_ids else []
    return templates.TemplateResponse(request, "cart.html", context(request, db, products=products))


@router.post("/account", response_class=HTMLResponse)
def account_update(
    request: Request,
    csrf_token: str = Form(...),
    personalization_enabled: str | None = Form(None),
    digest_enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user, session = require_user(request, db)
    validate_csrf(request, session, csrf_token)
    user.personalization_enabled = personalization_enabled == "on"
    user.digest_enabled = user.personalization_enabled and digest_enabled == "on"
    db.add(AuditLog(actor_user_id=user.id, action="preferences.update", object_type="user", object_id=user.id, audit_metadata={"personalization": user.personalization_enabled, "digest": user.digest_enabled}))
    db.commit()
    return templates.TemplateResponse(request, "account.html", context(request, db, user=user, saved=True))


@router.post("/account/personalization-history/delete")
def delete_personalization_history(
    request: Request,
    csrf_token: str = Form(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
):
    """Explicit, authenticated reset; toggling consent alone never destroys history."""
    user, session = require_user(request, db)
    validate_csrf(request, session, csrf_token)
    if confirmation.strip().upper() != "DELETE":
        raise HTTPException(400, "Type DELETE to confirm the personalization history reset")
    recommendation_ids = list(db.scalars(select(Recommendation.id).where(Recommendation.user_id == user.id)).all())
    if recommendation_ids:
        delivery_ids = list(db.scalars(select(Delivery.id).where(Delivery.recommendation_id.in_(recommendation_ids))).all())
        if delivery_ids:
            db.execute(delete(DeliveryAttempt).where(DeliveryAttempt.delivery_id.in_(delivery_ids)))
            db.execute(delete(Delivery).where(Delivery.id.in_(delivery_ids)))
        db.execute(delete(RecommendationExposure).where(RecommendationExposure.recommendation_id.in_(recommendation_ids)))
        db.execute(delete(RecommendationItem).where(RecommendationItem.recommendation_id.in_(recommendation_ids)))
        db.execute(delete(Recommendation).where(Recommendation.id.in_(recommendation_ids)))
    db.execute(delete(ServiceInvocation).where(ServiceInvocation.user_id == user.id))
    db.execute(delete(RecommendationRun).where(RecommendationRun.user_id == user.id))
    db.execute(delete(BehavioralSignal).where(BehavioralSignal.user_id == user.id))
    db.execute(delete(ActivityEvent).where(ActivityEvent.user_id == user.id))
    db.execute(delete(UserInterestProfile).where(UserInterestProfile.user_id == user.id))
    db.add(UserInterestProfile(user_id=user.id, profile_version=0, journey_stage="exploration"))
    db.add(AuditLog(
        actor_user_id=user.id,
        action="personalization.history_deleted",
        object_type="user",
        object_id=user.id,
        audit_metadata={"scope": "events_signals_profiles_recommendations_runs_telemetry_deliveries"},
    ))
    db.commit()
    return RedirectResponse("/account?history_deleted=1", status_code=303)


@router.post("/api/events/batch")
def ingest_events(payload: EventBatchInput, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    user, session = require_user(request, db)
    validate_csrf(request, session, None)
    if not user.personalization_enabled:
        return {"accepted": 0, "duplicates": 0, "disabled": True}
    accepted = duplicates = rejected = 0
    existing_ids = set(
        db.scalars(select(ActivityEvent.event_id).where(ActivityEvent.event_id.in_([event.event_id for event in payload.events]))).all()
    )
    for event in payload.events:
        if event.event_id in existing_ids:
            duplicates += 1
            continue
        product = db.get(Product, event.product_id) if event.product_id else None
        product_required = event.event_type in {
            "product_impression", "product_viewed", "product_clicked", "active_dwell", "added_to_cart",
            "cart_viewed", "removed_from_cart", "recommendation_clicked", "recommendation_dismissed",
            "purchase_started", "purchase_completed",
        }
        if product_required and (not product or product.status != "active"):
            rejected += 1
            continue
        if event.event_type == "search_submitted" and not (event.search_query or "").strip():
            rejected += 1
            continue
        if event.event_type == "active_dwell" and (event.duration_ms is None or not 15_000 <= event.duration_ms <= 1_800_000):
            rejected += 1
            continue
        if event.event_type == "category_selected":
            category = (event.category or "").strip()
            if not category or not db.scalar(select(Product.id).where(Product.status == "active", Product.category == category).limit(1)):
                rejected += 1
                continue
        if event.event_type in {"recommendation_clicked", "recommendation_dismissed"}:
            recommendation = db.get(Recommendation, event.recommendation_id) if event.recommendation_id else None
            if not recommendation or recommendation.user_id != user.id or not event.product_id or not db.scalar(
                select(RecommendationItem.id).where(
                    RecommendationItem.recommendation_id == recommendation.id,
                    RecommendationItem.product_id == event.product_id,
                ).limit(1)
            ):
                rejected += 1
                continue
        if len(json.dumps(event.properties, default=str)) > 4000:
            rejected += 1
            continue
        occurred_at = event.occurred_at or datetime.now(timezone.utc)
        now = utcnow()
        if occurred_at < now - timedelta(days=30) or occurred_at > now + timedelta(minutes=5):
            rejected += 1
            continue
        authoritative_category = product.category if product else (event.category.strip() if event.category else None)
        safe_properties = {
            key: value for key, value in event.properties.items()
            if key in {"checkpoint", "page_visit_id", "source"} and isinstance(value, (str, int, float, bool, type(None)))
        }
        row = ActivityEvent(
                event_id=event.event_id,
                user_id=user.id,
                session_id=session.id,
                event_type=event.event_type,
                product_id=event.product_id,
                search_query=(event.search_query or "").strip() or None,
                category=authoritative_category,
                duration_ms=event.duration_ms,
                page_path=event.page_path,
                recommendation_id=event.recommendation_id,
                event_properties=safe_properties,
                occurred_at=occurred_at,
            )
        try:
            # The initial lookup avoids ordinary duplicate work; the savepoint
            # also makes simultaneous retries with the same event ID idempotent.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            duplicates += 1
            continue
        existing_ids.add(event.event_id)
        accepted += 1
    db.commit()
    if accepted:
        course_visit = request.headers.get("X-SmartReco-Context") == "course-visit"
        background.add_task(
            process_activity_and_maybe_recommend,
            user.id,
            allow_recommendation=not course_visit,
        )
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


@router.get("/api/recommendations/current")
def current_recommendation(request: Request, db: Session = Depends(get_db)):
    user, _ = require_user(request, db)
    if not user.personalization_enabled:
        return {"recommendation": None, "items": [], "disabled": True}
    recommendation, rows = load_recommendation(db, user.id)
    if not recommendation:
        return {"recommendation": None, "items": []}
    return {
        "recommendation": {"id": recommendation.id, "headline": recommendation.headline, "narrative": recommendation.narrative, "model": recommendation.model},
        "items": [
            {"product_id": row["product"].id, "title": row["product"].title, "slug": row["product"].slug, "rank": row["item"].rank, "reason": row["item"].explanation}
            for row in rows
        ],
    }


@router.get("/api/personalization/current")
def current_personalization(request: Request, current_product_id: str | None = None, db: Session = Depends(get_db)):
    user, _ = require_user(request, db)
    if not user.personalization_enabled:
        return {"disabled": True, "signals": [], "recommendations": [], "lifecycle": None, "top_interests": []}
    signals = list(reversed(signal_summary(db, user.id, limit=10)))
    current_product = db.get(Product, current_product_id) if current_product_id else None
    lifecycle = contextual_lifecycle(db, user.id, current_product.id) if current_product else None
    rows = lifecycle["rows"] if lifecycle else personalized_course_rows(db, user.id, limit=3)
    return {
        "signals": [
            {
                "id": signal.id,
                "type": signal.signal_type,
                "label": SIGNAL_LABELS.get(signal.signal_type, signal.signal_type.replace("_", " ").title()),
                "topic": signal.topic.replace("_", " ").title(),
                "strength": round(signal.strength, 2),
                "observed_at": iso_utc(signal.last_observed_at),
                "product": db.get(Product, signal.product_id).title if signal.product_id and db.get(Product, signal.product_id) else None,
                "reason": signal.reason,
            }
            for signal in signals
        ],
        "recommendations": [
            {
                "product_id": row["product"].id,
                "title": row["product"].title,
                "slug": row["product"].slug,
                "category": row["product"].category,
                "level": row["product"].level,
                "price": float(row["product"].price),
                "reason": row["explanation"],
                "rank": row["rank"],
                "recommendation_id": row["recommendation_id"],
                "confidence_score": round(row["item"].confidence_score, 4) if row.get("item") else None,
                "interest_likelihood": round(row["item"].interest_likelihood, 4) if row.get("item") else None,
            }
            for row in rows
        ],
        "lifecycle": (
            {
                "state": lifecycle["state"],
                "run_id": lifecycle["run"].id if lifecycle["run"] else None,
                "run_status": lifecycle["run"].status if lifecycle["run"] else None,
                "current_node": lifecycle["run"].current_node if lifecycle["run"] else None,
                "error_code": lifecycle["run"].error_code if lifecycle["run"] and lifecycle["state"] == "failed" else None,
                "recommendation_id": lifecycle["recommendation"].id if lifecycle["recommendation"] else None,
                "generated_at": iso_utc(lifecycle["recommendation"].generated_at) if lifecycle["recommendation"] else None,
                "headline": lifecycle["recommendation"].headline if lifecycle["recommendation"] else None,
                "narrative": lifecycle["recommendation"].narrative if lifecycle["recommendation"] else None,
                "model": lifecycle["recommendation"].model if lifecycle["recommendation"] else None,
                "context_product_id": current_product.id,
                "context_product_title": current_product.title,
            }
            if lifecycle else None
        ),
        "top_interests": overall_interest_topics(db, user.id),
    }


@router.post("/api/recommendations/contextual/{product_id}")
def request_contextual_recommendation(
    product_id: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user, session = require_user(request, db)
    validate_csrf(request, session, None)
    product = db.get(Product, product_id)
    if not product or product.status != "active":
        raise HTTPException(404, "Course not found")
    result = queue_contextual_recommendation(user.id, product.id)
    if result["created"]:
        background.add_task(execute_contextual_recommendation, result["run_id"])
    return result


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    recommendation_impressions = db.scalar(select(func.count(ActivityEvent.id)).where(ActivityEvent.event_type == "recommendation_impression")) or 0
    recommendation_clicks = db.scalar(select(func.count(ActivityEvent.id)).where(ActivityEvent.event_type == "recommendation_clicked")) or 0
    purchases = db.scalar(select(func.count(ActivityEvent.id)).where(ActivityEvent.event_type == "purchase_completed")) or 0
    metrics = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "events": db.scalar(select(func.count(ActivityEvent.id))) or 0,
        "signals": db.scalar(select(func.count(BehavioralSignal.id))) or 0,
        "runs": db.scalar(select(func.count(RecommendationRun.id))) or 0,
        "failed_runs": db.scalar(select(func.count(RecommendationRun.id)).where(RecommendationRun.status == "failed")) or 0,
        "pending_sync": db.scalar(select(func.count(CatalogOutbox.id)).where(CatalogOutbox.status.in_(["pending", "failed"]))) or 0,
        "recommendation_ctr": f"{(recommendation_clicks / recommendation_impressions * 100) if recommendation_impressions else 0:.1f}%",
        "purchases": purchases,
        "delivery_failures": db.scalar(select(func.count(Delivery.id)).where(Delivery.status.in_(["failed", "overdue"]))) or 0,
    }
    metric_descriptions = {
        "users": "Acquisition, account status, and learner activity",
        "events": "Behavior captured across learners and sessions",
        "signals": "Interpreted intent evidence and confidence",
        "runs": "Recommendation workflow throughput and health",
        "failed_runs": "Failed workflows requiring investigation",
        "pending_sync": "Catalog changes awaiting vector synchronization",
        "recommendation_ctr": "Recommendation impressions converted to clicks",
        "purchases": "Purchase conversions attributed to learner activity",
        "delivery_failures": "Failed or overdue proactive deliveries",
    }
    recent_events = list(db.scalars(select(ActivityEvent).order_by(ActivityEvent.id.desc()).limit(12)).all())
    event_rows = [
        {"event": event, "user": db.get(User, event.user_id), "product": db.get(Product, event.product_id) if event.product_id else None}
        for event in recent_events
    ]
    recent_runs = list(db.scalars(select(RecommendationRun).order_by(RecommendationRun.created_at.desc()).limit(8)).all())
    return templates.TemplateResponse(request, "admin/dashboard.html", context(request, db, metrics=metrics, metric_descriptions=metric_descriptions, event_rows=event_rows, runs=recent_runs, mesh_enabled=mesh_gateway.enabled, event_labels=EVENT_LABELS, activity_detail=activity_detail))

@router.get("/api/admin/overview/details")
def admin_overview_details(metric: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return build_overview_detail(db, metric)



@router.get("/admin/activity", response_class=HTMLResponse)
def admin_activity(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    events = list(db.scalars(select(ActivityEvent).order_by(ActivityEvent.id.desc()).limit(100)).all())
    signals = list(db.scalars(select(BehavioralSignal).order_by(BehavioralSignal.last_observed_at.desc()).limit(100)).all())
    profiles = list(db.scalars(select(UserInterestProfile).order_by(UserInterestProfile.updated_at.desc()).limit(50)).all())
    users = {user.id: user for user in db.scalars(select(User)).all()}
    products = {product.id: product for product in db.scalars(select(Product)).all()}
    return templates.TemplateResponse(request, "admin/activity.html", context(request, db, events=events, signals=signals, profiles=profiles, users=users, products=products, event_labels=EVENT_LABELS, signal_labels=SIGNAL_LABELS, activity_detail=activity_detail))


@router.get("/api/admin/activity")
def admin_activity_api(request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    require_admin(request, db)
    rows = list(db.scalars(select(ActivityEvent).where(ActivityEvent.id > after_id).order_by(ActivityEvent.id).limit(100)).all())
    return {
        "items": [
            {"id": row.id, "time": iso_utc(row.received_at), "user_id": row.user_id, "user_name": (db.get(User, row.user_id).display_name if db.get(User, row.user_id) else "Unknown user"), "event_type": row.event_type, "event_label": EVENT_LABELS.get(row.event_type, row.event_type.replace("_", " ").title()), "product_id": row.product_id, "detail": activity_detail(row, db.get(Product, row.product_id) if row.product_id else None), "duration_ms": row.duration_ms}
            for row in rows
        ],
        "next_after_id": rows[-1].id if rows else after_id,
    }


@router.get("/admin/products", response_class=HTMLResponse)
def admin_products(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    products = list(db.scalars(select(Product).order_by(Product.updated_at.desc())).all())
    states = {state.product_id: state for state in db.scalars(select(ProductVectorState)).all()}
    return templates.TemplateResponse(request, "admin/products.html", context(request, db, products=products, vector_states=states))


@router.get("/admin/products/new", response_class=HTMLResponse)
def admin_product_new(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return templates.TemplateResponse(request, "admin/product_form.html", context(request, db, product=None, error=None))


def _product_from_form(title, slug, description, category, level, skills, outcomes, price, currency, duration_minutes, status):
    return ProductInput(
        title=title,
        slug=slug,
        description=description,
        category=category,
        level=level,
        skills=[value.strip() for value in skills.split(",") if value.strip()],
        outcomes=[value.strip() for value in outcomes.split(",") if value.strip()],
        price=float(price),
        currency=currency.upper(),
        duration_minutes=int(duration_minutes),
        status=status,
    )


@router.post("/admin/products")
def admin_product_create(
    request: Request, background: BackgroundTasks, csrf_token: str = Form(...), title: str = Form(...), slug: str = Form(...),
    description: str = Form(...), category: str = Form(...), level: str = Form(...), skills: str = Form(""), outcomes: str = Form(""),
    price: float = Form(...), currency: str = Form("USD"), duration_minutes: int = Form(60), status: str = Form("active"), db: Session = Depends(get_db),
):
    admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    data = _product_from_form(title, slug, description, category, level, skills, outcomes, price, currency, duration_minutes, status)
    product = create_product(db, data)
    db.add(AuditLog(actor_user_id=admin.id, action="product.create", object_type="product", object_id=product.id))
    db.commit()
    background.add_task(sync_pending_catalog)
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/admin/products/{product_id}/edit", response_class=HTMLResponse)
def admin_product_edit(product_id: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "admin/product_form.html", context(request, db, product=product, error=None))


@router.post("/admin/products/{product_id}")
def admin_product_update(
    product_id: str, request: Request, background: BackgroundTasks, csrf_token: str = Form(...), title: str = Form(...), slug: str = Form(...),
    description: str = Form(...), category: str = Form(...), level: str = Form(...), skills: str = Form(""), outcomes: str = Form(""),
    price: float = Form(...), currency: str = Form("USD"), duration_minutes: int = Form(60), status: str = Form("active"), db: Session = Depends(get_db),
):
    admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404)
    update_product(db, product, _product_from_form(title, slug, description, category, level, skills, outcomes, price, currency, duration_minutes, status))
    db.add(AuditLog(actor_user_id=admin.id, action="product.update", object_type="product", object_id=product.id, audit_metadata={"version": product.version}))
    db.commit()
    background.add_task(sync_pending_catalog)
    return RedirectResponse("/admin/products", status_code=303)


@router.post("/admin/products/{product_id}/archive")
def admin_product_archive(product_id: str, request: Request, background: BackgroundTasks, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404)
    if product.status != "archived":
        archive_product(db, product)
        db.add(AuditLog(actor_user_id=admin.id, action="product.archive", object_type="product", object_id=product.id, audit_metadata={"version": product.version}))
        db.commit()
        background.add_task(sync_pending_catalog)
    return RedirectResponse("/admin/products", status_code=303)


@router.get("/admin/runs", response_class=HTMLResponse)
def admin_runs(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    runs = list(db.scalars(select(RecommendationRun).order_by(RecommendationRun.created_at.desc()).limit(100)).all())
    users = {user.id: user for user in db.scalars(select(User)).all()}
    return templates.TemplateResponse(request, "admin/runs.html", context(request, db, runs=runs, users=users))


@router.get("/api/admin/runs")
def admin_runs_api(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    runs = list(
        db.scalars(select(RecommendationRun).order_by(RecommendationRun.created_at.desc()).limit(100)).all()
    )
    users = {user.id: user for user in db.scalars(select(User)).all()}
    products = {product.id: product for product in db.scalars(select(Product)).all()}
    return {
        "items": [
            {
                "id": run.id,
                "created_at": iso_utc(run.created_at),
                "user_id": run.user_id,
                "user_name": users[run.user_id].display_name if run.user_id in users else "Unknown learner",
                "scope": "Course detail" if run.context_product_id else "Overall interests",
                "context_product_id": run.context_product_id,
                "context_product_title": products[run.context_product_id].title if run.context_product_id in products else None,
                "trigger_reason": run.trigger_reason,
                "status": run.status,
                "current_node": run.current_node or "queued",
                "model": run.model or (run.graph_state or {}).get("current_model"),
                "tokens": run.input_tokens + run.output_tokens,
                "error_code": run.error_code,
                "error_detail": run.error_detail,
            }
            for run in runs
        ],
        "refreshed_at": iso_utc(utcnow()),
    }


def _observability_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD in UTC") from exc


def _observability_date_range(
    start_date: str | None = None,
    end_date: str | None = None,
    selected_date: str | None = None,
) -> tuple[str | None, str | None]:
    selected_date = _observability_date(selected_date)
    start_date = _observability_date(start_date) or selected_date
    end_date = _observability_date(end_date) or selected_date
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="Start date must be on or before end date")
    return start_date, end_date


def observability_invocations(
    db: Session,
    user_id: str | None = None,
    selected_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[ServiceInvocation]:
    stmt = select(ServiceInvocation).order_by(ServiceInvocation.started_at.desc())
    if user_id:
        stmt = stmt.where(ServiceInvocation.user_id == user_id)
    start_date, end_date = _observability_date_range(start_date, end_date, selected_date)
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        stmt = stmt.where(ServiceInvocation.started_at >= start)
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        stmt = stmt.where(ServiceInvocation.started_at < end)
    return list(db.scalars(stmt).all())


def observability_snapshot(
    db: Session,
    user_id: str | None = None,
    selected_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    selected_date = _observability_date(selected_date)
    start_date, end_date = _observability_date_range(start_date, end_date, selected_date)
    all_invocations = observability_invocations(db, user_id)
    available_dates = sorted(
        {as_reporting_time(row.started_at).date().isoformat() for row in all_invocations},
        reverse=True,
    )
    invocations = observability_invocations(db, user_id, start_date=start_date, end_date=end_date)
    service_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    for invocation in invocations:
        service_counts[invocation.service] = service_counts.get(invocation.service, 0) + 1
        if invocation.model:
            model_counts[invocation.model] = model_counts.get(invocation.model, 0) + 1
    llm_calls = [row for row in invocations if row.service == "llm"]
    completed_latencies = [row.latency_ms for row in invocations if row.latency_ms is not None]
    metrics = {
        "llm_calls": len(llm_calls),
        "total_tokens": sum(row.input_tokens + row.output_tokens for row in llm_calls),
        "input_tokens": sum(row.input_tokens for row in llm_calls),
        "output_tokens": sum(row.output_tokens for row in llm_calls),
        "estimated_cost": sum(row.estimated_cost or 0 for row in llm_calls),
        "rag_calls": service_counts.get("rag", 0),
        "mcp_calls": service_counts.get("mcp", 0),
        "graph_runs": service_counts.get("langgraph", 0),
        "failures": sum(row.status == "failed" for row in invocations),
        "average_latency": round(sum(completed_latencies) / len(completed_latencies)) if completed_latencies else 0,
    }
    return {
        "invocations": invocations,
        "metrics": metrics,
        "service_counts": service_counts,
        "model_counts": model_counts,
        "reconciliation": _scoped_reconciliation(db, invocations, user_id),
        "available_dates": available_dates,
        "selected_date": selected_date or "",
        "selected_start_date": start_date or "",
        "selected_end_date": end_date or "",
    }


OBSERVABILITY_DETAIL_DEFINITIONS = {
    "llm_calls": {
        "title": "LLM provider-attempt performance",
        "subtitle": "Each row is one actual Mesh model attempt; correlated rows map one-to-one to LangSmith LLM spans.",
        "service": "llm",
        "primary_field": "calls",
        "primary_label": "Provider attempts",
        "primary_format": "integer",
        "columns": [
            ("calls", "Attempts", "integer"), ("success_rate", "Success", "percent"),
            ("avg_latency", "Avg response", "milliseconds"), ("p95_latency", "P95 response", "milliseconds"),
            ("total_tokens", "Tokens", "integer"),
        ],
    },
    "total_tokens": {
        "title": "Token consumption",
        "subtitle": "Input and generated tokens used by recommendation copy calls.",
        "service": "llm",
        "primary_field": "total_tokens",
        "primary_label": "Tokens",
        "primary_format": "integer",
        "columns": [
            ("total_tokens", "Total", "integer"), ("input_tokens", "Input", "integer"),
            ("output_tokens", "Output", "integer"), ("tokens_per_call", "Per call", "decimal"),
            ("calls", "Provider attempts", "integer"),
        ],
    },
    "estimated_cost": {
        "title": "Tentative model cost",
        "subtitle": "Estimated spend from known prices; free models remain explicitly zero.",
        "service": "llm",
        "primary_field": "estimated_cost",
        "primary_label": "Estimated cost",
        "primary_format": "currency",
        "columns": [
            ("estimated_cost", "Cost", "currency"), ("cost_per_call", "Per call", "currency"),
            ("priced_calls", "Priced calls", "integer"), ("unpriced_calls", "Unpriced", "integer"),
            ("calls", "Provider attempts", "integer"),
        ],
    },
    "rag_calls": {
        "title": "RAG retrieval health",
        "subtitle": "Semantic retrieval and behavioral ranking performance.",
        "service": "rag",
        "primary_field": "calls",
        "primary_label": "Retrievals",
        "primary_format": "integer",
        "columns": [
            ("calls", "Retrievals", "integer"), ("success_rate", "Success", "percent"),
            ("avg_latency", "Avg latency", "milliseconds"), ("p95_latency", "P95 latency", "milliseconds"),
            ("failures", "Failures", "integer"),
        ],
    },
    "mcp_calls": {
        "title": "MCP verification health",
        "subtitle": "Live catalog verification calls made before recommendations are shown.",
        "service": "mcp",
        "primary_field": "calls",
        "primary_label": "MCP calls",
        "primary_format": "integer",
        "columns": [
            ("calls", "Tool calls", "integer"), ("success_rate", "Success", "percent"),
            ("avg_latency", "Avg latency", "milliseconds"), ("p95_latency", "P95 latency", "milliseconds"),
            ("failures", "Failures", "integer"),
        ],
    },
    "graph_runs": {
        "title": "LangGraph workflow health",
        "subtitle": "End-to-end recommendation workflow completion and runtime.",
        "service": "langgraph",
        "primary_field": "calls",
        "primary_label": "Graph runs",
        "primary_format": "integer",
        "columns": [
            ("calls", "Runs", "integer"), ("success_rate", "Success", "percent"),
            ("avg_latency", "Avg runtime", "milliseconds"), ("p95_latency", "P95 runtime", "milliseconds"),
            ("failures", "Failed runs", "integer"),
        ],
    },
    "average_latency": {
        "title": "System response time",
        "subtitle": "Runtime distribution across every observed recommendation dependency.",
        "service": None,
        "primary_field": "avg_latency",
        "primary_label": "Average latency",
        "primary_format": "milliseconds",
        "columns": [
            ("avg_latency", "Average", "milliseconds"), ("p95_latency", "P95", "milliseconds"),
            ("min_latency", "Fastest", "milliseconds"), ("max_latency", "Slowest", "milliseconds"),
            ("calls", "Completed calls", "integer"),
        ],
    },
    "failures": {
        "title": "Failure and recovery health",
        "subtitle": "Visible dependency failures, affected learners, and dominant error types.",
        "service": None,
        "primary_field": "failures",
        "primary_label": "Failures",
        "primary_format": "integer",
        "columns": [
            ("failures", "Failures", "integer"), ("failure_rate", "Failure rate", "percent"),
            ("calls", "Observed calls", "integer"), ("top_error", "Top error", "text"),
            ("avg_latency", "Avg latency", "milliseconds"),
        ],
    },
}


def _summarize_invocations(rows: list[ServiceInvocation], user_name: str | None = None) -> dict:
    latencies = sorted(row.latency_ms for row in rows if row.latency_ms is not None)
    failures = [row for row in rows if row.status == "failed"]
    error_counts: dict[str, int] = {}
    model_counts: dict[str, int] = {}
    for row in rows:
        if row.model:
            model_counts[row.model] = model_counts.get(row.model, 0) + 1
        if row.status == "failed":
            error = row.error_code or (row.invocation_metadata or {}).get("failure_scope") or "Unknown failure"
            error_counts[error] = error_counts.get(error, 0) + 1
    calls = len(rows)
    successes = sum(row.status == "succeeded" for row in rows)
    total_tokens = sum(row.input_tokens + row.output_tokens for row in rows)
    estimated_cost = sum(row.estimated_cost or 0 for row in rows)
    p95_index = max(0, min(len(latencies) - 1, int(len(latencies) * .95 + .9999) - 1)) if latencies else 0
    top_error = max(error_counts, key=error_counts.get) if error_counts else "None"
    top_model = max(model_counts, key=model_counts.get) if model_counts else "None"
    correlated = [
        row for row in rows
        if row.service == "llm" and row.correlation_id and row.langsmith_run_id
        and row.workload not in {"legacy", "demo"} and not row.is_demo
    ]
    exported = sum(row.langsmith_export_status == "exported" for row in correlated)
    return {
        "user_name": user_name,
        "calls": calls,
        "successes": successes,
        "pending": calls - successes - len(failures),
        "failures": len(failures),
        "success_rate": round(successes / calls * 100, 1) if calls else 0,
        "failure_rate": round(len(failures) / calls * 100, 1) if calls else 0,
        "avg_latency": round(sum(latencies) / len(latencies)) if latencies else 0,
        "p95_latency": latencies[p95_index] if latencies else 0,
        "min_latency": latencies[0] if latencies else 0,
        "max_latency": latencies[-1] if latencies else 0,
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "total_tokens": total_tokens,
        "tokens_per_call": round(total_tokens / calls, 1) if calls else 0,
        "estimated_cost": round(estimated_cost, 8),
        "cost_per_call": round(estimated_cost / calls, 8) if calls else 0,
        "priced_calls": sum(row.estimated_cost is not None for row in rows),
        "unpriced_calls": sum(row.estimated_cost is None for row in rows),
        "top_error": top_error,
        "top_model": top_model,
        "correlated_attempts": len(correlated),
        "langsmith_matched": exported,
        "export_coverage": round(exported / len(correlated) * 100, 1) if correlated else 0,
        "export_pending": sum(row.langsmith_export_status == "pending" for row in correlated),
        "export_delayed": sum(row.langsmith_export_status == "delayed" for row in correlated),
        "retry_attempts": sum((row.attempt_number or 1) > 1 for row in correlated),
        "demo_attempts": sum(row.service == "llm" and (row.workload == "demo" or row.is_demo) for row in rows),
    }


def _detail_health(metric: str, summary: dict) -> dict:
    if not summary["calls"]:
        return {"status": "neutral", "label": "No data yet", "message": "No matching service calls have been recorded."}
    failure_rate = summary["failure_rate"]
    latency = summary["avg_latency"]
    if metric == "failures":
        status = "good" if failure_rate == 0 else "watch" if failure_rate <= 5 else "critical"
        label = "Healthy" if status == "good" else "Needs attention" if status == "watch" else "Degraded"
    elif metric in {"llm_calls", "graph_runs"}:
        status = "good" if failure_rate <= 2 and latency < 10000 else "watch" if failure_rate <= 10 and latency < 20000 else "critical"
        label = "Healthy" if status == "good" else "Monitor" if status == "watch" else "Degraded"
    elif metric in {"rag_calls", "mcp_calls"}:
        status = "good" if failure_rate <= 2 and latency < 1000 else "watch" if failure_rate <= 10 and latency < 3000 else "critical"
        label = "Healthy" if status == "good" else "Monitor" if status == "watch" else "Degraded"
    elif metric == "average_latency":
        status = "good" if latency < 3000 else "watch" if latency < 10000 else "critical"
        label = "Responsive" if status == "good" else "Elevated" if status == "watch" else "Slow"
    else:
        status = "good" if failure_rate <= 2 else "watch" if failure_rate <= 10 else "critical"
        label = "Healthy" if status == "good" else "Monitor" if status == "watch" else "Degraded"
    return {
        "status": status,
        "label": label,
        "message": f"{summary['success_rate']:.1f}% successful | {summary['avg_latency']} ms average latency",
    }


OPERATION_PURPOSES = {
    "contextual_catalog_retrieval": "Rank next courses",
    "contextual_behavioral_courses": "Rank next courses",
    "catalog_semantic_retrieval": "Find relevant courses",
    "get_verified_product_details": "Verify catalog facts",
    "recommendation_workflow": "Orchestrate recommendation",
    "personalized_persuasive_copy_attempt": "Write personal story",
    "derive_behavior_profile": "Update user interests",
}


def _chart_series(rows: list[ServiceInvocation], group: str, measure: str) -> list[dict]:
    buckets: dict[tuple[str, str], list[ServiceInvocation]] = {}
    for row in rows:
        name = getattr(row, group, None) or "Other"
        day = as_reporting_time(row.started_at).date().isoformat()
        buckets.setdefault((str(name), day), []).append(row)
    series: dict[str, list[dict]] = {}
    for (name, day), values in buckets.items():
        value = _chart_measure(values, measure)
        series.setdefault(name, []).append({"x": day, "y": value})
    return [{"name": name, "points": sorted(points, key=lambda point: point["x"])} for name, points in sorted(series.items())]


def _chart_measure(values: list[ServiceInvocation], measure: str) -> int | float:
    if measure == "calls":
        return len(values)
    if measure == "latency":
        latencies = [row.latency_ms for row in values if row.latency_ms is not None]
        return round(sum(latencies) / len(latencies)) if latencies else 0
    if measure == "input_tokens":
        return sum(row.input_tokens for row in values)
    if measure == "output_tokens":
        return sum(row.output_tokens for row in values)
    if measure == "cost":
        return round(sum(row.estimated_cost or 0 for row in values), 8)
    if measure == "failures":
        return sum(row.status == "failed" for row in values)
    return round(sum(row.status == "failed" for row in values) / len(values) * 100, 1)


def _hourly_metric(rows: list[ServiceInvocation], group: str, measure: str) -> dict[str, list[dict]]:
    buckets: dict[tuple[str, str, int], list[ServiceInvocation]] = {}
    for row in rows:
        local_time = as_reporting_time(row.started_at)
        name = getattr(row, group, None) or "Other"
        buckets.setdefault((local_time.date().isoformat(), str(name), local_time.hour), []).append(row)
    by_date: dict[str, dict[str, list[dict]]] = {}
    for (day, name, hour), values in buckets.items():
        by_date.setdefault(day, {}).setdefault(name, []).append({
            "x": f"{hour:02d}:00",
            "y": _chart_measure(values, measure),
        })
    return {
        day: [
            {"name": name, "points": sorted(points, key=lambda point: point["x"])}
            for name, points in sorted(series.items())
        ]
        for day, series in by_date.items()
    }


def _hourly_latency(rows: list[ServiceInvocation]) -> dict[str, list[dict]]:
    by_date: dict[str, dict[int, list[int]]] = {}
    for row in rows:
        if row.latency_ms is None:
            continue
        local_time = as_reporting_time(row.started_at)
        day = local_time.date().isoformat()
        by_date.setdefault(day, {}).setdefault(local_time.hour, []).append(row.latency_ms)
    return {
        day: [{"name": "Average latency", "points": [
            {"x": f"{hour:02d}:00", "y": round(sum(values) / len(values))}
            for hour, values in sorted(hours.items())
        ]}]
        for day, hours in by_date.items()
    }


def _operation_rows(rows: list[ServiceInvocation]) -> list[dict]:
    grouped: dict[tuple[str, str], list[ServiceInvocation]] = {}
    for row in rows:
        grouped.setdefault((row.service, row.operation), []).append(row)
    result = []
    for (service, operation), values in grouped.items():
        latencies = [row.latency_ms for row in values if row.latency_ms is not None]
        result.append({
            "service": service.upper(),
            "operation": operation.replace("_", " ").title(),
            "purpose": OPERATION_PURPOSES.get(operation, "Support recommendation"),
            "calls": len(values),
            "avg_latency": round(sum(latencies) / len(latencies)) if latencies else 0,
            "failures": sum(row.status == "failed" for row in values),
            "model": next((row.model for row in values if row.model), None),
        })
    return sorted(result, key=lambda item: item["calls"], reverse=True)


def _metric_insights(metric: str, rows: list[ServiceInvocation], db: Session) -> dict:
    charts: list[dict] = []
    operations = _operation_rows(rows)
    if metric == "llm_calls":
        charts = [
            {"title": "Calls by model", "subtitle": "Daily model traffic", "format": "integer", "series": _chart_series(rows, "model", "calls"), "drilldown": _hourly_metric(rows, "model", "calls"), "drilldown_hint": "Select a date to see hourly LLM-call volume."},
            {"title": "Response time by model", "subtitle": "Daily average", "format": "milliseconds", "series": _chart_series(rows, "model", "latency"), "drilldown": _hourly_metric(rows, "model", "latency"), "drilldown_hint": "Select a date to see hourly LLM latency."},
        ]
    elif metric == "total_tokens":
        charts = [
            {"title": "Input tokens by model", "subtitle": "Daily prompt volume", "format": "integer", "series": _chart_series(rows, "model", "input_tokens"), "drilldown": _hourly_metric(rows, "model", "input_tokens"), "drilldown_hint": "Select a date to see hourly input-token usage."},
            {"title": "Output tokens by model", "subtitle": "Daily generated volume", "format": "integer", "series": _chart_series(rows, "model", "output_tokens"), "drilldown": _hourly_metric(rows, "model", "output_tokens"), "drilldown_hint": "Select a date to see hourly output-token usage."},
        ]
    elif metric == "estimated_cost":
        charts = [
            {"title": "Estimated cost by model", "subtitle": "Daily priced model usage", "format": "currency", "series": _chart_series(rows, "model", "cost"), "drilldown": _hourly_metric(rows, "model", "cost"), "drilldown_hint": "Select a date to see hourly estimated cost."},
            {"title": "Priced calls by model", "subtitle": "Daily provider-attempt volume", "format": "integer", "series": _chart_series(rows, "model", "calls"), "drilldown": _hourly_metric(rows, "model", "calls"), "drilldown_hint": "Select a date to see hourly provider-attempt volume."},
        ]
    elif metric in {"rag_calls", "mcp_calls"}:
        label = "RAG" if metric == "rag_calls" else "MCP"
        charts = [
            {"title": f"{label} calls by service", "subtitle": "Daily operation traffic", "format": "integer", "series": _chart_series(rows, "operation", "calls"), "drilldown": _hourly_metric(rows, "operation", "calls"), "drilldown_hint": f"Select a date to see hourly {label} call volume."},
            {"title": "Latency", "subtitle": "Daily average response time", "format": "milliseconds", "series": _chart_series(rows, "service", "latency"), "drilldown": _hourly_latency(rows), "drilldown_hint": "Select a date to see hourly latency."},
        ]
    elif metric == "graph_runs":
        charts = [
            {"title": "Flows called", "subtitle": "Daily workflow volume", "format": "integer", "series": _chart_series(rows, "operation", "calls"), "drilldown": _hourly_metric(rows, "operation", "calls"), "drilldown_hint": "Select a date to see hourly LangGraph-run volume."},
            {"title": "Workflow runtime", "subtitle": "Daily average duration", "format": "milliseconds", "series": _chart_series(rows, "operation", "latency"), "drilldown": _hourly_metric(rows, "operation", "latency"), "drilldown_hint": "Select a date to see hourly workflow latency."},
        ]
    elif metric == "average_latency":
        charts = [
            {"title": "Latency by step", "subtitle": "Daily service bottlenecks", "format": "milliseconds", "series": _chart_series(rows, "service", "latency"), "drilldown": _hourly_metric(rows, "service", "latency"), "drilldown_hint": "Select a date to see hourly latency by workflow step."},
            {"title": "Time-of-day latency", "subtitle": "Daily response-time pattern", "format": "milliseconds", "series": _chart_series(rows, "service", "latency"), "drilldown": _hourly_latency(rows), "drilldown_hint": "Select a date to see hourly latency."},
        ]
    elif metric == "failures":
        failed = [row for row in rows if row.status == "failed"]
        charts = [
            {"title": "Failure rate", "subtitle": "Daily rate by service", "format": "percent", "series": _chart_series(rows, "service", "failure_rate"), "drilldown": _hourly_metric(rows, "service", "failure_rate"), "drilldown_hint": "Select a date to see hourly failure rate."},
            {"title": "Failure count", "subtitle": "Daily failures by service", "format": "integer", "series": _chart_series(failed, "service", "failures"), "drilldown_hint": "Select a date to see failures grouped by error type.", "drilldown": {
                day: [{"name": "Failure count", "points": [
                    {"x": error, "y": sum((row.error_code or "Unknown failure") == error for row in day_rows)}
                    for error in sorted({row.error_code or "Unknown failure" for row in day_rows})
                ]}]
                for day, day_rows in {
                    date: [row for row in failed if as_reporting_time(row.started_at).date().isoformat() == date]
                    for date in sorted({as_reporting_time(row.started_at).date().isoformat() for row in failed})
                }.items()
            }},
        ]
        operations = _operation_rows(failed)
    graph = None
    if metric == "graph_runs":
        run_ids = [row.recommendation_run_id for row in rows if row.recommendation_run_id]
        graph_runs = list(db.scalars(select(RecommendationRun).where(RecommendationRun.id.in_(run_ids))).all()) if run_ids else []
        flow_groups: dict[str, list[RecommendationRun]] = {}
        for run in graph_runs:
            flow_groups.setdefault("Course detail flow" if run.context_product_id else "Overall interest flow", []).append(run)
        flows = []
        for name, values in flow_groups.items():
            durations = [
                round((run.completed_at - run.started_at).total_seconds() * 1000)
                for run in values if run.completed_at and run.started_at
            ]
            flows.append({
                "service": "LANGGRAPH", "operation": name, "purpose": "Build recommendations",
                "calls": len(values), "avg_latency": round(sum(durations) / len(durations)) if durations else 0,
                "failures": sum(run.status == "failed" for run in values), "model": None,
            })
        node_rows = list(db.scalars(select(ServiceInvocation).where(ServiceInvocation.recommendation_run_id.in_(run_ids))).all()) if run_ids else []
        graph = {
            "nodes": [
                {"id": "load", "label": "Load context", "purpose": "Read behavior"},
                {"id": "retrieve", "label": "Retrieve & rank", "purpose": "Find course fit"},
                {"id": "verify", "label": "MCP verify", "purpose": "Confirm catalog"},
                {"id": "generate", "label": "LLM generate", "purpose": "Write story"},
                {"id": "validate", "label": "Validate output", "purpose": "Block hallucinations"},
                {"id": "persist", "label": "Persist", "purpose": "Save evidence"},
            ],
            "edges": ["load→retrieve", "retrieve→verify", "verify→generate", "generate→validate", "validate→persist"],
            "flows": sorted(flows, key=lambda item: item["calls"], reverse=True),
            "bottlenecks": _operation_rows([row for row in node_rows if row.service in {"rag", "mcp", "llm"}]),
        }
    return {
        "charts": charts,
        "operations": operations,
        "graph": graph,
        "llm_failures": _operation_rows([row for row in rows if row.service == "llm" and row.status == "failed"]),
    }


def _scoped_reconciliation(db: Session, rows: list[ServiceInvocation], user_id: str | None) -> dict:
    """Return reconciliation counts for the popup's current learner/date scope."""
    result = reconciliation_summary(db, user_id)
    attempts = [row for row in rows if row.service == "llm"]
    correlated = [row for row in attempts if row.correlation_id and row.langsmith_run_id]
    matched = sum(row.langsmith_export_status == "exported" for row in correlated)
    pending = sum(row.langsmith_export_status == "pending" for row in correlated)
    delayed = sum(row.langsmith_export_status == "delayed" for row in correlated)
    matched_rows = [row for row in correlated if row.langsmith_export_status == "exported"]

    def langsmith_usage(row: ServiceInvocation) -> dict:
        return ((row.invocation_metadata or {}).get("langsmith_usage") or {})

    comparable_rows = [row for row in matched_rows if langsmith_usage(row)]
    local_input_tokens = sum(row.input_tokens for row in comparable_rows)
    local_output_tokens = sum(row.output_tokens for row in comparable_rows)
    langsmith_input_tokens = sum(int(langsmith_usage(row).get("input_tokens") or 0) for row in comparable_rows)
    langsmith_output_tokens = sum(int(langsmith_usage(row).get("output_tokens") or 0) for row in comparable_rows)
    latency_comparable_rows = [
        row for row in matched_rows
        if row.latency_ms is not None and (row.invocation_metadata or {}).get("langsmith_latency_ms") is not None
    ]
    local_latencies = sorted(row.latency_ms for row in latency_comparable_rows)
    langsmith_latencies = [
        int((row.invocation_metadata or {}).get("langsmith_latency_ms"))
        for row in latency_comparable_rows
    ]

    def p95(values: list[int]) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(len(ordered) * .95 + .9999) - 1))
        return ordered[index]

    local_average_latency = round(sum(local_latencies) / len(local_latencies)) if local_latencies else 0
    langsmith_average_latency = round(sum(langsmith_latencies) / len(langsmith_latencies)) if langsmith_latencies else 0
    result.update({
        "provider_attempts": len(attempts),
        "correlated_attempts": len(correlated),
        "matched_spans": matched,
        "pending_attempts": pending,
        "delayed_attempts": delayed,
        "demo_attempts": sum(row.workload == "demo" or row.is_demo for row in attempts),
        "backfilled_attempts": sum(
            bool((row.invocation_metadata or {}).get("langsmith_backfill_submitted_at")) for row in correlated
        ),
        "coverage": round(matched / len(correlated) * 100, 1) if correlated else 0.0,
        "token_comparable_spans": len(comparable_rows),
        "token_uncomparable_attempts": len(attempts) - len(comparable_rows),
        "local_history_input_tokens": sum(row.input_tokens for row in attempts),
        "local_history_output_tokens": sum(row.output_tokens for row in attempts),
        "local_input_tokens": local_input_tokens,
        "langsmith_input_tokens": langsmith_input_tokens,
        "input_token_delta": local_input_tokens - langsmith_input_tokens,
        "local_output_tokens": local_output_tokens,
        "langsmith_output_tokens": langsmith_output_tokens,
        "output_token_delta": local_output_tokens - langsmith_output_tokens,
        "latency_comparable_spans": len(latency_comparable_rows),
        "local_average_latency_ms": local_average_latency,
        "langsmith_average_latency_ms": langsmith_average_latency,
        "average_latency_delta_ms": local_average_latency - langsmith_average_latency,
        "local_p95_latency_ms": p95(local_latencies),
        "langsmith_p95_latency_ms": p95(langsmith_latencies),
    })
    if delayed:
        result["status"] = "degraded"
        result["message"] = f"{delayed} attempt(s) in this selection are missing from LangSmith after the export window."
    elif pending:
        result["status"] = "pending"
        result["message"] = f"{pending} attempt(s) in this selection are still inside the normal LangSmith ingestion window."
    elif correlated:
        result["status"] = "healthy"
        result["message"] = "Every correlation-enabled attempt in this selection has a matching LangSmith LLM span."
    else:
        result["status"] = "neutral"
        result["message"] = "This selection contains provider attempts recorded before one-to-one correlation was enabled."
    return result


def observability_detail(
    db: Session,
    metric: str,
    user_id: str | None = None,
    selected_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    selected_date = _observability_date(selected_date)
    start_date, end_date = _observability_date_range(start_date, end_date, selected_date)
    definition = OBSERVABILITY_DETAIL_DEFINITIONS[metric]
    rows = observability_invocations(db, user_id)
    if definition["service"]:
        rows = [row for row in rows if row.service == definition["service"]]
    available_dates = sorted(
        {as_reporting_time(row.started_at).date().isoformat() for row in rows},
        reverse=True,
    )
    if start_date:
        rows = [row for row in rows if as_reporting_time(row.started_at).date().isoformat() >= start_date]
    if end_date:
        rows = [row for row in rows if as_reporting_time(row.started_at).date().isoformat() <= end_date]
    users = {user.id: user for user in db.scalars(select(User)).all()}
    by_date: dict[str, list[ServiceInvocation]] = {}
    by_user: dict[str, list[ServiceInvocation]] = {}
    by_date_user: dict[str, dict[str, list[ServiceInvocation]]] = {}
    for row in rows:
        day = as_reporting_time(row.started_at).date().isoformat()
        learner_key = row.user_id or "system"
        by_date.setdefault(day, []).append(row)
        by_user.setdefault(learner_key, []).append(row)
        by_date_user.setdefault(day, {}).setdefault(learner_key, []).append(row)

    def learner_summary(learner_key: str, learner_rows: list[ServiceInvocation]) -> dict:
        name = users[learner_key].display_name if learner_key in users else "System"
        return {"user_id": None if learner_key == "system" else learner_key, **_summarize_invocations(learner_rows, name)}

    summary = _summarize_invocations(rows)
    daily = [{"date": day, **_summarize_invocations(day_rows)} for day, day_rows in sorted(by_date.items(), reverse=True)]
    user_rows = [learner_summary(key, value) for key, value in by_user.items()]
    user_rows.sort(key=lambda row: (row[definition["primary_field"]], row["calls"]), reverse=True)
    users_by_date = {
        day: sorted(
            [learner_summary(key, value) for key, value in learners.items()],
            key=lambda row: (row[definition["primary_field"]], row["calls"]),
            reverse=True,
        )
        for day, learners in by_date_user.items()
    }
    columns = [{"key": key, "label": label, "format": value_format} for key, label, value_format in definition["columns"]]
    return {
        "metric": metric,
        "title": definition["title"],
        "subtitle": definition["subtitle"],
        "primary_field": definition["primary_field"],
        "primary_label": definition["primary_label"],
        "primary_format": definition["primary_format"],
        "columns": columns,
        "summary": summary,
        "health": _detail_health(metric, summary),
        "daily": daily,
        "users": user_rows,
        "users_by_date": users_by_date,
        "available_dates": available_dates,
        "selected_date": selected_date or "",
        "selected_start_date": start_date or "",
        "selected_end_date": end_date or "",
        "date_grain": "UTC day",
        "generated_at": iso_utc(utcnow()),
        "insights": _metric_insights(metric, rows, db),
        "reconciliation": _scoped_reconciliation(db, rows, user_id) if metric in {"llm_calls", "total_tokens", "average_latency"} else None,
        "trace_source": "Durable provider attempts reconciled to LangSmith traceable spans",
    }


@router.get("/admin/observability", response_class=HTMLResponse)
def admin_observability(
    request: Request,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    snapshot = observability_snapshot(db, selected_date=date, start_date=start_date, end_date=end_date)
    return templates.TemplateResponse(
        request,
        "admin/observability.html",
        context(
            request, db, selected_date=snapshot["selected_date"], selected_start_date=snapshot["selected_start_date"],
            selected_end_date=snapshot["selected_end_date"], available_dates=snapshot["available_dates"],
            invocations=snapshot["invocations"][:150], metrics=snapshot["metrics"], service_counts=snapshot["service_counts"], model_counts=snapshot["model_counts"],
            reconciliation=snapshot["reconciliation"],
            user_lookup={user.id: user for user in db.scalars(select(User)).all()},
            langsmith_connected=get_settings().langsmith_connected, langsmith_project=get_settings().langsmith_project,
            active_model=get_settings().active_chat_model,
        ),
    )


@router.get("/api/admin/observability")
def admin_observability_api(
    request: Request,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    snapshot = observability_snapshot(db, selected_date=date, start_date=start_date, end_date=end_date)
    user_lookup = {user.id: user for user in db.scalars(select(User)).all()}
    return {
        "metrics": snapshot["metrics"],
        "service_counts": snapshot["service_counts"],
        "model_counts": snapshot["model_counts"],
        "reconciliation": snapshot["reconciliation"],
        "available_dates": snapshot["available_dates"],
        "selected_date": snapshot["selected_date"],
        "selected_start_date": snapshot["selected_start_date"],
        "selected_end_date": snapshot["selected_end_date"],
        "items": [
            {
                "id": row.id,
                "started_at": iso_utc(row.started_at),
                "user_id": row.user_id,
                "user_name": user_lookup[row.user_id].display_name if row.user_id in user_lookup else "System",
                "service": row.service,
                "operation": row.operation,
                "model": row.model,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "estimated_cost": row.estimated_cost,
                "latency_ms": row.latency_ms,
                "status": row.status,
                "attempt": (row.invocation_metadata or {}).get("attempt"),
                "failure_scope": (row.invocation_metadata or {}).get("failure_scope"),
                "try_next_model": (row.invocation_metadata or {}).get("try_next_model"),
                "request_id": (row.invocation_metadata or {}).get("request_id"),
                "provider_receipt": row.provider_receipt,
                "correlation_id": row.correlation_id,
                "workload": row.workload,
                "langsmith_export_status": row.langsmith_export_status,
                "langsmith_run_url": row.langsmith_run_url,
                "error_code": row.error_code,
                "error_detail": row.error_detail,
            }
            for row in snapshot["invocations"][:150]
        ],
        "refreshed_at": iso_utc(utcnow()),
    }


@router.get("/api/admin/observability/details")
def admin_observability_details_api(
    request: Request,
    metric: str,
    user_id: str | None = None,
    date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    if metric not in OBSERVABILITY_DETAIL_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Unsupported observability metric")
    return observability_detail(db, metric, user_id, date, start_date, end_date)


def _admin_schedule_changes_today(db: Session, now: datetime | None = None) -> int:
    now = (now or utcnow()).astimezone(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.action == "delivery.schedule.updated",
            AuditLog.created_at >= day_start,
            AuditLog.created_at < day_end,
        )
    ) or 0


@router.get("/admin/deliveries", response_class=HTMLResponse)
def admin_deliveries(
    request: Request,
    schedule_saved: bool = False,
    schedule_limit: bool = False,
    schedule_unchanged: bool = False,
    deliveries_created: int = 0,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    deliveries = list(db.scalars(select(Delivery).order_by(Delivery.scheduled_for.desc()).limit(100)).all())
    rows = []
    for delivery in deliveries:
        attempts = list(db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == delivery.id).order_by(DeliveryAttempt.attempt_number)).all())
        rows.append({"delivery": delivery, "user": db.get(User, delivery.user_id), "attempts": attempts})
    changes_used = _admin_schedule_changes_today(db)
    return templates.TemplateResponse(
        request,
        "admin/deliveries.html",
        context(
            request,
            db,
            rows=rows,
            digest_time_gmt=configured_digest_time_gmt(db),
            schedule_changes_used=changes_used,
            schedule_changes_remaining=max(0, MAX_DAILY_DIGESTS - changes_used),
            max_daily_digests=MAX_DAILY_DIGESTS,
            schedule_saved=schedule_saved,
            schedule_limit=schedule_limit,
            schedule_unchanged=schedule_unchanged,
            deliveries_created=deliveries_created,
        ),
    )


@router.post("/admin/deliveries/schedule-time")
def admin_delivery_schedule_time(
    request: Request,
    csrf_token: str = Form(...),
    digest_time_gmt: str = Form(...),
    db: Session = Depends(get_db),
):
    admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    normalized = digest_time_gmt.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise HTTPException(400, "Digest time must use 24-hour HH:MM format in GMT")
    if normalized == configured_digest_time_gmt(db):
        return RedirectResponse("/admin/deliveries?schedule_unchanged=true", status_code=303)
    if _admin_schedule_changes_today(db) >= MAX_DAILY_DIGESTS:
        return RedirectResponse("/admin/deliveries?schedule_limit=true", status_code=303)
    admins = db.scalars(select(User).where(User.role == "admin")).all()
    for admin_user in admins:
        admin_user.digest_time_gmt = normalized
    audit = AuditLog(actor_user_id=admin.id, action="delivery.schedule.updated", object_type="delivery", audit_metadata={"digest_time_gmt": normalized, "timezone": "GMT", "daily_limit": MAX_DAILY_DIGESTS})
    db.add(audit)
    db.flush()
    slot_key = audit.id
    db.commit()
    result = schedule_admin_digest_slot(normalized, slot_key)
    return RedirectResponse(
        f"/admin/deliveries?schedule_saved=true&deliveries_created={result['created']}",
        status_code=303,
    )


@router.post("/admin/deliveries/run")
def admin_deliveries_run(request: Request, background: BackgroundTasks, csrf_token: str = Form(...), db: Session = Depends(get_db)):
    admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    db.add(AuditLog(actor_user_id=admin.id, action="delivery.dispatch.requested", object_type="delivery"))
    db.commit()
    background.add_task(schedule_due_digests)
    background.add_task(dispatch_due_deliveries)
    return RedirectResponse("/admin/deliveries", status_code=303)


@router.get("/admin/model-lab", response_class=HTMLResponse)
def admin_model_lab(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    users = list(db.scalars(select(User).where(User.role == "user").order_by(User.created_at.desc())).all())
    model_options = [
        {"id": get_settings().mesh_free_model, "tier": "Free", "default": True},
        {"id": get_settings().mesh_paid_model, "tier": "Paid", "default": False},
        {"id": get_settings().mesh_premium_model, "tier": "Premium", "default": False},
    ]
    return templates.TemplateResponse(request, "admin/model_lab.html", context(request, db, users=users, model_options=model_options, mesh_enabled=mesh_gateway.enabled))


@router.post("/api/admin/model-compare")
def admin_model_compare(
    request: Request, user_id: str = Form(...), csrf_token: str = Form(...),
    selected_models: list[str] | None = Form(None), db: Session = Depends(get_db),
):
    _admin, session = require_admin(request, db)
    validate_csrf(request, session, csrf_token)
    if not mesh_gateway.enabled:
        raise HTTPException(503, "Configure MESH_API_KEY before running live model comparison")
    profile = db.get(UserInterestProfile, user_id)
    if not profile:
        raise HTTPException(404, "User has no behavioral profile")
    profile_data = profile_to_dict(profile)
    products, metrics = retrieve_and_rank(profile_data, limit=3)
    allowed = {get_settings().mesh_free_model, get_settings().mesh_paid_model, get_settings().mesh_premium_model}
    requested = [model for model in (selected_models or [get_settings().mesh_free_model]) if model in allowed]
    if not requested:
        raise HTTPException(400, "Select at least one configured model")
    results = []
    for model in requested:
        handle = begin_invocation(
            "llm",
            "model_lab_comparison",
            user_id=user_id,
            model=model,
            metadata={"provider": "Mesh API", "attempt": 1},
            workload="model_lab",
            attempt_number=1,
        )
        try:
            result = execute_traced_mesh_attempt(
                profile=profile_data,
                products=products,
                model=model,
                concise=False,
                handle=handle,
                user_id=user_id,
                recommendation_run_id=None,
                attempt_number=1,
                workload="model_lab",
                langsmith_extra={
                    "tags": ["smartreco", "mesh", "model-lab", "provider-attempt"],
                    "metadata": {
                        "telemetry_schema": "provider-attempt-v1",
                        "local_invocation_id": handle.id,
                        "local_correlation_id": handle.correlation_id,
                        "user_id": user_id,
                        "attempt_number": 1,
                        "workload": "model_lab",
                        "ls_provider": "mesh_api",
                        "ls_model_name": model,
                    },
                },
            )
            finish_invocation(
                handle,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_receipt=result.request_id,
                failover_decision="not_needed",
            )
            results.append({"status": "succeeded", "model": result.model, "output": result.data.model_dump(), "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        except Exception as exc:
            finish_invocation(handle, status="failed", error=exc, failover_decision="stop")
            results.append({"status": "failed", "model": model, "error": "The provider rejected or could not complete this model call."})
    return {
        "metrics": metrics,
        "results": results,
    }
