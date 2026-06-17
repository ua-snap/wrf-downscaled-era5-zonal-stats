# MERIT Catchment Daily Zonal Stats

This project computes daily zonal statistics for MERIT catchments from curated WRF-downscaled ERA5 4 km gridded files. The pipeline is variable-agnostic: which variable family/families to process, how each is reduced, and what the output columns are named are all driven by config, not hardcoded. The example config currently shipped (`config/pipeline_config.json`) processes air temperature (`t2_min`/`t2_mean`/`t2_max`), but any other curated variable family on the same grid can be substituted without code changes (see [Config-Driven Parameters](#config-driven-parameters)).

Primary runtime configuration lives in `config/pipeline_config.json`.

## Inputs
- Catchments: shapefile path set via `io.shapefile` in config, keyed on the field named in `io.id_field` (default `COMID`)
- Curated variable root: path set via `io.data_root` in config, containing one subdirectory per variable family named in `variables[].family`, e.g. for the shipped temperature config:
  - `t2_min/t2_min_<YEAR>_daily_era5_4km_3338.nc`
  - `t2_mean/t2_mean_<YEAR>_daily_era5_4km_3338.nc`
  - `t2_max/t2_max_<YEAR>_daily_era5_4km_3338.nc`

## Environment (micromamba)
Create environment (run from the project root):

```bash
micromamba create -y -f environment.yml
micromamba activate wrf-era5-zonal-stats
```

## Run with Slurm (`t2small`)
Submit batch job (run from the project root):

```bash
sbatch slurm/run_zonal.sbatch
```

Run with a non-default config path:

```bash
CONFIG_PATH=config/pipeline_config.json sbatch slurm/run_zonal.sbatch
```

Optional: run by year with a Slurm array and then finalize:

```bash
jid=$(sbatch slurm/run_zonal_array.sbatch | awk '{print $4}')
sbatch --dependency=afterok:${jid} slurm/finalize_zonal.sbatch
```

Or use the helper script (submits both jobs and prints IDs):

```bash
./slurm/submit_array_then_finalize.sh
```

The helper auto-derives the Slurm `--array` range from `years.start` and `years.end` in config.

Helper with custom config:

```bash
CONFIG_PATH=config/pipeline_config.json ./slurm/submit_array_then_finalize.sh
```

Check logs:

```bash
ls -lh logs/
```

## Outputs
- Final table: path set via `outputs.final_path` in config (e.g. `outputs/zonal_t2_daily_<START>_<END>.parquet` for the shipped temperature config)
- Year partitions: path set via `outputs.yearly_dir` in config (e.g. `outputs/zonal_t2_daily/year=YYYY/part.parquet`)
- QC summary: path set via `outputs.qc_path` in config (e.g. `outputs/qc_summary.json`)
- Preflight summary: path set via `outputs.preflight_summary` in config (e.g. `outputs/preflight_summary.json`)

## What the pipeline computes
Per catchment ID per date, one column per entry in `variables` plus a fixed cell count. With the shipped temperature config:
- `cell_count` (fixed intersecting cell count)
- `t2_min_zonal_min`
- `t2_mean_zonal_mean`
- `t2_max_zonal_max`

## Zonal Method and Intersecting Cells
- The script first rasterizes catchment polygons to the source grid in the CRS set by `spatial.target_crs` (default EPSG:3338).
- Cell membership mode is controlled by `spatial.cell_membership` in config (`all_touched` or `center`); both Slurm launchers inherit this setting.
- In `all_touched` mode, a grid cell is counted as intersecting if any part of that cell is touched by the catchment polygon. In `center` mode, a cell is counted only if its center point falls within the catchment polygon.
- `cell_count` is fixed per catchment because this geometric lookup is created once and reused for all dates.
- For each entry in `variables`, the configured `reducer` (`min`, `mean`, or `max`) is applied daily over each catchment's member cells, producing the named `output` column. With the shipped temperature config:
  - `t2_min_zonal_min`: minimum of member-cell values from `t2_min`
  - `t2_mean_zonal_mean`: arithmetic mean of member-cell values from `t2_mean`
  - `t2_max_zonal_max`: maximum of member-cell values from `t2_max`

To override the configured cell membership mode for a single run, pass `--cell-membership all_touched` or `--cell-membership center` to the Python command or sbatch script.

## Config-Driven Parameters
Edit `config/pipeline_config.json` to swap variables, datasets, or polygons without code changes:
- `io.shapefile`, `io.id_field`, `io.data_root`
- `years.start`, `years.end`
- `variables`: list of `{family, reducer, output}` entries — add, remove, or repurpose entries to process any variable family present under `io.data_root`, not just temperature
- `naming.file_template`: e.g. `{family}_{year}_daily_era5_4km_3338.nc`
- `spatial.target_crs`, `spatial.cell_membership`
- `outputs.*` for preflight/yearly/final/qc paths and format
- `performance.time_chunk`, `performance.workers_default`

## Scripts
- `scripts/check_inputs.py`: preflight validation of shapefile and per-variable input files
- `scripts/zonal_watersheds.py`: main per-year zonal computation, finalize, and QC
- `scripts/finalize_zonal.py`: standalone merge of yearly outputs and QC (used by the Slurm array + finalize workflow)
- `scripts/config_utils.py`: shared config loading helpers
