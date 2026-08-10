from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


ALLOWED_EVENT_TYPES = {
    "page_viewed",
    "category_selected",
    "search_submitted",
    "product_impression",
    "product_viewed",
    "product_clicked",
    "active_dwell",
    "added_to_cart",
    "cart_viewed",
    "removed_from_cart",
    "recommendation_impression",
    "recommendation_clicked",
    "recommendation_dismissed",
    "purchase_started",
    "purchase_completed",
}


class RegisterInput(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=200)


class DigestEmailInput(BaseModel):
    email: EmailStr


class ProductInput(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)
    description: str = Field(min_length=20, max_length=10000)
    category: str = Field(min_length=2, max_length=120)
    level: str = Field(default="All levels", max_length=40)
    skills: list[str] = Field(default_factory=list, max_length=30)
    outcomes: list[str] = Field(default_factory=list, max_length=30)
    price: float = Field(ge=0, le=100000)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    duration_minutes: int = Field(default=60, ge=1, le=100000)
    rating: float = Field(default=4.5, ge=0, le=5)
    popularity: float = Field(default=0, ge=0)
    status: Literal["draft", "active", "archived"] = "active"


class EventInput(BaseModel):
    event_id: str = Field(min_length=8, max_length=64)
    event_type: str
    session_id: str | None = Field(default=None, max_length=64)
    product_id: str | None = None
    search_query: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=120)
    duration_ms: int | None = Field(default=None, ge=0, le=1_800_000)
    page_path: str | None = Field(default=None, max_length=500)
    recommendation_id: str | None = None
    occurred_at: datetime | None = None
    properties: dict = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_TYPES:
            raise ValueError("Unsupported event type")
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc)


class EventBatchInput(BaseModel):
    events: list[EventInput] = Field(min_length=1, max_length=100)


class RecommendationCopyItem(BaseModel):
    product_id: str
    reason: str = Field(min_length=10, max_length=500)


class RecommendationCopy(BaseModel):
    headline: str = Field(min_length=5, max_length=180)
    narrative: str = Field(min_length=20, max_length=1200)
    item_copy: list[RecommendationCopyItem] = Field(min_length=1, max_length=5)
