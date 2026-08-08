from sqlalchemy import select

from app.db import SessionLocal
from app.models import Product
from app.services.observability import observed_call


def get_verified_product_details(
    product_ids: list[str],
    *,
    user_id: str | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """Governed read-only catalog boundary shared by FastMCP and LangGraph."""
    unique_ids = list(dict.fromkeys(product_ids))
    with observed_call(
        "mcp",
        "get_verified_product_details",
        user_id=user_id,
        run_id=run_id,
        metadata={"product_count": len(unique_ids), "read_only": True},
    ):
        db = SessionLocal()
        try:
            products = list(
                db.scalars(
                    select(Product).where(Product.id.in_(unique_ids), Product.status == "active")
                ).all()
            )
            lookup = {product.id: product for product in products}
            return [
                {
                    "id": product.id,
                    "title": product.title,
                    "description": product.description,
                    "category": product.category,
                    "level": product.level,
                    "price": float(product.price),
                    "currency": product.currency,
                    "version": product.version,
                }
                for product_id in unique_ids
                if (product := lookup.get(product_id)) is not None
            ]
        finally:
            db.close()
