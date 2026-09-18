#!/bin/bash
# 02-A9 queue runner: for each (name, prompt-file) pair, wait on the concurrency gate
# (queue_gate.py), then run one headless Claude session in the foreground, then a
# fixed cooldown sleep before the next pair.
#
# Usage: queue_run.sh [--dry-run] <name> <prompt-file> [<name> <prompt-file> ...]
#
# Every real gate decision is logged, timestamped, to
# ~/Library/Logs/inference-grid/queue.log. --dry-run prints the commands this would
# run (the gate wait, the claude invocation, the cooldown sleep) without running any
# of them or touching the log -- safe to run by hand to preview, and what tests use.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUEUE_GATE="${QUEUE_GATE:-$SCRIPT_DIR/queue_gate.py}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${QUEUE_LOG_DIR:-$HOME/Library/Logs/inference-grid}"
QUEUE_LOG="$LOG_DIR/queue.log"
REPORT_DIR="${QUEUE_REPORT_DIR:-plans/reports}"
GATE_WAIT_SECONDS="${GATE_WAIT_SECONDS:-7200}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-90}"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi

if [ "$#" -eq 0 ] || [ $(( $# % 2 )) -ne 0 ]; then
  echo "usage: queue_run.sh [--dry-run] <name> <prompt-file> [<name> <prompt-file> ...]" >&2
  exit 2
fi

log() {
  # $1: message. Only called on the real (non-dry-run) path.
  mkdir -p "$LOG_DIR"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$QUEUE_LOG"
}

while [ "$#" -gt 0 ]; do
  name="$1"; prompt_file="$2"; shift 2
  gate_cmd=("$PYTHON_BIN" "$QUEUE_GATE" --wait "$GATE_WAIT_SECONDS")

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "${gate_cmd[@]}"
  else
    if "${gate_cmd[@]}"; then
      log "$name: gate allowed"
    else
      log "$name: gate refused after ${GATE_WAIT_SECONDS}s wait, skipping"
      continue
    fi
  fi

  log_out="$REPORT_DIR/$(date -u +%Y-%m-%d)-$name.log"
  claude_cmd=(caffeinate -i claude -p "$(cat "$prompt_file")" --model sonnet --dangerously-skip-permissions --max-turns 300 --output-format text)

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "${claude_cmd[@]} >> $log_out 2>&1 < /dev/null"
    echo "sleep $COOLDOWN_SECONDS"
    continue
  fi

  mkdir -p "$REPORT_DIR"
  "${claude_cmd[@]}" >> "$log_out" 2>&1 < /dev/null
  log "$name: run finished"
  sleep "$COOLDOWN_SECONDS"
done
