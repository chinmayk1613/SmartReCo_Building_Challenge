from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEAD_REVISION = "20260807_0006"


def _upgrade_head(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def _check_schema(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def test_fresh_alembic_chain_reaches_head_and_is_repeatable(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-smartreco.db"
    runtime_qdrant_path = tmp_path / "qdrant"
    production_database = (PROJECT_ROOT / ".smartreco" / "smartreco.db").resolve()
    assert database_path.resolve() != production_database

    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "QDRANT_PATH": str(runtime_qdrant_path),
            "SCHEDULER_ENABLED": "false",
        }
    )

    first_upgrade = _upgrade_head(environment)
    assert database_path.is_file(), first_upgrade.stderr

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert revision == (HEAD_REVISION,)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {
            "users",
            "products",
            "activity_events",
            "behavioral_signals",
            "user_interest_profiles",
            "recommendation_runs",
            "recommendations",
            "recommendation_items",
            "service_invocations",
            "trace_reconciliation_runs",
            "product_vector_state",
            "deliveries",
            "delivery_attempts",
            "audit_logs",
        }.issubset(tables)

        def columns(table: str) -> set[str]:
            return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}

        assert {"scope_key", "context_product_id"}.issubset(columns("recommendation_runs"))
        assert {"recommendation_type", "context_product_id"}.issubset(columns("recommendations"))
        assert {"confidence_score", "interest_likelihood"}.issubset(columns("recommendation_items"))
        assert {
            "correlation_id",
            "workload",
            "provider_receipt",
            "langsmith_trace_id",
            "langsmith_run_id",
            "langsmith_export_status",
            "is_demo",
        }.issubset(columns("service_invocations"))
        assert {
            "embedding_provider",
            "embedding_model",
            "vector_dimension",
            "index_schema_version",
        }.issubset(columns("product_vector_state"))

    second_upgrade = _upgrade_head(environment)
    assert second_upgrade.returncode == 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)

    schema_check = _check_schema(environment)
    assert "No new upgrade operations detected" in schema_check.stdout
