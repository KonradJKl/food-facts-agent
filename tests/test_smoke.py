import httpx2
import pytest


def test_package_is_importable() -> None:
    import food_facts_agent

    assert food_facts_agent.__name__ == "food_facts_agent"


def test_real_http_requests_are_blocked() -> None:
    with pytest.raises(RuntimeError, match="network"):
        httpx2.get("https://world.openfoodfacts.org/")
