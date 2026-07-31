# Copyright (c) 2026
from __future__ import annotations

import json
import math
import subprocess
import sys
from typing import TYPE_CHECKING, TypedDict, cast

import polars as pl
import pytest

from scorelib_param import io_jsonc
from scorelib_param.cli import compute_score_file, compute_score_part
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.expression import evaluate_expression

if TYPE_CHECKING:
    from pathlib import Path

    from scorelib_param.models import DvtBudgetCoefFile, RunConfig


class DvtInputs(TypedDict):
    """計算関数に `**` 展開でそのまま渡す dVtBudget 入力の組(実行時はただの dict)。"""

    dvtbudget_coef: DvtBudgetCoefFile
    board_temperatures: dict[int, float]


@pytest.fixture
def run_config(fixtures_dir: Path) -> RunConfig:
    """tests/fixtures/config.jsonc を読み込んだ RunConfig を返す。

    Returns:
        検証済みの RunConfig モデル。

    """
    return io_jsonc.load_run_config(fixtures_dir / "config.jsonc")


@pytest.fixture
def dvt_inputs(dvtbudget_coef_path: Path, data_dir_mini: Path) -> DvtInputs:
    """係数と初期温度など dVtBudget 計算に必要な入力一式を返す。

    Returns:
        dvtbudget_coef と board_temperatures を詰めた、パーツ計算関数に
        そのまま渡せるキーワード引数の dict。

    """
    return {
        "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
        "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
    }


def _expected_fbc_part(expanded_mini_dir: Path, wlgroup: dict[str, tuple[int, int]]) -> float:
    """FBC_A2B_upper1_rel パーツをテスト内で独立に再計算する。

    tests/fixtures/config.jsonc の FBC_A2B_upper1_rel パーツを、現行スクリプトが
    生成した正解展開データを使って**テスト内で独立に**(eager に一歩ずつ)再計算する。

    このパーツは派生軸 WLgroup を参照しているため、グループ列は読み込み時から
    存在する扱い: 分母事前集計と相対化のペアリングもグループ内で閉じる
    (グループ横断の分母にしたい場合は denominator_pre_aggregation に
    WLgroup 自体を足す書き方になる)。

    Returns:
        独立再計算で得たパーツのスコア値。

    """
    df = pl.read_csv(expanded_mini_dir / "FBC_expanded.csv")
    cols = ["Board", "Chip", "Block", "WL", "STR", "State", "Read_Label", "Read_Override", "FBC"]
    df = df.select(cols).with_columns(pl.col("Read_Override").cast(pl.Boolean))

    def to_group(wl: int) -> str:
        for name, (lo, hi) in wlgroup.items():
            if lo <= wl <= hi:
                return name
        msg = f"WL {wl} not covered"
        raise AssertionError(msg)

    df = df.with_columns(pl.col("WL").map_elements(to_group, return_dtype=pl.Utf8).alias("grp"))

    num = df.filter(pl.col("Read_Override")).drop("Read_Override")
    den = df.filter(~pl.col("Read_Override")).drop("Read_Override")

    # 分母の事前集計: WL平均 → STR平均(grp はキーとして残り続ける)
    den = den.group_by(["Board", "Chip", "Block", "STR", "State", "Read_Label", "grp"]).agg(pl.col("FBC").mean())
    den = den.group_by(["Board", "Chip", "Block", "State", "Read_Label", "grp"]).agg(pl.col("FBC").mean())
    den = den.rename({"FBC": "denom"})

    rel = num.join(den, on=["Board", "Chip", "Block", "State", "Read_Label", "grp"], how="left")
    rel = rel.with_columns(((pl.col("FBC") + 1) / (pl.col("denom") + 1)).alias("FBC")).drop("denom")

    rel = rel.filter(pl.col("Read_Label") == "read_level_upper1").drop("Read_Label")
    rel = rel.filter(pl.col("State") == "A2B").drop("State")

    # WL mean within each WLgroup (derived axis), then max across groups
    rel = rel.drop("WL").group_by(["Board", "Chip", "Block", "STR", "grp"]).agg(pl.col("FBC").mean())
    rel = rel.group_by(["Board", "Chip", "Block", "STR"]).agg(pl.col("FBC").max())

    # STR: mean over subset {0, 1}
    rel = rel.filter(pl.col("STR").is_in([0, 1]))
    rel = rel.group_by(["Board", "Chip", "Block"]).agg(pl.col("FBC").mean())
    # Board 平均 → Chip 平均 → Block max
    rel = rel.group_by(["Chip", "Block"]).agg(pl.col("FBC").mean())
    rel = rel.group_by(["Block"]).agg(pl.col("FBC").mean())
    return cast("float", rel["FBC"].max())


def test_fbc_part_matches_independent_recomputation(expanded_mini_dir: Path, run_config: RunConfig) -> None:
    """FBC パーツの計算がテスト内の独立再計算と一致することを検証する。"""
    part = next(p for p in run_config.optimization.score_parts if p.name == "FBC_A2B_upper1_rel")
    actual = compute_score_part(expanded_mini_dir, part, group_defs=run_config.group_defs())
    expected = _expected_fbc_part(expanded_mini_dir, run_config.optimization.WLgroup)
    assert actual == pytest.approx(expected)


def test_group_axis_reduced_after_other_axes(expanded_mini_dir: Path, run_config: RunConfig) -> None:
    """旧 group_reduce では表現できなかったユーザシナリオを検証する。

    まず WLgroup 内で WL を平均し、Board/Chip/Block/STR を集約した後、
    **最後に** WLgroup を max で潰す。
    """
    from scorelib_param.models import ScorePart

    wlgroup = run_config.optimization.WLgroup
    part = ScorePart.model_validate(
        {
            "name": "late_group",
            "type": "FBC",
            "order": ["Read_Label", "State", "Read_Override", "WL", "STR", "Board", "Chip", "Block", "WLgroup"],
            "aggregations": {
                "Read_Label": {"op": "filter", "value": "read_level_upper1"},
                "State": {"op": "filter", "value": "A2B"},
                "Read_Override": {"op": "filter", "value": True},
                "WL": {"op": "mean"},
                "STR": {"op": "mean"},
                "Board": {"op": "max"},
                "Chip": {"op": "mean"},
                "Block": {"op": "mean"},
                "WLgroup": {"op": "max"},
            },
        }
    )
    actual = compute_score_part(expanded_mini_dir, part, group_defs=run_config.group_defs())

    df = pl.read_csv(expanded_mini_dir / "FBC_expanded.csv")
    df = df.filter(
        (pl.col("Read_Label") == "read_level_upper1")
        & (pl.col("State") == "A2B")
        & pl.col("Read_Override").cast(pl.Boolean)
    )

    def to_group(wl: int) -> str:
        return next(n for n, (lo, hi) in wlgroup.items() if lo <= wl <= hi)

    df = df.with_columns(pl.col("WL").map_elements(to_group, return_dtype=pl.Utf8).alias("g"))
    df = df.group_by(["g", "STR", "Board", "Chip", "Block"]).agg(pl.col("FBC").mean())
    df = df.group_by(["g", "Board", "Chip", "Block"]).agg(pl.col("FBC").mean())
    df = df.group_by(["g", "Chip", "Block"]).agg(pl.col("FBC").max())
    df = df.group_by(["g", "Block"]).agg(pl.col("FBC").mean())
    df = df.group_by(["g"]).agg(pl.col("FBC").mean())
    assert actual == pytest.approx(df["FBC"].max())


def test_group_values_outside_ranges_rejected(data_dir_mini: Path) -> None:
    """範囲外のデータ行が静かに混ざらないことを検証する。

    どの範囲にも入らないデータ行が「名無し(null)グループ」として静かに
    混ざってはならない(値一覧つきのエラーになること)。
    """
    from scorelib_param.models import GroupDef, ScorePart

    part = ScorePart.model_validate({"name": "p", "type": "FBC", "order": ["G"], "aggregations": {"G": {"op": "max"}}})
    defs = {"G": GroupDef(axis="WL", groups={"g0": (0, 1)})}  # データには WL > 1 の行がある
    with pytest.raises(ValueError, match="not covered by any group"):
        compute_score_part(data_dir_mini, part, group_defs=defs)


def test_group_def_name_clashing_with_source_axis_rejected(data_dir_mini: Path, run_config: RunConfig) -> None:
    """元軸と同名のグループ定義が拒否されることを検証する。"""
    from scorelib_param.models import GroupDef, ScorePart

    part = ScorePart.model_validate(
        {"name": "p", "type": "FBC", "order": ["WL"], "aggregations": {"WL": {"op": "max"}}}
    )
    bad = {"WL": GroupDef(axis="WL", groups={"g": (0, 100)})}
    with pytest.raises(ValueError, match="same name as its source axis"):
        compute_score_part(data_dir_mini, part, group_defs=bad)


def test_dvtbudget_part_is_finite(
    data_dir_mini: Path,
    run_config: RunConfig,
    dvt_inputs: DvtInputs,
) -> None:
    """パーツ dVtBudget_R2A が有限値になることを検証する。"""
    part = next(p for p in run_config.optimization.score_parts if p.name == "dVtBudget_R2A")
    value = compute_score_part(
        data_dir_mini,
        part,
        group_defs=run_config.group_defs(),
        generation=run_config.Generation,
        **dvt_inputs,
    )
    assert math.isfinite(value)


def test_compute_score_file_returns_all_parts(
    data_dir_mini: Path,
    run_config: RunConfig,
    dvt_inputs: DvtInputs,
) -> None:
    """compute_score_file が全パーツと Score を返すことを検証する。"""
    result = compute_score_file(data_dir_mini, run_config, **dvt_inputs)
    assert set(result) == {"Score", "FBC_A2B_upper1_rel", "dVtBudget_R2A"}
    expected_score = evaluate_expression(
        run_config.optimization.expression,
        {k: v for k, v in result.items() if k != "Score"},
    )
    assert result["Score"] == pytest.approx(expected_score)


def test_custom_part_computes(data_dir_mini: Path, fixtures_dir: Path) -> None:
    """type=custom のパーツが計算され、expression に合成されることを検証する。"""
    from scorelib_param.models import RunConfig

    rc = RunConfig.model_validate(
        {
            "Generation": "B9LS",
            "optimization": {
                "score_parts": [
                    {"name": "fixed_value", "type": "custom"},
                    {
                        "name": "shifted",
                        "type": "custom",
                        "function": "mean_fbc_plus_offset",
                        "params": {"offset": 10},
                    },
                ],
                "expression": "fixed_value + shifted",
            },
        }
    )
    result = compute_score_file(data_dir_mini, rc, custom_parts_path=fixtures_dir / "custom_parts.py")
    assert result["fixed_value"] == 42.0
    df = pl.read_csv(data_dir_mini / "FBC.csv")
    assert result["shifted"] == pytest.approx(float(cast("float", df["FBC"].mean())) + 10)
    fixed_value, shifted = result["fixed_value"], result["shifted"]
    assert fixed_value is not None
    assert shifted is not None
    assert result["Score"] == pytest.approx(fixed_value + shifted)


def test_custom_part_errors(data_dir_mini: Path, fixtures_dir: Path) -> None:
    """type=custom のエラー処理(未定義関数・非有限値・モジュール無し)を検証する。"""
    from scorelib_param.custom import (
        CustomContext,
        compute_custom_part,
        list_custom_functions,
        load_custom_module,
    )
    from scorelib_param.models import ScorePart

    module = load_custom_module(fixtures_dir / "custom_parts.py")
    assert list_custom_functions(module) == [
        "broken_returns_nan",
        "fixed_value",
        "mean_fbc_plus_offset",
    ]  # _private_helper and the pl import are excluded

    ctx = CustomContext(data_dir=data_dir_mini)
    with pytest.raises(TypeError, match="not found"):
        compute_custom_part(ScorePart(name="nope", type="custom"), module, ctx)
    with pytest.raises(ValueError, match="finite"):
        compute_custom_part(ScorePart(name="broken_returns_nan", type="custom"), module, ctx)
    with pytest.raises(ValueError, match="no custom"):
        compute_score_part(data_dir_mini, ScorePart(name="fixed_value", type="custom"))


def test_custom_fields_rejected_on_pipeline_parts() -> None:
    """type=custom 専用フィールドが他 type のパーツで拒否されることを検証する。"""
    from scorelib_param.models import ScorePart

    with pytest.raises(Exception, match="only valid on type='custom'"):
        ScorePart.model_validate({"name": "p", "type": "FBC", "function": "f"})
    with pytest.raises(Exception, match="takes no order"):
        ScorePart.model_validate(
            {"name": "p", "type": "custom", "order": ["WL"], "aggregations": {"WL": {"op": "mean"}}}
        )


def test_cli_subprocess_end_to_end(data_dir_mini: Path, fixtures_dir: Path, dvtbudget_coef_path: Path) -> None:
    """CLI を subprocess で実行して JSON のスコア出力が得られることを検証する。"""
    cmd = [
        sys.executable,
        "-m",
        "scorelib_param.cli",
        "--config",
        str(fixtures_dir / "config.jsonc"),
        "--data-dir",
        str(data_dir_mini),
        "--dvtbudget-coef",
        str(dvtbudget_coef_path),
        "--initial-temperature",
        str(data_dir_mini / "initial_temperature.csv"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert set(result) == {"Score", "FBC_A2B_upper1_rel", "dVtBudget_R2A"}
    assert all(isinstance(v, float) for v in result.values())
