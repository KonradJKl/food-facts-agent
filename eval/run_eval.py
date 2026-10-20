import argparse
import io
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from anthropic import Anthropic, AnthropicError
from dotenv import load_dotenv

from food_facts_agent.agent import AgentResult, ToolCall, run_agent
from food_facts_agent.cli import PROJECT_ROOT, SYSTEM_PROMPT_PATH
from food_facts_agent.off_client import OFFClient

QUESTIONS_PATH = Path(__file__).parent / "questions.json"
RESULTS_PATH = Path(__file__).parent / "results.json"

BARCODE_PATTERN = re.compile(r"(?<!\d)\d{8,14}(?!\d)")
NUMBER_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")
LIST_MARKER_PATTERN = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
GERMAN_WORDS = set(
    "der die das und ist nicht ein eine mit hat für pro auf laut es sie ich kein keine".split()
)
ENGLISH_WORDS = set("the and is not a with has for per on according it i no of to".split())


@dataclass
class Evaluation:
    id: str
    tools_ok: bool
    citation_ok: bool
    facts_ok: bool
    language_ok: bool
    stop_ok: bool
    numbers_total: int
    unmatched_numbers: list[str] = field(default_factory=list)
    unknown_barcodes: list[str] = field(default_factory=list)
    failed_patterns: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        checks = (self.tools_ok, self.citation_ok, self.facts_ok, self.language_ok, self.stop_ok)
        return all(checks) and not self.unmatched_numbers


def evaluate(question: dict[str, Any], result: AgentResult) -> Evaluation:
    answer = result.answer
    cited = cited_barcodes(answer)
    unknown = sorted(cited - known_barcodes(question["question"], result.tool_calls))
    expected = set(question["expect_barcodes"])
    cites_found_product = bool(cited) or not found_products(result.tool_calls)
    numbers = answer_numbers(answer)
    allowed = allowed_numbers(question["question"], result.tool_calls)
    # General facts a question may need, such as barcode lengths; not product data.
    allowed.update(question.get("allow_numbers", []))
    failed = failed_patterns(answer, question["must_match"], question["must_not_match"])
    return Evaluation(
        id=question["id"],
        tools_ok=question["expect_tools"] in (None, bool(result.tool_calls)),
        citation_ok=not unknown
        and cites_found_product
        and (not expected or bool(cited & expected)),
        facts_ok=not failed,
        language_ok=answer_language(answer) == question["language"],
        stop_ok=result.stop_reason == "end_turn",
        numbers_total=len(numbers),
        unmatched_numbers=[number for number in numbers if not is_grounded(number, allowed)],
        unknown_barcodes=unknown,
        failed_patterns=failed,
    )


def cited_barcodes(answer: str) -> set[str]:
    return set(BARCODE_PATTERN.findall(answer))


def known_barcodes(question: str, tool_calls: list[ToolCall]) -> set[str]:
    texts = [question]
    for call in tool_calls:
        texts.append(json.dumps(call.input))
        texts.append(call.content)
    return set(BARCODE_PATTERN.findall(" ".join(texts)))


def found_products(tool_calls: list[ToolCall]) -> bool:
    for call in tool_calls:
        result = json.loads(call.content)
        if "barcode" in result or result.get("products"):
            return True
    return False


def answer_numbers(answer: str) -> list[str]:
    """Return the numbers of an answer as written, without barcodes and list markers."""
    text = BARCODE_PATTERN.sub(" ", answer)
    text = LIST_MARKER_PATTERN.sub(" ", text)
    return NUMBER_PATTERN.findall(text)


def allowed_numbers(question: str, tool_calls: list[ToolCall]) -> set[float]:
    """Numbers an answer may contain: values and numbers inside texts of the tool results,
    the number of products a search returned, numbers from the question, and the 100 of
    "per 100 g"."""
    numbers = {100.0, *_numbers_in_text(question)}
    for call in tool_calls:
        result = json.loads(call.content)
        _collect_numbers(result, numbers)
        if "products" in result:
            numbers.add(float(len(result["products"])))
    return numbers


def is_grounded(number: str, allowed: set[float]) -> bool:
    normalized = number.replace(",", ".")
    value = float(normalized)
    decimals = len(normalized.partition(".")[2])
    for candidate in allowed:
        if abs(value - candidate) < 1e-9:
            return True
        # The prompt allows rounding only for values with more than two decimals, such as
        # 0.1075 to "0,11", and the answer has to keep at least one decimal.
        if decimals and round(candidate, 2) != candidate:
            if abs(value - candidate) <= 0.5 * 10**-decimals + 1e-9:
                return True
    return False


def answer_language(answer: str) -> str:
    # Ingredient lists and product names rarely contain these short words, so a German
    # quote in an English answer does not change the result.
    words = re.findall(r"[a-zäöüß]+", answer.lower())
    german = sum(word in GERMAN_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    return "de" if german >= english else "en"


def failed_patterns(answer: str, must_match: list[str], must_not_match: list[str]) -> list[str]:
    failed = [p for p in must_match if not re.search(p, answer, re.IGNORECASE)]
    failed += [f"not {p}" for p in must_not_match if re.search(p, answer, re.IGNORECASE)]
    return failed


def _numbers_in_text(text: str) -> list[float]:
    return [float(number.replace(",", ".")) for number in NUMBER_PATTERN.findall(text)]


def _collect_numbers(value: Any, numbers: set[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        numbers.add(float(value))
    elif isinstance(value, str):
        numbers.update(_numbers_in_text(value))
    elif isinstance(value, list):
        for item in value:
            _collect_numbers(item, numbers)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, numbers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the evaluation questions against the agent.")
    parser.add_argument(
        "--only",
        action="append",
        metavar="ID",
        help="run only this question and print its answer; can be repeated; results.json stays",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, metavar="N", help="run every question N times"
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="evaluate the answers in results.json again with the current questions, no API calls",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.recheck and (args.only or args.repeat != 1):
        parser.error("--recheck cannot be combined with --only or --repeat")
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.recheck:
        return recheck()

    try:
        questions = load_questions(args.only)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    load_dotenv(PROJECT_ROOT / ".env")
    model = os.environ.get("ANTHROPIC_MODEL")
    email = os.environ.get("OFF_CONTACT_EMAIL")
    if not (model and email and os.environ.get("ANTHROPIC_API_KEY")):
        print(
            "Set ANTHROPIC_API_KEY, ANTHROPIC_MODEL and OFF_CONTACT_EMAIL in .env, "
            "see .env.example.",
            file=sys.stderr,
        )
        return 2

    try:
        off = OFFClient(contact_email=email, cache_dir=PROJECT_ROOT / ".cache" / "off")
    except ValueError as exc:
        print(f"OFF_CONTACT_EMAIL: {exc}", file=sys.stderr)
        return 2

    system = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    try:
        runs = run_questions(questions, args.repeat, Anthropic(), off, model, system)
    except AnthropicError as exc:
        print(f"Claude API error, no results saved: {exc}", file=sys.stderr)
        return 1
    finally:
        off.close()

    if args.only:
        for run in runs:
            print(f"{run['id']} (round {run['round']}):\n{run['answer']}\n")
    report = {"model": model, "date": date.today().isoformat(), "runs": runs}
    return finish(report, save=not args.only)


def recheck() -> int:
    """Evaluate the answers saved in results.json again with the current questions and checks."""
    if not RESULTS_PATH.exists():
        print("There is no results.json yet; run the evaluation first.", file=sys.stderr)
        return 2
    report = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    questions = {question["id"]: question for question in load_questions()}
    missing = {run["id"] for run in report["runs"]} - set(questions)
    if missing:
        print(f"Unknown question id in results.json: {', '.join(sorted(missing))}", file=sys.stderr)
        return 2
    for run in report["runs"]:
        # Patterns come from questions.json, the question text from the run it was asked in.
        question = {**questions[run["id"]], "question": run["question"]}
        tool_calls = [ToolCall(**call) for call in run["tool_calls"]]
        result = AgentResult(run["answer"], run["stop_reason"], tool_calls, run["steps"])
        evaluation = evaluate(question, result)
        run["evaluation"] = {**asdict(evaluation), "passed": evaluation.passed}
    return finish(report, save=True)


def finish(report: dict[str, Any], save: bool) -> int:
    runs = report["runs"]
    print(results_table(runs))
    details = failure_details(runs)
    if details:
        print(f"\nFailures:\n{details}")
    if save:
        RESULTS_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if all(run["evaluation"]["passed"] for run in runs) else 1


def load_questions(only: list[str] | None = None) -> list[dict[str, Any]]:
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if not only:
        return questions
    unknown = set(only) - {question["id"] for question in questions}
    if unknown:
        raise ValueError(f"Unknown question id: {', '.join(sorted(unknown))}")
    return [question for question in questions if question["id"] in only]


def run_questions(
    questions: list[dict[str, Any]],
    repeat: int,
    llm: Anthropic,
    off: OFFClient,
    model: str,
    system: str,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    total = len(questions) * repeat
    for round_number in range(1, repeat + 1):
        for question in questions:
            result = run_agent(question["question"], llm=llm, off=off, model=model, system=system)
            evaluation = evaluate(question, result)
            runs.append(
                {
                    "id": question["id"],
                    "kind": question["kind"],
                    "round": round_number,
                    "question": question["question"],
                    **asdict(result),
                    "evaluation": {**asdict(evaluation), "passed": evaluation.passed},
                }
            )
            status = "passed" if evaluation.passed else "failed"
            print(f"[{len(runs)}/{total}] {question['id']}: {status}", file=sys.stderr)
    return runs


def results_table(runs: list[dict[str, Any]]) -> str:
    """Markdown table with one row per question; with --repeat, cells count passing rounds."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault(run["id"], []).append(run)

    header = [
        "Question",
        "Kind",
        "Passed",
        "Tools",
        "Citation",
        "Numbers",
        "Facts",
        "Language",
        "Stop",
    ]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for question_id, group in groups.items():
        evaluations = [run["evaluation"] for run in group]
        numbers = sum(e["numbers_total"] for e in evaluations)
        grounded = numbers - sum(len(e["unmatched_numbers"]) for e in evaluations)
        row = [
            question_id,
            group[0]["kind"],
            _share(evaluations, "passed"),
            _share(evaluations, "tools_ok"),
            _share(evaluations, "citation_ok"),
            f"{grounded}/{numbers}",
            _share(evaluations, "facts_ok"),
            _share(evaluations, "language_ok"),
            _share(evaluations, "stop_ok"),
        ]
        lines.append("| " + " | ".join(row) + " |")

    passed = sum(run["evaluation"]["passed"] for run in runs)
    lines.append(f"\nPassed {passed} of {len(runs)} runs.")
    return "\n".join(lines)


def _share(evaluations: list[dict[str, Any]], key: str) -> str:
    return f"{sum(e[key] for e in evaluations)}/{len(evaluations)}"


def failure_details(runs: list[dict[str, Any]]) -> str:
    lines = []
    for run in runs:
        evaluation = run["evaluation"]
        if evaluation["passed"]:
            continue
        checks = ["tools", "citation", "facts", "language", "stop"]
        problems = [check for check in checks if not evaluation[f"{check}_ok"]]
        if evaluation["unmatched_numbers"]:
            problems.append("ungrounded numbers " + ", ".join(evaluation["unmatched_numbers"]))
        if evaluation["unknown_barcodes"]:
            problems.append("unknown barcodes " + ", ".join(evaluation["unknown_barcodes"]))
        if evaluation["failed_patterns"]:
            problems.append("patterns " + "; ".join(evaluation["failed_patterns"]))
        lines.append(f"- {run['id']} (round {run['round']}): {', '.join(problems)}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
