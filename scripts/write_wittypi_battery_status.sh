#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WITTYPI_DIR="${WITTYPI_DIR:-$HOME/wittypi}"
OUTPUT_PATH="${INKYPI_BATTERY_JSON:-$REPO_DIR/src/config/battery.json}"

if [ ! -f "$WITTYPI_DIR/utilities.sh" ]; then
  echo "Witty Pi utilities not found at $WITTYPI_DIR/utilities.sh" >&2
  exit 1
fi

# shellcheck source=/dev/null
. "$WITTYPI_DIR/utilities.sh"

read_metric() {
  local function_name="$1"
  if declare -F "$function_name" >/dev/null 2>&1; then
    "$function_name" 2>/dev/null | sed -E 's/[^0-9.+-].*$//' || true
  fi
}

vin="$(read_metric get_input_voltage)"
vout="$(read_metric get_output_voltage)"
iout="$(read_metric get_output_current)"

if [ -z "$vin" ]; then
  echo "Could not read Witty Pi input voltage." >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"
tmp_path="${OUTPUT_PATH}.tmp"

python3 - "$tmp_path" "$vin" "$vout" "$iout" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, vin, vout, iout = sys.argv[1:5]

def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

data = {
    "updated": datetime.now(timezone.utc).isoformat(),
    "vin": number(vin),
}

vout_value = number(vout)
iout_value = number(iout)
if vout_value is not None:
    data["vout"] = vout_value
if iout_value is not None:
    data["iout"] = iout_value

with open(path, "w", encoding="utf-8") as file:
    json.dump(data, file, indent=2)
    file.write("\n")
PY

mv "$tmp_path" "$OUTPUT_PATH"
echo "Wrote battery status to $OUTPUT_PATH"
