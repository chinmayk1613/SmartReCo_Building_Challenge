from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_settings
from app.services.delivery import dispatch_due_deliveries, recover_stale_deliveries, schedule_due_digests
from app.services.langsmith_reconciliation import reconcile_langsmith_traces
from app.services.recommendation import retry_stale_runs
from app.services.retention import enforce_retention
from app.services.vector_store import sync_pending_catalog


scheduler = BackgroundScheduler(timezone="UTC")


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(sync_pending_catalog, "interval", seconds=20, id="catalog-sync", max_instances=1, coalesce=True)
    scheduler.add_job(retry_stale_runs, "interval", minutes=1, id="stale-run-recovery", max_instances=1, coalesce=True)
    scheduler.add_job(schedule_due_digests, "interval", minutes=15, id="digest-scheduler", max_instances=1, coalesce=True)
    scheduler.add_job(dispatch_due_deliveries, "interval", seconds=30, id="delivery-dispatch", max_instances=1, coalesce=True)
    scheduler.add_job(recover_stale_deliveries, "interval", minutes=2, id="delivery-recovery", max_instances=1, coalesce=True)
    scheduler.add_job(enforce_retention, "cron", hour=2, minute=20, id="retention-enforcement", max_instances=1, coalesce=True)
    scheduler.add_job(
        reconcile_langsmith_traces,
        "interval",
        seconds=max(15, get_settings().langsmith_reconciliation_seconds),
        id="langsmith-reconciliation",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
