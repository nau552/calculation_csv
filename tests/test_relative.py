# Copyright (c) 2026
import polars as pl
import pytest

from scorelib_param.models import AxisAggregation, RelativeConfig
from scorelib_param.relative import apply_relative


def test_relative_with_denominator_pre_aggregation_and_offset() -> None:
    """分母の事前集計と offset を伴う相対化を検証する。"""
    lf = pl.LazyFrame(
        {
            "Board": [1, 1, 1, 1, 1, 1, 1, 1],
            "WL": [0, 0, 1, 1, 0, 0, 1, 1],
            "STR": [0, 1, 0, 1, 0, 1, 0, 1],
            "IsEval": [False, False, False, False, True, True, True, True],
            "value": [10, 20, 30, 40, 100, 200, 300, 400],
        }
    )
    relative = RelativeConfig(
        split_axis="IsEval",
        numerator_when=True,
        denominator_when=False,
        denominator_offset=5.0,
        denominator_pre_aggregation=[
            AxisAggregation(axis="WL", op="mean"),
            AxisAggregation(axis="STR", op="mean"),
        ],
    )

    out = apply_relative(lf, "value", relative).collect()
    # denominator: WL-mean per STR -> STR0:20, STR1:30; STR-mean -> 25; +offset(5) = 30
    # offset(5) is added to the numerator side as well
    result = {(row["WL"], row["STR"]): row["value"] for row in out.to_dicts()}
    assert result[0, 0] == pytest.approx(105 / 30)
    assert result[0, 1] == pytest.approx(205 / 30)
    assert result[1, 0] == pytest.approx(305 / 30)
    assert result[1, 1] == pytest.approx(405 / 30)
    assert "IsEval" not in out.columns


def test_relative_without_pre_aggregation() -> None:
    """事前集計なしの相対化を検証する。"""
    lf = pl.LazyFrame(
        {
            "Key": ["x", "x"],
            "IsEval": [False, True],
            "value": [10.0, 50.0],
        }
    )
    relative = RelativeConfig(split_axis="IsEval", numerator_when=True, denominator_when=False, denominator_offset=0.0)
    out = apply_relative(lf, "value", relative).collect()
    assert out.height == 1
    assert out["value"][0] == pytest.approx(5.0)


def test_unset_numerator_or_denominator_rejected() -> None:
    """分子/分母の未設定(None)は必ず0行マッチになる設定忘れ。

    実行前に明確なエラーで拒否する(UIの「設定完了まで検証エラー表示」の実体)。
    """
    for side in ("numerator_when", "denominator_when"):
        cfg = {"split_axis": "Measure", "numerator_when": 1, "denominator_when": 0}
        cfg[side] = None
        with pytest.raises(Exception, match="both numerator_when and denominator_when"):
            RelativeConfig.model_validate(cfg)


def test_labels_annotation_round_trips() -> None:
    """Labels 注記(Measure 番号 → dataName の表示名)は実行に影響しないこと。

    model_dump で保存内容に残ること(docs/spec_change_dataname_measure.md 6.1節)。
    """
    cfg = RelativeConfig.model_validate(
        {
            "split_axis": "Measure",
            "numerator_when": 1,
            "denominator_when": 0,
            "labels": {"1": "evaluation_param_read_level_1", "0": "reference_param_read_level_1"},
        }
    )
    assert cfg.model_dump()["labels"]["1"] == "evaluation_param_read_level_1"
    lf = pl.LazyFrame({"Measure": [0, 1], "value": [10.0, 50.0]})
    out = apply_relative(lf, "value", cfg).collect()
    assert out["value"][0] == pytest.approx(5.0)


def test_relative_diff_mode() -> None:
    """相対化の diff モード(差を取り denominator_offset は無視)を検証する。"""
    lf = pl.LazyFrame(
        {
            "Key": ["x", "x", "y", "y"],
            "IsEval": [False, True, False, True],
            "value": [10.0, 50.0, 7.0, 5.0],
        }
    )
    relative = RelativeConfig(
        split_axis="IsEval",
        numerator_when=True,
        denominator_when=False,
        mode="diff",
        denominator_offset=123.0,  # diff モードでは無視されるはず
    )
    out = apply_relative(lf, "value", relative).collect()
    result = {row["Key"]: row["value"] for row in out.to_dicts()}
    assert result["x"] == pytest.approx(40.0)  # 50 - 10
    assert result["y"] == pytest.approx(-2.0)  # 5 - 7
