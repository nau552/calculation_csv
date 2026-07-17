"""Resolve only the axes a ScorePart actually needs, without materializing a
fully expanded (FBC_expanded.csv-like) dataframe.

For a given `type_` (e.g. "FBC"), the following files are expected in
`data_dir`, following the current naming convention (see
score_gui_design.md section 3.1/3.2):

- ``{type_}.csv``: measured axes (Board, Chip, Block, WL, STR, State, ...)
  + ``Measure`` + a value column named after the type (e.g. ``FBC``)
- ``parameterLabel_{type_}.csv``: resolves ``Measure`` -> Erase/Program/Read
  Label + Override, joined on (InBatchEpoch, Board, Chip, Block, Measure)
- ``dataName_{type_}.csv``: resolves ``Measure`` -> DataName (numeric),
  same join key
- ``map_*.csv``: shared numeric -> text lookups (2 columns, no header).
  The mapping file for an axis is found generically: ``{Erase,Program,Read}_Label``
  -> ``map_Label.csv``, ``*_Override`` -> ``map_Override.csv``, ``DataName`` ->
  ``map_dataName.csv``, and any other axis ``X`` -> ``map_X.csv`` if that file
  exists (e.g. ``State`` -> ``map_State.csv``, ``Page`` -> ``map_Page.csv``).
  Axes with no mapping file (WL, STR, Board, ...) stay numeric.

Only mapping files needed for the requested axes are read/joined.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import polars as pl

JOIN_KEYS = ["InBatchEpoch", "Board", "Chip", "Block", "Measure"]

OVERRIDE_SUFFIX = "_Override"


def _map_file_for_axis(data_dir: Path, axis: str) -> Path | None:
    if axis.endswith("_Label"):
        name = "map_Label.csv"
    elif axis.endswith(OVERRIDE_SUFFIX):
        name = "map_Override.csv"
    elif axis == "DataName":
        name = "map_dataName.csv"
    else:
        name = f"map_{axis}.csv"
    path = data_dir / name
    return path if path.exists() else None


def _scan_map_file(path: Path, code_col: str, text_col: str) -> pl.LazyFrame:
    return pl.scan_csv(path, has_header=False, new_columns=[code_col, text_col])


def resolve_axes(
    data_dir: str | Path,
    type_: str,
    required_axes: Iterable[str],
) -> pl.LazyFrame:
    """Return a LazyFrame with `type_` (the value column) plus every axis in
    `required_axes`, resolved to human-readable text where applicable
    (Label/Override/State/DataName), joined in only where actually needed.
    """
    data_dir = Path(data_dir)
    required_axes = set(required_axes)
    value_col = type_

    lf = pl.scan_csv(data_dir / f"{type_}.csv")
    base_cols = set(lf.collect_schema().names())

    # Axes not present in {type}.csv itself come from parameterLabel_{type}.csv
    # (Erase/Program/Read Label + Override) or dataName_{type}.csv (DataName).
    missing_axes = required_axes - base_cols

    label_path = data_dir / f"parameterLabel_{type_}.csv"
    if missing_axes - {"DataName"} and label_path.exists():
        label_lf = pl.scan_csv(label_path)
        label_cols = [c for c in label_lf.collect_schema().names() if c not in JOIN_KEYS]
        take = [c for c in label_cols if c in missing_axes]
        if take:
            lf = lf.join(label_lf.select(JOIN_KEYS + take), on=JOIN_KEYS, how="left")

    if "DataName" in missing_axes:
        dn_lf = pl.scan_csv(data_dir / f"dataName_{type_}.csv")
        lf = lf.join(dn_lf.select(JOIN_KEYS + ["DataName"]), on=JOIN_KEYS, how="left")

    unresolvable = required_axes - set(lf.collect_schema().names())
    if unresolvable:
        raise ValueError(
            f"axes {sorted(unresolvable)} not found for type '{type_}' "
            f"(not in {type_}.csv, parameterLabel_{type_}.csv, or dataName_{type_}.csv)"
        )

    # Measure itself is only a join key, never exposed as an axis.
    if "Measure" in lf.collect_schema().names():
        lf = lf.drop("Measure")

    for axis in sorted(required_axes):
        map_path = _map_file_for_axis(data_dir, axis)
        if map_path is None:
            continue
        code_col, text_col = f"__code_{axis}", f"__text_{axis}"
        map_lf = _scan_map_file(map_path, code_col, text_col)
        lf = lf.join(map_lf, left_on=axis, right_on=code_col, how="left")
        lf = lf.drop(axis).rename({text_col: axis})
        if axis.endswith(OVERRIDE_SUFFIX) and lf.collect_schema()[axis] != pl.Boolean:
            # map_Override.csv's text column is usually auto-inferred as
            # Boolean by the CSV reader already (TRUE/FALSE literals); only
            # normalize by hand when it comes through as text instead.
            lf = lf.with_columns(
                pl.col(axis).cast(pl.Utf8).str.to_uppercase().is_in(["TRUE", "1"]).alias(axis)
            )

    # Keep only the value column + requested axes (drop anything incidental,
    # e.g. join keys that were not asked for).
    keep = [value_col] + sorted(a for a in required_axes if a in lf.collect_schema().names())
    lf = lf.select(keep)
    return lf
