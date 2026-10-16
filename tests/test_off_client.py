import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

from food_facts_agent.off_client import (
    OFFClient,
    OFFResponseError,
    OFFUnavailableError,
    ProductNotFoundError,
    RateLimiter,
    _search_query,
)

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://world.openfoodfacts.org/api/v3/product/3017624010701.json"
PARAMS = {"fields": "code,product_name"}
DAY = 24 * 3600

Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeClock:
    """Stands in for both time.time and time.sleep; sleeping moves the clock forward."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler: Handler, cache_dir: Path) -> tuple[OFFClient, FakeClock]:
    clock = FakeClock()
    client = OFFClient(
        contact_email="test@example.com",
        cache_dir=cache_dir,
        transport=httpx2.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
    )
    return client, clock


def counting_handler(response: httpx2.Response) -> tuple[Handler, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return response

    return handler, requests


def get_product_json(client: OFFClient, params: dict[str, str] = PARAMS) -> dict | None:
    return client._get_json(URL, params, max_age=DAY, limiter=client._product_limiter)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("email", ["", "you-at-example.com"])
def test_client_requires_contact_email(email: str) -> None:
    with pytest.raises(ValueError):
        OFFClient(contact_email=email)


def test_request_sends_user_agent_and_params(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, _ = make_client(handler, tmp_path)

    client._request(URL, PARAMS, client._product_limiter)

    assert requests[0].headers["User-Agent"] == "FoodFactsAgent/0.1 (test@example.com)"
    assert requests[0].url.params["fields"] == "code,product_name"


def test_request_retries_503_with_backoff(tmp_path: Path) -> None:
    statuses = iter([503, 503, 200])
    client, clock = make_client(lambda request: httpx2.Response(next(statuses)), tmp_path)

    response = client._request(URL, PARAMS, client._product_limiter)

    assert response.status_code == 200
    assert len(clock.sleeps) == 2
    assert 1.0 <= clock.sleeps[0] <= 1.5
    assert 2.0 <= clock.sleeps[1] <= 2.5


def test_request_gives_up_after_max_attempts(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(503))
    client, clock = make_client(handler, tmp_path)

    with pytest.raises(OFFUnavailableError, match="HTTP 503"):
        client._request(URL, PARAMS, client._product_limiter)
    assert len(requests) == 3
    assert len(clock.sleeps) == 2


def test_request_retries_timeouts(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    client, clock = make_client(handler, tmp_path)

    with pytest.raises(OFFUnavailableError, match="ReadTimeout"):
        client._request(URL, PARAMS, client._product_limiter)
    assert len(clock.sleeps) == 2


def test_request_uses_retry_after_header(tmp_path: Path) -> None:
    responses = iter([httpx2.Response(429, headers={"Retry-After": "7"}), httpx2.Response(200)])
    client, clock = make_client(lambda request: next(responses), tmp_path)

    client._request(URL, PARAMS, client._product_limiter)

    assert clock.sleeps == [7.0]


@pytest.mark.parametrize("status", [302, 400, 404])
def test_request_does_not_retry_other_status_codes(tmp_path: Path, status: int) -> None:
    client, clock = make_client(lambda request: httpx2.Response(status), tmp_path)

    assert client._request(URL, PARAMS, client._product_limiter).status_code == status
    assert clock.sleeps == []


def test_get_json_serves_second_call_from_cache(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={"code": "3017624010701"}))
    client, _ = make_client(handler, tmp_path)

    first = get_product_json(client)
    second = get_product_json(client)

    assert first == second == {"code": "3017624010701"}
    assert len(requests) == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_get_json_uses_params_in_cache_key(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, _ = make_client(handler, tmp_path)

    get_product_json(client, {"fields": "code"})
    get_product_json(client, {"fields": "code,brands"})

    assert len(requests) == 2


def test_get_json_refetches_expired_entry(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, clock = make_client(handler, tmp_path)

    get_product_json(client)
    clock.now += DAY + 1
    get_product_json(client)

    assert len(requests) == 2


@pytest.mark.parametrize("status", [302, 404])
def test_get_json_caches_not_found_as_none(tmp_path: Path, status: int) -> None:
    handler, requests = counting_handler(httpx2.Response(status))
    client, _ = make_client(handler, tmp_path)

    assert get_product_json(client) is None
    assert get_product_json(client) is None
    assert len(requests) == 1


def test_get_json_ignores_corrupt_cache_file(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={"code": "1"}))
    client, _ = make_client(handler, tmp_path)
    get_product_json(client)
    client._cache_path(URL, PARAMS).write_text("{broken", encoding="utf-8")

    assert get_product_json(client) == {"code": "1"}
    assert len(requests) == 2


@pytest.mark.parametrize(
    "response",
    [httpx2.Response(200, text="<html>maintenance</html>"), httpx2.Response(400, json={})],
)
def test_get_json_rejects_bad_responses_without_caching(
    tmp_path: Path, response: httpx2.Response
) -> None:
    client, _ = make_client(lambda request: response, tmp_path)

    with pytest.raises(OFFResponseError):
        get_product_json(client)
    assert list(tmp_path.iterdir()) == []


def test_get_json_waits_once_the_product_limit_is_reached(tmp_path: Path) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, clock = make_client(handler, tmp_path)

    for i in range(16):
        get_product_json(client, {"fields": f"field_{i}"})

    assert len(requests) == 16
    assert clock.sleeps == [60.0]


def test_get_json_cache_hits_do_not_count_against_the_limit(tmp_path: Path) -> None:
    handler, _ = counting_handler(httpx2.Response(200, json={}))
    client, clock = make_client(handler, tmp_path)

    for _ in range(30):
        get_product_json(client)

    assert clock.sleeps == []


def test_rate_limiter_allows_a_burst_up_to_the_limit() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 60.0, clock, clock.sleep)

    for _ in range(3):
        limiter.wait()

    assert clock.sleeps == []


def test_rate_limiter_sleeps_until_the_oldest_call_leaves_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 60.0, clock, clock.sleep)
    for _ in range(3):
        limiter.wait()
        clock.now += 10

    limiter.wait()

    assert clock.sleeps == [30.0]


def test_rate_limiter_forgets_calls_outside_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 60.0, clock, clock.sleep)
    for _ in range(3):
        limiter.wait()
    clock.now += 60

    for _ in range(3):
        limiter.wait()

    assert clock.sleeps == []


def test_get_product_requests_fields_and_returns_product(tmp_path: Path) -> None:
    handler, requests = counting_handler(
        httpx2.Response(200, json=load_fixture("product_found.json"))
    )
    client, _ = make_client(handler, tmp_path)

    product = client.get_product("3017624010701")

    assert requests[0].url.path == "/api/v3/product/3017624010701.json"
    assert "nutriments" in requests[0].url.params["fields"].split(",")
    assert product["product_name"] == "Nutella"
    assert product["nutriments"]["sugars_100g"] == 56.3
    assert "palm oil" in product["ingredients_text_en"]


def test_get_product_returns_sparse_product_unchanged(tmp_path: Path) -> None:
    fixture = load_fixture("product_sparse.json")
    client, _ = make_client(lambda request: httpx2.Response(200, json=fixture), tmp_path)

    product = client.get_product("4251680801722")

    assert product == fixture["product"]
    assert "nutriments" not in product


@pytest.mark.parametrize(
    "response",
    [httpx2.Response(404, json=load_fixture("product_not_found.json")), httpx2.Response(302)],
)
def test_get_product_raises_not_found(tmp_path: Path, response: httpx2.Response) -> None:
    client, _ = make_client(lambda request: response, tmp_path)

    with pytest.raises(ProductNotFoundError) as excinfo:
        client.get_product("4099999999994")
    assert excinfo.value.barcode == "4099999999994"


@pytest.mark.parametrize(
    "barcode", ["", "1234567", "301762401070x", "../../search", " 3017624010701"]
)
def test_get_product_rejects_invalid_barcode_without_request(tmp_path: Path, barcode: str) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, _ = make_client(handler, tmp_path)

    with pytest.raises(ValueError):
        client.get_product(barcode)
    assert requests == []


def test_search_products_sends_query_and_returns_hits(tmp_path: Path) -> None:
    handler, requests = counting_handler(
        httpx2.Response(200, json=load_fixture("search_muesli_de.json"))
    )
    client, _ = make_client(handler, tmp_path)

    result = client.search_products("Müsli")

    params = requests[0].url.params
    assert requests[0].url.host == "search.openfoodfacts.org"
    assert params["q"] == 'Müsli countries_tags:"en:germany"'
    assert params["langs"] == "de,en"
    assert params["page_size"] == "20"
    assert "nutriments" in params["fields"].split(",")
    assert result["count"] == 1374
    assert len(result["products"]) == 10
    assert result["products"][0]["code"] == "4260188784710"


def test_search_products_returns_empty_list(tmp_path: Path) -> None:
    fixture = load_fixture("search_empty.json")
    client, _ = make_client(lambda request: httpx2.Response(200, json=fixture), tmp_path)

    assert client.search_products("xyzzyquux") == {"count": 0, "products": []}


def test_search_products_has_its_own_rate_limit(tmp_path: Path) -> None:
    fixture = load_fixture("search_empty.json")
    client, clock = make_client(lambda request: httpx2.Response(200, json=fixture), tmp_path)

    for i in range(11):
        client.search_products(f"query{i}")

    assert clock.sleeps == [60.0]


@pytest.mark.parametrize(
    ("text", "country", "category", "expected"),
    [
        ("muesli", None, None, "muesli"),
        (
            "muesli",
            "en:germany",
            "en:mueslis",
            'muesli categories_tags:"en:mueslis" countries_tags:"en:germany"',
        ),
        ("Ben & Jerry's", None, None, "Ben Jerry s"),
        ('nutella OR brands:"x"', None, None, "nutella brands x"),
        (
            'a) AND (countries_tags:"en:france"',
            "en:germany",
            None,
            'a countries_tags en france countries_tags:"en:germany"',
        ),
    ],
)
def test_search_query_keeps_text_out_of_the_query_syntax(
    text: str, country: str | None, category: str | None, expected: str
) -> None:
    assert _search_query(text, country, category) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "?!"},
        {"query": "AND OR"},
        {"query": "muesli", "country": 'en:germany" OR x'},
        {"query": "muesli", "category": "mueslis"},
    ],
)
def test_search_products_rejects_bad_input_without_request(tmp_path: Path, kwargs: dict) -> None:
    handler, requests = counting_handler(httpx2.Response(200, json={}))
    client, _ = make_client(handler, tmp_path)

    with pytest.raises(ValueError):
        client.search_products(**kwargs)
    assert requests == []
