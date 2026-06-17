#!/bin/bash
#SBATCH --job-name=zonal_stats_finalize
#SBATCH --partition=t2small
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=24G
#SBATCH --time=01:00:00
#SBATCH --output=logs/zonal_stats_finalize_%j.out
#SBATCH --error=logs/zonal_stats_finalize_%j.err

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

# shellcheck disable=SC1091
source "$(micromamba shell hook --shell bash)"
micromamba activate "$ENV_NAME"

python scripts/finalize_zonal.py \
  --config "$CONFIG_PATH"
