"""Verify the configured Mesh model through SmartReco's real output schema."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Product
from app.services.mesh import mesh_gateway


def main() -> None:
    settings = get_settings()
    if not settings.mesh_api_key:
        raise SystemExit("MESH_API_KEY is not configured")
    db = SessionLocal()
    try:
        products = list(db.scalars(select(Product).where(Product.status == "active").limit(3)).all())
    finally:
        db.close()
    candidates = [
        {
            "id": product.id,
            "title": product.title,
            "category": product.category,
            "description": product.description,
            "level": product.level,
            "price": float(product.price),
            "default_reason": f"It matches a recent interest in {product.category}.",
        }
        for product in products
    ]
    result = mesh_gateway.generate_copy(
        {
            "primary_intent": "python development",
            "secondary_intents": [{"topic": "large language models", "strength": 0.8}],
            "recent_searches": ["Python Zero to Hero", "LLM foundations"],
            "journey_stage": "exploration",
        },
        candidates,
    )
    print(
        {
            "model": result.model,
            "fallback": result.used_fallback,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "headline": result.data.headline,
            "recommended_items": len(result.data.item_copy),
        }
    )


if __name__ == "__main__":
    main()
