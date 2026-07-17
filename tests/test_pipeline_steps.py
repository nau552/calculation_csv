"""Tests for virtual pipeline steps in `order`: __relative__, __dvtbudget__,
and user-named transforms like __offset__ (see cli.py)."""
import pytest

from scorelib import io_jsonc
from scorelib.cli import compute_score_part
from scorelib.dvtbudget import load_board_temperatures
from scorelib.models import ScorePart


def _base_aggs():
    return {
        "Read_Label": {"op": "filter", "value": "read_level_upper1"},
        "State": {"op": "filter", "value": "A2B"},
        "WL": {"op": "mean"},
        "STR": {"op": "mean"},
        "Board": {"op": "mean"},
        "Chip": {"op": "mean"},
        "Block": {"op": "mean"},
    }


def test_explicit_offset_step_equals_ratio_time_offset(data_dir_mini):
    """offset -> mean(WL) -> mean(STR) -> relative  must equal the classic
    form (denominator pre-aggregated over WL/STR, offset applied to both
    sides at ratio time), because mean commutes with adding a constant.
    """
    classic = ScorePart.model_validate(
        {
            "name": "classic",
            "type": "FBC",
            "relative": {
                "enabled": True,
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 1,
                "denominator_pre_aggregation": [
                    {"axis": "WL", "op": "mean"},
                    {"axis": "STR", "op": "mean"},
                ],
            },
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": _base_aggs(),
        }
    )

    pipeline = ScorePart.model_validate(
        {
            "name": "pipeline",
            "type": "FBC",
            "relative": {
                "enabled": True,
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 0,  # offset now an explicit step instead
            },
            "order": [
                "Read_Label", "State",
                "__offset__",           # add 1 to every FBC value
                "WL", "STR",            # aggregated per numerator/denominator side
                "__relative__",         # then take the ratio
                "Board", "Chip", "Block",
            ],
            "aggregations": {**_base_aggs(), "__offset__": {"op": "add", "value": 1}},
        }
    )

    v_classic = compute_score_part(data_dir_mini, classic)
    v_pipeline = compute_score_part(data_dir_mini, pipeline)
    assert v_pipeline == pytest.approx(v_classic)


def test_explicit_dvtbudget_step_equals_default_placement(data_dir_mini, dvtbudget_coef_path):
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")

    common = {
        "type": "dVtBudget",
        "relative": {
            "enabled": True,
            "split_axis": "Read_Override",
            "numerator_when": True,
            "denominator_when": False,
            "denominator_offset": 1,
        },
        "aggregations": _base_aggs(),
    }
    implicit = ScorePart.model_validate(
        {"name": "implicit", "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"], **common}
    )
    explicit = ScorePart.model_validate(
        {
            "name": "explicit",
            "order": ["__relative__", "__dvtbudget__", "Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            **common,
        }
    )

    kwargs = {"generation": "B9LS", "dvtbudget_coef": coef, "board_temperatures": temps}
    assert compute_score_part(data_dir_mini, explicit, **kwargs) == pytest.approx(
        compute_score_part(data_dir_mini, implicit, **kwargs)
    )


def test_dvtbudget_step_after_state_aggregation_raises(data_dir_mini, dvtbudget_coef_path):
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")
    part = ScorePart.model_validate(
        {
            "name": "bad",
            "type": "dVtBudget",
            "relative": {
                "enabled": True,
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 1,
            },
            # State is filtered away BEFORE the conversion -> must fail clearly
            "order": ["__relative__", "Read_Label", "State", "__dvtbudget__", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": _base_aggs(),
        }
    )
    with pytest.raises(ValueError, match="Board/State"):
        compute_score_part(
            data_dir_mini, part, generation="B9LS", dvtbudget_coef=coef, board_temperatures=temps
        )


def test_unknown_virtual_step_raises(data_dir_mini):
    part = ScorePart.model_validate(
        {
            "name": "bad",
            "type": "FBC",
            "order": ["__mystery__", "Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": _base_aggs(),
        }
    )
    with pytest.raises(ValueError, match="__mystery__"):
        compute_score_part(data_dir_mini, part)
