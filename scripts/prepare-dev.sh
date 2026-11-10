#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${1:-$TASK_ROOT/../hermes-agent}"
DEV_DIR="$TASK_ROOT/.dev"
if [ ! -d "$DEV_DIR/gateway" ]; then
  mkdir -p "$DEV_DIR"
  git -C "$SOURCE_DIR" archive HEAD | tar -x -C "$DEV_DIR"
  git -C "$SOURCE_DIR" rev-parse HEAD > "$DEV_DIR/UPSTREAM_COMMIT"
  git -C "$DEV_DIR" init -q
fi
# Migrate an existing isolated development checkout; never change the source.
ACTIVITY_PATCH="$TASK_ROOT/patches/hermes-activity-callbacks.patch"
if git -C "$DEV_DIR" apply --reverse --check "$ACTIVITY_PATCH" 2>/dev/null; then
  git -C "$DEV_DIR" apply --reverse "$ACTIVITY_PATCH"
  echo "Removed the obsolete activity patch from the isolated checkout."
elif [ -f "$DEV_DIR/gateway/activity_callbacks.py" ]; then
  echo "Unrecognized activity bridge in $DEV_DIR; use a fresh isolated checkout." >&2
  exit 1
fi
for PATCH_NAME in hermes-delivery-context.patch; do
  PATCH_FILE="$TASK_ROOT/patches/$PATCH_NAME"
  if [ "$PATCH_NAME" = hermes-delivery-context.patch ] &&
     ! git -C "$DEV_DIR" apply --reverse --check "$PATCH_FILE" 2>/dev/null &&
     ! git -C "$DEV_DIR" apply --check "$PATCH_FILE" 2>/dev/null; then
    PATCH_NAME=hermes-delivery-context-653bc4f.patch
    PATCH_FILE="$TASK_ROOT/patches/$PATCH_NAME"
  fi
  if git -C "$DEV_DIR" apply --reverse --check "$PATCH_FILE" 2>/dev/null; then
    echo "Isolated Hermes checkout already has $PATCH_NAME."
  else
    git -C "$DEV_DIR" apply --check "$PATCH_FILE"
    git -C "$DEV_DIR" apply "$PATCH_FILE"
    echo "Applied $PATCH_NAME to the isolated checkout."
  fi
done
# The canonical runner uses tracked Python paths for its bytecode preparation.
git -C "$DEV_DIR" add gateway/delivery_ledger.py gateway/platforms/base.py \
  gateway/platforms/event.py gateway/run_startup.py gateway/run_busy.py
