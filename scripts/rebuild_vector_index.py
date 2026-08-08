import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.vector_store import get_vector_store, rebuild_vector_index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely rebuild and verify SmartReco's Qdrant index from active SQL products."
    )
    parser.add_argument("--verify-only", action="store_true", help="Inspect compatibility without modifying Qdrant")
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="Explicitly allow a local deterministic-hash rebuild (never reported as semantic)",
    )
    parser.add_argument("--json", action="store_true", help="Print the machine-readable report")
    parser.add_argument("--batch-size", type=int, default=32, help="Embedding request batch size")
    args = parser.parse_args()

    store = None
    if args.verify_only:
        store = get_vector_store()
        report = store.verify_index().as_dict()
    else:
        report = rebuild_vector_index(require_semantic=not args.allow_degraded, batch_size=max(1, args.batch_size))
        store = get_vector_store()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"SmartReco vector index: {report.get('status', 'UNAVAILABLE')}")
        print(report.get("message", "No verification message was returned."))
        print(
            f"SQL active products: {report.get('expected_sql_count', 'not checked')} | "
            f"Qdrant vectors: {report.get('qdrant_count', 'not checked')} | "
            f"Embedding calls: {report.get('embedding_calls', 0)}"
        )
        if report.get("rebuild_required"):
            print("Action: rebuild required; incompatible vectors will not be used for semantic retrieval.")
        if report.get("error_code"):
            print(f"Status code: {report['error_code']}")
    if report.get("status") == "UNAVAILABLE" or report.get("rebuild_required"):
        if store:
            store.client.close()
        raise SystemExit(2)
    if store:
        store.client.close()


if __name__ == "__main__":
    main()
