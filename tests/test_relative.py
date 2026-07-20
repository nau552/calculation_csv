import polars as pl
import pytest

from scorelib_param.models import AxisAggregation, RelativeConfig
from scorelib_param.relative import apply_relative


def test_relative_with_denominator_pre_aggregation_and_offset():
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
    assert result[(0, 0)] == pytest.approx(105 / 30)
    assert result[(0, 1)] == pytest.approx(205 / 30)
    assert result[(1, 0)] == pytest.approx(305 / 30)
    assert result[(1, 1)] == pytest.approx(405 / 30)
    assert "IsEval" not in out.columns


def test_relative_without_pre_aggregation():
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


def test_enabled_true_is_silently_dropped():
    cfg = RelativeConfig.model_validate(
        {"enabled": True, "split_axis": "x", "numerator_when": True, "denominator_when": False}
    )
    assert cfg.split_axis == "x"
    assert "enabled" not in cfg.model_dump()


def test_enabled_false_is_rejected_loudly():
    with pytest.raises(Exception, match="enabled has been removed"):
        RelativeConfig.model_validate(
            {"enabled": False, "split_axis": "x", "numerator_when": True, "denominator_when": False}
        )


def test_relative_diff_mode():
    lf = pl.LazyFrame(
        {
            "Key": ["x", "x", "y", "y"],
            "IsEval": [False, True, False, True],
            "value": [10.0, 50.0, 7.0, 5.0],
        }
    )
    relative = RelativeConfig(
        split_axis="IsEval", numerator_when=True, denominator_when=False,
        mode="diff",
        denominator_offset=123.0,  # diff モードでは無視されるはず
    )
    out = apply_relative(lf, "value", relative).collect()
    result = {row["Key"]: row["value"] for row in out.to_dicts()}
    assert result["x"] == pytest.approx(40.0)   # 50 - 10
    assert result["y"] == pytest.approx(-2.0)   # 5 - 7
