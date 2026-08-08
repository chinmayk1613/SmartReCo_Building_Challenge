import re


_STOPWORDS = {"the", "a", "an", "for", "with", "and", "course", "courses", "to"}


def normalize_topic(value: str | None) -> str:
    """Canonical topic/category key used by signals and every ranking path."""
    if not value:
        return "general"
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return "_".join(token for token in tokens if token not in _STOPWORDS)[:160] or "general"


def topics_overlap(left: str | None, right: str | None) -> bool:
    left_key = normalize_topic(left)
    right_key = normalize_topic(right)
    return left_key != "general" and right_key != "general" and (
        left_key in right_key or right_key in left_key
    )
