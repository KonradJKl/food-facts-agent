import inspect
import json
from pathlib import Path

import pytest

from food_facts_agent import tools
from food_facts_agent.off_client import OFFUnavailableError, ProductNotFoundError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeOFFClient:
    """Serves recorded responses instead of calling Open Food Facts."""

    def __init__(self, *product_fixtures: str, search_fixture: str = "search_empty.json") -> None:
        self.products = {}
        for name in product_fixtures:
            product = load_fixture(name)["product"]
            self.products[product["code"]] = product
        search = load_fixture(search_fixture)
        self.search_result = {"count": search["count"], "products": search["hits"]}
        self.search_calls: list[dict] = []
        self.error: Exception | None = None

    def get_product(self, barcode: str) -> dict:
        if self.error:
            raise self.error
        if barcode not in self.products:
            raise ProductNotFoundError(barcode)
        return self.products[barcode]

    def search_products(
        self,
        query: str,
        country: str | None = "en:germany",
        category: str | None = None,
        page_size: int = 20,
    ) -> dict:
        self.search_calls.append(
            {"query": query, "country": country, "category": category, "page_size": page_size}
        )
        if self.error:
            raise self.error
        return self.search_result


def test_get_product_returns_compact_product() -> None:
    client = FakeOFFClient("product_found.json")

    result = tools.get_product(client, "3017624010701")

    assert result == {
        "barcode": "3017624010701",
        "name": "Nutella",
        "brands": "Ferrero",
        "quantity": "400.0 g",
        "serving_size": None,
        "nutriscore": "e",
        "nova_group": None,
        "nutrition_per_100g": {
            "energy_kcal": 539,
            "fat_g": 30.9,
            "saturated_fat_g": 10.6,
            "carbohydrates_g": 57.5,
            "sugars_g": 56.3,
            "fiber_g": None,
            "proteins_g": 6.3,
            "salt_g": 0.1075,
        },
        "ingredients": (
            "sugar, palm oil, hazelnuts, skimmed milk powder, fat reduced cocoa, "
            "emulsifier, vanillin"
        ),
        "allergens": ["en:nuts"],
        "traces": ["en:milk", "en:nuts", "en:soybeans"],
        "ingredients_analysis": [
            "en:palm-oil-content-unknown",
            "en:vegan-status-unknown",
            "en:vegetarian-status-unknown",
        ],
        "labels": ["en:no-gluten"],
        "countries": ["en:france", "en:united-kingdom"],
        "url": "https://world.openfoodfacts.org/product/3017624010701",
    }


def test_get_product_reports_missing_data_as_none() -> None:
    client = FakeOFFClient("product_sparse.json")

    result = tools.get_product(client, "4251680801722")

    assert result["name"] == "Sylter Müsli"
    assert result["nutriscore"] == "unknown"
    assert result["ingredients"] is None
    assert result["ingredients_analysis"] is None
    assert set(result["nutrition_per_100g"]) == set(tools.NUTRIENTS)
    assert all(value is None for value in result["nutrition_per_100g"].values())


def test_get_product_lets_client_errors_through() -> None:
    with pytest.raises(ProductNotFoundError):
        tools.get_product(FakeOFFClient(), "4099999999994")


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ({"product_name": "My muesli", "product_name_de": "Beere"}, "My muesli"),
        ({"product_name": "  ", "product_name_de": " Müsli "}, "Müsli"),
        ({"product_name": None, "product_name_en": "Muesli"}, "Muesli"),
        ({}, None),
    ],
)
def test_first_skips_missing_and_blank_values(product: dict, expected: str | None) -> None:
    assert tools._first(product, "product_name", "product_name_de", "product_name_en") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ferrero", "Ferrero"),
        ("Ferrero, Nutella", "Ferrero, Nutella"),
        (["Knusperone", " H. & J. Brüggen", " Aldi"], "Knusperone, H. & J. Brüggen, Aldi"),
        ("", None),
        ([], None),
        (None, None),
    ],
)
def test_brands_accepts_string_and_list(
    value: str | list[str] | None, expected: str | None
) -> None:
    assert tools._brands(value) == expected


def test_search_products_returns_compact_hits() -> None:
    client = FakeOFFClient(search_fixture="search_muesli_de.json")

    result = tools.search_products(client, "Müsli")

    assert result["total_matches"] == 1374
    assert len(result["products"]) == 10
    assert result["products"][0] == {
        "barcode": "4260188784710",
        "name": "My muesli",
        "brands": "My muesli",
        "quantity": "85g",
        "nutriscore": "a",
        "nutrition_per_100g": {
            "energy_kcal": 383,
            "fat_g": 11,
            "saturated_fat_g": 1.5,
            "carbohydrates_g": 56.1,
            "sugars_g": 12.6,
            "fiber_g": 9,
            "proteins_g": 10.4,
            "salt_g": 0.03,
        },
    }


def test_search_products_keeps_hits_without_nutrition() -> None:
    client = FakeOFFClient(search_fixture="search_muesli_de.json")

    hit = tools.search_products(client, "Müsli")["products"][1]

    assert hit["name"] == "Sylter Müsli"
    assert all(value is None for value in hit["nutrition_per_100g"].values())


def test_search_products_cleans_brand_lists() -> None:
    client = FakeOFFClient(search_fixture="search_muesli_de.json")

    hit = tools.search_products(client, "Müsli")["products"][4]

    assert hit["brands"] == "Knusperone, H. & J. Brüggen, Aldi"


def test_search_products_returns_empty_list() -> None:
    result = tools.search_products(FakeOFFClient(), "xyzzyquux")

    assert result == {"total_matches": 0, "products": []}


@pytest.mark.parametrize(("worldwide", "country"), [(False, "en:germany"), (True, None)])
def test_search_products_limits_to_germany_unless_worldwide(
    worldwide: bool, country: str | None
) -> None:
    client = FakeOFFClient()

    tools.search_products(client, "Müsli", category="en:mueslis", worldwide=worldwide)

    assert client.search_calls == [
        {"query": "Müsli", "country": country, "category": "en:mueslis", "page_size": 20}
    ]


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda tool: tool["name"])
def test_tool_schema_follows_strict_mode_rules(tool: dict) -> None:
    schema = tool["input_schema"]

    assert tool["strict"] is True
    assert tool["description"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert all(prop["description"] for prop in schema["properties"].values())


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda tool: tool["name"])
def test_tool_schema_matches_python_function(tool: dict) -> None:
    schema = tool["input_schema"]
    signature = inspect.signature(getattr(tools, tool["name"]))
    params = {name: p for name, p in signature.parameters.items() if name != "client"}

    assert set(schema["properties"]) == set(params)
    assert set(schema["required"]) == {
        name for name, p in params.items() if p.default is inspect.Parameter.empty
    }


def test_run_tool_returns_result_as_json() -> None:
    client = FakeOFFClient(search_fixture="search_muesli_de.json")

    content, is_error = tools.run_tool(client, "search_products", {"query": "Müsli"})

    assert is_error is False
    assert json.loads(content)["total_matches"] == 1374
    assert "Sylter Müsli" in content


def test_run_tool_passes_optional_arguments() -> None:
    client = FakeOFFClient()

    tools.run_tool(client, "search_products", {"query": "Müsli", "worldwide": True})

    assert client.search_calls[0]["country"] is None


@pytest.mark.parametrize(
    ("name", "tool_input", "error", "message"),
    [
        (
            "get_product",
            {"barcode": "4099999999994"},
            None,
            "No product with barcode 4099999999994",
        ),
        (
            "get_product",
            {"barcode": "123"},
            ValueError("A barcode has 8 to 14 digits, got '123'"),
            "8 to 14 digits",
        ),
        (
            "search_products",
            {"query": "Müsli"},
            OFFUnavailableError("https://search.openfoodfacts.org/search failed 3 times"),
            "failed 3 times",
        ),
        ("get_weather", {"city": "Berlin"}, None, "Unknown tool: get_weather"),
    ],
)
def test_run_tool_reports_errors_to_the_model(
    name: str, tool_input: dict, error: Exception | None, message: str
) -> None:
    client = FakeOFFClient()
    client.error = error

    content, is_error = tools.run_tool(client, name, tool_input)

    assert is_error is True
    assert message in json.loads(content)["error"]


def test_run_tool_does_not_hide_wrong_arguments() -> None:
    with pytest.raises(TypeError):
        tools.run_tool(FakeOFFClient(), "get_product", {"code": "3017624010701"})
