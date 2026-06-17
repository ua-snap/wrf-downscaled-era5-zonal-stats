#!/bin/bash
set -euo pipefail

# Resolved from this script's own location, so it works regardless of the caller's cwd.
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARRAY_SCRIPT=${1:-slurm/run_zonal_array.sbatch}
FINALIZE_SCRIPT=${2:-slurm/finalize_zonal.sbatch}
CONFIG_PATH=${CONFIG_PATH:-"$WORKDIR/config/pipeline_config.json"}

cd "$WORKDIR"

if ! command -v sbatch >/dev/null 2>&1; then
  echo "ERROR: sbatch not found in PATH" >&2
  exit 1
fi

if [[ ! -f "$ARRAY_SCRIPT" ]]; then
  echo "ERROR: array script not found: $ARRAY_SCRIPT" >&2
  exit 1
fi

if [[ ! -f "$FINALIZE_SCRIPT" ]]; then
  echo "ERROR: finalize script not found: $FINALIZE_SCRIPT" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "ERROR: config file not found: $CONFIG_PATH" >&2
  exit 1
fi

read -r START_YEAR END_YEAR <<<"$(python -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["years"]["start"], c["years"]["end"])' "$CONFIG_PATH")"
if [[ -z "$START_YEAR" || -z "$END_YEAR" ]]; then
  echo "ERROR: could not parse years.start/years.end from config" >&2
  exit 1
fi
if [[ "$END_YEAR" -lt "$START_YEAR" ]]; then
  echo "ERROR: years.end ($END_YEAR) is less than years.start ($START_YEAR)" >&2
  exit 1
fi

ARRAY_RANGE="0-$((END_YEAR - START_YEAR))"

array_job_id=$(sbatch --parsable --array="$ARRAY_RANGE" --export=ALL,CONFIG_PATH="$CONFIG_PATH" "$ARRAY_SCRIPT" | cut -d ';' -f1)
finalize_job_id=$(sbatch --parsable --export=ALL,CONFIG_PATH="$CONFIG_PATH" --dependency="afterok:${array_job_id}" "$FINALIZE_SCRIPT" | cut -d ';' -f1)

echo "Submitted array job: ${array_job_id}"
echo "Submitted finalize job: ${finalize_job_id} (dependency: afterok:${array_job_id})"
echo "Config path: ${CONFIG_PATH}"
echo "Array range: ${ARRAY_RANGE} (derived from years ${START_YEAR}-${END_YEAR})"
