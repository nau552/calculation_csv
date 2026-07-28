import polars as pl
import pytest

from scorelib_param.aggregate import aggregate_score_part, group_column_expr
from scorelib_param.models import AggregationSpec


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


def test_simple_op_with_value_list_restricts_before_reducing():
    lf = pl.LazyFrame({"STR": [0, 1, 2, 3, 4], "value": [10, 20, 30, 40, 50]})
    order = ["STR"]
    aggregations = {"STR": AggregationSpec(op="mean", value=[0, 1, 2])}
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(20.0)


def test_legacy_spellings_normalized():
    # values は value の別名として受理。*_subset op は通常opへ自動変換
    spec = AggregationSpec.model_validate({"op": "mean_subset", "values": [0, 1]})
    assert spec.op == "mean"
    assert spec.value == [0, 1]


def test_both_value_and_values_rejected():
    with pytest.raises(Exception, match="not both"):
        AggregationSpec.model_validate({"op": "mean", "value": [0], "values": [1]})


def test_filter_accepts_single_element_list():
    spec = AggregationSpec(op="filter", value=["A2B"])
    assert spec.value == "A2B"


def test_filter_accepts_multiple_selections_as_is_in():
    spec = AggregationSpec(op="filter", value=[0, 1])
    assert spec.value == [0, 1]


def test_filter_rejects_empty_list_and_nested_list():
    with pytest.raises(Exception, match="at least one"):
        AggregationSpec(op="filter", value=[])
    with pytest.raises(Exception, match="not a nested list"):
        AggregationSpec(op="filter", value=[["A2B", "B2A"]])


def test_filter_with_list_keeps_matching_rows_as_replicates():
    """複数値 filter（is_in）: 該当行を残して軸列を落とす。残った行は後段の
    集計に複製として流れ込む（sum なら選択値ぶんの行が全部足される）。"""
    lf = pl.LazyFrame(
        {
            "Measure": [0, 1, 2, 0, 1, 2],
            "Key": ["x", "x", "x", "y", "y", "y"],
            "value": [1.0, 10.0, 100.0, 2.0, 20.0, 200.0],
        }
    )
    order = ["Measure", "Key"]
    aggregations = {
        "Measure": AggregationSpec(op="filter", value=[0, 1]),
        "Key": AggregationSpec(op="sum"),
    }
    # Measure 0,1 の行が残り、Key の sum で複製ごと畳まれる: (1+10)+(2+20)=33
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(33.0)


def test_group_reduce_removed_with_guidance():
    """group_reduce op は派生グループ軸（groupDefs + order に定義名）へ
    置き換えられた。旧 config は新しい書き方への案内つきで失敗すること。"""
    with pytest.raises(Exception, match="removed.*groupDefs"):
        AggregationSpec.model_validate(
            {"op": "group_reduce", "group_def": "g", "inner_op": "min", "outer_op": "max"}
        )


def test_derived_group_axis_aggregates_like_a_real_axis():
    """（_with_group_columns が読み込み時に作るのと同様の）作成済みグループ列は
    任意の位置で潰せる: 先に WL、後からグループ間 max。"""
    lf = pl.LazyFrame({"WL": [0, 1, 2, 3], "value": [10.0, 5.0, 8.0, 20.0]})
    lf = lf.with_columns(group_column_expr("WL", {"g1": (0, 1), "g2": (2, 3)}).alias("g")).drop("WL")
    aggregations = {"g": AggregationSpec(op="max")}
    # per-group means survive until g is reduced: g1 mean(10,5)=7.5, g2 mean(8,20)=14
    lf = lf.group_by("g").agg(pl.col("value").mean())
    assert aggregate_score_part(lf, "value", ["g"], aggregations) == pytest.approx(14.0)


def test_expr_op():
    lf = pl.LazyFrame({"WL": [0, 1, 2], "value": [10, 20, 30]})
    order = ["WL"]
    aggregations = {"WL": AggregationSpec(op="expr", expr="mean(values) + 1")}
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(21.0)


def test_diff_op():
    lf = pl.LazyFrame(
        {
            "Board": [0, 0, 1, 1],
            "State": ["R2A", "B2A", "R2A", "B2A"],
            "value": [10.0, 3.0, 20.0, 5.0],
        }
    )
    order = ["State", "Board"]
    aggregations = {
        "State": AggregationSpec(op="diff", value=["R2A", "B2A"]),
        "Board": AggregationSpec(op="mean"),
    }
    # Board0: 10-3=7, Board1: 20-5=15, mean -> 11
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(11.0)


def test_diff_op_requires_two_selections():
    with pytest.raises(Exception, match="exactly two"):
        AggregationSpec(op="diff", value=["R2A"])


def test_sum_with_scalar_value_wrapped_to_list():
    spec = AggregationSpec(op="sum", value="R2A")
    assert spec.value == ["R2A"]


def test_expr_op_by_lookup():
    lf = pl.LazyFrame(
        {
            "Board": [0, 0, 0, 1, 1, 1],
            "State": ["R2A", "A2B", "B2A", "R2A", "A2B", "B2A"],
            "value": [10.0, 4.0, 2.0, 20.0, 6.0, 4.0],
        }
    )
    order = ["State", "Board"]
    aggregations = {
        "State": AggregationSpec(op="expr", expr="0.5 * by['R2A'] + by['A2B'] - by['B2A']"),
        "Board": AggregationSpec(op="mean"),
    }
    # Board0: 5+4-2=7, Board1: 10+6-4=12, mean -> 9.5
    assert aggregate_score_part(lf, "value", order, aggregations) == pytest.approx(9.5)


def test_incomplete_order_raises():
    lf = pl.LazyFrame({"WL": [0, 1], "STR": [0, 1], "value": [10, 20]})
    order = ["WL"]
    aggregations = {"WL": AggregationSpec(op="mean")}
    with pytest.raises(ValueError):
        aggregate_score_part(lf, "value", order, aggregations)
