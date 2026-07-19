"""Derive UI-facing metadata (available types, axes, value candidates) from
a definition-file directory.

Phase1 assumption (see score_gui_ui_design.md section 5.1): the directory is
the output set of a past run of the same experiment family (result_tmp-like),
so types/axes/value candidates can be read from real files. When the current
GUI later provides a manifest describing future outputs, only this module's
data source changes -- the function signatures stay.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import polars as pl

from . import jsonc
from .axis_resolve import JOIN_KEYS, OVERRIDE_SUFFIX, _map_file_for_axis, resolve_axes

# csv stems that are never a measurement type
_RESERVED_STEMS = {"initial_temperature", "reference_param", "FBC_expanded"}
_TYPE_FILE_PREFIXES = ("parameterLabel_", "dataName_", "map_")


def detect_types(data_dir: str | Path) -> List[str]:
    """Measurement types present in the directory, from file naming
    (parameterLabel_{t}.csv / dataName_{t}.csv) plus plain {t}.csv files
    that look like measurement output (have a Measure column)."""
    data_dir = Path(data_dir)
    types: set[str] = set()
    for f in data_dir.glob("*.csv"):
        stem = f.stem
        if stem.startswith("parameterLabel_"):
            types.add(stem[len("parameterLabel_"):])
        elif stem.startswith("dataName_"):
            types.add(stem[len("dataName_"):])
    for f in data_dir.glob("*.csv"):
        stem = f.stem
        if stem in types or stem in _RESERVED_STEMS or stem.startswith(_TYPE_FILE_PREFIXES):
            continue
        try:
            cols = pl.scan_csv(f).collect_schema().names()
        except Exception:
            continue
        if "Measure" in cols:
            types.add(stem)
    return sorted(types)


def find_dvtbudget_coefs(data_dir: str | Path) -> List[Path]:
    """Every jsonc file whose CONTENT looks like a dVtBudget coefficient
    table (generation -> temperature -> state -> {a, b}). Identification is
    by shape, never by filename; callers decide what to do when more than
    one matches (the UI refuses to pick silently)."""
    from .io_jsonc import load_dvtbudget_coef

    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            coef = load_dvtbudget_coef(f)
        except Exception:
            continue
        if coef.root and all(
            temps and all(states for states in temps.values()) for temps in coef.root.values()
        ):
            # require the {a, b} leaf shape to have matched at least one entry
            found.append(f)
    return found


def find_dvtbudget_coef(data_dir: str | Path) -> Optional[Path]:
    found = find_dvtbudget_coefs(data_dir)
    return found[0] if found else None


def find_generation_info(data_dir: str | Path, generation: Optional[str]) -> Optional[Path]:
    """The per-generation chip-info json ({Generation}.json: numWLs,
    numStrings, ...) when it happens to sit in the data directory."""
    if not generation:
        return None
    p = Path(data_dir) / f"{generation}.json"
    return p if p.is_file() else None


def find_run_configs(data_dir: str | Path) -> List[Path]:
    """Every jsonc file whose CONTENT has a top-level optimization{} block
    (sample.jsonc shape). Shape-based like find_dvtbudget_coefs; the two
    shapes are mutually exclusive (a coef table has no "optimization" key,
    a run config never validates as a 3-level {a, b} table)."""
    found = []
    for f in sorted(Path(data_dir).glob("*.jsonc")):
        try:
            content = jsonc.load(f)
        except Exception:
            continue
        if isinstance(content, dict) and "optimization" in content:
            found.append(f)
    return found


def find_run_config(data_dir: str | Path) -> Optional[Path]:
    found = find_run_configs(data_dir)
    return found[0] if found else None


def available_part_types(data_dir: str | Path) -> List[str]:
    """Types offered in the UI's type dropdown: measurement types, plus
    dVtBudget when FBC data and a coefficient file are both present."""
    types = detect_types(data_dir)
    if "FBC" in types and find_dvtbudget_coef(data_dir) is not None:
        types.append("dVtBudget")
    return types


def axis_catalog(data_dir: str | Path, type_: str) -> Dict[str, Optional[list]]:
    """Axes usable by score parts of `type_`, in default display order
    (measured axes in csv-header order, then label axes), each mapped to its
    value candidates (None = free numeric input only).

    For type_ == "dVtBudget" the FBC catalog is returned (it reads FBC.csv).
    """
    data_dir = Path(data_dir)
    source_type = "FBC" if type_ == "dVtBudget" else type_

    measured: List[str] = []
    tcsv = data_dir / f"{source_type}.csv"
    if tcsv.exists():
        cols = pl.scan_csv(tcsv).collect_schema().names()
        measured = [c for c in cols if c not in ("Measure", source_type)]

    label_axes: List[str] = []
    plabel = data_dir / f"parameterLabel_{source_type}.csv"
    if plabel.exists():
        cols = pl.scan_csv(plabel).collect_schema().names()
        label_axes = [c for c in cols if c not in JOIN_KEYS]

    catalog: Dict[str, Optional[list]] = {}
    for axis in measured + [a for a in label_axes if a not in measured]:
        catalog[axis] = _candidates(data_dir, source_type, axis, tcsv if axis in measured else None)
    return catalog


def _candidates(data_dir: Path, source_type: str, axis: str, tcsv: Optional[Path]) -> Optional[list]:
    if axis.endswith(OVERRIDE_SUFFIX):
        # False (non-override / reference measurement) first: it always exists
        return [False, True]
    map_path = _map_file_for_axis(data_dir, axis)
    if map_path is not None:
        m = pl.read_csv(map_path, has_header=False, new_columns=["code", "text"])
        full = m["text"].to_list()
        # Prefer the values actually present in the past data (in map order):
        # the part skeleton filters on the first candidate, and a candidate
        # absent from the data would make the skeleton fail with
        # "filter matched no rows". Fall back to the full map vocabulary.
        try:
            present = set(
                resolve_axes(data_dir, source_type, {axis})
                .select(axis).unique().collect()[axis].to_list()
            )
        except Exception:
            return full
        narrowed = [v for v in full if v in present]
        return narrowed or full
    if tcsv is not None:
        return pl.scan_csv(tcsv).select(axis).unique().sort(axis).collect()[axis].to_list()
    return None
