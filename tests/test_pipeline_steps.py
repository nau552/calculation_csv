# Copyright (c) 2026
"""order 内の仮想パイプラインステップのテスト。

__relative__、__dvtbudget__、__offset__ のようなユーザ命名の変換ステップ
(cli.py 参照)。
"""

from pathlib import Path
from typing import TypedDict

import pytest

from scorelib_param import io_jsonc
from scorelib_param.cli import compute_score_part
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.models import DvtBudgetCoefFile, ScorePart


class DvtKwargs(TypedDict):
    """パーツ計算に `**` 展開でそのまま渡す dVtBudget 共通キーワード引数(実行時はただの dict)。"""

    generation: str
    dvtbudget_coef: DvtBudgetCoefFile
    board_temperatures: dict[int, float]


def _base_aggs() -> dict[str, dict[str, str]]:
    return {
        "Read_Label": {"op": "filter", "value": "read_level_upper1"},
        "State": {"op": "filter", "value": "A2B"},
        "WL": {"op": "mean"},
        "STR": {"op": "mean"},
        "Board": {"op": "mean"},
        "Chip": {"op": "mean"},
        "Block": {"op": "mean"},
    }


def test_explicit_offset_step_equals_ratio_time_offset(data_dir_mini: Path) -> None:
    """Offset → WL平均 → STR平均 → 相対化 が古典形と一致することを検証する。

    古典形(分母を WL/STR で事前集計し、比を取る時点で offset を両辺に加算)と
    一致するはず。平均は定数加算と可換だから。
    """
    classic = ScorePart.model_validate(
        {
            "name": "classic",
            "type": "FBC",
            "relative": {
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
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 0,  # offset は明示ステップ側で加算する
            },
            "order": [
                "Read_Label",
                "State",
                "__offset__",  # add 1 to every FBC value
                "WL",
                "STR",  # 分子側・分母側それぞれで先に集計
                "__relative__",  # その後に比を取る
                "Board",
                "Chip",
                "Block",
            ],
            "aggregations": {**_base_aggs(), "__offset__": {"op": "add", "value": 1}},
        }
    )

    v_classic = compute_score_part(data_dir_mini, classic)
    v_pipeline = compute_score_part(data_dir_mini, pipeline)
    assert v_pipeline == pytest.approx(v_classic)


def test_explicit_dvtbudget_step_equals_default_placement(data_dir_mini: Path, dvtbudget_coef_path: Path) -> None:
    """__dvtbudget__ 明示ステップが既定位置での適用と同値であることを検証する。"""
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")

    common = {
        "type": "dVtBudget",
        "relative": {
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

    kwargs: DvtKwargs = {"generation": "B9LS", "dvtbudget_coef": coef, "board_temperatures": temps}
    assert compute_score_part(data_dir_mini, explicit, **kwargs) == pytest.approx(
        compute_score_part(data_dir_mini, implicit, **kwargs)
    )


def test_dvtbudget_step_after_state_aggregation_raises(data_dir_mini: Path, dvtbudget_coef_path: Path) -> None:
    """State を潰した後の __dvtbudget__ がエラーになることを検証する。"""
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")
    part = ScorePart.model_validate(
        {
            "name": "bad",
            "type": "dVtBudget",
            "relative": {
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": 1,
            },
            # 変換より**前**に State を filter で潰している → 明確に失敗すべき
            "order": ["__relative__", "Read_Label", "State", "__dvtbudget__", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": _base_aggs(),
        }
    )
    with pytest.raises(ValueError, match="Board/State"):
        compute_score_part(data_dir_mini, part, generation="B9LS", dvtbudget_coef=coef, board_temperatures=temps)


def test_state_diff_within_part_equals_two_parts_subtracted(data_dir_mini: Path, dvtbudget_coef_path: Path) -> None:
    """State 'diff' op in one part equals subtracting two filtered parts.

    dVtBudget(R2A) - dVtBudget(B2A) computed as a single part with a
    State 'diff' op must equal computing two filtered parts and subtracting,
    because every aggregation after State here is linear (mean).
    """
    coef = io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path)
    temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")
    kwargs: DvtKwargs = {"generation": "B9LS", "dvtbudget_coef": coef, "board_temperatures": temps}

    def dvt_part(name: str, state_agg: dict[str, str | list[str]]) -> ScorePart:
        return ScorePart.model_validate(
            {
                "name": name,
                "type": "dVtBudget",
                "relative": {
                    "split_axis": "Read_Override",
                    "numerator_when": True,
                    "denominator_when": False,
                    "denominator_offset": 1,
                },
                "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
                "aggregations": {**_base_aggs(), "State": state_agg},
            }
        )

    combined = compute_score_part(
        data_dir_mini, dvt_part("combined", {"op": "diff", "value": ["R2A", "B2A"]}), **kwargs
    )
    r2a = compute_score_part(data_dir_mini, dvt_part("r2a", {"op": "filter", "value": "R2A"}), **kwargs)
    b2a = compute_score_part(data_dir_mini, dvt_part("b2a", {"op": "filter", "value": "B2A"}), **kwargs)
    assert combined == pytest.approx(r2a - b2a)


def test_unknown_virtual_step_raises(data_dir_mini: Path) -> None:
    """未知の仮想ステップ名がエラーになることを検証する。"""
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
