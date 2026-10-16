import hashlib
import json
import random
import re
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

PRODUCT_URL = "https://world.openfoodfacts.org/api/v3/product/{barcode}.json"
SEARCH_URL = "https://search.openfoodfacts.org/search"

PRODUCT_FIELDS = (
    "code",
    "product_name",
    "product_name_de",
    "product_name_en",
    "brands",
    "quantity",
    "serving_size",
    "categories_tags",
    "countries_tags",
    "labels_tags",
    "ingredients_text",
    "ingredients_text_de",
    "ingredients_text_en",
    "allergens_tags",
    "traces_tags",
    "ingredients_analysis_tags",
    "nutriments",
    "nutrition_grades",
    "nova_group",
)
PRODUCT_MAX_AGE = 7 * 24 * 3600

SEARCH_FIELDS = (
    "code",
    "product_name",
    "product_name_de",
    "product_name_en",
    "brands",
    "quantity",
    "nutriments",
    "nutrition_grades",
    "countries_tags",
)
SEARCH_LANGS = "de,en"
SEARCH_MAX_AGE = 24 * 3600
LUCENE_OPERATORS = {"AND", "OR", "NOT"}
TAG_PATTERN = re.compile(r"[a-z]{2}:[\w-]+")

PRODUCT_READS_PER_MINUTE = 15
SEARCHES_PER_MINUTE = 10

MAX_ATTEMPTS = 3
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 30.0
# 302 means the barcode belongs to another database such as Open Beauty Facts.
NOT_FOUND_STATUS_CODES = {302, 404}


class OFFError(Exception):
    pass


class ProductNotFoundError(OFFError):
    def __init__(self, barcode: str) -> None:
        super().__init__(f"No product with barcode {barcode} in Open Food Facts")
        self.barcode = barcode


class OFFUnavailableError(OFFError):
    """All retries failed with 429, 5xx, a timeout or a network error."""


class OFFResponseError(OFFError):
    """The API answered with an unexpected status code or a body that is not valid JSON."""


class RateLimiter:
    """Allows at most max_calls in any window of `period` seconds and sleeps otherwise."""

    def __init__(
        self,
        max_calls: int,
        period: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def wait(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            self._sleep(self._calls.popleft() + self.period - now)
        self._calls.append(self._clock())


class OFFClient:
    def __init__(
        self,
        contact_email: str,
        cache_dir: Path = Path(".cache/off"),
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if "@" not in contact_email:
            raise ValueError("Open Food Facts requires a contact email in the User-Agent")
        self.cache_dir = cache_dir
        self._sleep = sleep
        self._clock = clock
        self._product_limiter = RateLimiter(PRODUCT_READS_PER_MINUTE, 60.0, clock, sleep)
        self._search_limiter = RateLimiter(SEARCHES_PER_MINUTE, 60.0, clock, sleep)
        self._http = httpx2.Client(
            headers={"User-Agent": f"FoodFactsAgent/0.1 ({contact_email})"},
            timeout=httpx2.Timeout(10.0, connect=5.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def get_product(self, barcode: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]{8,14}", barcode):
            raise ValueError(f"A barcode has 8 to 14 digits, got {barcode!r}")
        url = PRODUCT_URL.format(barcode=barcode)
        params = {"fields": ",".join(PRODUCT_FIELDS)}
        body = self._get_json(url, params, PRODUCT_MAX_AGE, self._product_limiter)
        if body is None:
            raise ProductNotFoundError(barcode)
        return body["product"]

    def search_products(
        self,
        query: str,
        country: str | None = "en:germany",
        category: str | None = None,
        page_size: int = 20,
    ) -> dict[str, Any]:
        params = {
            "q": _search_query(query, country, category),
            "langs": SEARCH_LANGS,
            "page_size": str(page_size),
            "fields": ",".join(SEARCH_FIELDS),
        }
        body = self._get_json(SEARCH_URL, params, SEARCH_MAX_AGE, self._search_limiter)
        if body is None:
            raise OFFResponseError("Search-a-licious answered with 404 or a redirect")
        return {"count": body["count"], "products": body["hits"]}

    def _get_json(
        self, url: str, params: dict[str, str], max_age: float, limiter: RateLimiter
    ) -> dict[str, Any] | None:
        """Return the JSON body, or None if Open Food Facts does not know the resource."""
        path = self._cache_path(url, params)
        entry = self._read_cache(path, max_age)
        if entry is None:
            response = self._request(url, params, limiter)
            entry = {
                "fetched_at": self._clock(),
                "url": url,
                "params": params,
                "status_code": response.status_code,
                "body": _parse_body(response),
            }
            _write_json_atomic(path, entry)
        return entry["body"]

    def _cache_path(self, url: str, params: dict[str, str]) -> Path:
        key = json.dumps([url, params], sort_keys=True)
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _read_cache(self, path: Path, max_age: float) -> dict[str, Any] | None:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self._clock() - entry["fetched_at"] > max_age:
            return None
        return entry

    def _request(self, url: str, params: dict[str, str], limiter: RateLimiter) -> httpx2.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after = None
            limiter.wait()
            try:
                response = self._http.get(url, params=params)
            except httpx2.TransportError as exc:
                problem = type(exc).__name__
            else:
                if response.status_code not in RETRY_STATUS_CODES:
                    return response
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After")
            if attempt == MAX_ATTEMPTS:
                raise OFFUnavailableError(f"{url} failed {MAX_ATTEMPTS} times, last: {problem}")
            self._sleep(_retry_delay(attempt, retry_after))


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    # Retry-After can also be an HTTP date; only the seconds form is used here.
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), MAX_RETRY_AFTER)
    return 2 ** (attempt - 1) + random.uniform(0, 0.5)


def _search_query(text: str, country: str | None, category: str | None) -> str:
    # Search-a-licious turns `field:"value"` terms into filters and the remaining words into a
    # full-text search. Punctuation and operators are dropped so the text cannot add filters
    # or change how the query is parsed. An explicit AND would put words into the filter part.
    parts = [
        word
        for word in re.sub(r"[^\w\s]", " ", text).split()
        if word.upper() not in LUCENE_OPERATORS
    ]
    if not parts:
        raise ValueError(f"Search text has no words to search for: {text!r}")
    for field, tag in (("categories_tags", category), ("countries_tags", country)):
        if tag is None:
            continue
        if not TAG_PATTERN.fullmatch(tag):
            raise ValueError(f"Expected a taxonomy tag like 'en:germany', got {tag!r}")
        parts.append(f'{field}:"{tag}"')
    return " ".join(parts)


def _parse_body(response: httpx2.Response) -> dict[str, Any] | None:
    if response.status_code in NOT_FOUND_STATUS_CODES:
        return None
    if response.status_code != 200:
        raise OFFResponseError(f"{response.url} returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise OFFResponseError(f"{response.url} returned a body that is not JSON") from exc


def _write_json_atomic(path: Path, entry: dict[str, Any]) -> None:
    # An interrupted run leaves at most a stray .tmp file, never half a cache entry.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
