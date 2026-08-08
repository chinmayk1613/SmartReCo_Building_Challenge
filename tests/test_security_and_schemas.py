import pytest
from pydantic import ValidationError

from app.schemas import ALLOWED_EVENT_TYPES, EventInput, ProductInput, RegisterInput
from app.security import hash_password, token_hash, verify_password


@pytest.mark.parametrize("password", ["123", "short", "abcdefghi", "", "ninechars"])
def test_short_passwords_are_rejected(password):
    with pytest.raises(ValueError):
        hash_password(password)


@pytest.mark.parametrize("password", ["LongEnough1!", "a secure passphrase", "0123456789", "CorrectHorseBatteryStaple"])
def test_valid_password_round_trip(password):
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(hashed, password)
    assert not verify_password(hashed, password + "x")


def test_token_hash_is_stable_and_non_plaintext():
    assert token_hash("secret") == token_hash("secret")
    assert token_hash("secret") != "secret"
    assert token_hash("secret") != token_hash("different")


@pytest.mark.parametrize("event_type", sorted(ALLOWED_EVENT_TYPES))
def test_all_documented_event_types_validate(event_type):
    value = EventInput(event_id="event-123456", event_type=event_type)
    assert value.event_type == event_type


@pytest.mark.parametrize("event_type", ["mousemove", "password_typed", "unknown", "", "scroll_pixel"])
def test_unknown_event_types_are_rejected(event_type):
    with pytest.raises(ValidationError):
        EventInput(event_id="event-123456", event_type=event_type)


@pytest.mark.parametrize("price", [-1, -100, 100001])
def test_invalid_product_prices_are_rejected(price):
    with pytest.raises(ValidationError):
        ProductInput(title="Valid product", slug="valid-product", description="A sufficiently long valid product description.", category="AI", price=price)


@pytest.mark.parametrize("slug", ["Has Spaces", "UPPERCASE", "bad_slug", "-leading", "trailing-"])
def test_invalid_slugs_are_rejected(slug):
    with pytest.raises(ValidationError):
        ProductInput(title="Valid product", slug=slug, description="A sufficiently long valid product description.", category="AI", price=1)


def test_register_email_is_validated():
    with pytest.raises(ValidationError):
        RegisterInput(email="not-an-email", display_name="Name", password="LongEnough1!")

