#!/usr/bin/env python3
"""Preflight input checks for configurable zonal statistics workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import geopandas as gpd
import xarray as xr

from config_utils import DEFAULT_CONFIG, cfg_get, load_config

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate zonal statistics inputs")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to JSON config file")
    parser.add_argument(
        "--shapefile",
        default=None,
        help="Optional shapefile override",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="Optional data root override",
    )
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--id-field", default=None)
    parser.add_argument(
        "--summary-out",
        default=None,
        help="Optional summary output override",
    )
    return parser.parse_args()


def expected_years(start_year: int, end_year: int) -> List[int]:
    return list(range(start_year, end_year + 1))


def discover_variable_files(root: Path, variable: str, file_template: str, years: List[int]) -> Dict[int, Path]:
    year_to_path: Dict[int, Path] = {}
    for year in years:
        path = root / variable / file_template.format(variable=variable, year=year)
        if path.exists():
            year_to_path[year] = path
    return year_to_path


def first_data_var(ds: xr.Dataset) -> str:
    for name, da in ds.data_vars.items():
        if "time" in da.dims and len(da.dims) >= 3:
            return name
    if ds.data_vars:
        return list(ds.data_vars)[0]
    raise ValueError("No data variables found in dataset")


def infer_spatial_dims(ds: xr.Dataset, data_var: str) -> Tuple[str, str]:
    dims = list(ds[data_var].dims)
    non_time = [d for d in dims if d != "time"]
    if len(non_time) < 2:
        raise ValueError(f"Could not infer 2 spatial dims from dims={dims}")

    x_dim = next((d for d in non_time if d.lower() in {"x", "lon", "longitude"}), None)
    y_dim = next((d for d in non_time if d.lower() in {"y", "lat", "latitude"}), None)

    if x_dim and y_dim:
        return x_dim, y_dim

    # fallback: assume last two non-time dims are spatial
    return non_time[-1], non_time[-2]


def validate_files(
    root: Path,
    variable_names: List[str],
    file_template: str,
    start_year: int,
    end_year: int,
) -> Dict[str, Dict[str, Iterable[int]]]:
    years = expected_years(start_year, end_year)
    summaries: Dict[str, Dict[str, Iterable[int]]] = {}

    for variable in variable_names:
        mapping = discover_variable_files(root, variable, file_template, years)
        available = sorted(mapping.keys())
        missing = [y for y in years if y not in mapping]
        summaries[variable] = {
            "available_years": available,
            "missing_years": missing,
            "count": len(available),
        }
        if missing:
            raise ValueError(f"{variable} missing years: {missing}")
    return summaries


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)

    shapefile = args.shapefile or cfg_get(cfg, "io.shapefile")
    data_root = args.data_root or cfg_get(cfg, "io.data_root")
    id_field = args.id_field or cfg_get(cfg, "io.id_field", "COMID")
    start_year = args.start_year if args.start_year is not None else int(cfg_get(cfg, "years.start"))
    end_year = args.end_year if args.end_year is not None else int(cfg_get(cfg, "years.end"))
    summary_out = args.summary_out or cfg_get(cfg, "outputs.preflight_summary")
    file_template = str(cfg_get(cfg, "naming.file_template"))

    variables_cfg = cfg.get("variables", [])
    variable_names = [str(v["variable"]) for v in variables_cfg]
    if not variable_names:
        raise ValueError("Config must contain at least one variable definition")

    summary_path = Path(summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    shp_path = Path(shapefile)
    if not shp_path.exists():
        raise FileNotFoundError(f"Shapefile not found: {shp_path}")

    root = Path(data_root)
    file_summary = validate_files(root, variable_names, file_template, start_year, end_year)

    gdf = gpd.read_file(shp_path)
    if id_field not in gdf.columns:
        raise ValueError(f"Missing ID field {id_field} in shapefile")

    id_series = gdf[id_field]
    if id_series.isna().any():
        raise ValueError(f"ID field {id_field} contains null values")
    if id_series.duplicated().any():
        dupes = id_series[id_series.duplicated()].unique()[:10]
        raise ValueError(f"ID field {id_field} has duplicates (examples): {dupes}")

    sample_variable = variable_names[0]
    sample_path = root / sample_variable / file_template.format(variable=sample_variable, year=start_year)
    with xr.open_dataset(sample_path) as ds:
        data_var = first_data_var(ds)
        x_dim, y_dim = infer_spatial_dims(ds, data_var)
        dims = dict(ds[data_var].sizes)
        time_size = int(ds[data_var].sizes.get("time", 0))

    result: Dict[str, Any] = {
        "status": "ok",
        "config": str(Path(args.config)),
        "shapefile": str(shp_path),
        "id_field": id_field,
        "catchment_count": int(len(gdf)),
        "shapefile_crs": str(gdf.crs),
        "file_template": file_template,
        "variables": variable_names,
        "years": {"start": start_year, "end": end_year},
        "file_summary": file_summary,
        "sample_dataset": str(sample_path),
        "sample_data_var": data_var,
        "sample_dims": dims,
        "sample_x_dim": x_dim,
        "sample_y_dim": y_dim,
        "sample_time_len": time_size,
    }

    summary_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
