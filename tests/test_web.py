from uuid import uuid4
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import (
    ActivityEvent,
    Product,
    Recommendation,
    RecommendationItem,
    RecommendationRun,
    ServiceInvocation,
    User,
    UserInterestProfile,
    UserSession,
)
from app.routes import contextual_course_rows, personalized_course_rows
from app.schemas import ProductInput
from app.services.catalog import create_product
from app.services.recommendation import current_cart_product_ids
from app.security import hash_password
from app.services.signals import derive_signals, recent_interest_topics
from tests.conftest import login


def test_health_and_home_are_public(client, products):
    assert client.get("/health").status_code == 200
    response = client.get("/")
    assert response.status_code == 200
    assert "Courses for every" in response.text


def test_every_public_anchor_resolves(client, products):
    response = client.get("/")
    paths = set(re.findall(r'<a[^>]+href="(/[^"]*)"', response.text))
    assert len(paths) >= 10
    failures = {path: client.get(path).status_code for path in paths if client.get(path).status_code >= 400}
    assert failures == {}


def test_search_and_category_controls_filter_catalog(client, products):
    search = client.get("/?q=Agentic")
    assert search.status_code == 200
    assert "Agentic AI Mastery 0" in search.text
    assert "Python for AI Mastery 2" not in search.text
    category = client.get("/?category=MLOps")
    assert category.status_code == 200
    assert "MLOps Mastery 3" in category.text
    assert "Generative AI Mastery 1" not in category.text


def test_anonymous_course_cta_leads_to_sign_in(client, products):
    response = client.get(f"/products/{products[0].slug}")
    assert response.status_code == 200
    assert 'href="/login"' in response.text
    assert "Sign in to add course" in response.text


def test_registration_creates_authenticated_session(client, db):
    client.get("/register")
    response = client.post("/register", data={"email_local":"new.person", "display_name":"New User", "password":"LongEnough123!", "form_csrf": client.cookies.get("smartreco_auth_csrf")}, follow_redirects=False)
    assert response.status_code == 303
    assert client.cookies.get("smartreco_session")
    registered = db.scalar(select(User).where(User.email == "new.person@smartreco.ai"))
    assert registered is not None
    assert registered.role == "user"
    assert registered.personalization_enabled is True
    profile = db.scalar(select(UserInterestProfile).where(UserInterestProfile.user_id == registered.id))
    assert profile is not None
    assert profile.journey_stage == "exploration"


def test_invalid_login_is_rejected(client, user):
    response = login(client, user.email, "wrong")
    assert response.status_code == 400


def test_user_cannot_access_admin(client, user):
    login(client)
    assert client.get("/admin").status_code == 403


def test_admin_can_access_all_admin_pages(client, admin):
    login(client, "admin@example.com")
    for path in ["/admin", "/admin/activity", "/admin/products", "/admin/runs", "/admin/observability", "/admin/deliveries", "/admin/model-lab"]:
        assert client.get(path).status_code == 200


def test_admin_browser_auth_redirects_to_login_and_returns_to_requested_page(client, admin):
    protected = client.get(
        "/admin/observability?date=2026-08-05",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login?next=/admin/observability?date=2026-08-05"
    assert client.get("/api/admin/observability").status_code == 401

    login_page = client.get(protected.headers["location"])
    assert 'name="next" value="/admin/observability?date=2026-08-05"' in login_page.text
    signed_in = client.post(
        "/login",
        data={
            "email": admin.email,
            "password": "VeryStrong123!",
            "form_csrf": client.cookies.get("smartreco_auth_csrf"),
            "next": "/admin/observability?date=2026-08-05",
        },
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/admin/observability?date=2026-08-05"


def test_admin_overview_cards_open_evidence_backed_drilldowns(client, admin, user):
    login(client, "admin@example.com")
    page = client.get("/admin")
    assert page.status_code == 200
    assert page.text.count("data-overview-detail=") == 9
    assert "admin-dashboard.js" in page.text

    metrics = [
        "users", "events", "signals", "runs", "failed_runs", "pending_sync",
        "recommendation_ctr", "purchases", "delivery_failures",
    ]
    for metric in metrics:
        response = client.get(f"/api/admin/overview/details?metric={metric}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["metric"] == metric
        assert len(detail["summary"]) == 4
        assert detail["columns"]

    users = client.get("/api/admin/overview/details?metric=users").json()
    assert "Acquired (UTC)" in users["columns"]
    assert any(user.email in row for row in users["rows"])
    assert client.get("/api/admin/overview/details?metric=not_real").status_code == 400

def test_every_admin_navigation_anchor_resolves(client, admin, products):
    login(client, "admin@example.com")
    pages = [client.get("/admin"), client.get("/admin/products")]
    paths = set().union(*(set(re.findall(r'<a[^>]+href="(/[^"]*)"', page.text)) for page in pages))
    failures = {path: client.get(path).status_code for path in paths if client.get(path).status_code >= 400}
    assert failures == {}


def test_admin_catalog_create_and_archive_buttons_work(client, db, admin):
    login(client, "admin@example.com")
    session = db.scalar(select(UserSession).where(UserSession.user_id == admin.id))
    create_response = client.post(
        "/admin/products",
        data={
            "csrf_token": session.csrf_token,
            "title": "Professional AI Systems",
            "slug": "professional-ai-systems",
            "description": "Build reliable and secure artificial intelligence systems for production teams.",
            "category": "Agentic AI",
            "level": "Advanced",
            "skills": "architecture, evaluation",
            "outcomes": "Ship a reliable AI service",
            "price": "149",
            "currency": "USD",
            "duration_minutes": "480",
            "status": "active",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 303
    product = db.scalar(select(Product).where(Product.slug == "professional-ai-systems"))
    archive_response = client.post(
        f"/admin/products/{product.id}/archive",
        data={"csrf_token": session.csrf_token},
        follow_redirects=False,
    )
    db.refresh(product)
    assert archive_response.status_code == 303
    assert product.status == "archived"


def test_user_can_opt_in_to_personalization_and_digest(client, db, user):
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    response = client.post("/account", data={"csrf_token": session.csrf_token, "personalization_enabled": "on", "digest_enabled": "on"})
    db.refresh(user)
    assert response.status_code == 200
    assert user.personalization_enabled is True
    assert user.digest_enabled is True


def test_disabled_personalization_drops_behavioral_events(client, db, user):
    user.personalization_enabled = False
    db.commit()
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    response = client.post("/api/events/batch", json={"events":[{"event_id":str(uuid4()), "event_type":"page_viewed"}]}, headers={"X-CSRF-Token":session.csrf_token})
    assert response.json()["disabled"] is True
    assert db.query(ActivityEvent).count() == 0


def test_event_api_requires_authentication(client):
    response = client.post("/api/events/batch", json={"events":[{"event_id":str(uuid4()), "event_type":"page_viewed"}]})
    assert response.status_code == 401


def test_event_api_requires_csrf(client, user):
    login(client)
    response = client.post("/api/events/batch", json={"events":[{"event_id":str(uuid4()), "event_type":"page_viewed"}]})
    assert response.status_code == 403


def test_event_batch_is_idempotent(client, db, user):
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    event_id = str(uuid4())
    payload = {"events":[{"event_id":event_id, "event_type":"page_viewed"}]}
    first = client.post("/api/events/batch", json=payload, headers={"X-CSRF-Token":session.csrf_token})
    second = client.post("/api/events/batch", json=payload, headers={"X-CSRF-Token":session.csrf_token})
    assert first.json()["accepted"] == 1
    assert second.json()["duplicates"] == 1
    assert db.query(ActivityEvent).filter_by(event_id=event_id).count() == 1


def test_duplicate_event_ids_inside_one_batch_are_idempotent(client, db, user):
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    event_id = str(uuid4())
    response = client.post(
        "/api/events/batch",
        json={"events": [
            {"event_id": event_id, "event_type": "page_viewed"},
            {"event_id": event_id, "event_type": "page_viewed"},
        ]},
        headers={"X-CSRF-Token": session.csrf_token},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["duplicates"] == 1
    assert db.query(ActivityEvent).filter_by(event_id=event_id).count() == 1


def test_course_visit_activity_updates_profile_without_starting_second_recommendation(client, db, user, products):
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    response = client.post(
        "/api/events/batch",
        json={"events": [{
            "event_id": str(uuid4()),
            "event_type": "added_to_cart",
            "product_id": products[0].id,
            "category": products[0].category,
            "page_path": f"/products/{products[0].slug}",
        }]},
        headers={
            "X-CSRF-Token": session.csrf_token,
            "X-SmartReco-Context": "course-visit",
        },
    )
    profile = db.get(UserInterestProfile, user.id)
    assert response.status_code == 200
    assert profile is not None
    assert profile.profile_version == 1
    assert db.scalar(select(RecommendationRun).where(RecommendationRun.user_id == user.id)) is None


def test_model_comparison_requires_mesh_key(client, db, admin, user):
    login(client, "admin@example.com")
    session = db.scalar(select(UserSession).where(UserSession.user_id == admin.id))
    response = client.post("/api/admin/model-compare", data={"user_id":user.id, "csrf_token":session.csrf_token})
    assert response.status_code == 503


def test_recent_interest_topics_use_only_last_ten_meaningful_interactions(db, user, products):
    events = [
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_viewed", product_id=products[0].id, category="Agentic AI")
    ]
    events.extend(
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_viewed", product_id=products[2].id, category="Python for AI")
        for _ in range(10)
    )
    db.add_all(events); db.commit()
    topics = recent_interest_topics(db, user.id)
    assert topics[0]["label"] == "Python For Ai"
    assert all(topic["label"] != "Agentic Ai" for topic in topics)


def test_personalized_course_rows_exclude_saved_purchased_and_current(db, user, products):
    db.add(UserInterestProfile(user_id=user.id, primary_intent="python_for_ai", category_weights={"python_for_ai": 1.0}, profile_hash=str(uuid4())))
    db.add_all([
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="added_to_cart", product_id=products[0].id, category=products[0].category),
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="purchase_completed", product_id=products[1].id, category=products[1].category),
    ])
    db.commit()
    rows = personalized_course_rows(db, user.id, current_product_id=products[2].id, limit=3)
    ids = {row["product"].id for row in rows}
    assert len(rows) == 3
    assert ids.isdisjoint({products[0].id, products[1].id, products[2].id})


def test_course_page_and_personalization_api_show_last_ten_signals(client, db, user, products):
    for index in range(12):
        product = products[index % len(products)]
        db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_viewed", product_id=product.id, category=product.category))
    db.commit(); derive_signals(db, user.id); db.commit()
    login(client)
    page = client.get(f"/products/{products[0].slug}")
    assert page.status_code == 200
    assert "Your latest signals" in page.text
    assert "newest at the bottom" in page.text
    assert page.text.count('class="signal-feed-row"') == 10
    assert "Your best next steps from this course" in page.text
    payload = client.get(f"/api/personalization/current?current_product_id={products[0].id}").json()
    assert len(payload["signals"]) == 10
    assert 1 <= len(payload["recommendations"]) <= 3
    assert all(item["product_id"] != products[0].id for item in payload["recommendations"])


def test_course_detail_recommendations_blend_behavior_without_admitting_unrelated_interest(client, db, user, products):
    db.add(UserInterestProfile(
        user_id=user.id,
        primary_intent="java_development",
        category_weights={"java_development": 1.0, "mlops": 0.95},
        profile_hash=str(uuid4()),
    ))
    for index in range(3):
        create_product(db, ProductInput(
            title=f"Related Data Engineering {index}", slug=f"related-data-engineering-{index}",
            description="A focused data engineering course covering pipelines, schemas, quality, and reliable analytical systems.",
            category="Data Engineering", level="Intermediate", skills=["pipelines", "schemas", "data quality"],
            outcomes=["Build a reliable data pipeline"], price=129 + index,
        ))
    create_product(db, ProductInput(
        title="Unrelated Java Foundations", slug="unrelated-java-foundations",
        description="Object-oriented Java syntax, JVM classes, collections, and desktop application patterns.",
        category="Java Development", level="Beginner", skills=["java", "jvm", "collections"],
        outcomes=["Build a Java application"], price=109,
    ))
    db.commit()
    current = products[4]
    rows = contextual_course_rows(db, user.id, current, record_invocation=False)
    assert len(rows) == 3
    categories = [row["product"].category for row in rows]
    assert categories.count("Data Engineering") <= 2
    assert "MLOps" in categories
    assert all("Java" not in row["product"].title for row in rows)
    login(client)
    page = client.get(f"/products/{current.slug}")
    assert "Your best next steps from this course" in page.text
    assert "your accumulated activity" in page.text
    payload = client.get(f"/api/personalization/current?current_product_id={current.id}").json()
    assert [item["category"] for item in payload["recommendations"]].count("Data Engineering") <= 2
    assert "MLOps" in {item["category"] for item in payload["recommendations"]}
    assert "Java Development" not in {item["category"] for item in payload["recommendations"]}


def test_course_detail_results_change_with_each_users_behavior_instead_of_listing_department(db, user):
    cloud_learner = User(
        email="cloud-learner@example.com",
        display_name="Cloud Learner",
        password_hash=hash_password("VeryStrong123!"),
    )
    db.add(cloud_learner)
    db.flush()
    db.add_all([
        UserInterestProfile(
            user_id=user.id,
            primary_intent="web_technologies",
            category_weights={"web_technologies": 1.0},
            recent_searches=["Spring web APIs"],
            profile_hash=str(uuid4()),
        ),
        UserInterestProfile(
            user_id=cloud_learner.id,
            primary_intent="cloud_devops",
            category_weights={"cloud_devops": 1.0},
            recent_searches=["cloud deployment"],
            profile_hash=str(uuid4()),
        ),
    ])
    java_courses = []
    for index in range(4):
        java_courses.append(create_product(db, ProductInput(
            title=f"Java Path {index}", slug=f"java-path-{index}",
            description="Java language, JVM, object-oriented design, and reliable application engineering.",
            category="Java Development", level="Beginner" if index == 0 else "Intermediate",
            skills=["java", "jvm", "object oriented design"], outcomes=["Build a Java service"], price=99 + index,
        )))
    web_course = create_product(db, ProductInput(
        title="Spring Web API Engineering", slug="spring-web-api-engineering",
        description="Build Spring HTTP APIs and connect Java services to modern web clients.",
        category="Web Technologies", level="Intermediate", skills=["java", "spring", "api"],
        outcomes=["Build a Spring web API"], price=149,
    ))
    cloud_course = create_product(db, ProductInput(
        title="Deploying Java to Cloud", slug="deploying-java-to-cloud",
        description="Package, deploy, and observe Java services on managed cloud infrastructure.",
        category="Cloud & DevOps", level="Intermediate", skills=["java", "cloud", "deployment"],
        outcomes=["Deploy a Java service"], price=159,
    ))
    db.commit()

    web_rows = contextual_course_rows(db, user.id, java_courses[0], record_invocation=False)
    cloud_rows = contextual_course_rows(db, cloud_learner.id, java_courses[0], record_invocation=False)
    web_ids = {row["product"].id for row in web_rows}
    cloud_ids = {row["product"].id for row in cloud_rows}

    assert len(web_rows) == len(cloud_rows) == 3
    assert web_course.id in web_ids
    assert cloud_course.id in cloud_ids
    assert web_ids != cloud_ids
    assert sum(row["product"].category == "Java Development" for row in web_rows) <= 2
    assert sum(row["product"].category == "Java Development" for row in cloud_rows) <= 2


def test_personalization_never_leaks_another_users_signals(client, db, user, products):
    second = User(email="second@example.com", display_name="Second Learner", password_hash=hash_password("VeryStrong123!"))
    db.add(second); db.commit()
    db.add_all([
        ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="private alpha"),
        ActivityEvent(event_id=str(uuid4()), user_id=second.id, event_type="search_submitted", search_query="private beta"),
    ])
    db.commit(); derive_signals(db, user.id); derive_signals(db, second.id); db.commit()
    login(client)
    page = client.get(f"/products/{products[0].slug}")
    payload = client.get("/api/personalization/current").json()
    assert "Private Alpha" in page.text
    assert "Private Beta" not in page.text
    assert {signal["topic"] for signal in payload["signals"]} == {"Private Alpha"}


def test_user_cannot_read_or_submit_feedback_for_another_users_recommendation(client, db, user, products):
    other = User(email="other-owner@example.com", display_name="Other Owner", password_hash=hash_password("VeryStrong123!"))
    db.add(other); db.flush()
    run = RecommendationRun(
        user_id=other.id, trigger_type="test", trigger_reason="ownership",
        idempotency_key=str(uuid4()), profile_hash="other", status="succeeded",
    )
    db.add(run); db.flush()
    recommendation = Recommendation(
        run_id=run.id, user_id=other.id, headline="Private recommendation",
        narrative="This recommendation belongs only to the other learner.", model="test", profile_snapshot={},
    )
    db.add(recommendation); db.flush()
    db.add(RecommendationItem(
        recommendation_id=recommendation.id, product_id=products[0].id, rank=1,
        semantic_score=0.8, behavior_score=0.8, final_score=0.8,
        explanation="Private grounded item", product_version=products[0].version,
    ))
    db.commit()
    login(client)
    session = db.scalar(select(UserSession).where(UserSession.user_id == user.id))
    current = client.get("/api/recommendations/current").json()
    assert current["recommendation"] is None
    event_id = str(uuid4())
    response = client.post(
        "/api/events/batch",
        headers={"X-CSRF-Token": session.csrf_token},
        json={"events": [{
            "event_id": event_id,
            "event_type": "recommendation_dismissed",
            "product_id": products[0].id,
            "recommendation_id": recommendation.id,
        }]},
    )
    assert response.status_code == 200
    assert response.json()["rejected"] == 1
    assert db.scalar(select(ActivityEvent).where(ActivityEvent.event_id == event_id)) is None


def test_catalog_text_is_html_escaped_in_jinja_views(client, db, products):
    products[0].title = "<script>alert('xss')</script>"
    db.commit()
    response = client.get("/")
    assert response.status_code == 200
    assert "<script>alert('xss')</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_admin_recent_activity_uses_full_learner_name(client, db, admin, user):
    first = ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="secure rag")
    db.add(first); db.commit()
    login(client, "admin@example.com")
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Learner" in response.text
    assert f"({user.id[:8]})" in response.text
    second = ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="product_viewed", product_id=None)
    db.add(second); db.commit()
    live = client.get(f"/api/admin/activity?after_id={first.id}").json()
    assert [item["id"] for item in live["items"]] == [second.id]
    assert live["items"][0]["user_name"] == user.display_name
    assert live["next_after_id"] == second.id


def test_agent_runs_show_learner_name_and_id(client, db, admin, user):
    from app.models import RecommendationRun
    run = RecommendationRun(user_id=user.id, trigger_type="test", trigger_reason="test evidence", idempotency_key=str(uuid4()), profile_hash=str(uuid4()), status="failed", error_code="APIConnectionError", error_detail="Connection error")
    db.add(run); db.commit(); login(client, "admin@example.com")
    response = client.get("/admin/runs")
    assert user.display_name in response.text
    assert f"({user.id[:8]})" in response.text
    assert "Connection error" in response.text


def test_agent_runs_live_api_exposes_scope_source_and_node(client, db, admin, user, products):
    from app.models import RecommendationRun
    run = RecommendationRun(
        user_id=user.id,
        scope_key=f"course:{products[0].id}",
        context_product_id=products[0].id,
        trigger_type="course_context_opened",
        trigger_reason=f"Generate next steps from {products[0].title}",
        idempotency_key=str(uuid4()),
        profile_hash=str(uuid4()),
        status="running",
        current_node="retrieve_and_rank",
    )
    db.add(run); db.commit(); login(client, "admin@example.com")
    payload = client.get("/api/admin/runs").json()
    item = next(row for row in payload["items"] if row["id"] == run.id)
    assert item["user_name"] == user.display_name
    assert item["user_id"] == user.id
    assert item["scope"] == "Course detail"
    assert item["context_product_title"] == products[0].title
    assert item["current_node"] == "retrieve_and_rank"


def test_observability_main_filter_is_utc_date_scoped(client, db, admin, user):
    second = User(email="second@example.com", display_name="Second Learner", password_hash=hash_password("VeryStrong123!"))
    db.add(second); db.commit()
    db.add_all([
        ServiceInvocation(user_id=user.id, service="llm", operation="copy", status="succeeded", model="tencent/hy3", input_tokens=10, output_tokens=20, estimated_cost=0, started_at=datetime(2026, 8, 5, 10, tzinfo=timezone.utc)),
        ServiceInvocation(user_id=second.id, service="rag", operation="retrieve", status="succeeded", started_at=datetime(2026, 8, 4, 10, tzinfo=timezone.utc)),
    ]); db.commit()
    login(client, "admin@example.com")
    response = client.get("/admin/observability?date=2026-08-05")
    assert response.status_code == 200
    assert "30" in response.text
    timeline = response.text.split("Invocation timeline", 1)[1]
    assert "Second Learner" not in timeline
    assert ">RAG<" not in timeline
    assert "Filter all telemetry by UTC date" in response.text
    assert "Filter all telemetry by learner" not in response.text
    assert "2026-08-05 UTC</strong>" in response.text
    live = client.get("/api/admin/observability?date=2026-08-05").json()
    assert live["metrics"]["total_tokens"] == 30
    assert live["items"][0]["user_name"] == user.display_name
    assert live["selected_date"] == "2026-08-05"
    assert live["available_dates"] == ["2026-08-05", "2026-08-04"]
    assert "refreshed_at" in live
    assert 'observability.js?v=14' in response.text
    assert "data-observability-updated" in response.text
    assert response.text.count('data-kpi-detail=') == 8
    assert 'data-kpi-dialog' in response.text
    dialog_markup = response.text.split('data-kpi-dialog', 1)[1]
    assert dialog_markup.index('kpi-dialog-controls-top') < dialog_markup.index('data-kpi-health')
    detail = client.get(f"/api/admin/observability/details?metric=llm_calls&user_id={user.id}").json()
    assert detail["metric"] == "llm_calls"
    assert detail["summary"]["calls"] == 1
    assert detail["summary"]["total_tokens"] == 30
    assert detail["date_grain"] == "UTC day"
    assert detail["users"][0]["user_name"] == user.display_name
    assert detail["users"][0]["user_id"] == user.id
    assert client.get("/api/admin/observability?date=not-a-date").status_code == 400


def test_observability_details_are_date_and_user_grained(client, db, admin, user):
    second = User(email="second@example.com", display_name="Second Learner", password_hash=hash_password("VeryStrong123!"))
    db.add(second); db.commit()
    now = datetime.now(timezone.utc)
    db.add_all([
        ServiceInvocation(user_id=user.id, service="llm", operation="copy", status="succeeded", model="minimax/m2-her", input_tokens=80, output_tokens=20, latency_ms=6000, estimated_cost=0, started_at=now),
        ServiceInvocation(user_id=user.id, service="llm", operation="copy", status="failed", model="tencent/hy3", latency_ms=17000, error_code="ValidationError", started_at=now - timedelta(days=1)),
        ServiceInvocation(user_id=second.id, service="llm", operation="copy", status="succeeded", model="minimax/m2-her", input_tokens=50, output_tokens=10, latency_ms=7000, estimated_cost=0, started_at=now - timedelta(days=1)),
        ServiceInvocation(user_id=second.id, service="rag", operation="retrieve", status="succeeded", latency_ms=40, started_at=now),
    ]); db.commit()
    login(client, "admin@example.com")

    detail = client.get("/api/admin/observability/details?metric=llm_calls").json()
    assert detail["summary"]["calls"] == 3
    assert detail["summary"]["successes"] == 2
    assert detail["summary"]["failures"] == 1
    assert len(detail["daily"]) == 2
    assert len(detail["insights"]["charts"]) == 2
    assert {series["name"] for series in detail["insights"]["charts"][0]["series"]} == {"minimax/m2-her", "tencent/hy3"}
    assert "LangSmith" in detail["trace_source"]
    assert {row["user_name"] for row in detail["users"]} == {user.display_name, second.display_name}
    assert set(detail["users_by_date"]) == {row["date"] for row in detail["daily"]}
    assert detail["available_dates"] == [row["date"] for row in detail["daily"]]
    selected_date = detail["available_dates"][0]
    filtered = client.get(f"/api/admin/observability/details?metric=llm_calls&date={selected_date}").json()
    assert filtered["selected_date"] == selected_date
    assert filtered["available_dates"] == detail["available_dates"]
    assert len(filtered["daily"]) == 1
    assert filtered["daily"][0]["date"] == selected_date
    assert filtered["summary"]["calls"] == filtered["daily"][0]["calls"]
    assert detail["columns"][0] == {"key": "calls", "label": "Attempts", "format": "integer"}

    for metric in ["total_tokens", "estimated_cost", "rag_calls", "mcp_calls", "graph_runs", "average_latency", "failures"]:
        response = client.get(f"/api/admin/observability/details?metric={metric}")
        assert response.status_code == 200
        assert response.json()["metric"] == metric
    for metric in ["llm_calls", "total_tokens", "estimated_cost", "rag_calls", "mcp_calls", "graph_runs", "average_latency", "failures"]:
        charts = client.get(f"/api/admin/observability/details?metric={metric}").json()["insights"]["charts"]
        assert charts
        assert all(chart.get("drilldown") is not None for chart in charts)
        assert all(chart.get("drilldown_hint") for chart in charts)
    rag = client.get("/api/admin/observability/details?metric=rag_calls").json()
    assert rag["insights"]["operations"][0]["purpose"] == "Support recommendation"
    assert "drilldown" in rag["insights"]["charts"][1]
    tokens = client.get("/api/admin/observability/details?metric=total_tokens").json()
    assert all("drilldown" in chart for chart in tokens["insights"]["charts"])
    assert all(chart.get("drilldown_hint") for chart in tokens["insights"]["charts"])
    graph = client.get("/api/admin/observability/details?metric=graph_runs").json()
    assert [node["id"] for node in graph["insights"]["graph"]["nodes"]] == ["load", "retrieve", "verify", "generate", "validate", "persist"]
    failures = client.get("/api/admin/observability/details?metric=failures").json()
    assert failures["insights"]["charts"][0]["title"] == "Failure rate"
    assert all("drilldown" in chart for chart in failures["insights"]["charts"])
    assert client.get("/api/admin/observability/details?metric=unknown").status_code == 400


def test_observability_groups_dates_and_hours_in_utc(client, db, admin, user):
    db.add(ServiceInvocation(
        user_id=user.id,
        service="llm",
        operation="copy",
        status="succeeded",
        model="minimax/m2-her",
        input_tokens=25,
        output_tokens=10,
        started_at=datetime(2026, 8, 4, 20, 15, tzinfo=timezone.utc),
    ))
    db.commit(); login(client, "admin@example.com")
    detail = client.get("/api/admin/observability/details?metric=total_tokens").json()
    assert detail["daily"][0]["date"] == "2026-08-04"
    assert detail["date_grain"] == "UTC day"
    for chart in detail["insights"]["charts"]:
        assert "2026-08-04" in chart["drilldown"]
        assert chart["drilldown"]["2026-08-04"][0]["points"][0]["x"] == "20:00"


def test_database_normalizes_all_datetime_values_to_utc(db, user):
    source_timezone = timezone(timedelta(hours=5, minutes=30))
    row = ServiceInvocation(
        user_id=user.id,
        service="llm",
        operation="utc_normalization_check",
        status="succeeded",
        started_at=datetime(2026, 8, 5, 1, 30, tzinfo=source_timezone),
    )
    db.add(row); db.commit(); db.expire(row)
    assert row.started_at.utcoffset() == timedelta(0)
    assert row.started_at == datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)


def test_date_scoped_provider_attempts_include_demo_history(client, db, admin, user):
    occurred = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    db.add_all([
        ServiceInvocation(
            user_id=user.id, service="llm", operation="copy", status="succeeded",
            model="minimax/m2-her", workload="demo", is_demo=True,
            langsmith_export_status="demo", started_at=occurred,
        ),
        ServiceInvocation(
            user_id=user.id, service="llm", operation="copy", status="succeeded",
            model="minimax/m2-her", workload="recommendation",
            langsmith_export_status="disabled", started_at=occurred,
        ),
    ])
    db.commit(); login(client, "admin@example.com")
    detail = client.get("/api/admin/observability/details?metric=llm_calls&date=2026-08-04").json()
    assert detail["summary"]["calls"] == 2
    assert detail["reconciliation"]["provider_attempts"] == 2
    assert detail["reconciliation"]["demo_attempts"] == 1
    assert "Historical legacy" not in client.get("/admin/observability").text


def test_token_reconciliation_compares_matching_langsmith_usage(client, db, admin, user):
    occurred = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    db.add_all([
        ServiceInvocation(
            user_id=user.id, service="llm", operation="copy", status="succeeded",
            model="minimax/m2-her", correlation_id=str(uuid4()), langsmith_run_id=str(uuid4()),
            langsmith_export_status="exported", input_tokens=585, output_tokens=700,
            latency_ms=1200, started_at=occurred,
            invocation_metadata={
                "langsmith_usage": {"input_tokens": 585, "output_tokens": 700, "total_tokens": 1285},
                "langsmith_latency_ms": 1180,
            },
        ),
        ServiceInvocation(
            user_id=user.id, service="llm", operation="historical", status="succeeded",
            model="minimax/m2-her", input_tokens=1000, output_tokens=2000,
            langsmith_export_status="legacy", started_at=occurred,
        ),
    ])
    db.commit(); login(client, "admin@example.com")
    detail = client.get("/api/admin/observability/details?metric=total_tokens&date=2026-08-05").json()
    reconciliation = detail["reconciliation"]
    assert detail["summary"]["input_tokens"] == 1585
    assert detail["summary"]["output_tokens"] == 2700
    assert reconciliation["token_comparable_spans"] == 1
    assert reconciliation["token_uncomparable_attempts"] == 1
    assert reconciliation["local_history_input_tokens"] == 1585
    assert reconciliation["local_history_output_tokens"] == 2700
    assert reconciliation["local_input_tokens"] == reconciliation["langsmith_input_tokens"] == 585
    assert reconciliation["local_output_tokens"] == reconciliation["langsmith_output_tokens"] == 700
    assert reconciliation["input_token_delta"] == reconciliation["output_token_delta"] == 0
    latency = client.get("/api/admin/observability/details?metric=average_latency&date=2026-08-05").json()["reconciliation"]
    assert latency["latency_comparable_spans"] == 1
    assert latency["local_average_latency_ms"] == 1200
    assert latency["langsmith_average_latency_ms"] == 1180
    assert latency["average_latency_delta_ms"] == 20
    assert latency["local_p95_latency_ms"] == 1200
    assert latency["langsmith_p95_latency_ms"] == 1180
    assert client.get("/api/admin/observability/details?metric=estimated_cost").json()["reconciliation"] is None


def test_observability_cards_and_details_use_the_same_complete_dataset(client, db, admin, user):
    db.add_all([
        ServiceInvocation(
            user_id=user.id,
            service="llm",
            operation="copy",
            status="succeeded",
            model="tencent/hy3",
            input_tokens=1,
            output_tokens=2,
            latency_ms=100,
            started_at=datetime(2026, 8, 4, 12, index % 60, tzinfo=timezone.utc),
        )
        for index in range(1005)
    ])
    db.commit(); login(client, "admin@example.com")
    snapshot = client.get("/api/admin/observability").json()
    detail = client.get("/api/admin/observability/details?metric=llm_calls").json()
    tokens = client.get("/api/admin/observability/details?metric=total_tokens").json()
    assert snapshot["metrics"]["llm_calls"] == 1005
    assert snapshot["metrics"]["llm_calls"] == detail["summary"]["calls"]
    assert snapshot["metrics"]["total_tokens"] == tokens["summary"]["total_tokens"] == 3015


def test_cart_is_tracked_and_controls_recommendation_eligibility(client, db, user, products):
    login(client)
    csrf = db.scalar(select(UserSession).where(UserSession.user_id == user.id)).csrf_token
    added = client.post("/api/events/batch", headers={"X-CSRF-Token": csrf}, json={"events": [{"event_id": str(uuid4()), "event_type": "added_to_cart", "product_id": products[0].id}]})
    assert added.status_code == 200
    cart = client.get("/cart")
    assert products[0].title in cart.text
    assert 'data-track="removed_from_cart"' in cart.text
    assert products[0].id in current_cart_product_ids(user.id)
    recommendations = client.get("/api/personalization/current").json()["recommendations"]
    assert products[0].id not in {row["product_id"] for row in recommendations}

    viewed = client.post("/api/events/batch", headers={"X-CSRF-Token": csrf}, json={"events": [{"event_id": str(uuid4()), "event_type": "cart_viewed", "product_id": products[0].id, "category": products[0].category}]})
    assert viewed.status_code == 200
    removed = client.post("/api/events/batch", headers={"X-CSRF-Token": csrf}, json={"events": [{"event_id": str(uuid4()), "event_type": "removed_from_cart", "product_id": products[0].id}]})
    assert removed.status_code == 200
    assert products[0].id not in current_cart_product_ids(user.id)
    assert products[0].title not in client.get("/cart").text


def test_security_headers_are_applied(client, products):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_login_is_rate_limited_after_repeated_failures(client, user):
    for _ in range(5):
        assert login(client, user.email, "wrong").status_code == 400
    response = login(client, user.email, "wrong")
    assert response.status_code == 429
    assert "Too many sign-in attempts" in response.text


def test_login_and_registration_require_pre_auth_csrf(client, user):
    login_response = client.post("/login", data={"email": user.email, "password": "VeryStrong123!", "form_csrf": "forged"})
    register_response = client.post("/register", data={"email_local": "new", "display_name": "New User", "password": "LongEnough123!", "form_csrf": "forged"})
    assert login_response.status_code == 403
    assert register_response.status_code == 403


def test_home_hides_weak_interest_widget(client, db, user, products):
    db.add(ActivityEvent(event_id=str(uuid4()), user_id=user.id, event_type="search_submitted", search_query="agentic ai")); db.commit()
    derive_signals(db, user.id); db.commit(); login(client)
    response = client.get("/")
    assert "Your complete activity history" not in response.text
    assert "Your strongest interests so far" not in response.text
    assert "Top course for this interest" not in response.text
    assert "signal-ribbon" not in response.text


def test_admin_navigation_calls_learner_catalog_user_view(client, admin):
    login(client, "admin@example.com")
    response = client.get("/admin")
    assert ">User view</a>" in response.text
    assert ">Observability</a>" in response.text
