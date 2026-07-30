# Copyright (c) 2026
"""SharedComputeContext(type単位共有+prefix キャッシュ)の等価性・挙動テスト。

この最適化は観測上見えてはならない: compute_score_file(キャッシュ共有)は、
各パーツを単独で計算した場合(パーツごとに読み直し・キャッシュ無し)と
完全に同じ値を返す必要がある。
"""

from collections.abc import Iterable
from pathlib import Path

import polars as pl
import pytest

from scorelib_param import axis_resolve, cli, io_jsonc
from scorelib_param.aggregate import collapse
from scorelib_param.cli import SharedComputeContext, compute_score_file, compute_score_part
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.models import DvtBudgetCoefFile, RelativeConfig, RunConfig, ScorePart


@pytest.fixture
def mini_config(fixtures_dir: Path) -> RunConfig:
    """Fixtures の config.jsonc を読み込んだ RunConfig。"""
    return io_jsonc.load_run_config(fixtures_dir / "config.jsonc")


@pytest.fixture
def dvt_inputs(dvtbudget_coef_path: Path, data_dir_mini: Path) -> dict[str, DvtBudgetCoefFile | dict[int, float]]:
    """係数と Board 温度の組(dVtBudget 計算に必要な入力)。"""
    return {
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


def _dvt_part(name: str, state: str, offset: float = 1, board_op: str = "mean") -> ScorePart:
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
                "Board": {"op": board_op},
                "Chip": {"op": "mean"},
                "Block": {"op": "mean"},
            },
        }
    )


def test_shared_equals_standalone_for_fixture_config(
    data_dir_mini: Path, mini_config: RunConfig, dvt_inputs: dict[str, DvtBudgetCoefFile | dict[int, float]]
) -> None:
    """キャッシュ共有計算が各パーツ単独計算と同じ値を返すことを検証する。"""
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


def test_resolve_runs_once_per_type(
    data_dir_mini: Path,
    mini_config: RunConfig,
    dvt_inputs: dict[str, DvtBudgetCoefFile | dict[int, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同じ type の resolve が1回しか走らないことを検証する。"""
    calls = []
    original = axis_resolve.resolve_axes

    def counting(data_dir: str | Path, type_: str, axes: Iterable[str]) -> pl.LazyFrame:
        calls.append(type_)
        return original(data_dir, type_, axes)

    monkeypatch.setattr(cli.axis_resolve, "resolve_axes", counting)
    compute_score_file(data_dir_mini, mini_config, **dvt_inputs)

    # fixture の config は FBC パーツ + dVtBudget パーツ(読み元はFBC):
    # パーツ数によらず FBC の resolve は合計1回のはず
    assert calls.count("FBC") == 1


def test_states_prefiltered_instead_of_shared(
    data_dir_mini: Path,
    dvt_inputs: dict[str, DvtBudgetCoefFile | dict[int, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State の filter だけが違うパーツ同士は前段を共有**しない**ことを検証する。

    filter 前絞り(cli._hoistable_prefilters)により、相対化は各パーツが自分の
    State に絞った行だけで実行される(全行の相対化1回を共有するより、
    絞り済み*パーツ数の方が速い)。値は従来(前絞りなし・共有あり)と同一で
    なければならない。
    """
    relative_calls = []
    original = cli.apply_relative

    def counting(lf: pl.LazyFrame, value_col: str, relative: RelativeConfig) -> pl.LazyFrame:
        relative_calls.append(1)
        return original(lf, value_col, relative)

    monkeypatch.setattr(cli, "apply_relative", counting)

    parts = [_dvt_part(f"p_{s}", s) for s in ["R2A", "A2B", "B2A", "A2R"]]
    ctx = SharedComputeContext(data_dir_mini, parts)
    values = {}
    for p in parts:
        values[p.name] = compute_score_part(data_dir_mini, p, generation="B9LS", shared_ctx=ctx, **dvt_inputs)

    assert len(relative_calls) == len(parts)  # 前絞りが違うためパーツごとに計算
    for p in parts:
        standalone = compute_score_part(data_dir_mini, p, generation="B9LS", **dvt_inputs)
        assert values[p.name] == pytest.approx(standalone, rel=1e-12)


def test_prefix_shared_when_prefilters_match(
    data_dir_mini: Path,
    dvt_inputs: dict[str, DvtBudgetCoefFile | dict[int, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前絞りまで同一のパーツ同士は、引き続き resolve + 相対化 + dVtBudget の前段を共有すること。

    違いはキャッシュ点より後の Board 集計のみ。
    """
    relative_calls = []
    original = cli.apply_relative

    def counting(lf: pl.LazyFrame, value_col: str, relative: RelativeConfig) -> pl.LazyFrame:
        relative_calls.append(1)
        return original(lf, value_col, relative)

    monkeypatch.setattr(cli, "apply_relative", counting)

    parts = [_dvt_part("mean", "A2B", board_op="mean"), _dvt_part("max", "A2B", board_op="max")]
    ctx = SharedComputeContext(data_dir_mini, parts)
    values = {}
    for p in parts:
        values[p.name] = compute_score_part(data_dir_mini, p, generation="B9LS", shared_ctx=ctx, **dvt_inputs)

    assert len(relative_calls) == 1  # 相対化は1回計算され、2つ目は再利用
    for p in parts:
        standalone = compute_score_part(data_dir_mini, p, generation="B9LS", **dvt_inputs)
        assert values[p.name] == pytest.approx(standalone, rel=1e-12)


def test_different_offset_does_not_share_prefix(
    data_dir_mini: Path, dvt_inputs: dict[str, DvtBudgetCoefFile | dict[int, float]]
) -> None:
    """相対化設定(offset)が違うパーツは、他パーツのキャッシュ済み前段を再利用してはならない。"""
    a = _dvt_part("a", "A2B", offset=1)
    b = _dvt_part("b", "A2B", offset=20)
    ctx = SharedComputeContext(data_dir_mini, [a, b])

    va = compute_score_part(data_dir_mini, a, generation="B9LS", shared_ctx=ctx, **dvt_inputs)
    vb = compute_score_part(data_dir_mini, b, generation="B9LS", shared_ctx=ctx, **dvt_inputs)
    assert va != vb  # offset が違えば値も違うはず(共有されていない証拠)

    vb_standalone = compute_score_part(data_dir_mini, b, generation="B9LS", **dvt_inputs)
    assert vb == pytest.approx(vb_standalone, rel=1e-12)


def test_collapse_with_identity_axes() -> None:
    """identity_axes 指定時の collapse が軸の組み合わせごとの行を返すことを検証する。"""
    lf = pl.LazyFrame({"Epoch": [0, 1, 2], "value": [1.0, 2.0, 3.0]})
    df = collapse(lf, "value", identity_axes=["Epoch"])
    assert df.sort("Epoch")["value"].to_list() == [1.0, 2.0, 3.0]


def test_collapse_identity_axes_mismatch_raises() -> None:
    """identity_axes 以外の軸が残っていると collapse がエラーになることを検証する。"""
    lf = pl.LazyFrame({"Epoch": [0], "WL": [0], "value": [1.0]})
    with pytest.raises(ValueError, match="expected aggregation to collapse to columns"):
        collapse(lf, "value", identity_axes=["Epoch"])
