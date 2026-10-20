# food-facts-agent

A command line agent that answers questions about food products with data from Open Food Facts, using the Claude API with tool use.

Work in progress.

## Prompt design

The system prompt is in [prompts/system.md](prompts/system.md). It is written for `claude-opus-5-5` and follows Anthropic's guidance for that model: plain sentences that give the reason for a rule, goals instead of step-by-step scripts, and no all-caps words, "CRITICAL" markers or threats. The main decisions:

- **Every product fact comes from a tool result.** Nutrition values, ingredients, allergens, labels and the Nutri-Score must appear in a tool result of the same run, even for well-known products. The prompt states this once, with the reason that people act on the answers.
- **Missing is not zero.** A `null` field means Open Food Facts has no value. The model says so instead of estimating and leaves the product out of a ranking on that value. A `0` is a real value.
- **Rules for data quirks found in the recorded API responses.** Nutella's ingredient analysis says its palm oil content is unknown, although "palm oil" is in its ingredient list, so the ingredient list wins. A product without an ingredient list has an empty allergen list, so an empty list does not mean allergen-free. "Das Ungesüßte Beeren-Müsli" has 13 g sugars per 100 g, so the model judges products by their values, not by their names.
- **Comparisons cover only the search results.** A search returns at most 20 products ordered by relevance. The answer says how many products the search returned out of the total number of matches.
- **Answers that a script can check.** Each product is cited by its name as returned, untranslated, followed by `(Barcode <digits>)`, and each number stands next to its nutrient. German answers use a decimal comma, and the number check in the evaluation accepts both separators. Values are rounded only beyond two decimals, and computed values such as differences appear only when the user asks for them.
- **The prompt does not name the tools.** When to call each tool is part of the tool descriptions in [tools.py](src/food_facts_agent/tools.py). The prompt does not tell the model to limit tool calls, because current models follow such limits literally.
- **Plain terminal text.** The CLI prints the answer as it is, so the prompt asks for plain sentences and simple dash lists instead of Markdown.
- **A fixed prompt.** The prompt contains no date or other changing text. It is part of the cached request prefix, and it stays identical across the requests of one run, which Claude Opus 5.5 requires for the thinking blocks that the agent sends back.
- **No request for visible reasoning.** How much the model thinks is set with the `effort` parameter in the code. Asking the model to show its reasoning can make the API decline the request.

## Evaluation

[eval/questions.json](eval/questions.json) holds 15 questions, 11 in German and 4 in English: 6 fact questions, 2 comparisons and 7 traps (an unknown barcode, an invalid barcode, missing nutrition values, a missing ingredient list, a product name that claims "unsweetened", an ambiguous brand name and a question that is not about food). The expected values were looked up in Open Food Facts on 2026-10-19.

[eval/run_eval.py](eval/run_eval.py) asks every question through the agent and checks each answer:

- **Tools:** tools were used for product questions and not for the question about the Eiffel Tower. The question with the invalid barcode accepts answers with or without a tool call.
- **Citation:** every barcode in the answer appears in the question or in a tool result, and at least one is cited when a tool found a product. Some questions expect a specific barcode, such as the winner of a comparison.
- **Numbers:** every number in the answer appears in a tool result, either as a value or inside a text such as an ingredient list, or in the question. The 100 of "per 100 g", the number of products a search returned and general facts a question lists, such as barcode lengths, are also accepted. Values with up to two decimals must match exactly; longer values may be rounded, so 0.1075 may appear as "0,11".
- **Facts:** regular expressions per question, for example "must contain 56,3" or "must not say palmölfrei".
- **Language:** the answer is written in the language of the question, judged by counting common short words.
- **Stop:** the run ended with a final answer.

```bash
.venv/Scripts/python eval/run_eval.py
```

`run_eval.py` does not record token usage, so the cost of the run below was not measured. One question asked with `food-facts --verbose`, which logs the token counts of every step, cost about 5 US cents with `claude-opus-5-5`, so a full run of 15 questions costs roughly 75 cents. `--only ID` runs single questions and prints their answers, `--repeat N` runs every question N times, and `--recheck` evaluates the answers saved in [eval/results.json](eval/results.json) again with the current questions and checks, without API calls.

### Results

`claude-opus-5-5`, effort `medium`, one run per question on 2026-10-19:

| Question | Kind | Passed | Tools | Citation | Numbers | Facts | Language | Stop |
|---|---|---|---|---|---|---|---|---|
| sugar-barcode-de | fact | 1/1 | 1/1 | 1/1 | 3/3 | 1/1 | 1/1 | 1/1 |
| salt-barcode-en | fact | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 1/1 | 1/1 |
| palm-oil-de | fact | 1/1 | 1/1 | 1/1 | 7/7 | 1/1 | 1/1 | 1/1 |
| palm-oil-en | fact | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 1/1 | 1/1 |
| traces-soy-de | fact | 1/1 | 1/1 | 1/1 | 7/7 | 1/1 | 1/1 | 1/1 |
| compare-three-en | comparison | 1/1 | 1/1 | 1/1 | 7/7 | 1/1 | 1/1 | 1/1 |
| compare-search-de | comparison | 1/1 | 1/1 | 1/1 | 27/27 | 1/1 | 1/1 | 1/1 |
| allergy-de | fact | 1/1 | 1/1 | 1/1 | 4/4 | 1/1 | 1/1 | 1/1 |
| trap-unknown-barcode | trap | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 1/1 | 1/1 |
| trap-invalid-barcode | trap | 1/1 | 1/1 | 1/1 | 9/9 | 1/1 | 1/1 | 1/1 |
| trap-missing-values | trap | 1/1 | 1/1 | 1/1 | 2/2 | 1/1 | 1/1 | 1/1 |
| trap-no-ingredients | trap | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 | 1/1 |
| trap-name-claim | trap | 1/1 | 1/1 | 1/1 | 11/11 | 1/1 | 1/1 | 1/1 |
| trap-ambiguous-name | trap | 0/1 | 1/1 | 1/1 | 30/32 | 1/1 | 1/1 | 1/1 |
| trap-not-food-en | trap | 1/1 | 1/1 | 1/1 | 0/0 | 1/1 | 1/1 | 1/1 |

14 of 15 answers passed. The "Numbers" column counts grounded numbers out of all numbers in the answer.

- **trap-ambiguous-name failed for a real reason.** The answer listed 13 mueslis of the brand with their exact sugar values and named the products without values, but its first paragraph says the sugar values lie "zwischen etwa 3 und 28 g". The values are 3.4 and 28.5, and the prompt allows rounding only for values with more than two decimals.
- **trap-invalid-barcode was changed after the first run.** The model did not call a tool: it explained that 1234567 has only 7 digits and that barcodes have 8 to 14, mentioning EAN-8 and EAN-13. That is correct, but the question expected a tool call and treated 8, 13 and 14 as ungrounded numbers. The question now accepts answers with or without a tool call and allows these three numbers. Before this change the run scored 13 of 15; `--recheck` re-evaluated the saved answers.
- **One run is not a pass rate.** Answers vary between runs. `--repeat 3` shows how stable the results are, at three times the cost.

### Limits of the checks

- Numbers are not tied to products. A value of another product in the same tool results counts as grounded, and after a search many numbers are allowed: an invented "20 %" would match the 20 products a search returns.
- For a comparison the checks look at which barcodes the answer cites, not at which product the sentence calls the winner.
- The regular expressions were tested against about 180 example answers written for this purpose, correct and wrong ones. Careful wordings such as "ist nicht als glutenfrei gekennzeichnet" can still fail a check.
- Computed values such as amounts per portion count as ungrounded, so no question asks for them.
- compare-search-de depends on the search query: if the model searches with other words, the expected winner can change.
- Open Food Facts data changes, so expected values can go out of date.
