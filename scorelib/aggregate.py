"""Sequential per-axis aggregation.

Given a LazyFrame with a value column and one or more axis columns, apply
the aggregation instructions for each axis, in `order`, until every axis is
collapsed and a single scalar remains for the value column.

See score_gui_design.md section 4.2 for the op catalogue.
"""
from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

import polars as pl

from .expression import evaluate_expression
from .models import AggregationSpec

_SIMPLE_OPS = {"mean", "sum", "min", "max"}
_SUBSET_OPS = {"mean_subset", "sum_subset", "min_subset", "max_subset"}
TRANSFORM_OPS = {"add"}

GroupDefs = Mapping[str, Mapping[str, Tuple[int, int]]]


def _group_col_expr(axis: str, ranges: Mapping[str, Tuple[int, int]]) -> pl.Expr:
    expr = pl.lit(None, dtype=pl.Utf8)
    for name, (lo, hi) in ranges.items():
        expr = pl.when((pl.col(axis) >= lo) & (pl.col(axis) <= hi)).then(pl.lit(name)).otherwise(expr)
    return expr


def _reduce(lf: pl.LazyFrame, value_col: str, group_keys: Sequence[str], op: str) -> pl.LazyFrame:
    agg_expr = getattr(pl.col(value_col), op)().alias(value_col)
    if group_keys:
        return lf.group_by(list(group_keys)).agg(agg_expr)
    return lf.select(agg_expr)


def apply_transform(lf: pl.LazyFrame, value_col: str, spec: AggregationSpec) -> pl.LazyFrame:
    """Apply a row-wise transform to the value column. Unlike apply_axis_op
    this collapses no axis; it is used by virtual "__xxx__" steps in `order`
    (e.g. an explicit offset-addition step placed before relative-ization).
    """
    if spec.op == "add":
        if spec.value is None:
            raise ValueError("transform op 'add' requires 'value'")
        return lf.with_columns((pl.col(value_col) + spec.value).alias(value_col))
    raise ValueError(f"unknown transform op '{spec.op}' (expected one of {sorted(TRANSFORM_OPS)})")


def apply_axis_op(
    lf: pl.LazyFrame,
    value_col: str,
    axis: str,
    spec: AggregationSpec,
    group_keys: Sequence[str],
    group_defs: GroupDefs | None = None,
) -> pl.LazyFrame:
    """Apply a single axis's aggregation instruction, collapsing `axis` away
    (the resulting frame no longer has an `axis` column, only `group_keys` +
    `value_col`).
    """
    if spec.op == "filter":
        return lf.filter(pl.col(axis) == spec.value).drop(axis)

    if spec.op in _SIMPLE_OPS:
        return _reduce(lf, value_col, group_keys, spec.op)

    if spec.op in _SUBSET_OPS:
        base_op = spec.op.split("_")[0]
        filtered = lf.filter(pl.col(axis).is_in(spec.values or []))
        return _reduce(filtered, value_col, group_keys, base_op)

    if spec.op == "group_reduce":
        if not group_defs or spec.group_def not in group_defs:
            raise ValueError(f"unknown group_def '{spec.group_def}' for axis '{axis}'")
        ranges = group_defs[spec.group_def]
        group_col = f"__grp_{axis}__"
        lf = lf.with_columns(_group_col_expr(axis, ranges).alias(group_col)).drop(axis)
        inner_keys = list(group_keys) + [group_col]
        lf = _reduce(lf, value_col, inner_keys, spec.inner_op or "mean")
        return _reduce(lf, value_col, list(group_keys), spec.outer_op or "mean")

    if spec.op == "expr":
        if not spec.expr:
            raise ValueError(f"expr op for axis '{axis}' requires 'expr'")
        if group_keys:
            df = lf.group_by(list(group_keys)).agg(pl.col(value_col)).collect()
            result = [
                evaluate_expression(spec.expr, {"values": vals}) for vals in df[value_col].to_list()
            ]
            return df.drop(value_col).with_columns(pl.Series(value_col, result)).lazy()
        all_values = lf.select(pl.col(value_col)).collect()[value_col].to_list()
        result_value = evaluate_expression(spec.expr, {"values": all_values})
        return pl.LazyFrame({value_col: [float(result_value)]})

    raise ValueError(f"unknown aggregation op '{spec.op}'")


def apply_aggregations(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: Dict[str, AggregationSpec],
    group_defs: GroupDefs | None = None,
) -> pl.LazyFrame:
    """Apply the aggregation instructions for each axis in `order`, collapsing
    those axes away one by one. Does not require the result to be a scalar --
    callers that process a partial order (e.g. axes before/after the
    `__relative__` step) use this directly.
    """
    for axis in order:
        if axis not in aggregations:
            raise ValueError(f"axis '{axis}' listed in order but has no aggregation instruction")
        spec = aggregations[axis]
        schema_cols = lf.collect_schema().names()
        if axis not in schema_cols:
            raise ValueError(f"axis '{axis}' not present (already aggregated away?): columns = {schema_cols}")
        group_keys = [c for c in schema_cols if c not in (value_col, axis)]
        lf = apply_axis_op(lf, value_col, axis, spec, group_keys, group_defs)
    return lf


def collapse_to_scalar(lf: pl.LazyFrame, value_col: str) -> float:
    df = lf.collect()
    if df.height != 1 or df.columns != [value_col]:
        raise ValueError(
            f"expected aggregation to collapse to a single value, got {df.height} rows "
            f"with columns {df.columns} (order did not cover all axes?)"
        )
    return float(df[value_col][0])


def aggregate_score_part(
    lf: pl.LazyFrame,
    value_col: str,
    order: Sequence[str],
    aggregations: Dict[str, AggregationSpec],
    group_defs: GroupDefs | None = None,
) -> float:
    """Run the full per-axis aggregation pipeline for a ScorePart, returning
    the resulting scalar value.
    """
    lf = apply_aggregations(lf, value_col, order, aggregations, group_defs)
    return collapse_to_scalar(lf, value_col)
