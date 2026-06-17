#!/bin/bash
#SBATCH --job-name=zonal_stats
#SBATCH --partition=t2small
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH --time=08:00:00
#SBATCH --output=logs/zonal_stats_%j.out
#SBATCH --error=logs/zonal_stats_%j.err

set -euo pipefail

# Resolved from the directory `sbatch` was invoked from; submit from the project root.
WORKDIR="${SLURM_SUBMIT_DIR:-$(pwd)}"
ENV_NAME=wrf-era5-zonal-stats
CONFIG_PATH=${CONFIG_PATH:-"$WORKDIR/config/pipeline_config.json"}

cd "$WORKDIR"

if ! command -v micromamba >/dev/null 2>&1; then
  echo "ERROR: micromamba not found in PATH" >&2
  exit 1
fi

# shellcheck disable=SC1090
eval "$(micromamba shell hook --shell bash)"
micromamba activate "$ENV_NAME"

python scripts/check_inputs.py \
  --config "$CONFIG_PATH"

python scripts/zonal_watersheds.py \
  --config "$CONFIG_PATH" \
  --workers "${SLURM_CPUS_PER_TASK:-8}"
