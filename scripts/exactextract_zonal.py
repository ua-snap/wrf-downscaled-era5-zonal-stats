#!/usr/bin/env python3
"""Zonal statistics using the exactextract engine.

See PLAN.md for the background, benchmarks, and gotchas this module's design
is based on. Key points repeated here as code comments where they affect
correctness; see PLAN.md for the full "why".
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor used below)
import xarray as xr
from exactextract import exact_extract


def load_polygons(shapefile: Path, id_field: str, target_crs: str) -> gpd.GeoDataFrame:
    """Read polygons, reproject to target_crs if needed, cast id_field to int64.

    Unlike the rasterize engine's build_cell_lookup(), no largest-area-first
    sort is needed here: exactextract computes each feature's coverage
    independently via true vector/raster overlay, so overlapping or nested
    catchments each correctly get their own fractional coverage.
    """
    gdf = gpd.read_file(shapefile)
    if id_field not in gdf.columns:
        raise ValueError(f"Missing {id_field} in shapefile")
    if gdf.crs is None:
        raise ValueError("Shapefile CRS is undefined")
    if str(gdf.crs).upper() != str(target_crs).upper():
        gdf = gdf.to_crs(target_crs)

    gdf = gdf[[id_field, "geometry"]].copy()
    gdf[id_field] = gdf[id_field].astype(np.int64)
    return gdf


def open_variable_dataarray(
    path: Path, first_data_var_fn: Callable[[xr.Dataset], str], target_crs: str
) -> xr.DataArray:
    """Open one variable's yearly NetCDF, select the data var, and set CRS explicitly.

    Do not rely on automatic CRS detection: xr.open_dataset() does not pick up
    this repo's NetCDFs' CF grid_mapping variable, and exactextract silently
    falls back to EPSG:4326 if no CRS is set on the DataArray it's given.
    """
    ds = xr.open_dataset(path)
    var = first_data_var_fn(ds)
    da = ds[var]
    da = da.rio.write_crs(target_crs, inplace=False)
    return da


def _bands_to_long(
    wide: pd.DataFrame,
    id_field: str,
    stat: str,
    times: pd.DatetimeIndex,
    value_name: str,
) -> pd.DataFrame:
    """Reshape one stat's band_1_<stat> ... band_N_<stat> wide columns to long (id, date, value).

    exact_extract() returns one row per polygon with one column per
    (time step, stat) pair when given a multi-day DataArray -- every time
    step is treated as a separate raster band. This melts that wide layout
    back into the (id_field, date, value) long format the rest of this
    pipeline uses.
    """
    band_cols = [f"band_{i + 1}_{stat}" for i in range(len(times))]
    long_df = wide.melt(
        id_vars=[id_field], value_vars=band_cols, var_name="_band_col", value_name=value_name
    )
    band_idx = long_df["_band_col"].str.extract(r"band_(\d+)_" + stat + r"$")[0].astype(int)
    long_df["date"] = band_idx.map(lambda i: times[i - 1])
    return long_df[[id_field, "date", value_name]]


def per_year_stats_exactextract(
    year: int,
    data_root: Path,
    variables_cfg: List[Dict[str, str]],
    file_template: str,
    gdf: gpd.GeoDataFrame,
    id_field: str,
    target_crs: str,
    discover_year_file_fn: Callable[[Path, str, int, str], Path],
    first_data_var_fn: Callable[[xr.Dataset], str],
) -> pd.DataFrame:
    """One year's zonal stats for all configured variables, via exactextract.

    cell_count is computed once, from the first variable in variables_cfg,
    using count(coverage_weight=none) -- a literal tally of cells that
    intersect each polygon at all, excluding that day's NaN cells. This
    assumes the NaN mask is identical across all configured variables for a
    given year (verified for year 2000 during planning; see PLAN.md finding
    #8 -- spot-check this for additional years before fully trusting it at
    scale).
    """
    base_df: Optional[pd.DataFrame] = None
    times_ref: Optional[pd.DatetimeIndex] = None

    for i, vcfg in enumerate(variables_cfg):
        variable = str(vcfg["variable"])
        aggregation_method = str(vcfg["aggregation_method"])
        out_name = str(vcfg["output"])

        path = discover_year_file_fn(data_root, variable, year, file_template)
        da = open_variable_dataarray(path, first_data_var_fn, target_crs)

        times = pd.DatetimeIndex(da["time"].values)
        if times_ref is None:
            times_ref = times
        elif len(times) != len(times_ref) or not np.array_equal(times.values, times_ref.values):
            raise ValueError(f"Time coordinate mismatch in year {year} for {variable}")

        # Only the first variable's call also requests cell_count, via
        # count(coverage_weight=none) -- a literal tally of intersecting
        # cells, not the coverage-weighted default `count` (see PLAN.md
        # finding #9). The op string passed here includes the option, but
        # the auto-generated output column is still named "band_N_count"
        # (the options aren't reflected in the default field name) -- that's
        # why _bands_to_long is called with stat="count" below, not the full
        # op string.
        ops = (
            [aggregation_method]
            if i > 0
            else [aggregation_method, "count(coverage_weight=none)"]
        )
        wide = exact_extract(
            da,
            gdf,
            ops,
            include_cols=id_field,
            output="pandas",
            strategy="raster-sequential",
        )

        metric_long = _bands_to_long(wide, id_field, aggregation_method, times, out_name)
        if i == 0:
            count_long = _bands_to_long(wide, id_field, "count", times, "cell_count")
            base_df = metric_long.merge(count_long, on=[id_field, "date"])
        else:
            base_df = base_df.merge(metric_long, on=[id_field, "date"])

    assert base_df is not None
    ordered_cols = [id_field, "date", "cell_count"] + [str(v["output"]) for v in variables_cfg]
    out = base_df[ordered_cols].copy()
    out[id_field] = out[id_field].astype(np.int64)
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values([id_field, "date"]).reset_index(drop=True)
