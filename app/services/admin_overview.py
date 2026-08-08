from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityEvent, BehavioralSignal, CatalogOutbox, Delivery, DeliveryAttempt, Product, RecommendationRun, User, utcnow


VALID_METRICS = {"users", "events", "signals", "runs", "failed_runs", "pending_sync", "recommendation_ctr", "purchases", "delivery_failures"}


def utc(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    if value is None:
        return "?"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime(pattern)


def card(label: str, value, note: str) -> dict:
    return {"label": label, "value": value, "note": note}


def result(metric: str, title: str, subtitle: str, summary: list[dict], columns: list[str], rows: list[list]) -> dict:
    return {"metric": metric, "title": title, "subtitle": subtitle, "summary": summary, "columns": columns, "rows": rows,
            "generated_at": utcnow().isoformat(), "empty_message": "No matching evidence has been recorded yet."}


def run_duration(run: RecommendationRun) -> int | None:
    return max(0, int((run.completed_at - run.started_at).total_seconds() * 1000)) if run.started_at and run.completed_at else None


def users_detail(db: Session) -> dict:
    now = utcnow()
    users = list(db.scalars(select(User).order_by(User.created_at.desc(), User.display_name)).all())
    stats = {uid: (count, latest) for uid, count, latest in db.execute(
        select(ActivityEvent.user_id, func.count(ActivityEvent.id), func.max(ActivityEvent.received_at)).group_by(ActivityEvent.user_id)).all()}
    learners = [user for user in users if user.role == "user"]
    active = sum(user.is_active for user in users)
    rows = [[f"{user.display_name} ({user.id[:8]})", user.email, user.role.title(), utc(user.created_at, "%Y-%m-%d %H:%M"),
             utc(stats.get(user.id, (0, None))[1], "%Y-%m-%d %H:%M"), stats.get(user.id, (0, None))[0], "Active" if user.is_active else "Inactive"] for user in users]
    return result("users", "User acquisition and lifecycle", "Every account onboarded into SmartReco, with acquisition and engagement evidence.",
        [card("Total accounts", len(users), "Learners and administrators"), card("Active accounts", active, f"{active / len(users) * 100 if users else 0:.1f}% of accounts"),
         card("New in 30 days", sum(user.created_at >= now - timedelta(days=30) for user in users), "UTC acquisition window"),
         card("Personalization on", sum(user.personalization_enabled for user in learners), f"of {len(learners)} learners")],
        ["User", "Email", "Role", "Acquired (UTC)", "Latest activity (UTC)", "Events", "Status"], rows)


def events_detail(db: Session) -> dict:
    now = utcnow(); events = list(db.scalars(select(ActivityEvent).order_by(ActivityEvent.received_at.desc())).all()); grouped = {}
    for event in events:
        bucket = grouped.setdefault(event.event_type, {"count": 0, "users": set(), "latest": event.received_at})
        bucket["count"] += 1; bucket["users"].add(event.user_id); bucket["latest"] = max(bucket["latest"], event.received_at)
    top = max(grouped, key=lambda name: grouped[name]["count"], default="?")
    rows = [[name.replace("_", " ").title(), data["count"], len(data["users"]), f"{data['count'] / len(events) * 100:.1f}%", utc(data["latest"])]
            for name, data in sorted(grouped.items(), key=lambda item: item[1]["count"], reverse=True)]
    return result("events", "Behavioral event coverage", "Accepted frontend activity before interpretation into durable behavioral signals.",
        [card("Accepted events", len(events), "All persisted activity"), card("Learners observed", len({event.user_id for event in events}), "Unique owners"),
         card("Last 24 hours", sum(event.received_at >= now - timedelta(hours=24) for event in events), "UTC window"), card("Top activity", top.replace("_", " ").title(), "Most frequent event")],
        ["Activity", "Events", "Learners", "Share", "Latest accepted (UTC)"], rows)


def signals_detail(db: Session) -> dict:
    signals = list(db.scalars(select(BehavioralSignal).order_by(BehavioralSignal.last_observed_at.desc())).all()); grouped = {}; topics = {}
    for signal in signals:
        bucket = grouped.setdefault(signal.signal_type, {"count": 0, "users": set(), "strength": 0.0, "confidence": 0.0, "latest": signal.last_observed_at})
        bucket["count"] += 1; bucket["users"].add(signal.user_id); bucket["strength"] += signal.strength; bucket["confidence"] += signal.confidence
        bucket["latest"] = max(bucket["latest"], signal.last_observed_at); topics[signal.topic] = topics.get(signal.topic, 0) + 1
    avg = sum(signal.confidence for signal in signals) / len(signals) if signals else 0
    rows = [[name.replace("_", " ").title(), data["count"], len(data["users"]), f"{data['strength'] / data['count']:.2f}",
             f"{data['confidence'] / data['count'] * 100:.1f}%", utc(data["latest"])] for name, data in sorted(grouped.items(), key=lambda item: item[1]["count"], reverse=True)]
    return result("signals", "Behavioral signal quality", "Weighted evidence derived from searches, views, dwell, cart, dismissal, and conversions.",
        [card("Signals", len(signals), "Persisted interpreted evidence"), card("Learners profiled", len({signal.user_id for signal in signals}), "Unique owners"),
         card("Average confidence", f"{avg * 100:.1f}%", "Across all signals"), card("Leading topic", max(topics, key=topics.get, default="?"), "Most evidenced topic")],
        ["Signal", "Records", "Learners", "Avg strength", "Avg confidence", "Latest observed (UTC)"], rows)


def runs_detail(db: Session, failed_only: bool) -> dict:
    query = select(RecommendationRun).order_by(RecommendationRun.created_at.desc())
    if failed_only: query = query.where(RecommendationRun.status == "failed")
    runs = list(db.scalars(query).all()); users = {user.id: user for user in db.scalars(select(User)).all()}
    all_count = db.scalar(select(func.count(RecommendationRun.id))) or 0; failed_count = db.scalar(select(func.count(RecommendationRun.id)).where(RecommendationRun.status == "failed")) or 0
    durations = [value for run in runs if (value := run_duration(run)) is not None]
    if failed_only:
        errors = {}
        for run in runs: errors[run.error_code or "Unclassified"] = errors.get(run.error_code or "Unclassified", 0) + 1
        metric, title, subtitle = "failed_runs", "Recommendation run failures", "Failed graph executions with node, trigger, retry, and error evidence for triage."
        summary = [card("Failed runs", len(runs), "Requires review"), card("Failure rate", f"{failed_count / all_count * 100 if all_count else 0:.1f}%", "Across all runs"),
                   card("Affected learners", len({run.user_id for run in runs}), "Unique users"), card("Top error", max(errors, key=errors.get, default="?"), "Most frequent code")]
    else:
        succeeded = sum(run.status == "succeeded" for run in runs); metric, title, subtitle = "runs", "Recommendation workflow health", "Durable recommendation runs across triggers, learners, scopes, and graph states."
        summary = [card("Total runs", len(runs), "Durable LangGraph executions"), card("Succeeded", succeeded, f"{succeeded / len(runs) * 100 if runs else 0:.1f}% success rate"),
                   card("In progress", sum(run.status in {"queued", "running"} for run in runs), "Queued or running"), card("Average duration", f"{sum(durations) / len(durations) if durations else 0:.0f} ms", "Completed runs")]
    rows = [[utc(run.created_at), f"{users[run.user_id].display_name} ({run.user_id[:8]})" if run.user_id in users else run.user_id[:8], run.scope_key, run.trigger_type,
             run.current_node or "Queued", f"{run_duration(run)} ms" if run_duration(run) is not None else "?", run.retry_count, f"{run.status}: {run.error_code or 'no error'}"] for run in runs[:150]]
    return result(metric, title, subtitle, summary, ["Created (UTC)", "Learner", "Scope", "Trigger", "Current node", "Duration", "Retries", "Status / error"], rows)


def sync_detail(db: Session) -> dict:
    now = utcnow(); items = list(db.scalars(select(CatalogOutbox).where(CatalogOutbox.status.in_(["pending", "failed"])).order_by(CatalogOutbox.created_at)).all())
    products = {product.id: product for product in db.scalars(select(Product)).all()}; oldest = min((item.created_at for item in items), default=None)
    rows = [[utc(item.created_at), products[item.product_id].title if item.product_id in products else item.product_id[:8], item.event_type, item.product_version,
             item.attempt_count, utc(item.available_at), f"{item.status}: {item.last_error or 'ready'}"] for item in items]
    return result("pending_sync", "Catalog synchronization queue", "Database changes awaiting reliable propagation to the semantic vector index.",
        [card("Pending or failed", len(items), "Matches overview"), card("Pending", sum(item.status == "pending" for item in items), "Eligible for processing"),
         card("Failed", sum(item.status == "failed" for item in items), "Retry required"), card("Oldest age", f"{int((now - oldest).total_seconds() / 60)} min" if oldest else "?", "Queue age")],
        ["Created (UTC)", "Course", "Change", "Version", "Attempts", "Available (UTC)", "Status / error"], rows)


def ctr_detail(db: Session) -> dict:
    events = list(db.scalars(select(ActivityEvent).where(ActivityEvent.event_type.in_(["recommendation_impression", "recommendation_clicked"])).order_by(ActivityEvent.received_at.desc())).all()); daily = {}
    for event in events:
        day = utc(event.received_at, "%Y-%m-%d"); bucket = daily.setdefault(day, {"impressions": 0, "clicks": 0, "users": set()})
        bucket["impressions" if event.event_type == "recommendation_impression" else "clicks"] += 1; bucket["users"].add(event.user_id)
    impressions = sum(item["impressions"] for item in daily.values()); clicks = sum(item["clicks"] for item in daily.values())
    rows = [[day, data["impressions"], data["clicks"], f"{data['clicks'] / data['impressions'] * 100 if data['impressions'] else 0:.1f}%", len(data["users"])] for day, data in sorted(daily.items(), reverse=True)]
    return result("recommendation_ctr", "Recommendation engagement", "Click-through performance from persisted recommendation impressions and clicks.",
        [card("CTR", f"{clicks / impressions * 100 if impressions else 0:.1f}%", "Clicks / impressions"), card("Impressions", impressions, "Recommendation displays"),
         card("Clicks", clicks, "Recommendation selections"), card("Engaged learners", len({event.user_id for event in events if event.event_type == "recommendation_clicked"}), "Unique clickers")],
        ["UTC date", "Impressions", "Clicks", "CTR", "Learners reached"], rows)


def purchases_detail(db: Session) -> dict:
    events = list(db.scalars(select(ActivityEvent).where(ActivityEvent.event_type == "purchase_completed").order_by(ActivityEvent.received_at.desc())).all())
    users = {user.id: user for user in db.scalars(select(User)).all()}; products = {product.id: product for product in db.scalars(select(Product)).all()}; counts = {}
    for event in events:
        if event.product_id: counts[event.product_id] = counts.get(event.product_id, 0) + 1
    top_id = max(counts, key=counts.get, default=None); revenue = sum(float(products[event.product_id].price) for event in events if event.product_id in products)
    rows = [[utc(event.received_at), f"{users[event.user_id].display_name} ({event.user_id[:8]})" if event.user_id in users else event.user_id[:8],
             products[event.product_id].title if event.product_id in products else "Unknown course", products[event.product_id].category if event.product_id in products else "?",
             f"${float(products[event.product_id].price):,.2f}" if event.product_id in products else "?", event.recommendation_id[:8] if event.recommendation_id else "Organic"] for event in events[:150]]
    return result("purchases", "Purchase conversion", "Persisted learner purchases and courses converted from behavioral journeys.",
        [card("Purchases", len(events), "Completed events"), card("Unique buyers", len({event.user_id for event in events}), "Distinct learners"),
         card("Catalog value", f"${revenue:,.2f}", "Current course prices"), card("Top course", products[top_id].title if top_id in products else "?", "Most purchased")],
        ["Purchased (UTC)", "Learner", "Course", "Category", "Price", "Recommendation"], rows)


def deliveries_detail(db: Session) -> dict:
    deliveries = list(db.scalars(select(Delivery).where(Delivery.status.in_(["failed", "overdue"])).order_by(Delivery.scheduled_for.desc())).all())
    users = {user.id: user for user in db.scalars(select(User)).all()}; attempts = {did: count for did, count in db.execute(select(DeliveryAttempt.delivery_id, func.count(DeliveryAttempt.id)).group_by(DeliveryAttempt.delivery_id)).all()}
    rows = [[utc(item.scheduled_for), f"{users[item.user_id].display_name} ({item.user_id[:8]})" if item.user_id in users else item.user_id[:8], item.channel,
             attempts.get(item.id, 0), item.provider_receipt or "?", item.status] for item in deliveries[:150]]
    return result("delivery_failures", "Delivery exceptions", "Failed and overdue proactive recommendation deliveries with retry evidence.",
        [card("Exceptions", len(deliveries), "Failed plus overdue"), card("Failed", sum(item.status == "failed" for item in deliveries), "Terminal failures"),
         card("Overdue", sum(item.status == "overdue" for item in deliveries), "Past schedule"), card("Affected learners", len({item.user_id for item in deliveries}), "Unique recipients")],
        ["Scheduled (UTC)", "Learner", "Channel", "Attempts", "Provider receipt", "Status"], rows)


def build_overview_detail(db: Session, metric: str) -> dict:
    if metric not in VALID_METRICS:
        raise HTTPException(400, "Unknown overview metric")
    builders = {"users": users_detail, "events": events_detail, "signals": signals_detail, "runs": lambda session: runs_detail(session, False),
                "failed_runs": lambda session: runs_detail(session, True), "pending_sync": sync_detail, "recommendation_ctr": ctr_detail,
                "purchases": purchases_detail, "delivery_failures": deliveries_detail}
    return builders[metric](db)
