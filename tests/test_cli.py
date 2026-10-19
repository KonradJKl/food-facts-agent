import io
import logging
import sys
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from anthropic import APIConnectionError

from food_facts_agent import cli
from food_facts_agent.agent import AgentResult
from food_facts_agent.off_client import OFFClient

QUESTION = "Enthält Nutella Palmöl?"


class FakeAgent:
    """Stands in for run_agent and records how main() calls it."""

    def __init__(self) -> None:
        self.result = AgentResult("Nutella enthält Palmöl.", "end_turn", [], 2)
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    def __call__(self, question: str, **kwargs: Any) -> AgentResult:
        self.calls.append({"question": question, **kwargs})
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> FakeAgent:
    """Keeps main() away from the real .env, the Claude API and Open Food Facts."""
    fake = FakeAgent()
    monkeypatch.setattr(cli, "run_agent", fake)
    monkeypatch.setattr(cli, "load_dotenv", lambda path: False)
    monkeypatch.setattr(cli, "Anthropic", lambda: "fake-llm")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("OFF_CONTACT_EMAIL", "test@example.com")
    return fake


@pytest.fixture
def package_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("food_facts_agent")
    yield logger
    logger.setLevel(logging.NOTSET)


def test_prints_answer_and_passes_settings(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([QUESTION]) == 0

    assert capsys.readouterr() == ("Nutella enthält Palmöl.\n", "")
    call = fake_agent.calls[0]
    assert call["question"] == QUESTION
    assert call["model"] == "claude-opus-5-5"
    assert call["llm"] == "fake-llm"
    assert call["system"] == cli.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert isinstance(call["off"], OFFClient)
    assert call["off"]._http.is_closed


def test_incomplete_answer_is_printed_with_a_note(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_agent.result = AgentResult("Nutella enthält", "max_tokens", [], 1)

    assert cli.main([QUESTION]) == 1

    assert capsys.readouterr() == ("Nutella enthält\n", "Stopped early: max_tokens\n")


def test_empty_answer_is_not_printed_as_a_blank_line(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_agent.result = AgentResult("", "end_turn", [], 1)

    assert cli.main([QUESTION]) == 0

    assert capsys.readouterr().out == "(The model returned no text.)\n"


@pytest.mark.parametrize("variable", ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "OFF_CONTACT_EMAIL"])
def test_missing_setting_stops_before_any_request(
    fake_agent: FakeAgent,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.delenv(variable)

    assert cli.main([QUESTION]) == 2

    assert fake_agent.calls == []
    assert ".env.example" in capsys.readouterr().err


def test_email_without_at_sign_is_a_setting_error(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OFF_CONTACT_EMAIL", "konrad")

    assert cli.main([QUESTION]) == 2

    assert fake_agent.calls == []
    assert capsys.readouterr().err.startswith("OFF_CONTACT_EMAIL: ")


def test_answer_is_written_as_utf8_to_a_cp1252_stdout(
    fake_agent: FakeAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_agent.result = AgentResult("Zucker ≤ 5 g", "end_turn", [], 1)
    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", stdout)

    assert cli.main([QUESTION]) == 0

    stdout.flush()
    assert buffer.getvalue().decode("utf-8") == "Zucker ≤ 5 g\n"


def test_api_error_is_reported_without_traceback(
    fake_agent: FakeAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_agent.error = APIConnectionError(request=request)

    assert cli.main([QUESTION]) == 1

    assert capsys.readouterr() == ("", "Claude API error: Connection error.\n")
    assert fake_agent.calls[0]["off"]._http.is_closed


@pytest.mark.parametrize(("args", "level"), [([], logging.NOTSET), (["-v"], logging.INFO)])
def test_verbose_switches_on_info_logs_of_the_package(
    fake_agent: FakeAgent, package_logger: logging.Logger, args: list[str], level: int
) -> None:
    cli.main([*args, QUESTION])

    assert package_logger.level == level
