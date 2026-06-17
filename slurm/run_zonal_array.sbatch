#!/bin/bash
#SBATCH --job-name=zonal_stats_array
#SBATCH --partition=t2small
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=04:00:00
#SBATCH --array=0-24
#SBATCH --output=logs/zonal_stats_array_%A_%a.out
#SBATCH --error=logs/zonal_stats_array_%A_%a.err

set -euo pipefail

# Resolved from the directory `sbatch` was invoked from; submit from the project root.
WORKDIR="${SLURM_SUBMIT_DIR:-$(pwd)}"
ENV_NAME=wrf-era5-zonal-stats
CONFIG_PATH=${CONFIG_PATH:-"$WORKDIR/config/pipeline_config.json"}

START_YEAR=${START_YEAR:-2000}
END_YEAR=${END_YEAR:-2024}

cd "$WORKDIR"

if ! command -v micromamba >/dev/null 2>&1; then
  echo "ERROR: micromamba not found in PATH" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$(micromamba shell hook --shell bash)"
micromamba activate "$ENV_NAME"

# Read configured year bounds if available.
if [[ -f "$CONFIG_PATH" ]]; then
  START_YEAR=$(python -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["years"]["start"])' "$CONFIG_PATH")
  END_YEAR=$(python -c 'import json,sys; c=json.load(open(sys.argv[1])); print(c["years"]["end"])' "$CONFIG_PATH")
fi

YEAR=$((START_YEAR + SLURM_ARRAY_TASK_ID))
if [[ "$YEAR" -gt "$END_YEAR" ]]; then
  echo "Array task year $YEAR is outside configured range ${START_YEAR}-${END_YEAR}; exiting."
  exit 0
fi

if [[ "${SLURM_ARRAY_TASK_ID}" == "0" ]]; then
  python scripts/check_inputs.py \
    --config "$CONFIG_PATH" \
    --start-year ${START_YEAR} \
    --end-year ${END_YEAR}
fi

python scripts/zonal_watersheds.py \
  --config "$CONFIG_PATH" \
  --start-year ${YEAR} \
  --end-year ${YEAR} \
  --workers "${SLURM_CPUS_PER_TASK:-8}" \
  --skip-finalize \
  --skip-qc
