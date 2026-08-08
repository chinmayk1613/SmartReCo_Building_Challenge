from __future__ import annotations

from statistics import mean, median


def calculate_ai_efficiency(
    *,
    raw_events: int,
    mesh_copy_llm_calls: int,
    live_mesh_observed: bool,
) -> dict:
    """Compare measured provider attempts with the declared naive per-event baseline."""
    naive_calls = max(0, int(raw_events))
    actual_calls = max(0, int(mesh_copy_llm_calls))
    reduction = None
    if live_mesh_observed and naive_calls:
        reduction = round((naive_calls - actual_calls) / naive_calls * 100, 2)
    return {
        "naive_theoretical_llm_calls": naive_calls,
        "measured_mesh_copy_llm_calls": actual_calls,
        "llm_call_reduction_percent": reduction,
        "reduction_claim_status": "measured" if reduction is not None else "not_run_live",
    }


def latency_summary(values_ms: list[float]) -> dict:
    """Informational timing summary with no machine-dependent pass/fail threshold."""
    values = sorted(max(0.0, float(value)) for value in values_ms)
    if not values:
        return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    p95_index = min(len(values) - 1, max(0, round(0.95 * len(values) + 0.5) - 1))
    return {
        "count": len(values),
        "mean_ms": round(mean(values), 2),
        "p50_ms": round(median(values), 2),
        "p95_ms": round(values[p95_index], 2),
        "max_ms": round(values[-1], 2),
    }
