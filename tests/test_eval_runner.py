import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
import run_eval
from anthropic import APIConnectionError

from food_facts_agent.agent import AgentResult, ToolCall

SUGAR = "Wie viel Zucker hat 4008400401621 pro 100 g?"
TOWER = "How tall is the Eiffel Tower?"
QUESTIONS = [
    {
        "id": "sugar",
        "kind": "fact",
        "language": "de",
        "question": SUGAR,
        "expect_tools": True,
        "expect_barcodes": ["4008400401621"],
        "must_match": ["56,3"],
        "must_not_match": [],
        "note": "",
    },
    {
        "id": "not-food",
        "kind": "trap",
        "language": "en",
        "question": TOWER,
        "expect_tools": False,
        "expect_barcodes": [],
        "must_match": ["food"],
        "must_not_match": [],
        "note": "",
    },
]
PRODUCT = ToolCall(
    "get_product",
    {"barcode": "4008400401621"},
    json.dumps({"barcode": "4008400401621", "nutrition_per_100g": {"sugars_g": 56.3}}),
    False,
)


class FakeAgent:
    """Stands in for run_agent and answers each question with a prepared result."""

    def __init__(self) -> None:
        self.answers = {
            SUGAR: AgentResult(
                "Nutella (Barcode 4008400401621) hat 56,3 g Zucker pro 100 g.",
                "end_turn",
                [PRODUCT],
                2,
            ),
            TOWER: AgentResult("I only answer questions about food products.", "end_turn", [], 1),
        }
        self.error: Exception | None = None
        self.questions: list[str] = []
        self.settings: list[dict[str, Any]] = []

    def __call__(self, question: str, **kwargs: Any) -> AgentResult:
        self.questions.append(question)
        self.settings.append(kwargs)
        if self.error:
            raise self.error
        return self.answers[question]


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAgent:
    """Points run_eval at temporary files and keeps it away from .env and the APIs."""
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(json.dumps(QUESTIONS), encoding="utf-8")
    fake = FakeAgent()
    monkeypatch.setattr(run_eval, "QUESTIONS_PATH", questions_path)
    monkeypatch.setattr(run_eval, "RESULTS_PATH", tmp_path / "results.json")
    monkeypatch.setattr(run_eval, "run_agent", fake)
    monkeypatch.setattr(run_eval, "load_dotenv", lambda path: False)
    monkeypatch.setattr(run_eval, "Anthropic", lambda: "fake-llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("OFF_CONTACT_EMAIL", "test@example.com")
    return fake


def test_runs_all_questions_and_saves_results(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main([]) == 0

    out = capsys.readouterr().out
    assert "| sugar | fact | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 1/1 | 1/1 |" in out
    assert "| not-food | trap | 1/1 | 1/1 | 1/1 | 0/0 | 1/1 | 1/1 | 1/1 |" in out
    assert "Passed 2 of 2 runs." in out
    report = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    assert report["model"] == "claude-opus-5-5"
    assert [run["id"] for run in report["runs"]] == ["sugar", "not-food"]
    assert report["runs"][0]["tool_calls"][0]["name"] == "get_product"
    assert report["runs"][0]["evaluation"]["passed"] is True
    system = run_eval.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    for settings in fake_agent.settings:
        assert settings["llm"] == "fake-llm"
        assert settings["model"] == "claude-opus-5-5"
        assert settings["system"] == system


def test_repeat_runs_every_question_several_times(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main(["--repeat", "2"]) == 0

    assert fake_agent.questions == [SUGAR, TOWER, SUGAR, TOWER]
    assert "| sugar | fact | 2/2 | 2/2 | 2/2 | 4/4 | 2/2 | 2/2 | 2/2 |" in capsys.readouterr().out
    report = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    assert [run["round"] for run in report["runs"]] == [1, 1, 2, 2]


def test_only_prints_the_answer_and_keeps_saved_results(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main(["--only", "sugar"]) == 0

    assert fake_agent.questions == [SUGAR]
    assert "hat 56,3 g Zucker pro 100 g." in capsys.readouterr().out
    assert not run_eval.RESULTS_PATH.exists()


def test_unknown_question_id_stops_before_any_request(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main(["--only", "nope"]) == 2

    assert fake_agent.questions == []
    assert "Unknown question id: nope" in capsys.readouterr().err


def test_failed_run_sets_the_exit_code_and_is_explained(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_agent.answers[SUGAR] = AgentResult("Nutella hat 57 g Zucker.", "end_turn", [PRODUCT], 2)

    assert run_eval.main([]) == 1

    out = capsys.readouterr().out
    assert "Passed 1 of 2 runs." in out
    assert "| sugar | fact | 0/1 | 1/1 | 0/1 | 0/1 | 0/1 | 1/1 | 1/1 |" in out
    assert "- sugar (round 1): citation, facts, ungrounded numbers 57, patterns 56,3" in out
    report = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    assert [run["evaluation"]["passed"] for run in report["runs"]] == [False, True]


def test_repeat_below_one_is_rejected(fake_agent: FakeAgent) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_eval.main(["--repeat", "0"])

    assert exit_info.value.code == 2
    assert fake_agent.questions == []
    assert not run_eval.RESULTS_PATH.exists()


def test_email_without_at_sign_is_a_setting_error(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFF_CONTACT_EMAIL", "konrad")

    assert run_eval.main([]) == 2

    assert fake_agent.questions == []
    assert capsys.readouterr().err.startswith("OFF_CONTACT_EMAIL: ")


def test_missing_setting_stops_before_any_request(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    assert run_eval.main([]) == 2

    assert fake_agent.questions == []
    assert ".env.example" in capsys.readouterr().err


def test_api_error_saves_no_results(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_agent.error = APIConnectionError(request=request)

    assert run_eval.main([]) == 1

    assert "Claude API error, no results saved" in capsys.readouterr().err
    assert not run_eval.RESULTS_PATH.exists()


def test_recheck_evaluates_saved_answers_again_without_requests(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main([]) == 0
    stricter = [{**QUESTIONS[0], "must_match": ["57"]}, QUESTIONS[1]]
    run_eval.QUESTIONS_PATH.write_text(json.dumps(stricter), encoding="utf-8")

    assert run_eval.main(["--recheck"]) == 1

    assert fake_agent.questions == [SUGAR, TOWER]
    assert "- sugar (round 1): facts, patterns 57" in capsys.readouterr().out
    report = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    assert [run["evaluation"]["passed"] for run in report["runs"]] == [False, True]


def test_recheck_needs_a_results_file(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main(["--recheck"]) == 2

    assert "no results.json yet" in capsys.readouterr().err


def test_recheck_cannot_be_combined_with_only(fake_agent: FakeAgent) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_eval.main(["--recheck", "--only", "sugar"])

    assert exit_info.value.code == 2


def test_recheck_keeps_model_and_date(fake_agent: FakeAgent) -> None:
    assert run_eval.main([]) == 0
    report = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    report["date"] = "2026-01-01"
    run_eval.RESULTS_PATH.write_text(json.dumps(report), encoding="utf-8")

    assert run_eval.main(["--recheck"]) == 0

    rechecked = json.loads(run_eval.RESULTS_PATH.read_text(encoding="utf-8"))
    assert (rechecked["model"], rechecked["date"]) == ("claude-opus-5-5", "2026-01-01")


def test_recheck_keeps_results_of_removed_questions(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main([]) == 0
    saved = run_eval.RESULTS_PATH.read_text(encoding="utf-8")
    run_eval.QUESTIONS_PATH.write_text(json.dumps(QUESTIONS[:1]), encoding="utf-8")

    assert run_eval.main(["--recheck"]) == 2

    assert "Unknown question id in results.json: not-food" in capsys.readouterr().err
    assert run_eval.RESULTS_PATH.read_text(encoding="utf-8") == saved


def test_recheck_cannot_be_combined_with_repeat(fake_agent: FakeAgent) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_eval.main(["--recheck", "--repeat", "2"])

    assert exit_info.value.code == 2
