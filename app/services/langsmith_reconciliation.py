from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

from langsmith import Client
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import ServiceInvocation, TraceReconciliationRun, ensure_utc, utcnow
from app.services.observability import sanitize_telemetry


PROVIDER_SPAN_NAME = "smartreco-mesh-provider-attempt"
PROVIDER_TELEMETRY_SCHEMA = "provider-attempt-v1"
BACKFILL_SCHEMA = "provider-attempt-backfill-v1"


def _aware(value):
    if value is None:
        return None
    return ensure_utc(value)


def _pseudonymous_user_ref(user_id: str | None) -> str | None:
    if not user_id:
        return None
    secret = get_settings().app_secret
    return "learner_" + hashlib.sha256(f"{secret}:{user_id}".encode()).hexdigest()[:16]


def _metadata_datetime(value: str | None):
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _backfill_missing_provider_span(client: Client, row: ServiceInvocation, settings, now) -> bool:
    """Export a missing durable attempt as an explicitly historical LangSmith LLM span.

    This preserves observability without claiming the original provider response was
    retained or that a failed call succeeded.
    """
    metadata = dict(row.invocation_metadata or {})
    submitted_at = _metadata_datetime(metadata.get("langsmith_backfill_submitted_at"))
    retry_window = timedelta(seconds=max(30, settings.langsmith_export_delay_seconds))
    if submitted_at and now - submitted_at < retry_window:
        row.langsmith_export_status = "pending"
        return False
    run_id = UUID(str(row.langsmith_run_id))
    original_start = _aware(row.started_at)
    dotted_order = original_start.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)
    status_text = "failed" if row.status == "failed" else "completed"
    backfill_metadata = {
        "telemetry_schema": PROVIDER_TELEMETRY_SCHEMA,
        "backfill_schema": BACKFILL_SCHEMA,
        "historical_backfill": True,
        "original_provider_status": row.status,
        "original_error_code": row.error_code,
        "local_invocation_id": row.id,
        "local_correlation_id": row.correlation_id,
        "user_ref": _pseudonymous_user_ref(row.user_id),
        "recommendation_run_id": row.recommendation_run_id,
        "attempt_number": row.attempt_number,
        "workload": row.workload,
        "ls_provider": "mesh_api",
        "ls_model_name": row.model,
        "provider_receipt": row.provider_receipt,
    }
    error = None
    if row.status == "failed":
        error = sanitize_telemetry(f"{row.error_code or 'ProviderError'}: {row.error_detail or 'provider attempt failed'}")
    client.create_run(
        id=run_id,
        trace_id=run_id,
        name=PROVIDER_SPAN_NAME,
        run_type="llm",
        project_name=settings.langsmith_project,
        start_time=original_start,
        end_time=_aware(row.completed_at) or original_start,
        dotted_order=dotted_order,
        inputs={"historical_provider_attempt": status_text},
        outputs={"historical_result": "Local operational evidence backfilled after connectivity recovery"},
        error=error,
        extra={"metadata": backfill_metadata},
        tags=["smartreco", "mesh", "provider-attempt", "historical-backfill"],
        prompt_tokens=int(row.input_tokens or 0),
        completion_tokens=int(row.output_tokens or 0),
        total_tokens=int((row.input_tokens or 0) + (row.output_tokens or 0)),
        hide_inputs=True,
        hide_outputs=True,
    )
    row.langsmith_trace_id = str(run_id)
    row.langsmith_export_status = "pending"
    metadata.pop("langsmith_backfill_error", None)
    metadata.pop("langsmith_backfill_last_attempt_at", None)
    row.invocation_metadata = {
        **metadata,
        "langsmith_backfill_submitted_at": now.isoformat(),
        "langsmith_backfill_attempts": int(metadata.get("langsmith_backfill_attempts", 0)) + 1,
        "langsmith_backfill_schema": BACKFILL_SCHEMA,
    }
    return True


def _list_provider_runs(client: Client, settings, started_at):
    return list(client.list_runs(
        project_name=settings.langsmith_project,
        run_type="llm",
        start_time=started_at - timedelta(days=max(1, settings.langsmith_reconciliation_days)),
        filter=f'eq(name, "{PROVIDER_SPAN_NAME}")',
        select=[
            "id", "trace_id", "name", "run_type", "start_time", "end_time", "error", "extra",
            "prompt_tokens", "completion_tokens", "total_tokens",
        ],
    ))


def reconcile_langsmith_traces() -> dict:
    """Match durable Mesh attempts to exported LangSmith LLM spans.

    The worker never fabricates historical matches. Only spans carrying the current
    provider-attempt schema participate in the one-to-one reconciliation contract.
    """
    settings = get_settings()
    started_at = utcnow()
    db = SessionLocal()
    snapshot = TraceReconciliationRun(
        project_name=settings.langsmith_project,
        status="running",
        started_at=started_at,
    )
    db.add(snapshot)
    db.commit()
    try:
        llm_rows = list(db.scalars(select(ServiceInvocation).where(ServiceInvocation.service == "llm")).all())
        correlated = [
            row for row in llm_rows
            if row.correlation_id and row.langsmith_run_id
        ]
        legacy = [row for row in llm_rows if row.workload == "legacy"]
        demo = [row for row in llm_rows if row.workload == "demo" or row.is_demo]
        snapshot.local_correlated_attempts = len(correlated)
        snapshot.legacy_attempts = len(legacy)
        snapshot.demo_attempts = len(demo)

        if not settings.langsmith_connected:
            snapshot.status = "disabled"
            snapshot.pending_attempts = sum(row.langsmith_export_status == "disabled" for row in correlated)
            snapshot.completed_at = utcnow()
            db.commit()
            return reconciliation_summary(db)

        client = Client(api_key=settings.langsmith_api_key)
        remote_runs = _list_provider_runs(client, settings, started_at)
        remote_by_id = {str(run.id): run for run in remote_runs}
        remote_by_correlation: dict[str, object] = {}
        for run in remote_runs:
            metadata = ((run.extra or {}).get("metadata") or {}) if getattr(run, "extra", None) else {}
            if metadata.get("telemetry_schema") != PROVIDER_TELEMETRY_SCHEMA:
                continue
            correlation_id = metadata.get("local_correlation_id")
            if correlation_id:
                remote_by_correlation[str(correlation_id)] = run

        now = utcnow()
        delay = timedelta(seconds=max(30, settings.langsmith_export_delay_seconds))
        backfilled = 0
        for row in correlated:
            remote = remote_by_id.get(str(row.langsmith_run_id)) or remote_by_correlation.get(str(row.correlation_id))
            if remote or now - _aware(row.started_at) <= delay:
                continue
            try:
                backfilled += int(_backfill_missing_provider_span(client, row, settings, now))
            except Exception as exc:
                row.invocation_metadata = {
                    **(row.invocation_metadata or {}),
                    "langsmith_backfill_error": sanitize_telemetry(f"{type(exc).__name__}: {exc}"),
                    "langsmith_backfill_last_attempt_at": now.isoformat(),
                }
                row.langsmith_export_status = "delayed"
        if backfilled:
            db.commit()
            client.flush(timeout=20)
            remote_runs = _list_provider_runs(client, settings, started_at)
            remote_by_id = {str(run.id): run for run in remote_runs}
            remote_by_correlation = {}
            for run in remote_runs:
                metadata = ((run.extra or {}).get("metadata") or {}) if getattr(run, "extra", None) else {}
                correlation_id = metadata.get("local_correlation_id")
                if correlation_id:
                    remote_by_correlation[str(correlation_id)] = run
        matched_ids: set[str] = set()
        for row in correlated:
            remote = remote_by_id.get(str(row.langsmith_run_id)) or remote_by_correlation.get(str(row.correlation_id))
            row.langsmith_last_checked_at = now
            if remote:
                row.langsmith_export_status = "exported"
                row.langsmith_exported_at = _aware(getattr(remote, "end_time", None)) or now
                remote_started_at = _aware(getattr(remote, "start_time", None))
                remote_completed_at = _aware(getattr(remote, "end_time", None))
                langsmith_latency_ms = (
                    max(0, round((remote_completed_at - remote_started_at).total_seconds() * 1000))
                    if remote_started_at and remote_completed_at else None
                )
                row.invocation_metadata = {
                    **(row.invocation_metadata or {}),
                    "langsmith_usage": {
                        "input_tokens": int(getattr(remote, "prompt_tokens", 0) or 0),
                        "output_tokens": int(getattr(remote, "completion_tokens", 0) or 0),
                        "total_tokens": int(getattr(remote, "total_tokens", 0) or 0),
                    },
                    "langsmith_latency_ms": langsmith_latency_ms,
                }
                try:
                    row.langsmith_run_url = client.get_run_url(run=remote, project_name=settings.langsmith_project)
                except Exception:
                    # URL construction is presentation-only and must not affect reconciliation.
                    pass
                matched_ids.add(str(remote.id))
            elif (
                (submitted_at := _metadata_datetime((row.invocation_metadata or {}).get("langsmith_backfill_submitted_at")))
                and now - submitted_at <= delay
            ):
                row.langsmith_export_status = "pending"
            elif now - _aware(row.started_at) > delay:
                row.langsmith_export_status = "delayed"
            else:
                row.langsmith_export_status = "pending"

        schema_runs = [
            run for run in remote_runs
            if (((run.extra or {}).get("metadata") or {}).get("telemetry_schema") == PROVIDER_TELEMETRY_SCHEMA)
        ]
        snapshot.remote_provider_spans = len(schema_runs)
        snapshot.matched_attempts = sum(row.langsmith_export_status == "exported" for row in correlated)
        snapshot.pending_attempts = sum(row.langsmith_export_status == "pending" for row in correlated)
        snapshot.delayed_attempts = sum(row.langsmith_export_status == "delayed" for row in correlated)
        snapshot.unmatched_remote_spans = sum(str(run.id) not in matched_ids for run in schema_runs)
        snapshot.status = (
            "degraded"
            if snapshot.delayed_attempts or snapshot.unmatched_remote_spans
            else "pending" if snapshot.pending_attempts
            else "healthy"
        )
        snapshot.completed_at = now
        db.commit()
        return reconciliation_summary(db)
    except Exception as exc:
        db.rollback()
        snapshot = db.get(TraceReconciliationRun, snapshot.id)
        if snapshot:
            snapshot.status = "unavailable"
            snapshot.error_detail = sanitize_telemetry(f"{type(exc).__name__}: {str(exc)}")
            snapshot.completed_at = utcnow()
            db.commit()
        return reconciliation_summary(db)
    finally:
        db.close()


def reconciliation_summary(db, user_id: str | None = None) -> dict:
    stmt = select(ServiceInvocation).where(ServiceInvocation.service == "llm")
    if user_id:
        stmt = stmt.where(ServiceInvocation.user_id == user_id)
    rows = list(db.scalars(stmt).all())
    correlated = [
        row for row in rows
        if row.correlation_id and row.langsmith_run_id
    ]
    matched = sum(row.langsmith_export_status == "exported" for row in correlated)
    pending = sum(row.langsmith_export_status == "pending" for row in correlated)
    delayed = sum(row.langsmith_export_status == "delayed" for row in correlated)
    disabled = sum(row.langsmith_export_status == "disabled" for row in correlated)
    demo = sum(row.workload == "demo" or row.is_demo for row in rows)
    backfilled = sum(bool((row.invocation_metadata or {}).get("langsmith_backfill_submitted_at")) for row in correlated)
    latest = db.scalar(
        select(TraceReconciliationRun)
        .order_by(TraceReconciliationRun.started_at.desc())
        .limit(1)
    )
    coverage = round(matched / len(correlated) * 100, 1) if correlated else 0.0
    if not get_settings().langsmith_connected:
        status = "disabled"
        message = "LangSmith export is disabled; local provider evidence remains available."
    elif latest and latest.status == "unavailable":
        status = "unavailable"
        message = "The last LangSmith ingestion check failed; local telemetry is still live and will retry."
    elif delayed:
        status = "degraded"
        message = f"{delayed} provider attempt(s) have not appeared in LangSmith within the export window."
    elif pending:
        status = "pending"
        message = f"{pending} provider attempt(s) are waiting for LangSmith ingestion."
    elif correlated:
        status = "healthy"
        message = (
            f"Every correlated Mesh provider attempt has a matching LangSmith LLM span; "
            f"{backfilled} historical attempt(s) were restored from the durable local ledger."
            if backfilled else
            "Every correlated Mesh provider attempt has a matching LangSmith LLM span."
        )
    else:
        status = "neutral"
        message = "Provider attempts created before correlation IDs remain in the total but cannot be matched retrospectively."
    return {
        "status": status,
        "message": message,
        "provider_attempts": len(rows),
        "correlated_attempts": len(correlated),
        "matched_spans": matched,
        "pending_attempts": pending,
        "delayed_attempts": delayed,
        "disabled_attempts": disabled,
        "demo_attempts": demo,
        "backfilled_attempts": backfilled,
        "coverage": coverage,
        "unmatched_remote_spans": latest.unmatched_remote_spans if latest else 0,
        "last_checked_at": latest.completed_at if latest else None,
        "agent_status": latest.status if latest else "not_run",
        "agent_error": latest.error_detail if latest else None,
        "span_name": PROVIDER_SPAN_NAME,
        "project": get_settings().langsmith_project,
        "explanation": (
            "Reconciliation compares each correlation-enabled local provider attempt with the LangSmith LLM span "
            "carrying the same run or correlation ID. It exposes delayed or missing trace ingestion instead of "
            "silently assuming that export succeeded. When connectivity returns, missing durable attempts are "
            "exported as explicitly labeled historical backfill spans and reconciled normally."
        ),
    }
