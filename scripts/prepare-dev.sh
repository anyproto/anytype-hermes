#!/usr/bin/env bash
# Prepare stock Hermes without modifying the source checkout or applying patches.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${1:-$TASK_ROOT/../hermes-agent}"
DEV_DIR="${2:-$TASK_ROOT/.dev}"
SOURCE_SHA="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
if [ -e "$DEV_DIR" ]; then
  DEV_ROOT="$(git -C "$DEV_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ "$DEV_ROOT" != "$(cd "$DEV_DIR" && pwd -P)" ] ||
     [ "$(git -C "$DEV_DIR" rev-parse HEAD 2>/dev/null || true)" != "$SOURCE_SHA" ] ||
     [ -n "$(git -C "$DEV_DIR" status --porcelain --untracked-files=normal)" ]; then
    echo "Existing checkout is modified or uses another revision: $DEV_DIR" >&2
    echo "Pass a fresh destination as the second argument; existing work is preserved." >&2
    exit 1
  fi
else
  git clone --shared --no-checkout "$SOURCE_DIR" "$DEV_DIR"
  git -C "$DEV_DIR" checkout --detach "$SOURCE_SHA"
fi
echo "Stock Hermes $SOURCE_SHA ready at $DEV_DIR (no patches)."
