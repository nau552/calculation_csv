"""Tests for combined axes ("State&Read_Label") — selecting/reducing over
(State, Read_Label) pairs inside a single score part."""
import pytest

from scorelib import io_jsonc
from scorelib.cli import compute_score_part
from scorelib.dvtbudget import load_board_temperatures
from scorelib.models import ScorePart


@pytest.fixture
def dvt_kwargs(dvtbudget_coef_path, data_dir_mini):
    return {
        "generation": "B9LS",
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


_TAIL = {
    "WL": {"op": "mean"},
    "STR": {"op": "mean"},
    "Board": {"op": "mean"},
    "Chip": {"op": "mean"},
    "Block": {"op": "mean"},
}


def _combined_part(name, pair_agg):
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
            "order": ["State&Read_Label", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {"State&Read_Label": pair_agg, **_TAIL},
        }
    )


def _filtered_part(name, state_agg, read_label):
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
            "aggregations": {
                "Read_Label": {"op": "filter", "value": read_label},
                "State": state_agg,
                **_TAIL,
            },
        }
    )


def test_updown_sum_equals_two_filtered_parts_added(data_dir_mini, dvt_kwargs):
    """The user's real use case: upward Budget states (R2A, A2B) measured at
    read_level_upper1 plus downward states (A2R, B2A) at read_level_lower1,
    summed into one score part. Must equal the sum of the two per-direction
    parts because every later aggregation is linear."""
    combined = compute_score_part(
        data_dir_mini,
        _combined_part(
            "updown",
            {
                "op": "sum",
                "value": [
                    {"State": "R2A", "Read_Label": "read_level_upper1"},
                    {"State": "A2B", "Read_Label": "read_level_upper1"},
                    {"State": "A2R", "Read_Label": "read_level_lower1"},
                    {"State": "B2A", "Read_Label": "read_level_lower1"},
                ],
            },
        ),
        **dvt_kwargs,
    )
    up = compute_score_part(
        data_dir_mini,
        _filtered_part("up", {"op": "sum", "value": ["R2A", "A2B"]}, "read_level_upper1"),
        **dvt_kwargs,
    )
    down = compute_score_part(
        data_dir_mini,
        _filtered_part("down", {"op": "sum", "value": ["A2R", "B2A"]}, "read_level_lower1"),
        **dvt_kwargs,
    )
    assert combined == pytest.approx(up + down)


def test_pair_diff_across_labels(data_dir_mini, dvt_kwargs):
    """diff between (R2A, upper1) and (B2A, lower1) in one part equals the
    two filtered parts subtracted."""
    combined = compute_score_part(
        data_dir_mini,
        _combined_part(
            "pair_diff",
            {
                "op": "diff",
                "value": [
                    {"State": "R2A", "Read_Label": "read_level_upper1"},
                    {"State": "B2A", "Read_Label": "read_level_lower1"},
                ],
            },
        ),
        **dvt_kwargs,
    )
    a = compute_score_part(
        data_dir_mini,
        _filtered_part("a", {"op": "filter", "value": "R2A"}, "read_level_upper1"),
        **dvt_kwargs,
    )
    b = compute_score_part(
        data_dir_mini,
        _filtered_part("b", {"op": "filter", "value": "B2A"}, "read_level_lower1"),
        **dvt_kwargs,
    )
    assert combined == pytest.approx(a - b)


def test_expr_updown_difference(data_dir_mini, dvt_kwargs):
    """(up-direction sum) - (down-direction sum) via expr `by` lookups on a
    combined axis, cross-checked against two filtered parts subtracted."""
    combined = compute_score_part(
        data_dir_mini,
        _combined_part(
            "updown_diff",
            {
                "op": "expr",
                "expr": (
                    "by['R2A&read_level_upper1'] + by['A2B&read_level_upper1']"
                    " - by['A2R&read_level_lower1'] - by['B2A&read_level_lower1']"
                ),
            },
        ),
        **dvt_kwargs,
    )
    up = compute_score_part(
        data_dir_mini,
        _filtered_part("up", {"op": "sum", "value": ["R2A", "A2B"]}, "read_level_upper1"),
        **dvt_kwargs,
    )
    down = compute_score_part(
        data_dir_mini,
        _filtered_part("down", {"op": "sum", "value": ["A2R", "B2A"]}, "read_level_lower1"),
        **dvt_kwargs,
    )
    assert combined == pytest.approx(up - down)


def test_positional_pair_list_rejected():
    """The old positional form ["R2A", "read_level_upper1"] is ambiguous and
    must be rejected with a pointer to the dict form."""
    with pytest.raises(Exception, match="dict naming its axes"):
        _combined_part("bad", {"op": "sum", "value": ["R2A", "read_level_upper1"]})


def test_wrong_dict_keys_rejected():
    with pytest.raises(Exception, match="expects keys"):
        _combined_part("bad", {"op": "filter", "value": {"State": "A2B", "ReadLabel": "read_level_upper1"}})


def test_dict_selection_on_plain_axis_rejected():
    with pytest.raises(Exception, match="only valid on combined axes"):
        _filtered_part("bad", {"op": "filter", "value": {"State": "A2B"}}, "read_level_upper1")


def test_combined_filter_single_pair(data_dir_mini, dvt_kwargs):
    """filter with a single (State, Read_Label) pair equals the classic
    two-filter form."""
    combined = compute_score_part(
        data_dir_mini,
        _combined_part("single", {"op": "filter", "value": {"State": "A2B", "Read_Label": "read_level_upper1"}}),
        **dvt_kwargs,
    )
    classic = compute_score_part(
        data_dir_mini,
        _filtered_part("classic", {"op": "filter", "value": "A2B"}, "read_level_upper1"),
        **dvt_kwargs,
    )
    assert combined == pytest.approx(classic)
