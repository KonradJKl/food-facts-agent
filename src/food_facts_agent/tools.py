import json
from typing import Any

from anthropic.types.beta import BetaToolParam

from food_facts_agent.off_client import OFFClient, OFFError

PRODUCT_PAGE_URL = "https://world.openfoodfacts.org/product/{barcode}"

# Open Food Facts normalizes the *_100g values to grams, energy-kcal_100g to kcal.
NUTRIENTS = {
    "energy_kcal": "energy-kcal_100g",
    "fat_g": "fat_100g",
    "saturated_fat_g": "saturated-fat_100g",
    "carbohydrates_g": "carbohydrates_100g",
    "sugars_g": "sugars_100g",
    "fiber_g": "fiber_100g",
    "proteins_g": "proteins_100g",
    "salt_g": "salt_100g",
}
SEARCH_PAGE_SIZE = 20

# No regex for the barcode: with strict mode the model could not pass on a wrong barcode
# from the user and might change digits instead. The client rejects it with a clear error.
TOOLS: list[BetaToolParam] = [
    {
        "name": "get_product",
        "description": (
            "Look up one food product in Open Food Facts by its barcode (EAN/UPC, 8 to 14 "
            "digits). Call this whenever the user gives a barcode, and after search_products "
            "when you need ingredients, allergens or other details of a candidate. A null "
            "value means Open Food Facts has no data for that field. Returns an error if the "
            "barcode is unknown."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "barcode": {
                    "type": "string",
                    "description": "The barcode digits as the user gave them, without spaces.",
                },
            },
            "required": ["barcode"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_products",
        "description": (
            "Search Open Food Facts for food products by name, brand or product type, for "
            'example "Seitenbacher Müsli" or "Haferflocken". Call this when the user names a '
            "product or a product type instead of giving a barcode. Searches products sold in "
            "Germany unless worldwide is true. Returns the total number of matches and up to "
            f"{SEARCH_PAGE_SIZE} products ordered by relevance, not by any nutrient, so a "
            "comparison only covers the products returned."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Product name, brand or product type in German or English, plain words."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Open Food Facts category tag such as en:mueslis or en:breakfast-cereals. "
                        "Only set it if you are sure the tag exists; a wrong tag returns no "
                        "results."
                    ),
                },
                "worldwide": {
                    "type": "boolean",
                    "description": "Search all countries instead of only Germany. Default false.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]


def get_product(client: OFFClient, barcode: str) -> dict[str, Any]:
    product = client.get_product(barcode)
    return {
        **_summary(product),
        "serving_size": _first(product, "serving_size"),
        "nova_group": product.get("nova_group"),
        "ingredients": _first(
            product, "ingredients_text_de", "ingredients_text_en", "ingredients_text"
        ),
        "allergens": product.get("allergens_tags"),
        "traces": product.get("traces_tags"),
        "ingredients_analysis": product.get("ingredients_analysis_tags"),
        "labels": product.get("labels_tags"),
        "countries": product.get("countries_tags"),
        "url": PRODUCT_PAGE_URL.format(barcode=product["code"]),
    }


def search_products(
    client: OFFClient, query: str, category: str | None = None, worldwide: bool = False
) -> dict[str, Any]:
    result = client.search_products(
        query,
        country=None if worldwide else "en:germany",
        category=category,
        page_size=SEARCH_PAGE_SIZE,
    )
    return {
        "total_matches": result["count"],
        "products": [_summary(hit) for hit in result["products"]],
    }


TOOL_FUNCTIONS = {"get_product": get_product, "search_products": search_products}


def run_tool(client: OFFClient, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool call and return the tool_result content and whether it is an error.

    Errors the model can react to come back as content. A TypeError from wrong arguments
    is left to propagate because strict mode should make it impossible.
    """
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False), True
    try:
        result = function(client, **tool_input)
    except (OFFError, ValueError) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False), True
    return json.dumps(result, ensure_ascii=False), False


def _summary(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "barcode": product["code"],
        # product_name is the name on the product page; the German one can be just a
        # variant such as "Beere".
        "name": _first(product, "product_name", "product_name_de", "product_name_en"),
        "brands": _brands(product.get("brands")),
        "quantity": _first(product, "quantity"),
        "nutriscore": product.get("nutrition_grades"),
        "nutrition_per_100g": _nutrition(product.get("nutriments")),
    }


def _first(product: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _brands(value: str | list[str] | None) -> str | None:
    # The product API sends "A, B", Search-a-licious sends ["A", " B"].
    if isinstance(value, str):
        value = value.split(",")
    names = [name.strip() for name in value or [] if name.strip()]
    return ", ".join(names) or None


def _nutrition(nutriments: dict[str, Any] | None) -> dict[str, float | None]:
    nutriments = nutriments or {}
    return {name: nutriments.get(key) for name, key in NUTRIENTS.items()}
