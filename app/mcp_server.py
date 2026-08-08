from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Product, Recommendation, UserInterestProfile
from app.services.recommendation import profile_to_dict, retrieve_and_rank, saved_or_purchased_product_ids
from app.services.mcp_catalog import get_verified_product_details as verified_product_details
from app.services.signals import signal_summary
from app.services.observability import observed_call
from app.config import get_settings


mcp = FastMCP("SmartReco Read Tools")


def _require_trusted_local_boundary() -> None:
    """Fail closed if this identity-bearing MCP server is configured for non-local use."""
    if not get_settings().mcp_trusted_local_only:
        raise PermissionError(
            "Behavioral MCP tools require an authenticated principal outside trusted local stdio mode"
        )


@mcp.tool()
def get_behavior_profile(user_id: str) -> dict:
    """Return a user's structured behavioral profile without credentials or session data."""
    _require_trusted_local_boundary()
    with observed_call("mcp", "get_behavior_profile", user_id=user_id):
        db = SessionLocal()
        try:
            profile = db.get(UserInterestProfile, user_id)
            return profile_to_dict(profile) if profile else {"error": "profile_not_found"}
        finally:
            db.close()


@mcp.tool()
def search_product_catalog(user_id: str, limit: int = 5) -> list[dict]:
    """Retrieve and rank verified active catalog products for an existing profile."""
    _require_trusted_local_boundary()
    with observed_call("mcp", "search_product_catalog", user_id=user_id):
        db = SessionLocal()
        try:
            profile = db.get(UserInterestProfile, user_id)
            if not profile:
                return []
            products, _metrics = retrieve_and_rank(profile_to_dict(profile), limit=max(1, min(limit, 10)))
            return products
        finally:
            db.close()


@mcp.tool()
def get_verified_product_details(product_ids: list[str]) -> list[dict]:
    """Return authoritative SQL product details for the supplied IDs."""
    return verified_product_details(product_ids)


@mcp.tool()
def get_recent_behavioral_signals(user_id: str, limit: int = 10) -> list[dict]:
    """Return the latest derived behavior signals for a user, capped at ten."""
    _require_trusted_local_boundary()
    with observed_call("mcp", "get_recent_behavioral_signals", user_id=user_id):
        db = SessionLocal()
        try:
            return [
            {
                "signal_type": signal.signal_type,
                "topic": signal.topic,
                "product_id": signal.product_id,
                "strength": signal.strength,
                "reason": signal.reason,
                "observed_at": signal.last_observed_at.isoformat(),
            }
            for signal in signal_summary(db, user_id, limit=max(1, min(limit, 10)))
            ]
        finally:
            db.close()


@mcp.tool()
def get_personalized_course_candidates(user_id: str, current_product_id: str | None = None, limit: int = 3) -> list[dict]:
    """Return grounded top-course candidates while excluding saved, purchased, and current courses."""
    _require_trusted_local_boundary()
    with observed_call("mcp", "get_personalized_course_candidates", user_id=user_id):
        db = SessionLocal()
        try:
            profile = db.get(UserInterestProfile, user_id)
            if not profile:
                return []
            payload = profile_to_dict(profile)
            excluded = saved_or_purchased_product_ids(user_id)
            if current_product_id:
                excluded.add(current_product_id)
            payload["excluded_product_ids"] = sorted(excluded)
            products, _metrics = retrieve_and_rank(payload, limit=max(1, min(limit, 3)))
            return products
        finally:
            db.close()


if __name__ == "__main__":
    init_db()
    mcp.run()
