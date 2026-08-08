"""Make one synthetic provider attempt to verify Mesh/LangSmith correlation."""

from app.config import get_settings
from app.services.observability import begin_invocation, finish_invocation
from app.services.recommendation import execute_traced_mesh_attempt


def main() -> None:
    model = get_settings().mesh_free_model
    handle = begin_invocation(
        "llm",
        "synthetic_provider_reconciliation_check",
        model=model,
        metadata={"provider": "Mesh API", "attempt": 1, "synthetic": True},
        workload="verification",
        attempt_number=1,
    )
    profile = {
        "profile_version": "synthetic-qa",
        "primary_intent": "agentic workflows",
        "secondary_intents": [],
        "recent_searches": [],
        "journey_stage": "exploration",
    }
    products = [{
        "id": "synthetic-agentic-workflows",
        "title": "Agentic Workflow Foundations",
        "category": "Agentic AI",
        "level": "Intermediate",
        "description": "Design reliable stateful AI workflows with retrieval, validation, and recovery.",
        "skills": ["workflow design", "retrieval", "validation"],
        "outcomes": ["Build a reliable grounded workflow"],
        "default_reason": "It connects workflow design with grounded retrieval and validation.",
    }]
    try:
        result = execute_traced_mesh_attempt(
            profile=profile,
            products=products,
            model=model,
            concise=True,
            handle=handle,
            user_id="synthetic-qa",
            recommendation_run_id=None,
            attempt_number=1,
            workload="verification",
            langsmith_extra={
                "tags": ["smartreco", "synthetic-qa", "provider-attempt"],
                "metadata": {
                    "telemetry_schema": "provider-attempt-v1",
                    "local_invocation_id": handle.id,
                    "local_correlation_id": handle.correlation_id,
                    "user_id": "synthetic-qa",
                    "attempt_number": 1,
                    "workload": "verification",
                    "ls_provider": "mesh_api",
                    "ls_model_name": model,
                },
            },
        )
    except Exception as exc:
        finish_invocation(handle, status="failed", error=exc, failover_decision="stop")
        raise
    finish_invocation(
        handle,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        provider_receipt=result.request_id,
        failover_decision="not_needed",
    )
    print({
        "local_invocation_id": handle.id,
        "correlation_id": handle.correlation_id,
        "model": result.model,
        "tokens": result.input_tokens + result.output_tokens,
        "provider_receipt": result.request_id,
    })


if __name__ == "__main__":
    main()
