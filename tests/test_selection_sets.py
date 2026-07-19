"""名前付き選択セット（optimization.selectionSets + `ref` 参照）のテスト。"""
import pytest

from scorelib import io_jsonc
from scorelib.cli import compute_score_file, compute_score_part
from scorelib.dvtbudget import load_board_temperatures
from scorelib.models import AggregationSpec, RunConfig, ScorePart

UPDOWN = [
    {"State": "R2A", "Read_Label": "read_level_upper1"},
    {"State": "A2B", "Read_Label": "read_level_upper1"},
    {"State": "A2R", "Read_Label": "read_level_lower1"},
    {"State": "B2A", "Read_Label": "read_level_lower1"},
]


def _part(pair_agg):
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
def dvt_kwargs(dvtbudget_coef_path, data_dir_mini):
    return {
        "generation": "B9LS",
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


def test_ref_equals_inline(data_dir_mini, dvt_kwargs):
    inline = compute_score_part(data_dir_mini, _part({"op": "sum", "value": UPDOWN}), **dvt_kwargs)
    via_ref = compute_score_part(
        data_dir_mini,
        _part({"op": "sum", "ref": "updown_pairs"}),
        selection_sets={"updown_pairs": UPDOWN},
        **dvt_kwargs,
    )
    assert via_ref == pytest.approx(inline)


def test_unknown_ref_raises(data_dir_mini, dvt_kwargs):
    with pytest.raises(ValueError, match="unknown selection set 'nope'.*updown_pairs"):
        compute_score_part(
            data_dir_mini,
            _part({"op": "sum", "ref": "nope"}),
            selection_sets={"updown_pairs": UPDOWN},
            **dvt_kwargs,
        )


def test_ref_and_value_both_rejected():
    with pytest.raises(Exception, match="not both"):
        AggregationSpec(op="sum", value=["R2A"], ref="some_set")


def test_ref_on_op_without_selections_rejected():
    with pytest.raises(Exception, match="not applicable"):
        AggregationSpec(op="expr", expr="mean(values)", ref="some_set")


def test_resolved_content_is_validated(data_dir_mini, dvt_kwargs):
    """キー名の間違ったセットは、インラインで書いた場合と同じエラーで
    失敗すること（ref 解決後にも同じ検証が走る）。"""
    bad_set = [{"State": "R2A", "ReadLabel": "read_level_upper1"}]
    with pytest.raises(ValueError, match="expects keys"):
        compute_score_part(
            data_dir_mini,
            _part({"op": "sum", "ref": "bad"}),
            selection_sets={"bad": bad_set},
            **dvt_kwargs,
        )


def test_run_config_selection_sets_flow(data_dir_mini, dvtbudget_coef_path):
    """selectionSets defined in optimization{} are usable from score parts
    via compute_score_file, and diff over a 2-element set works."""
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
