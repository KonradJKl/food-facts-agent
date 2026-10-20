import json
import re
from pathlib import Path

import pytest
from run_eval import (
    allowed_numbers,
    answer_language,
    answer_numbers,
    cited_barcodes,
    evaluate,
    failed_patterns,
    is_grounded,
    known_barcodes,
)

from food_facts_agent.agent import AgentResult, ToolCall

QUESTIONS_PATH = Path(__file__).parents[1] / "eval" / "questions.json"
NUTELLA = "4008400401621"
UNKNOWN_BARCODE = "4099999999994"


def tool_call(name: str, tool_input: dict, result: dict, is_error: bool = False) -> ToolCall:
    return ToolCall(name, tool_input, json.dumps(result, ensure_ascii=False), is_error)


SEARCH = tool_call(
    "search_products",
    {"query": "Nutella"},
    {
        "total_matches": 80,
        "products": [{"barcode": f"40084{i:08d}", "name": "Nutella"} for i in range(20)],
    },
)
PRODUCT = tool_call(
    "get_product",
    {"barcode": NUTELLA},
    {
        "barcode": NUTELLA,
        "name": "Nutella",
        "quantity": "450 g",
        "nutrition_per_100g": {"sugars_g": 56.3, "salt_g": 0.107, "fiber_g": None},
        "ingredients": "Zucker, Palmöl, HASELNÜSSE (13%). MAGERMILCHPULVER (8,7%), Kakao (7,4%)",
        "allergens": ["en:milk", "en:nuts"],
    },
)
NOT_FOUND = tool_call(
    "get_product",
    {"barcode": UNKNOWN_BARCODE},
    {"error": f"No product with barcode {UNKNOWN_BARCODE} in Open Food Facts"},
    is_error=True,
)


def question(**overrides: object) -> dict:
    return {
        "id": "palm-oil-nutella",
        "question": "Enthält Nutella Palmöl?",
        "expect_tools": True,
        "expect_barcodes": [NUTELLA],
        "must_match": ["Palmöl"],
        "must_not_match": [],
        "language": "de",
        **overrides,
    }


def test_allowed_numbers_cover_values_texts_and_returned_products() -> None:
    allowed = allowed_numbers("Wie viel Zucker haben 30 g Nutella?", [SEARCH, PRODUCT])

    assert {56.3, 0.107, 450.0, 13.0, 8.7, 7.4, 80.0, 20.0, 100.0, 30.0} <= allowed


def test_allowed_numbers_do_not_turn_empty_lists_into_zero() -> None:
    sparse = tool_call(
        "get_product",
        {"barcode": "4251680801722"},
        {"nutrition_per_100g": {"fiber_g": None}, "allergens": [], "traces": []},
    )

    assert 0.0 not in allowed_numbers("Wie viele Ballaststoffe hat 4251680801722?", [sparse])


def test_allowed_numbers_ignore_booleans() -> None:
    call = tool_call("get_product", {"barcode": NUTELLA}, {"vegan": True})

    assert 1.0 not in allowed_numbers("Ist Nutella vegan?", [call])


def test_answer_numbers_skip_barcodes_and_list_markers() -> None:
    answer = "1. Nutella (Barcode 4008400401621): 56,3 g Zucker pro 100 g\n2) Salz: 0,11 g"

    assert answer_numbers(answer) == ["56,3", "100", "0,11"]


@pytest.mark.parametrize(
    ("number", "allowed", "expected"),
    [
        ("56,3", {56.3}, True),
        ("56.3", {56.3}, True),
        ("56,30", {56.3}, True),
        ("400", {400.0}, True),
        ("0,11", {0.1075}, True),
        ("0,1", {0.1075}, True),
        ("15,2", {15.199999809265137}, True),
        ("57", {56.3}, False),
        ("57", {57.5}, False),
        ("0", {0.02}, False),
        ("0", {0.0197}, False),
        ("0,12", {0.1075}, False),
        ("5", set(), False),
    ],
)
def test_is_grounded_needs_exact_values_and_rounds_only_beyond_two_decimals(
    number: str, allowed: set[float], expected: bool
) -> None:
    assert is_grounded(number, allowed) is expected


def test_known_barcodes_include_tool_inputs_and_results() -> None:
    known = known_barcodes("Wie viele Kalorien hat 4099999999994?", [SEARCH, NOT_FOUND])

    assert UNKNOWN_BARCODE in known
    assert "4008400000000" in known
    assert cited_barcodes(f"Nutella (Barcode {NUTELLA}) und 12345678") == {NUTELLA, "12345678"}


def test_failed_patterns_reports_missing_and_forbidden_matches() -> None:
    answer = "Nutella enthält Palmöl."

    assert failed_patterns(answer, ["palmöl", "zucker"], ["frei von"]) == ["zucker"]
    assert failed_patterns(answer, [], ["enthält"]) == ["not enthält"]


def test_evaluate_passes_a_grounded_answer() -> None:
    answer = (
        f"Ja. Nutella, 450 g (Barcode {NUTELLA}) nennt Palmöl an zweiter Stelle der Zutaten, "
        "dazu HASELNÜSSE (13%) und MAGERMILCHPULVER (8,7%). Zucker: 56,3 g pro 100 g, "
        "Salz 0,11 g. Die Suche hat 20 von 80 Treffern geliefert."
    )
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(), result)

    assert evaluation.passed
    assert evaluation.numbers_total == 8
    assert evaluation.unmatched_numbers == []


def test_evaluate_flags_an_invented_number() -> None:
    answer = f"Nutella (Barcode {NUTELLA}) hat 57 g Zucker pro 100 g und enthält Palmöl."
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(), result)

    assert evaluation.citation_ok
    assert evaluation.unmatched_numbers == ["57"]
    assert not evaluation.passed


def test_evaluate_flags_an_invented_barcode() -> None:
    answer = "Nutella (Barcode 4008400401638) enthält Palmöl."
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(), result)

    assert evaluation.unknown_barcodes == ["4008400401638"]
    assert not evaluation.citation_ok
    assert not evaluation.passed


def test_evaluate_requires_one_of_the_expected_barcodes() -> None:
    answer = "Nutella (Barcode 4008400000000) enthält Palmöl."
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(), result)

    assert evaluation.unknown_barcodes == []
    assert not evaluation.citation_ok


def test_evaluate_requires_the_expected_facts() -> None:
    answer = f"Nutella (Barcode {NUTELLA}) enthält Palmöl."
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(must_match=["Palmöl", "Soja"]), result)

    assert evaluation.failed_patterns == ["Soja"]
    assert not evaluation.facts_ok
    assert not evaluation.passed


def test_evaluate_flags_zero_for_a_missing_value() -> None:
    sparse = tool_call(
        "get_product",
        {"barcode": "4251680801722"},
        {"nutrition_per_100g": {"fiber_g": None}, "allergens": [], "traces": []},
    )
    trap = question(
        id="missing-values",
        question="Wie viele Ballaststoffe hat 4251680801722?",
        expect_barcodes=["4251680801722"],
        must_match=["keine Angabe"],
    )
    answer = (
        "Sylter Müsli (Barcode 4251680801722): Ballaststoffe 0 g pro 100 g, sonst keine Angabe."
    )
    result = AgentResult(answer, "end_turn", [sparse], 2)

    assert evaluate(trap, result).unmatched_numbers == ["0"]


def test_evaluate_accepts_a_not_found_answer_without_expected_barcodes() -> None:
    trap = question(
        id="unknown-barcode",
        question=f"Wie viele Kalorien hat {UNKNOWN_BARCODE}?",
        expect_barcodes=[],
        must_match=["keine|nicht gefunden"],
        must_not_match=["kcal"],
    )
    answer = f"Zum Barcode {UNKNOWN_BARCODE} hat Open Food Facts keine Angaben."
    result = AgentResult(answer, "end_turn", [NOT_FOUND], 2)

    assert evaluate(trap, result).passed


@pytest.mark.parametrize(
    ("expect_tools", "tool_calls", "expected"),
    [
        (False, [], True),
        (False, [SEARCH], False),
        (True, [], False),
        (None, [], True),
        (None, [SEARCH], True),
    ],
)
def test_evaluate_checks_whether_tools_were_used(
    expect_tools: bool | None, tool_calls: list[ToolCall], expected: bool
) -> None:
    off_topic = question(expect_tools=expect_tools, expect_barcodes=[], must_match=[])
    result = AgentResult("Dazu habe ich keine Daten.", "end_turn", tool_calls, 1)

    assert evaluate(off_topic, result).tools_ok is expected


def test_evaluate_requires_a_complete_answer() -> None:
    result = AgentResult(f"Nutella (Barcode {NUTELLA}) enthält Palmöl", "max_tokens", [PRODUCT], 2)

    evaluation = evaluate(question(), result)

    assert not evaluation.stop_ok
    assert not evaluation.passed


def test_evaluate_requires_a_citation_when_products_were_found() -> None:
    result = AgentResult("Nutella enthält Palmöl.", "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(expect_barcodes=[]), result)

    assert evaluation.unknown_barcodes == []
    assert not evaluation.citation_ok


def test_evaluate_needs_no_citation_when_no_product_was_found() -> None:
    trap = question(expect_barcodes=[], must_match=[])
    result = AgentResult("Dazu hat Open Food Facts keine Angaben.", "end_turn", [NOT_FOUND], 2)

    assert evaluate(trap, result).citation_ok


@pytest.mark.parametrize(
    ("answer", "language"),
    [
        ("Nutella enthält Palmöl, es steht an zweiter Stelle der Zutaten.", "de"),
        ("Ja, das Ungesüßte Beeren-Müsli hat 13 g Zucker pro 100 g.", "de"),
        ("Yes, Nutella contains palm oil; it is the second ingredient.", "en"),
        ('Yes. The ingredient list reads "Zucker, Palmöl, HASELNÜSSE (13%)".', "en"),
    ],
)
def test_answer_language_ignores_german_quotes(answer: str, language: str) -> None:
    assert answer_language(answer) == language


def test_evaluate_flags_an_answer_in_the_wrong_language() -> None:
    answer = f"Yes, Nutella (Barcode {NUTELLA}) contains palm oil, the second ingredient."
    result = AgentResult(answer, "end_turn", [SEARCH, PRODUCT], 3)

    evaluation = evaluate(question(must_match=["palm oil"]), result)

    assert not evaluation.language_ok
    assert not evaluation.passed


def test_questions_file_has_valid_entries() -> None:
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    fields = {"id", "kind", "language", "question", "expect_tools", "expect_barcodes"}
    fields |= {"must_match", "must_not_match", "note"}

    assert 10 <= len(questions) <= 15
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        assert set(q) - {"allow_numbers"} == fields, q["id"]
        assert q["expect_tools"] in {True, False, None}
        assert q["language"] in {"de", "en"}
        assert q["kind"] in {"fact", "comparison", "trap"}
        for pattern in q["must_match"] + q["must_not_match"]:
            re.compile(pattern)


def test_evaluate_accepts_numbers_the_question_allows() -> None:
    trap = question(
        id="invalid-barcode",
        question="Was steckt im Produkt mit dem 7-stelligen Barcode 1234567?",
        expect_tools=None,
        expect_barcodes=[],
        allow_numbers=[8, 13, 14],
        must_match=["Ziffer"],
    )
    answer = "1234567 hat nur 7 Ziffern, ein Barcode hat 8 bis 14 (meist EAN-13)."
    result = AgentResult(answer, "end_turn", [], 1)

    evaluation = evaluate(trap, result)

    assert evaluation.unmatched_numbers == []
    assert evaluation.passed
