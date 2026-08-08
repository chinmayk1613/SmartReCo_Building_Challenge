import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CatalogOutbox, Product
from app.schemas import ProductInput


def catalog_revision(db: Session) -> str:
    """Stable revision that changes for every catalog mutation, including archives."""
    rows = db.execute(select(Product.id, Product.version, Product.status).order_by(Product.id)).all()
    encoded = json.dumps([(row.id, row.version, row.status) for row in rows], separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_product_text(product: Product) -> str:
    return "\n".join(
        [
            f"Title: {product.title}",
            f"Category: {product.category}",
            f"Level: {product.level}",
            f"Skills: {', '.join(product.skills or [])}",
            f"Outcomes: {', '.join(product.outcomes or [])}",
            f"Description: {product.description}",
            f"Price: {float(product.price):.2f} {product.currency}",
        ]
    )


def checksum_for_payload(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def product_payload(product: Product) -> dict:
    return {
        "product_id": product.id,
        "title": product.title,
        "slug": product.slug,
        "description": product.description,
        "category": product.category,
        "level": product.level,
        "skills": product.skills or [],
        "outcomes": product.outcomes or [],
        "price": float(product.price),
        "currency": product.currency,
        "status": product.status,
        "version": product.version,
    }


def create_product(db: Session, data: ProductInput) -> Product:
    values = data.model_dump()
    checksum = checksum_for_payload(values)
    product = Product(**values, content_checksum=checksum)
    db.add(product)
    db.flush()
    db.add(
        CatalogOutbox(
            product_id=product.id,
            event_type="product.upsert",
            product_version=product.version,
            payload=product_payload(product),
        )
    )
    return product


def update_product(db: Session, product: Product, data: ProductInput) -> Product:
    values = data.model_dump()
    for field, value in values.items():
        setattr(product, field, value)
    product.version += 1
    product.content_checksum = checksum_for_payload(values)
    db.flush()
    db.add(
        CatalogOutbox(
            product_id=product.id,
            event_type="product.delete" if product.status == "archived" else "product.upsert",
            product_version=product.version,
            payload=product_payload(product),
        )
    )
    return product


def archive_product(db: Session, product: Product) -> Product:
    """Soft-delete a product while emitting the vector-index delete event."""
    product.status = "archived"
    product.version += 1
    payload = product_payload(product)
    product.content_checksum = checksum_for_payload(payload)
    db.flush()
    db.add(
        CatalogOutbox(
            product_id=product.id,
            event_type="product.delete",
            product_version=product.version,
            payload=payload,
        )
    )
    return product


def get_active_products(db: Session) -> list[Product]:
    return list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.popularity.desc())).all())
