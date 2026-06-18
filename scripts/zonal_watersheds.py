#!/usr/bin/env python3
"""Compute daily configurable zonal statistics by polygon ID from curated gridded data."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from config_utils import DEFAULT_CONFIG, cfg_get, load_config
from exactextract_zonal import load_polygons, per_year_stats_exactextract


VALID_AGGREGATION_METHODS = {"min", "mean", "max"}
VALID_ENGINES = {"rasterize", "exactextract"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configurable zonal stats by polygon")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to JSON config file")

    parser.add_argument("--shapefile", default=None)
    parser.add_argument("--id-field", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)

    parser.add_argument("--out", default=None)
    parser.add_argument("--out-format", choices=["parquet", "csv"], default=None)
    parser.add_argument("--yearly-dir", default=None)
    parser.add_argument("--qc-out", default=None)

    parser.add_argument("--time-chunk", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)

    parser.add_argument(
        "--cell-membership",
        choices=["all_touched", "center"],
        default=None,
        help="Cell inclusion rule for polygon rasterization (rasterize engine only)",
    )
    parser.add_argument("--target-crs", default=None, help="Target CRS override, e.g. EPSG:3338")
    parser.add_argument(
        "--engine",
        choices=sorted(VALID_ENGINES),
        default=None,
        help="Zonal stats engine: 'rasterize' (default) or 'exactextract'",
    )

    parser.add_argument("--skip-finalize", action="store_true")
    parser.add_argument("--skip-qc", action="store_true")
    return parser.parse_args()


def infer_workers(cli_workers: int | None, workers_default: int) -> int:
    if cli_workers is not None and cli_workers > 0:
        return cli_workers
    slurm = os.environ.get("SLURM_CPUS_PER_TASK")
    if slurm and slurm.isdigit() and int(slurm) > 0:
        return int(slurm)
    return workers_default


def discover_year_file(data_root: Path, variable: str, year: int, file_template: str) -> Path:
    candidate = data_root / variable / file_template.format(variable=variable, year=year)
    if not candidate.exists():
        raise FileNotFoundError(f"Missing file for {variable} {year}: {candidate}")
    return candidate


def first_data_var(ds: xr.Dataset) -> str:
    for name, da in ds.data_vars.items():
        if "time" in da.dims:
            return name
    if ds.data_vars:
        return list(ds.data_vars)[0]
    raise ValueError("No data variable found")


def infer_spatial_dims(da: xr.DataArray) -> Tuple[str, str]:
    dims = [d for d in da.dims if d != "time"]
    if len(dims) < 2:
        raise ValueError(f"Expected at least two non-time dims, got {da.dims}")

    x_dim = next((d for d in dims if d.lower() in {"x", "lon", "longitude"}), None)
    y_dim = next((d for d in dims if d.lower() in {"y", "lat", "latitude"}), None)

    if x_dim and y_dim:
        return x_dim, y_dim
    return dims[-1], dims[-2]


def make_transform_and_shape(da: xr.DataArray, x_dim: str, y_dim: str) -> Tuple[rasterio.Affine, Tuple[int, int]]:
    xs = np.asarray(da[x_dim].values)
    ys = np.asarray(da[y_dim].values)
    if xs.ndim != 1 or ys.ndim != 1:
        raise ValueError("Expected 1D x/y coordinates")

    nx = len(xs)
    ny = len(ys)
    if nx < 2 or ny < 2:
        raise ValueError("Need at least 2 cells in each spatial dimension")

    xres = float(np.median(np.diff(np.sort(xs))))
    yres = float(np.median(np.diff(np.sort(ys))))

    xmin = float(xs.min() - xres / 2.0)
    xmax = float(xs.max() + xres / 2.0)
    ymin = float(ys.min() - yres / 2.0)
    ymax = float(ys.max() + yres / 2.0)

    transform = from_bounds(xmin, ymin, xmax, ymax, nx, ny)
    return transform, (ny, nx)


def build_cell_lookup(
    shapefile: Path,
    id_field: str,
    template_da: xr.DataArray,
    x_dim: str,
    y_dim: str,
    all_touched: bool,
    target_crs: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    gdf = gpd.read_file(shapefile)
    if id_field not in gdf.columns:
        raise ValueError(f"Missing {id_field} in shapefile")

    if gdf.crs is None:
        raise ValueError("Shapefile CRS is undefined")
    if str(gdf.crs).upper() != str(target_crs).upper():
        gdf = gdf.to_crs(target_crs)

    gdf = gdf[[id_field, "geometry"]].copy()
    gdf[id_field] = gdf[id_field].astype(int)

    # rasterize() paints shapes in order and the last one drawn wins each pixel; sorting
    # largest-first ensures small polygons fully covered by a larger neighbor still claim
    # their own touched cells instead of being overwritten and dropped entirely.
    gdf = gdf.loc[gdf.geometry.area.sort_values(ascending=False).index]

    transform, out_shape = make_transform_and_shape(template_da, x_dim, y_dim)
    shapes = ((geom, int(poly_id)) for geom, poly_id in zip(gdf.geometry, gdf[id_field]))

    raster = rasterize(
        shapes=shapes,
        out_shape=out_shape,
        transform=transform,
        fill=0,
        all_touched=all_touched,
        dtype=np.int32,
    )

    flat = raster.reshape(-1)
    valid = flat > 0
    unique_ids, counts = np.unique(flat[valid], return_counts=True)
    return flat, unique_ids.astype(np.int64), counts.astype(np.int64)


def spatial_stack(da: xr.DataArray, x_dim: str, y_dim: str) -> xr.DataArray:
    return da.stack(cell=(y_dim, x_dim)).transpose("time", "cell")


def per_year_stats(
    year: int,
    data_root: Path,
    variables_cfg: List[Dict[str, str]],
    file_template: str,
    flat_ids: np.ndarray,
    valid_ids: np.ndarray,
    cell_counts: np.ndarray,
    time_chunk: int,
    id_field: str,
) -> pd.DataFrame:
    valid_mask = flat_ids > 0
    id_values = flat_ids[valid_mask]
    id_index = np.searchsorted(valid_ids, id_values)
    n_ids = len(valid_ids)

    times_ref: pd.DatetimeIndex | None = None
    metric_results: Dict[str, np.ndarray] = {}

    for vcfg in variables_cfg:
        variable = str(vcfg["variable"])
        aggregation_method = str(vcfg["aggregation_method"])
        out_name = str(vcfg["output"])

        path = discover_year_file(data_root, variable, year, file_template)
        with xr.open_dataset(path, chunks={"time": time_chunk}) as ds:
            var = first_data_var(ds)
            da = ds[var]
            x_dim, y_dim = infer_spatial_dims(da)
            arr = spatial_stack(da, x_dim, y_dim)

            times = pd.to_datetime(arr["time"].values)
            if times_ref is None:
                times_ref = pd.DatetimeIndex(times)
            elif len(times) != len(times_ref) or not np.array_equal(times.values, times_ref.values):
                raise ValueError(f"Time coordinate mismatch in year {year} for {variable}")

            n_time = len(times)
            out = np.full((n_time, n_ids), np.nan, dtype=np.float64)

            for ti in range(n_time):
                values = np.asarray(arr.isel(time=ti).values)[valid_mask].astype(np.float64, copy=False)
                finite = np.isfinite(values)
                if not finite.any():
                    continue

                idx = id_index[finite]
                vals = values[finite]

                if aggregation_method == "mean":
                    sums = np.bincount(idx, weights=vals, minlength=n_ids)
                    counts = np.bincount(idx, minlength=n_ids)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        day = sums / counts
                    day[counts == 0] = np.nan
                elif aggregation_method == "min":
                    day = np.full(n_ids, np.inf, dtype=np.float64)
                    np.minimum.at(day, idx, vals)
                    day[np.isinf(day)] = np.nan
                elif aggregation_method == "max":
                    day = np.full(n_ids, -np.inf, dtype=np.float64)
                    np.maximum.at(day, idx, vals)
                    day[np.isneginf(day)] = np.nan
                else:
                    raise ValueError(f"Unsupported aggregation method: {aggregation_method}")

                out[ti, :] = day

            metric_results[out_name] = out

    if times_ref is None:
        raise ValueError(f"No time coordinate found for year {year}")

    base_df = pd.DataFrame({id_field: valid_ids, "cell_count": cell_counts})
    grid = pd.MultiIndex.from_product([times_ref, valid_ids], names=["date", id_field]).to_frame(index=False)

    out = grid
    for metric_name, metric_values in metric_results.items():
        out[metric_name] = metric_values.reshape(-1)

    out = out.merge(base_df, on=id_field, how="left")
    ordered_cols = [id_field, "date", "cell_count"] + [str(v["output"]) for v in variables_cfg]
    out = out[ordered_cols]
    out[id_field] = out[id_field].astype(np.int64)
    out["cell_count"] = out["cell_count"].astype(np.int64)
    return out


def write_year_output(df: pd.DataFrame, yearly_dir: Path, year: int, out_format: str) -> Path:
    year_dir = yearly_dir / f"year={year}"
    year_dir.mkdir(parents=True, exist_ok=True)
    if out_format == "parquet":
        out_path = year_dir / "part.parquet"
        df.to_parquet(out_path, index=False)
    else:
        out_path = year_dir / "part.csv"
        df.to_csv(out_path, index=False)
    return out_path


def finalize_output(yearly_paths: List[Path], out_path: Path, out_format: str, id_field: str) -> None:
    frames = []
    for path in yearly_paths:
        if path.suffix == ".parquet":
            frames.append(pd.read_parquet(path))
        else:
            frames.append(pd.read_csv(path, parse_dates=["date"]))

    all_df = pd.concat(frames, ignore_index=True)
    all_df["date"] = pd.to_datetime(all_df["date"])
    all_df = all_df.sort_values([id_field, "date"]).reset_index(drop=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_format == "parquet":
        all_df.to_parquet(out_path, index=False)
    else:
        all_df.to_csv(out_path, index=False)


def build_qc_summary(
    df_path: Path,
    out_format: str,
    qc_out: Path,
    start_year: int,
    end_year: int,
    id_field: str,
    metric_cols: List[str],
) -> None:
    if out_format == "parquet":
        df = pd.read_parquet(df_path)
    else:
        df = pd.read_csv(df_path, parse_dates=["date"])

    df["date"] = pd.to_datetime(df["date"])

    start = pd.Timestamp(f"{start_year}-01-01")
    end = pd.Timestamp(f"{end_year}-12-31")
    expected_days = len(pd.date_range(start=start, end=end, freq="D"))

    n_polygons = int(df[id_field].nunique())
    expected_rows = int(n_polygons * expected_days)

    missing = {col: int(df[col].isna().sum()) for col in metric_cols}
    summary = {
        "rows": int(len(df)),
        "expected_rows": expected_rows,
        "polygons": n_polygons,
        "id_field": id_field,
        "date_min": str(df["date"].min().date()),
        "date_max": str(df["date"].max().date()),
        "missing_values": missing,
    }

    qc_out.parent.mkdir(parents=True, exist_ok=True)
    qc_out.write_text(json.dumps(summary, indent=2))

    if summary["rows"] != summary["expected_rows"]:
        raise ValueError(f"Row count mismatch: rows={summary['rows']} expected={summary['expected_rows']}")
    if summary["date_min"] != str(start.date()) or summary["date_max"] != str(end.date()):
        raise ValueError(
            f"Date coverage mismatch: {summary['date_min']}..{summary['date_max']} vs {start.date()}..{end.date()}"
        )


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)

    shapefile = Path(args.shapefile or cfg_get(cfg, "io.shapefile"))
    id_field = str(args.id_field or cfg_get(cfg, "io.id_field", "COMID"))
    data_root = Path(args.data_root or cfg_get(cfg, "io.data_root"))

    start_year = args.start_year if args.start_year is not None else int(cfg_get(cfg, "years.start"))
    end_year = args.end_year if args.end_year is not None else int(cfg_get(cfg, "years.end"))

    out_path = Path(args.out or cfg_get(cfg, "outputs.final_path"))
    out_format = str(args.out_format or cfg_get(cfg, "outputs.format", "parquet"))
    yearly_dir = Path(args.yearly_dir or cfg_get(cfg, "outputs.yearly_dir"))
    qc_out = Path(args.qc_out or cfg_get(cfg, "outputs.qc_path"))

    time_chunk = args.time_chunk if args.time_chunk is not None else int(cfg_get(cfg, "performance.time_chunk", 31))
    workers_default = int(cfg_get(cfg, "performance.workers_default", 8))
    workers = infer_workers(args.workers, workers_default)

    cell_membership = str(args.cell_membership or cfg_get(cfg, "spatial.cell_membership", "all_touched"))
    target_crs = str(args.target_crs or cfg_get(cfg, "spatial.target_crs", "EPSG:3338"))

    engine = str(args.engine or cfg_get(cfg, "spatial.engine", "rasterize"))
    if engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported engine {engine}; expected one of {sorted(VALID_ENGINES)}")
    if engine == "exactextract" and (args.cell_membership or cfg_get(cfg, "spatial.cell_membership")):
        print(
            "Note: spatial.cell_membership / --cell-membership is ignored by the "
            "exactextract engine; coverage-fraction weighting supersedes the "
            "binary all_touched/center distinction. See README.md."
        )

    variables_cfg = cfg.get("variables", [])
    if not variables_cfg:
        raise ValueError("Config must contain at least one variable definition")

    for v in variables_cfg:
        for key in ("variable", "aggregation_method", "output"):
            if key not in v:
                raise ValueError(f"Variable definition missing key '{key}': {v}")
        if str(v["aggregation_method"]) not in VALID_AGGREGATION_METHODS:
            raise ValueError(
                f"Unsupported aggregation method {v['aggregation_method']}; "
                f"expected one of {sorted(VALID_AGGREGATION_METHODS)}"
            )

    file_template = str(cfg_get(cfg, "naming.file_template"))
    if not file_template:
        raise ValueError("Config naming.file_template is required")

    os.environ.setdefault("OMP_NUM_THREADS", str(workers))
    os.environ.setdefault("MKL_NUM_THREADS", str(workers))

    year_paths: List[Path] = []

    if engine == "rasterize":
        sample = discover_year_file(data_root, str(variables_cfg[0]["variable"]), start_year, file_template)
        with xr.open_dataset(sample) as ds:
            var = first_data_var(ds)
            da = ds[var]
            x_dim, y_dim = infer_spatial_dims(da)
            flat_ids, valid_ids, cell_counts = build_cell_lookup(
                shapefile=shapefile,
                id_field=id_field,
                template_da=da,
                x_dim=x_dim,
                y_dim=y_dim,
                all_touched=(cell_membership == "all_touched"),
                target_crs=target_crs,
            )

        if len(valid_ids) == 0:
            raise ValueError("No rasterized polygon cells were found")

        for year in range(start_year, end_year + 1):
            print(f"Processing year {year}...")
            year_df = per_year_stats(
                year=year,
                data_root=data_root,
                variables_cfg=variables_cfg,
                file_template=file_template,
                flat_ids=flat_ids,
                valid_ids=valid_ids,
                cell_counts=cell_counts,
                time_chunk=time_chunk,
                id_field=id_field,
            )
            ypath = write_year_output(year_df, yearly_dir, year, out_format)
            year_paths.append(ypath)
            print(f"Wrote {ypath} ({len(year_df)} rows)")

    elif engine == "exactextract":
        gdf = load_polygons(shapefile, id_field, target_crs)

        for year in range(start_year, end_year + 1):
            print(f"Processing year {year}...")
            year_df = per_year_stats_exactextract(
                year=year,
                data_root=data_root,
                variables_cfg=variables_cfg,
                file_template=file_template,
                gdf=gdf,
                id_field=id_field,
                target_crs=target_crs,
                discover_year_file_fn=discover_year_file,
                first_data_var_fn=first_data_var,
            )
            ypath = write_year_output(year_df, yearly_dir, year, out_format)
            year_paths.append(ypath)
            print(f"Wrote {ypath} ({len(year_df)} rows)")

    if args.skip_finalize:
        print("Skipping finalize step; yearly outputs are complete.")
        return 0

    finalize_output(year_paths, out_path, out_format, id_field=id_field)
    print(f"Final output: {out_path}")

    if args.skip_qc:
        print("Skipping QC summary generation.")
        return 0

    metric_cols = [str(v["output"]) for v in variables_cfg]
    build_qc_summary(
        df_path=out_path,
        out_format=out_format,
        qc_out=qc_out,
        start_year=start_year,
        end_year=end_year,
        id_field=id_field,
        metric_cols=metric_cols,
    )
    print(f"QC summary: {qc_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
