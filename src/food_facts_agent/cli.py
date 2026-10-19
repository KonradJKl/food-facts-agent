import argparse
import io
import logging
import os
import sys
from pathlib import Path

from anthropic import Anthropic, AnthropicError
from dotenv import load_dotenv

from food_facts_agent.agent import run_agent
from food_facts_agent.off_client import OFFClient

# Prompt, .env and cache live in the project folder; this works with `pip install -e .`.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT_PATH = PROJECT_ROOT / "prompts" / "system.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="food-facts",
        description="Answer questions about food products with data from Open Food Facts.",
    )
    parser.add_argument("question", help="the question, in German or English")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every step and tool call")
    args = parser.parse_args(argv)
    # Redirected output on Windows defaults to cp1252, which cannot encode characters like "≤".
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")

    logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        logging.getLogger("food_facts_agent").setLevel(logging.INFO)

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

    try:
        result = run_agent(
            args.question,
            llm=Anthropic(),
            off=off,
            model=model,
            system=SYSTEM_PROMPT_PATH.read_text(encoding="utf-8"),
        )
    except AnthropicError as exc:
        print(f"Claude API error: {exc}", file=sys.stderr)
        return 1
    finally:
        off.close()

    print(result.answer or "(The model returned no text.)")
    if result.stop_reason != "end_turn":
        print(f"Stopped early: {result.stop_reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
