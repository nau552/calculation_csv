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
        # optional `value` list restricts the reduction to those selections
        target = lf.filter(pl.col(axis).is_in(spec.value)) if spec.value is not None else lf
        return _reduce(target, value_col, group_keys, spec.op)

    if spec.op == "diff":
        a_val, b_val = spec.value
        a = lf.filter(pl.col(axis) == a_val).drop(axis)
        b = lf.filter(pl.col(axis) == b_val).drop(axis).rename({value_col: "__b__"})
        keys = list(group_keys)
        joined = a.join(b, on=keys, how="left") if keys else a.join(b, how="cross")
        return joined.with_columns((pl.col(value_col) - pl.col("__b__")).alias(value_col)).drop("__b__")

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

        def _eval(vals: list, axis_vals: list) -> float:
            by: dict = {}
            for k, v in zip(axis_vals, vals):
                if k in by:
                    raise ValueError(
                        f"axis value '{k}' appears more than once within a group for axis "
                        f"'{axis}'; 'by' lookups require unique axis values"
                    )
                by[k] = v
            return evaluate_expression(spec.expr, {"values": vals, "by": by})

        if group_keys:
            df = lf.group_by(list(group_keys)).agg([pl.col(value_col), pl.col(axis)]).collect()
            result = [
                _eval(vals, axis_vals)
                for vals, axis_vals in zip(df[value_col].to_list(), df[axis].to_list())
            ]
            return df.drop(value_col, axis).with_columns(pl.Series(value_col, result)).lazy()
        df = lf.select([pl.col(value_col), pl.col(axis)]).collect()
        result_value = _eval(df[value_col].to_list(), df[axis].to_list())
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


def collapse(
    lf: pl.LazyFrame, value_col: str, identity_axes: Sequence[str] = ()
) -> pl.DataFrame:
    """Verify the pipeline collapsed everything except `identity_axes` and
    return the result as a DataFrame (one row per identity-axis combination).

    identity_axes is empty today (single-epoch operation -> one scalar); it
    exists so batch processing over historical epochs can later keep an Epoch
    column through the whole pipeline and get one row per epoch.
    """
    df = lf.collect()
    expected = set(identity_axes) | {value_col}
    if set(df.columns) != expected:
        raise ValueError(
            f"expected aggregation to collapse to columns {sorted(expected)}, got {df.columns} "
            "(order did not cover all axes?)"
        )
    if not identity_axes and df.height != 1:
        raise ValueError(
            f"expected aggregation to collapse to a single value, got {df.height} rows"
        )
    if df[value_col].null_count() > 0:
        raise ValueError(
            f"aggregation produced null for '{value_col}' — a filter value probably "
            "matched no rows (check filter values against the data)"
        )
    return df


def collapse_to_scalar(lf: pl.LazyFrame, value_col: str) -> float:
    return float(collapse(lf, value_col)[value_col][0])


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
