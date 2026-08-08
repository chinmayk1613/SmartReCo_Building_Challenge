from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Iterator
from uuid import uuid4
import re

from app.config import get_settings
from app.db import SessionLocal
from app.models import ServiceInvocation, utcnow


_SENSITIVE_KEY = re.compile(r"(password|secret|token|authorization|cookie|csrf|api[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"(?:rsk_|lsv2_|Bearer\s+)[A-Za-z0-9._-]+", re.I)


def sanitize_telemetry(value):
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else sanitize_telemetry(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_telemetry(item) for item in value[:100]]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)[:2000]
    return value


def estimate_cost(model: str | None, input_tokens: int = 0, output_tokens: int = 0) -> float | None:
    """Return a transparent estimate; unknown prices stay unknown instead of looking free."""
    if model in {
        "tencent/hy3",
        "minimax/m2-her",
        "deterministic-local-fallback",
        "deterministic-provider-fallback",
        "deterministic-validation-fallback",
    }:
        return 0.0
    pricing_per_million = {
        "openai/gpt-4o-mini": (0.15, 0.60),
        "openai/gpt-5.4-mini": (0.75, 4.50),
    }
    if model in pricing_per_million:
        input_rate, output_rate = pricing_per_million[model]
        return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 8)
    return None


@dataclass
class InvocationHandle:
    id: str
    started: float
    correlation_id: str


def begin_invocation(
    service: str,
    operation: str,
    *,
    user_id: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    metadata: dict | None = None,
    workload: str = "runtime",
    attempt_number: int | None = None,
    is_demo: bool = False,
) -> InvocationHandle:
    correlation_id = str(uuid4())
    export_status = "not_applicable"
    if service == "llm":
        export_status = "demo" if is_demo else "pending" if get_settings().langsmith_connected else "disabled"
    row = ServiceInvocation(
        correlation_id=correlation_id,
        user_id=user_id,
        recommendation_run_id=run_id,
        service=service,
        operation=operation,
        model=model,
        workload=workload,
        attempt_number=attempt_number,
        langsmith_export_status=export_status,
        is_demo=is_demo,
        invocation_metadata=sanitize_telemetry(metadata or {}),
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        return InvocationHandle(row.id, perf_counter(), correlation_id)
    finally:
        db.close()


def finish_invocation(
    handle: InvocationHandle,
    *,
    status: str = "succeeded",
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    metadata: dict | None = None,
    error: Exception | str | None = None,
    provider_receipt: str | None = None,
    failover_decision: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        row = db.get(ServiceInvocation, handle.id)
        if not row:
            return
        row.status = status
        row.latency_ms = max(0, round((perf_counter() - handle.started) * 1000))
        row.completed_at = utcnow()
        row.input_tokens = input_tokens
        row.output_tokens = output_tokens
        row.provider_receipt = provider_receipt or row.provider_receipt
        row.failover_decision = failover_decision or row.failover_decision
        if model:
            row.model = model
        row.estimated_cost = estimate_cost(row.model, input_tokens, output_tokens)
        if metadata:
            row.invocation_metadata = sanitize_telemetry({**(row.invocation_metadata or {}), **metadata})
        if error:
            row.error_code = type(error).__name__ if isinstance(error, Exception) else "ServiceError"
            row.error_detail = sanitize_telemetry(str(error))
        db.commit()
    finally:
        db.close()


def link_langsmith_span(
    handle: InvocationHandle,
    *,
    trace_id: str,
    run_id: str,
    run_url: str | None = None,
) -> None:
    """Persist the exact LangSmith span identity before the provider request begins."""
    db = SessionLocal()
    try:
        row = db.get(ServiceInvocation, handle.id)
        if not row:
            return
        row.langsmith_trace_id = trace_id
        row.langsmith_run_id = run_id
        row.langsmith_run_url = run_url
        row.langsmith_export_status = "pending"
        db.commit()
    finally:
        db.close()


@contextmanager
def observed_call(
    service: str,
    operation: str,
    *,
    user_id: str | None = None,
    run_id: str | None = None,
    model: str | None = None,
    metadata: dict | None = None,
    workload: str = "runtime",
) -> Iterator[InvocationHandle]:
    handle = begin_invocation(
        service,
        operation,
        user_id=user_id,
        run_id=run_id,
        model=model,
        metadata=metadata,
        workload=workload,
    )
    try:
        yield handle
    except Exception as exc:
        finish_invocation(handle, status="failed", error=exc)
        raise
    else:
        finish_invocation(handle)
