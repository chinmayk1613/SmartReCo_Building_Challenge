import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.evaluation import evaluate_recommendations
from app.services.vector_store import get_vector_store


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SmartReco's offline or opt-in live semantic ranking evaluation.")
    parser.add_argument("--json", action="store_true", help="Print the complete machine-readable report")
    parser.add_argument("--semantic", action="store_true", help="Require real Mesh embeddings and a verified semantic Qdrant index")
    args = parser.parse_args()
    report = evaluate_recommendations(semantic=args.semantic)
    if args.json:
        print(json.dumps(report, indent=2))
        get_vector_store().client.close()
        return
    if report["status"] != "ok":
        print(f"SmartReco evaluation not run: {report.get('reason', 'Seed the active catalog first.')}")
        print(f"Semantic status: {report.get('semantic_status', 'UNAVAILABLE')}")
        get_vector_store().client.close()
        raise SystemExit(2)
    summary = report["summary"]
    print(f"SmartReco {report['evaluation_mode'].replace('_', ' ')} recommendation evaluation")
    print(f"Semantic status: {report['semantic_status']}")
    print(f"Journeys: {summary['journey_count']}")
    print("\nSystem comparison (same catalog, journeys, labels, exclusions, and K=3)")
    print(f"{'System':<20} {'P@3':>7} {'R@3':>7} {'NDCG@3':>9} {'Diversity':>10} {'Coverage':>9} {'Separation':>11}")
    for row in report["comparison"]:
        print(
            f"{row['system']:<20} {row['mean_precision_at_k']:>7.3f} {row['mean_recall_at_k']:>7.3f} "
            f"{row['mean_ndcg_at_3']:>9.3f} {row['mean_diversity']:>10.3f} "
            f"{row['catalog_coverage']:>9.3f} {row['personalization_separation']:>11.3f}"
        )
    print("")
    print(f"Precision@3: {summary['mean_precision_at_k']:.3f} | Recall@3: {summary['mean_recall_at_k']:.3f} | NDCG@3: {summary['mean_ndcg_at_3']:.3f}")
    print(f"Diversity: {summary['mean_diversity']:.3f} | Coverage: {summary['catalog_coverage']:.3f} | Personalization separation: {summary['personalization_separation']:.3f}")
    print(f"Exclusion pass: {summary['exclusion_pass_rate']:.3f} | Hallucinated ID rate: {summary['hallucinated_product_id_rate']:.3f}")
    cost = "not calculated" if summary["estimated_ai_cost"] is None else f"{summary['estimated_ai_cost']:.2f}"
    print(f"Mesh embedding calls: {summary['mesh_embedding_calls']} | Recommendation-copy LLM calls: {summary['recommendation_copy_llm_calls']}")
    print(f"Mesh generation invoked: {summary['mesh_generation_invoked']} | Estimated AI cost: {cost} | Mean rank latency: {summary['mean_latency_ms']:.2f} ms")
    get_vector_store().client.close()


if __name__ == "__main__":
    main()
