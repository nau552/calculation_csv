"""Relative value (numerator/denominator) calculation.

split_axis (e.g. Read_Override or Program_Override, pending final
confirmation - see score_gui_design.md section 11) determines which rows are
"evaluation" (numerator) vs "reference" (denominator). The denominator can
optionally be pre-aggregated over some axes (e.g. mean over WL, STR) before
the ratio is taken.

denominator_offset is added to BOTH sides of the ratio
((num + offset) / (den + offset)): the denominator needs it to avoid division
by zero, and the numerator needs it because downstream transforms (dVtBudget's
log10) break on a relative value of exactly 0, which happens routinely when
the evaluated FBC is 0. Pending confirmation - see score_gui_design.md
section 11.
"""
from __future__ import annotations

import polars as pl

from .aggregate import GroupDefs, apply_axis_op
from .models import RelativeConfig


def apply_relative(
    lf: pl.LazyFrame,
    value_col: str,
    relative: RelativeConfig,
    group_defs: GroupDefs | None = None,
) -> pl.LazyFrame:
    axis = relative.split_axis

    numerator = lf.filter(pl.col(axis) == relative.numerator_when).drop(axis)
    denominator = lf.filter(pl.col(axis) == relative.denominator_when).drop(axis)

    for step in relative.denominator_pre_aggregation:
        schema_cols = denominator.collect_schema().names()
        group_keys = [c for c in schema_cols if c not in (value_col, step.axis)]
        denominator = apply_axis_op(denominator, value_col, step.axis, step, group_keys, group_defs)

    denominator = denominator.rename({value_col: "__denom__"})
    join_keys = [c for c in denominator.collect_schema().names() if c != "__denom__"]

    if join_keys:
        out = numerator.join(denominator, on=join_keys, how="left")
    else:
        # Denominator fully collapsed to a single scalar: broadcast it.
        out = numerator.join(denominator, how="cross")
    if relative.mode == "diff":
        # delta value: numerator - denominator. Offset would cancel out
        # ((num+o)-(den+o) == num-den), so it is simply not applied.
        combined = pl.col(value_col) - pl.col("__denom__")
    else:
        offset = relative.denominator_offset
        combined = (pl.col(value_col) + offset) / (pl.col("__denom__") + offset)
    out = out.with_columns(combined.alias(value_col)).drop("__denom__")
    return out
