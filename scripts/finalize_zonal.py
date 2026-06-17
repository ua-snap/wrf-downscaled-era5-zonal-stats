#!/usr/bin/env python3
"""Merge yearly zonal outputs and run full-period QC checks from config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

import pandas as pd

from config_utils import DEFAULT_CONFIG, cfg_get, load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize yearly zonal outputs")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to JSON config file")
    parser.add_argument("--yearly-dir", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--out-format", choices=["parquet", "csv"], default=None)
    parser.add_argument("--qc-out", default=None)
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--id-field", default=None)
    return parser.parse_args()


def expected_year_paths(yearly_dir: Path, start_year: int, end_year: int, out_format: str) -> List[Path]:
    suffix = ".parquet" if out_format == "parquet" else ".csv"
    name = "part.parquet" if out_format == "parquet" else "part.csv"
    paths: List[Path] = []
    for year in range(start_year, end_year + 1):
        p = yearly_dir / f"year={year}" / name
        if not p.exists() or p.suffix != suffix:
            raise FileNotFoundError(f"Missing yearly output for {year}: {p}")
        paths.append(p)
    return paths


def finalize_output(yearly_paths: List[Path], out_path: Path, out_format: str, id_field: str) -> None:
    frames = []
    for path in yearly_paths:
        if out_format == "parquet":
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
        raise ValueError(
            f"Row count mismatch: rows={summary['rows']} expected={summary['expected_rows']}"
        )
    if summary["date_min"] != str(start.date()) or summary["date_max"] != str(end.date()):
        raise ValueError(
            f"Date coverage mismatch: {summary['date_min']}..{summary['date_max']} vs {start.date()}..{end.date()}"
        )


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config)

    yearly_dir = Path(args.yearly_dir or cfg_get(cfg, "outputs.yearly_dir"))
    out_path = Path(args.out or cfg_get(cfg, "outputs.final_path"))
    out_format = str(args.out_format or cfg_get(cfg, "outputs.format", "parquet"))
    qc_out = Path(args.qc_out or cfg_get(cfg, "outputs.qc_path"))

    start_year = args.start_year if args.start_year is not None else int(cfg_get(cfg, "years.start"))
    end_year = args.end_year if args.end_year is not None else int(cfg_get(cfg, "years.end"))
    id_field = str(args.id_field or cfg_get(cfg, "io.id_field", "COMID"))

    variables_cfg = cfg.get("variables", [])
    metric_cols = [str(v["output"]) for v in variables_cfg]
    if not metric_cols:
        raise ValueError("Config must contain at least one variable definition")

    paths = expected_year_paths(yearly_dir, start_year, end_year, out_format)
    finalize_output(paths, out_path, out_format, id_field=id_field)
    print(f"Final output: {out_path}")

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
