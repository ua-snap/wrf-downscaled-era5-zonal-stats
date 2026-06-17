#!/usr/bin/env python3
"""Shared config helpers for zonal workflow scripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


DEFAULT_CONFIG = str(Path(__file__).resolve().parent.parent / "config" / "pipeline_config.json")


def load_config(path: str | Path) -> Dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def cfg_get(cfg: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    current: Any = cfg
    for key in dotted_key.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
