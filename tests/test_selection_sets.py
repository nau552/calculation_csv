# Copyright (c) 2026
"""名前付き選択セット(optimization.selectionSets + `ref` 参照)のテスト。"""

from pathlib import Path

import pytest

from scorelib_param import io_jsonc
from scorelib_param.cli import compute_score_file, compute_score_part
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.models import AggregationSpec, RunConfig, ScorePart

UPDOWN = [
    {"State": "R2A", "Read_Label": "read_level_upper1"},
    {"State": "A2B", "Read_Label": "read_level_upper1"},
    {"State": "A2R", "Read_Label": "read_level_lower1"},
    {"State": "B2A", "Read_Label": "read_level_lower1"},
]


def _part(pair_agg: dict[str, object]) -> ScorePart:
    return ScorePart.model_validate(
        {
            "name": "updown",
            "type": "dVtBudget",
            "relative": {
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 1,
            },
            "order": ["State&Read_Label", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {
                "State&Read_Label": pair_agg,
                "WL": {"op": "mean"},
                "STR": {"op": "mean"},
                "Board": {"op": "mean"},
                "Chip": {"op": "mean"},
                "Block": {"op": "mean"},
            },
        }
    )


@pytest.fixture
def dvt_kwargs(dvtbudget_coef_path: Path, data_dir_mini: Path) -> dict[str, object]:
    """パーツ計算に必要な dVtBudget の共通キーワード引数を返す。"""
    return {
        "generation": "B9LS",
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


def test_ref_equals_inline(data_dir_mini: Path, dvt_kwargs: dict[str, object]) -> None:
    """参照(ref)とインライン指定で同じ値になることを検証する。"""
    inline = compute_score_part(data_dir_mini, _part({"op": "sum", "value": UPDOWN}), **dvt_kwargs)
    via_ref = compute_score_part(
        data_dir_mini,
        _part({"op": "sum", "ref": "updown_pairs"}),
        selection_sets={"updown_pairs": UPDOWN},
        **dvt_kwargs,
    )
    assert via_ref == pytest.approx(inline)


def test_unknown_ref_raises(data_dir_mini: Path, dvt_kwargs: dict[str, object]) -> None:
    """未定義の ref は既知のセット名の案内つきでエラーになることを検証する。"""
    with pytest.raises(ValueError, match=r"unknown selection set 'nope'.*updown_pairs"):
        compute_score_part(
            data_dir_mini,
            _part({"op": "sum", "ref": "nope"}),
            selection_sets={"updown_pairs": UPDOWN},
            **dvt_kwargs,
        )


def test_ref_and_value_both_rejected() -> None:
    """同時指定(ref と value)は拒否されることを検証する。"""
    with pytest.raises(Exception, match="not both"):
        AggregationSpec(op="sum", value=["R2A"], ref="some_set")


def test_ref_on_op_without_selections_rejected() -> None:
    """選択を取らない op への ref 指定は拒否されることを検証する。"""
    with pytest.raises(Exception, match="not applicable"):
        AggregationSpec(op="expr", expr="mean(values)", ref="some_set")


def test_resolved_content_is_validated(data_dir_mini: Path, dvt_kwargs: dict[str, object]) -> None:
    """キー名の間違ったセットは、インラインで書いた場合と同じエラーで失敗すること。

    (ref 解決後にも同じ検証が走る)
    """
    bad_set = [{"State": "R2A", "ReadLabel": "read_level_upper1"}]
    with pytest.raises(ValueError, match="expects keys"):
        compute_score_part(
            data_dir_mini,
            _part({"op": "sum", "ref": "bad"}),
            selection_sets={"bad": bad_set},
            **dvt_kwargs,
        )


def test_run_config_selection_sets_flow(data_dir_mini: Path, dvtbudget_coef_path: Path) -> None:
    """SelectionSets defined in optimization{} are usable from score parts via compute_score_file.

    Diff over a 2-element set works.
    """
    config = RunConfig.model_validate(
        {
            "Generation": "B9LS",
            "optimization": {
                "selectionSets": {
                    "ud_diff": [
                        {"State": "R2A", "Read_Label": "read_level_upper1"},
                        {"State": "A2R", "Read_Label": "read_level_lower1"},
                    ]
                },
                "score_parts": [
                    {
                        "name": "p",
                        "type": "dVtBudget",
                        "relative": {
                            "split_axis": "Read_Override",
                            "numerator_when": True,
                            "denominator_when": False,
                            "denominator_offset": 1,
                        },
                        "order": ["State&Read_Label", "WL", "STR", "Board", "Chip", "Block"],
                        "aggregations": {
                            "State&Read_Label": {"op": "diff", "ref": "ud_diff"},
                            "WL": {"op": "mean"},
                            "STR": {"op": "mean"},
                            "Board": {"op": "mean"},
                            "Chip": {"op": "mean"},
                            "Block": {"op": "mean"},
                        },
                    }
                ],
                "expression": "p",
            },
        }
    )
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")
    result = compute_score_file(data_dir_mini, config, coef, temps)
    assert result["Score"] == pytest.approx(result["p"])
