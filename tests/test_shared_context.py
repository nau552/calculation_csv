"""Equivalence and cache-behavior tests for SharedComputeContext (案A+B).

The optimization must be observationally invisible: compute_score_file
(shared caches) must produce the same values as computing every part
standalone (fresh resolve per part, no cache).
"""
import polars as pl
import pytest

import scorelib.cli as cli
from scorelib import axis_resolve, io_jsonc
from scorelib.aggregate import collapse
from scorelib.cli import SharedComputeContext, compute_score_file, compute_score_part
from scorelib.dvtbudget import load_board_temperatures
from scorelib.models import ScorePart


@pytest.fixture
def mini_config(fixtures_dir):
    return io_jsonc.load_run_config(fixtures_dir / "config.jsonc")


@pytest.fixture
def dvt_inputs(dvtbudget_coef_path, data_dir_mini):
    return {
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


def _dvt_part(name: str, state: str, offset: float = 1) -> ScorePart:
    return ScorePart.model_validate(
        {
            "name": name,
            "type": "dVtBudget",
            "relative": {
                "split_axis": "Read_Override",
                "numerator_when": True,
                "denominator_when": False,
                "denominator_offset": offset,
            },
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {
                "Read_Label": {"op": "filter", "value": "read_level_upper1"},
                "State": {"op": "filter", "value": state},
                "WL": {"op": "mean"},
                "STR": {"op": "mean"},
                "Board": {"op": "mean"},
                "Chip": {"op": "mean"},
                "Block": {"op": "mean"},
            },
        }
    )


def test_shared_equals_standalone_for_fixture_config(data_dir_mini, mini_config, dvt_inputs):
    shared = compute_score_file(data_dir_mini, mini_config, **dvt_inputs)

    for part in mini_config.optimization.score_parts:
        standalone = compute_score_part(
            data_dir_mini,
            part,
            group_defs=mini_config.group_defs(),
            generation=mini_config.Generation,
            **dvt_inputs,
        )
        assert shared[part.name] == pytest.approx(standalone, rel=1e-12), part.name


def test_resolve_runs_once_per_type(data_dir_mini, mini_config, dvt_inputs, monkeypatch):
    calls = []
    original = axis_resolve.resolve_axes

    def counting(data_dir, type_, axes):
        calls.append(type_)
        return original(data_dir, type_, axes)

    monkeypatch.setattr(cli.axis_resolve, "resolve_axes", counting)
    compute_score_file(data_dir_mini, mini_config, **dvt_inputs)

    # fixture config has FBC parts + a dVtBudget part (source FBC): one FBC
    # resolve total, regardless of part count.
    assert calls.count("FBC") == 1


def test_prefix_shared_across_states(data_dir_mini, dvt_inputs, monkeypatch):
    """Parts differing only in their State filter must share the whole
    resolve + relative + dVtBudget prefix (the '全State分まとめて計算して
    選ぶ' pattern)."""
    relative_calls = []
    original = cli.apply_relative

    def counting(lf, value_col, relative):
        relative_calls.append(1)
        return original(lf, value_col, relative)

    monkeypatch.setattr(cli, "apply_relative", counting)

    parts = [_dvt_part(f"p_{s}", s) for s in ["R2A", "A2B", "B2A", "A2R"]]
    ctx = SharedComputeContext(data_dir_mini, parts)
    values = {}
    for p in parts:
        values[p.name] = compute_score_part(
            data_dir_mini, p, generation="B9LS", shared_ctx=ctx, **dvt_inputs
        )

    assert len(relative_calls) == 1  # relative computed once, reused 3 times
    for p in parts:
        standalone = compute_score_part(data_dir_mini, p, generation="B9LS", **dvt_inputs)
        assert values[p.name] == pytest.approx(standalone, rel=1e-12)


def test_different_offset_does_not_share_prefix(data_dir_mini, dvt_inputs):
    """A part with a different relative config must NOT reuse another
    part's cached prefix."""
    a = _dvt_part("a", "A2B", offset=1)
    b = _dvt_part("b", "A2B", offset=20)
    ctx = SharedComputeContext(data_dir_mini, [a, b])

    va = compute_score_part(data_dir_mini, a, generation="B9LS", shared_ctx=ctx, **dvt_inputs)
    vb = compute_score_part(data_dir_mini, b, generation="B9LS", shared_ctx=ctx, **dvt_inputs)
    assert va != vb  # different offsets must give different values

    vb_standalone = compute_score_part(data_dir_mini, b, generation="B9LS", **dvt_inputs)
    assert vb == pytest.approx(vb_standalone, rel=1e-12)


def test_collapse_with_identity_axes():
    lf = pl.LazyFrame({"Epoch": [0, 1, 2], "value": [1.0, 2.0, 3.0]})
    df = collapse(lf, "value", identity_axes=["Epoch"])
    assert df.sort("Epoch")["value"].to_list() == [1.0, 2.0, 3.0]


def test_collapse_identity_axes_mismatch_raises():
    lf = pl.LazyFrame({"Epoch": [0], "WL": [0], "value": [1.0]})
    with pytest.raises(ValueError):
        collapse(lf, "value", identity_axes=["Epoch"])
