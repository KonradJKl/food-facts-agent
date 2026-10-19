# food-facts-agent

A command line agent that answers questions about food products with data from Open Food Facts, using the Claude API with tool use.

Work in progress.

## Prompt design

The system prompt is in [prompts/system.md](prompts/system.md). It is written for `claude-opus-5-5` and follows Anthropic's guidance for that model: plain sentences that give the reason for a rule, goals instead of step-by-step scripts, and no all-caps words, "CRITICAL" markers or threats. The main decisions:

- **Every product fact comes from a tool result.** Nutrition values, ingredients, allergens, labels and the Nutri-Score must appear in a tool result of the same run, even for well-known products. The prompt states this once, with the reason that people act on the answers.
- **Missing is not zero.** A `null` field means Open Food Facts has no value. The model says so instead of estimating and leaves the product out of a ranking on that value. A `0` is a real value.
- **Rules for data quirks found in the recorded API responses.** Nutella's ingredient analysis says its palm oil content is unknown, although "palm oil" is in its ingredient list, so the ingredient list wins. A product without an ingredient list has an empty allergen list, so an empty list does not mean allergen-free. "Das Ungesüßte Beeren-Müsli" has 13 g sugars per 100 g, so the model judges products by their values, not by their names.
- **Comparisons cover only the search results.** A search returns at most 20 products ordered by relevance. The answer says how many products the search returned out of the total number of matches.
- **Answers that a script can check.** Each product is cited by its name as returned, untranslated, followed by `(Barcode <digits>)`, and each number stands next to its nutrient. German answers use a decimal comma, so the number check in the evaluation has to accept both separators. Values are rounded only beyond two decimals, and computed values such as differences appear only when the user asks for them.
- **The prompt does not name the tools.** When to call each tool is part of the tool descriptions in [tools.py](src/food_facts_agent/tools.py). The prompt does not tell the model to limit tool calls, because current models follow such limits literally.
- **Plain terminal text.** The CLI prints the answer as it is, so the prompt asks for plain sentences and simple dash lists instead of Markdown.
- **A fixed prompt.** The prompt contains no date or other changing text. It is part of the cached request prefix, and it stays identical across the requests of one run, which Claude Opus 5.5 requires for the thinking blocks that the agent sends back.
- **No request for visible reasoning.** How much the model thinks is set with the `effort` parameter in the code. Asking the model to show its reasoning can make the API decline the request.
