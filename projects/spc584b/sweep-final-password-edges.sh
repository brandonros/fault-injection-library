#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 PICO_SERIAL_PORT PASSWORD_FILE" >&2
  exit 2
fi

RPICO_PORT="$1"
PASSWORD_FILE="$2"
PYTHON="${PYTHON:-python3}"
ATTEMPTS="${ATTEMPTS:-0}"
FIRST_EDGE="${FIRST_EDGE:-256}"
LAST_EDGE="${LAST_EDGE:-264}"

for ((edge = FIRST_EDGE; edge <= LAST_EDGE; edge++)); do
  echo "Testing TCK edge $edge"

  "$PYTHON" "$SCRIPT_DIR/spc584b-password-glitch.py" \
    --rpico "$RPICO_PORT" \
    --password-file "$PASSWORD_FILE" \
    --edge-count "$edge" \
    --trigger-input default \
    --adapter-speed-khz 1000 \
    --reset-delay-ms 50 \
    --halt-timeout-ms 50 \
    --delay 0 1000 \
    --length 8 28 \
    --unique-grid-step-ns 4 \
    --glitch-power low \
    --attempts "$ATTEMPTS"

  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Stopping: unexpected debug access was observed."
    exit 10
  fi
  if [ "$rc" -ne 1 ]; then
    echo "Stopping: campaign error (exit $rc)." >&2
    exit "$rc"
  fi
done

echo "Sweep complete: no unexpected debug access observed."
exit 0
