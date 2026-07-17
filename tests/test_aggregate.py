import polars as pl
import pytest

from scorelib.aggregate import aggregate_score_part
from scorelib.models import AggregationSpec


def test_filter_then_mean():
    lf = pl.LazyFrame({"State": ["A", "A", "B", "B"], "WL": [0, 1, 0, 1], "value": [10, 20, 30, 40]})
    order = ["State", "WL"]
    aggregations = {
        "State": AggregationSpec(op="filter", value="A"),
        "WL": AggregationSpec(op="mean"),
    }
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(15.0)


def test_two_axis_mean_collapses_fully():
    lf = pl.LazyFrame(
        {
            "Group": ["a", "a", "a", "a", "b", "b", "b", "b"],
            "WL": [0, 1, 2, 3, 0, 1, 2, 3],
            "value": [10, 20, 30, 40, 5, 15, 25, 35],
        }
    )
    order = ["WL", "Group"]
    aggregations = {
        "WL": AggregationSpec(op="mean"),
        "Group": AggregationSpec(op="mean"),
    }
    # per-group WL mean: a -> 25, b -> 20; mean across groups -> 22.5
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(22.5)


def test_subset_op():
    lf = pl.LazyFrame({"STR": [0, 1, 2, 3, 4], "value": [10, 20, 30, 40, 50]})
    order = ["STR"]
    aggregations = {"STR": AggregationSpec(op="mean_subset", values=[0, 1, 2])}
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(20.0)


def test_group_reduce():
    lf = pl.LazyFrame({"WL": [0, 1, 2, 3], "value": [10, 5, 8, 20]})
    order = ["WL"]
    aggregations = {
        "WL": AggregationSpec(op="group_reduce", group_def="g", inner_op="min", outer_op="max"),
    }
    group_defs = {"g": {"g1": (0, 1), "g2": (2, 3)}}
    # g1 min(10,5)=5, g2 min(8,20)=8, outer max(5,8)=8
    assert aggregate_score_part(lf, "value", order, aggregations, group_defs) == pytest.approx(8.0)


def test_expr_op():
    lf = pl.LazyFrame({"WL": [0, 1, 2], "value": [10, 20, 30]})
    order = ["WL"]
    aggregations = {"WL": AggregationSpec(op="expr", expr="mean(values) + 1")}
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(21.0)


def test_incomplete_order_raises():
    lf = pl.LazyFrame({"WL": [0, 1], "STR": [0, 1], "value": [10, 20]})
    order = ["WL"]
    aggregations = {"WL": AggregationSpec(op="mean")}
    with pytest.raises(ValueError):
        aggregate_score_part(lf, "value", order, aggregations)
