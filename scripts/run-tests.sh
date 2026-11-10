#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_SOURCE="${HERMES_SOURCE:-$TASK_ROOT/.dev}"
export HERMES_PYTHON="${HERMES_PYTHON:-$TASK_ROOT/.venv/bin/python}"
exec bash "$HERMES_SOURCE/scripts/run_tests.sh" "$TASK_ROOT/tests" "$@" -- --hermes-source "$HERMES_SOURCE"
