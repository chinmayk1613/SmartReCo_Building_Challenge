from dataclasses import asdict, dataclass
from datetime import timedelta

from qdrant_client import QdrantClient, models as qmodels
from sqlalchemy import and_, or_, select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import CatalogOutbox, Product, ProductVectorState, utcnow
from app.services.catalog import canonical_product_text, product_payload
from app.services.mesh import EMBEDDING_DIMENSION, mesh_gateway
from app.services.observability import begin_invocation, finish_invocation


SEMANTIC = "SEMANTIC"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
INDEX_SCHEMA_VERSION = "smartreco-product-v1"


@dataclass(frozen=True)
class SemanticSearchResult:
    items: list[dict]
    status: str
    provider: str
    error_code: str | None = None


@dataclass(frozen=True)
class EmbeddingDescriptor:
    provider: str
    model: str
    dimension: int
    schema_version: str = INDEX_SCHEMA_VERSION

    def payload(self) -> dict:
        return {
            "embedding_provider": self.provider,
            "embedding_model": self.model,
            "vector_dimension": self.dimension,
            "index_schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class VectorIndexVerification:
    status: str
    compatible: bool
    rebuild_required: bool
    expected_sql_count: int
    qdrant_count: int
    missing_product_ids: list[str]
    stale_product_ids: list[str]
    incompatible_product_ids: list[str]
    descriptor: dict
    message: str
    error_code: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def expected_embedding_descriptor() -> EmbeddingDescriptor:
    settings = get_settings()
    return EmbeddingDescriptor(
        provider="mesh_api" if mesh_gateway.embeddings_enabled else "deterministic-local",
        model=settings.mesh_embedding_model if mesh_gateway.embeddings_enabled else "deterministic-hash-v1",
        dimension=EMBEDDING_DIMENSION,
    )


def evaluate_vector_snapshot(
    active_products: list[Product],
    states: dict[str, ProductVectorState],
    point_payloads: dict[str, dict],
    collection_dimension: int | None,
    descriptor: EmbeddingDescriptor,
) -> VectorIndexVerification:
    """Pure compatibility decision shared by runtime checks, rebuilds, and tests."""
    active_by_id = {product.id: product for product in active_products}
    active_ids = set(active_by_id)
    point_ids = set(point_payloads)
    missing = sorted(active_ids - point_ids)
    stale = sorted(point_ids - active_ids)
    incompatible: set[str] = set()
    expected_metadata = descriptor.payload()
    if collection_dimension != descriptor.dimension:
        incompatible.update(active_ids)
    for product_id, product in active_by_id.items():
        state = states.get(product_id)
        if (
            not state
            or state.status != "synced"
            or state.product_version != product.version
            or state.content_checksum != product.content_checksum
            or state.embedding_provider != descriptor.provider
            or state.embedding_model != descriptor.model
            or state.vector_dimension != descriptor.dimension
            or state.index_schema_version != descriptor.schema_version
        ):
            incompatible.add(product_id)
        payload = point_payloads.get(product_id) or {}
        if any(payload.get(key) != value for key, value in expected_metadata.items()):
            incompatible.add(product_id)
        if payload.get("version") != product.version or payload.get("content_checksum") != product.content_checksum:
            incompatible.add(product_id)
    incompatible_ids = sorted(incompatible)
    compatible = not missing and not stale and not incompatible_ids
    expected_status = SEMANTIC if descriptor.provider == "mesh_api" else DEGRADED
    return VectorIndexVerification(
        status=expected_status if compatible else UNAVAILABLE,
        compatible=compatible,
        rebuild_required=not compatible,
        expected_sql_count=len(active_ids),
        qdrant_count=len(point_ids),
        missing_product_ids=missing,
        stale_product_ids=stale,
        incompatible_product_ids=incompatible_ids,
        descriptor=descriptor.payload(),
        message=(
            f"Vector index verified for {len(active_ids)} active SQL products using "
            f"{descriptor.provider}/{descriptor.model}."
            if compatible else
            "Vector index provenance or contents do not match the configured embedding mode; rebuild required."
        ),
        error_code=None if compatible else "VECTOR_INDEX_REBUILD_REQUIRED",
    )


class ProductVectorStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client = (
            QdrantClient(url=self.settings.qdrant_url, api_key=self.settings.qdrant_api_key)
            if self.settings.qdrant_url
            else QdrantClient(path=self.settings.qdrant_path)
        )
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = {item.name for item in self.client.get_collections().collections}
        if self.settings.qdrant_collection not in collections:
            self.client.create_collection(
                collection_name=self.settings.qdrant_collection,
                vectors_config=qmodels.VectorParams(size=EMBEDDING_DIMENSION, distance=qmodels.Distance.COSINE),
            )

    def upsert(
        self,
        product: Product,
        vector: list[float],
        descriptor: EmbeddingDescriptor | None = None,
    ) -> None:
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(f"Expected {EMBEDDING_DIMENSION}-dimension embedding, received {len(vector)}")
        descriptor = descriptor or expected_embedding_descriptor()
        payload = {
            **product_payload(product),
            **descriptor.payload(),
            "content_checksum": product.content_checksum,
        }
        self.client.upsert(
            collection_name=self.settings.qdrant_collection,
            points=[qmodels.PointStruct(id=product.id, vector=vector, payload=payload)],
            wait=True,
        )

    def delete(self, product_id: str) -> None:
        self.client.delete(
            collection_name=self.settings.qdrant_collection,
            points_selector=qmodels.PointIdsList(points=[product_id]),
            wait=True,
        )

    def _collection_dimension(self) -> int | None:
        info = self.client.get_collection(self.settings.qdrant_collection)
        vectors = info.config.params.vectors
        return int(getattr(vectors, "size", 0) or 0) or None

    def _point_payloads(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.settings.qdrant_collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                result[str(point.id)] = point.payload or {}
            if offset is None:
                break
        return result

    def verify_index(self, descriptor: EmbeddingDescriptor | None = None) -> VectorIndexVerification:
        descriptor = descriptor or expected_embedding_descriptor()
        db = SessionLocal()
        try:
            active = list(db.scalars(select(Product).where(Product.status == "active")).all())
            states = {state.product_id: state for state in db.scalars(select(ProductVectorState)).all()}
            return evaluate_vector_snapshot(
                active,
                states,
                self._point_payloads(),
                self._collection_dimension(),
                descriptor,
            )
        except Exception as exc:
            return VectorIndexVerification(
                status=UNAVAILABLE,
                compatible=False,
                rebuild_required=True,
                expected_sql_count=0,
                qdrant_count=0,
                missing_product_ids=[],
                stale_product_ids=[],
                incompatible_product_ids=[],
                descriptor=descriptor.payload(),
                message="Vector index could not be inspected safely; rebuild or Qdrant recovery is required.",
                error_code=type(exc).__name__,
            )
        finally:
            db.close()

    def search_with_status(self, query: str, limit: int = 30) -> SemanticSearchResult:
        descriptor = expected_embedding_descriptor()
        verification = self.verify_index(descriptor)
        if not verification.compatible:
            return SemanticSearchResult([], UNAVAILABLE, descriptor.model, verification.error_code)
        try:
            vector = mesh_gateway.embed([query])[0]
            response = self.client.query_points(
                collection_name=self.settings.qdrant_collection,
                query=vector,
                query_filter=qmodels.Filter(
                    must=[qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="active"))]
                ),
                limit=limit,
                with_payload=True,
            )
            return SemanticSearchResult(
                items=[{"product_id": str(point.id), "semantic_score": float(point.score), "payload": point.payload or {}} for point in response.points],
                status=verification.status,
                provider=descriptor.model,
            )
        except Exception as exc:
            return SemanticSearchResult([], UNAVAILABLE, descriptor.model, type(exc).__name__)

    def search(self, query: str, limit: int = 30) -> list[dict]:
        """Compatibility API; new recommendation paths use status-aware retrieval."""
        return self.search_with_status(query, limit=limit).items


_vector_store: ProductVectorStore | None = None


def get_vector_store() -> ProductVectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = ProductVectorStore()
    return _vector_store


def rebuild_vector_index(*, require_semantic: bool = True, batch_size: int = 32) -> dict:
    """Rebuild Qdrant from authoritative SQL after all embeddings are generated safely.

    The default path refuses the deterministic fallback. SQL catalog rows are never
    deleted or rewritten by this operation.
    """
    descriptor = expected_embedding_descriptor()
    if require_semantic and descriptor.provider != "mesh_api":
        return {
            "status": UNAVAILABLE,
            "rebuild_performed": False,
            "semantic_mode": False,
            "embedding_calls": 0,
            "error_code": "MESH_EMBEDDINGS_UNAVAILABLE",
            "message": "Mesh embeddings are not enabled; no Qdrant data was changed.",
            "descriptor": descriptor.payload(),
        }
    db = SessionLocal()
    try:
        active = list(db.scalars(select(Product).where(Product.status == "active").order_by(Product.id)).all())
        vectors: list[list[float]] = []
        embedding_calls = 0
        try:
            for start in range(0, len(active), max(1, batch_size)):
                batch = active[start:start + max(1, batch_size)]
                if not batch:
                    continue
                generated = mesh_gateway.embed([canonical_product_text(product) for product in batch])
                embedding_calls += 1
                if len(generated) != len(batch) or any(len(vector) != descriptor.dimension for vector in generated):
                    raise ValueError("Mesh returned an unexpected embedding count or vector dimension")
                vectors.extend(generated)
        except Exception as exc:
            return {
                "status": UNAVAILABLE,
                "rebuild_performed": False,
                "semantic_mode": descriptor.provider == "mesh_api",
                "embedding_calls": embedding_calls,
                "error_code": type(exc).__name__,
                "message": "Embedding generation failed before Qdrant was modified.",
                "descriptor": descriptor.payload(),
            }

        store = get_vector_store()
        if store._collection_dimension() != descriptor.dimension:
            store.client.delete_collection(store.settings.qdrant_collection)
            store.client.create_collection(
                collection_name=store.settings.qdrant_collection,
                vectors_config=qmodels.VectorParams(size=descriptor.dimension, distance=qmodels.Distance.COSINE),
            )
        existing_ids = set(store._point_payloads())
        active_ids = {product.id for product in active}
        stale_ids = sorted(existing_ids - active_ids)
        for product, vector in zip(active, vectors, strict=True):
            store.upsert(product, vector, descriptor)
            state = db.get(ProductVectorState, product.id) or ProductVectorState(
                product_id=product.id,
                point_id=product.id,
                product_version=product.version,
                content_checksum=product.content_checksum,
                embedding_provider=descriptor.provider,
                embedding_model=descriptor.model,
                vector_dimension=descriptor.dimension,
                index_schema_version=descriptor.schema_version,
            )
            state.product_version = product.version
            state.content_checksum = product.content_checksum
            state.embedding_provider = descriptor.provider
            state.embedding_model = descriptor.model
            state.vector_dimension = descriptor.dimension
            state.index_schema_version = descriptor.schema_version
            state.status = "synced"
            state.synced_at = utcnow()
            state.last_error = None
            db.add(state)
        for stale_id in stale_ids:
            store.delete(stale_id)
            state = db.get(ProductVectorState, stale_id)
            if state:
                state.status = "deleted"
                state.synced_at = utcnow()
        db.commit()
        verification = store.verify_index(descriptor)
        return {
            **verification.as_dict(),
            "rebuild_performed": True,
            "semantic_mode": verification.status == SEMANTIC,
            "embedding_calls": embedding_calls,
            "removed_stale_vectors": len(stale_ids),
        }
    finally:
        db.close()


def sync_pending_catalog(limit: int = 25) -> dict:
    processed = failed = reclaimed = superseded = 0
    db = SessionLocal()
    try:
        now = utcnow()
        claimable = or_(
            and_(CatalogOutbox.status.in_(["pending", "failed"]), CatalogOutbox.available_at <= now),
            and_(CatalogOutbox.status == "processing", CatalogOutbox.lease_expires_at < now),
        )
        candidate_ids = list(db.scalars(select(CatalogOutbox.id).where(claimable).order_by(CatalogOutbox.created_at).limit(limit)).all())
        store = get_vector_store()
        descriptor = expected_embedding_descriptor()
        for record_id in candidate_ids:
            was_processing = db.scalar(select(CatalogOutbox.status).where(CatalogOutbox.id == record_id)) == "processing"
            claimed = db.execute(
                update(CatalogOutbox)
                .where(CatalogOutbox.id == record_id, claimable)
                .values(
                    status="processing",
                    attempt_count=CatalogOutbox.attempt_count + 1,
                    lease_expires_at=utcnow() + timedelta(minutes=5),
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                continue
            db.commit()
            record = db.get(CatalogOutbox, record_id)
            if was_processing:
                reclaimed += 1
            handle = begin_invocation(
                "rag", "catalog_vector_sync", metadata={"outbox_id": record.id, "product_version": record.product_version}
            )
            try:
                product = db.get(Product, record.product_id)
                if not product:
                    raise ValueError("Product no longer exists")
                if record.product_version != product.version:
                    record.status = "succeeded"
                    record.processed_at = utcnow()
                    record.lease_expires_at = None
                    record.last_error = "Superseded by a newer product version"
                    superseded += 1
                    finish_invocation(handle, metadata={"outcome": "superseded", "current_version": product.version})
                    db.commit()
                    continue
                if record.event_type == "product.delete" or product.status == "archived":
                    store.delete(product.id)
                else:
                    vector = mesh_gateway.embed([canonical_product_text(product)])[0]
                    store.upsert(product, vector)
                state = db.get(ProductVectorState, product.id) or ProductVectorState(
                    product_id=product.id,
                    product_version=product.version,
                    point_id=product.id,
                    embedding_provider=descriptor.provider,
                    embedding_model=descriptor.model,
                    vector_dimension=descriptor.dimension,
                    index_schema_version=descriptor.schema_version,
                    content_checksum=product.content_checksum,
                )
                state.product_version = product.version
                state.content_checksum = product.content_checksum
                state.embedding_provider = descriptor.provider
                state.embedding_model = descriptor.model
                state.vector_dimension = descriptor.dimension
                state.index_schema_version = descriptor.schema_version
                state.status = "deleted" if product.status == "archived" else "synced"
                state.synced_at = utcnow()
                state.last_error = None
                db.add(state)
                record.status = "succeeded"
                record.processed_at = utcnow()
                record.lease_expires_at = None
                processed += 1
                finish_invocation(
                    handle,
                    metadata={
                        "semantic_status": SEMANTIC if mesh_gateway.embeddings_enabled else DEGRADED,
                        "embedding_provider": state.embedding_model,
                    },
                )
            except Exception as exc:  # retry state must survive provider failures
                failed += 1
                record.status = "failed"
                record.last_error = str(exc)[:2000]
                record.available_at = utcnow() + timedelta(seconds=min(300, 2 ** record.attempt_count))
                record.lease_expires_at = None
                finish_invocation(handle, status="failed", error=exc, metadata={"semantic_status": UNAVAILABLE})
            db.commit()
        return {"processed": processed, "failed": failed, "reclaimed": reclaimed, "superseded": superseded}
    finally:
        db.close()
