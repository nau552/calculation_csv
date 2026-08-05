# Copyright (c) 2026
# ruff: file-ignore[import-outside-top-level] そのテストだけが使う依存は関数内で import する
"""変換ステップの拡張(add/sub/mul/div・複数回・グループ別重み)と Physical 記法グループ定義のテスト。

Physical 記法グループ定義は definedInLogical / WLgroupDefinLogical を扱う。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import polars as pl
import pytest
from pydantic import ValidationError

from scorelib_param.cli import compute_score_file, compute_score_part, resolve_group_defs
from scorelib_param.models import AggregationSpec, GroupDef, RunConfig, ScorePart


def _mean_part(
    name: str = "p", extra_order: Sequence[str] = (), extra_aggs: dict[str, dict[str, object]] | None = None
) -> ScorePart:
    """FBC を WL 平均だけで畳む最小パーツ(他の軸は暗黙集約)。

    Returns:
        extra_order / extra_aggs でステップを追加した ScorePart。

    """
    return ScorePart(
        name=name,
        type="FBC",
        order=["WL", *extra_order],
        aggregations=cast("dict[str, AggregationSpec]", {"WL": {"op": "mean"}, **(extra_aggs or {})}),
    )


def _raw_fbc(data_dir_mini: Path) -> pl.DataFrame:
    return pl.read_csv(data_dir_mini / "FBC.csv")


# ------------------------------------------------ 定数演算(add/sub/mul/div)


def test_constant_transforms_chained(data_dir_mini: Path) -> None:
    """定数演算(add/mul/sub/div)を連鎖適用した結果が手計算と一致することを検証する。"""
    base = compute_score_part(data_dir_mini, _mean_part())
    part = _mean_part(
        extra_order=["__s1__", "__s2__", "__s3__", "__s4__"],
        extra_aggs={
            "__s1__": {"op": "add", "value": 2},
            "__s2__": {"op": "mul", "value": -3},
            "__s3__": {"op": "sub", "value": 1},
            "__s4__": {"op": "div", "value": 2},
        },
    )
    actual = compute_score_part(data_dir_mini, part)
    assert actual == pytest.approx((((base + 2) * -3) - 1) / 2)


def test_sign_flip_with_mul(data_dir_mini: Path) -> None:
    """符号反転(mul -1)を検証する。"""
    base = compute_score_part(data_dir_mini, _mean_part())
    part = _mean_part(extra_order=["__flip__"], extra_aggs={"__flip__": {"op": "mul", "value": -1}})
    assert compute_score_part(data_dir_mini, part) == pytest.approx(-base)


# ------------------------------------------------ グループ別重み(by + 辞書)


def _wl_split(data_dir_mini: Path) -> tuple[dict[str, tuple[int, int]], dict[str, float]]:
    """Mini データの WL 範囲を2グループに割った定義と、素の csv からのグループ別 FBC 平均(独立再計算の正解)を返す。

    Returns:
        (グループ名→WL 範囲の dict, グループ名→FBC 平均の dict) のタプル。

    """
    df = _raw_fbc(data_dir_mini)
    wl_max = cast("int", df["WL"].max())
    groups = {"gLow": (0, 1), "gHigh": (2, wl_max)}
    means = {
        name: cast("float", df.filter((pl.col("WL") >= lo) & (pl.col("WL") <= hi))["FBC"].mean())
        for name, (lo, hi) in groups.items()
    }
    return groups, means


def test_group_weight_inline_dict(data_dir_mini: Path) -> None:
    """グループ別重み(by + 辞書のインライン指定)を検証する。"""
    groups, means = _wl_split(data_dir_mini)
    weights = {"gLow": 1.0, "gHigh": 10.0}
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "__weight__", "WLg"],
        aggregations=cast(
            "dict[str, AggregationSpec]",
            {
                "WL": {"op": "mean"},
                "__weight__": {"op": "mul", "by": "WLg", "value": weights},
                "WLg": {"op": "max"},
            },
        ),
    )
    defs = {"WLg": GroupDef(axis="WL", groups=groups)}
    actual = compute_score_part(data_dir_mini, part, group_defs=defs)
    assert actual == pytest.approx(max(weights[g] * means[g] for g in groups))


def test_group_weight_missing_group_errors(data_dir_mini: Path) -> None:
    """重み辞書にグループが欠けているとエラーになることを検証する。"""
    groups, _ = _wl_split(data_dir_mini)
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "__weight__", "WLg"],
        aggregations=cast(
            "dict[str, AggregationSpec]",
            {
                "WL": {"op": "mean"},
                "__weight__": {"op": "mul", "by": "WLg", "value": {"gLow": 1.0}},  # gHigh 欠落
                "WLg": {"op": "max"},
            },
        ),
    )
    with pytest.raises(ValueError, match=r"no entry in the transform weights.*gHigh"):
        compute_score_part(data_dir_mini, part, group_defs={"WLg": GroupDef(axis="WL", groups=groups)})


def test_group_weight_after_axis_collapsed_errors(data_dir_mini: Path) -> None:
    """軸を潰した後にグループ別重みを置くとエラーになることを検証する。"""
    groups, _ = _wl_split(data_dir_mini)
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "WLg", "__weight__"],  # WLg を潰した後に重み
        aggregations=cast(
            "dict[str, AggregationSpec]",
            {
                "WL": {"op": "mean"},
                "WLg": {"op": "max"},
                "__weight__": {"op": "mul", "by": "WLg", "value": {"gLow": 1.0, "gHigh": 2.0}},
            },
        ),
    )
    with pytest.raises(ValueError, match="not present at this step"):
        compute_score_part(data_dir_mini, part, group_defs={"WLg": GroupDef(axis="WL", groups=groups)})


def _weight_run_config(
    data_dir_mini: Path, weight: dict[str, float] | float | None, weight_spec: dict[str, object]
) -> RunConfig:
    groups, _ = _wl_split(data_dir_mini)
    return RunConfig.model_validate(
        {
            "Generation": "B9LS",
            "optimization": {
                "WLgroup": {k: list(v) for k, v in groups.items()},
                **({"WLgroupWeight": weight} if weight is not None else {}),
                "score_parts": [
                    {
                        "name": "w",
                        "type": "FBC",
                        "order": ["WL", "__weight__", "WLgroup"],
                        "aggregations": {
                            "WL": {"op": "mean"},
                            "__weight__": weight_spec,
                            "WLgroup": {"op": "max"},
                        },
                    }
                ],
                "expression": "w",
            },
        }
    )


def test_wlgroup_weight_ref_matches_inline(data_dir_mini: Path) -> None:
    """WLgroupWeight の ref 参照がインライン指定と同じ結果になることを検証する。"""
    weights = {"gLow": 1.0, "gHigh": 10.0}
    by_ref = _weight_run_config(data_dir_mini, weights, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"})
    inline = _weight_run_config(data_dir_mini, None, {"op": "mul", "by": "WLgroup", "value": weights})
    assert compute_score_file(data_dir_mini, by_ref)["w"] == pytest.approx(
        compute_score_file(data_dir_mini, inline)["w"]
    )


def test_wlgroup_weight_scalar_applies_to_all(data_dir_mini: Path) -> None:
    """スカラーの WLgroupWeight が全グループに適用されることを検証する。"""
    _, means = _wl_split(data_dir_mini)
    rc = _weight_run_config(data_dir_mini, 2.5, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"})
    assert compute_score_file(data_dir_mini, rc)["w"] == pytest.approx(2.5 * max(means.values()))


def test_unknown_weight_set_errors(data_dir_mini: Path) -> None:
    """未定義の重みセット参照はエラーになることを検証する。"""
    rc = _weight_run_config(data_dir_mini, None, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"})
    with pytest.raises(ValueError, match="unknown weight set 'WLgroupWeight'"):
        compute_score_file(data_dir_mini, rc)


# ------------------------------------------------ モデル検証


def test_by_rejected_for_non_transform_op() -> None:
    """変換op以外への 'by' 指定が拒否されることを検証する。"""
    with pytest.raises(ValidationError, match="'by' applies only to transform ops"):
        AggregationSpec.model_validate({"op": "filter", "value": "A2B", "by": "WLgroup"})


def test_transform_ref_requires_by() -> None:
    """'ref' 指定には 'by' が必須であることを検証する。"""
    with pytest.raises(ValidationError, match="also needs 'by'"):
        AggregationSpec.model_validate({"op": "mul", "ref": "WLgroupWeight"})


def test_div_by_zero_rejected() -> None:
    """0 除算になる指定が拒否されることを検証する。"""
    with pytest.raises(ValidationError, match="divide by zero"):
        AggregationSpec.model_validate({"op": "div", "value": 0})
    with pytest.raises(ValidationError, match="divide by zero"):
        AggregationSpec.model_validate({"op": "div", "by": "g", "value": {"a": 0}})


def test_bad_wlgroup_weight_rejected() -> None:
    """不正な WLgroupWeight の値が拒否されることを検証する。"""
    with pytest.raises(ValidationError, match="weight set 'WLgroupWeight'"):
        RunConfig.model_validate({"Generation": "G", "optimization": {"WLgroupWeight": "heavy"}})


def test_wlgroup_defin_logical_string_parsing() -> None:
    """WLgroupDefinLogical の文字列 'True'/'False' の解釈を検証する。"""
    for raw, expected in [("True", True), ("False", False), (True, True), (False, False)]:
        rc = RunConfig.model_validate(
            {
                "Generation": "G",
                "optimization": {"WLgroup": {"g": [0, 1]}, "WLgroupDefinLogical": raw},
            }
        )
        assert rc.group_defs()["WLgroup"].definedInLogical is expected
    with pytest.raises(ValidationError, match="WLgroupDefinLogical"):
        RunConfig.model_validate({"Generation": "G", "optimization": {"WLgroupDefinLogical": "yes"}})


# ------------------------------------------------ Physical 記法のグループ定義


def _logical_physical_configs(data_dir_mini: Path) -> tuple[RunConfig, RunConfig, int]:
    """同じ分割を Logical / Physical の両記法で書いた config の組。

    Returns:
        (Logical 記法の RunConfig, Physical 記法の RunConfig, WL 本数) のタプル。

    """
    df = _raw_fbc(data_dir_mini)
    n = int(cast("int", df["WL"].max())) + 1
    logical = {"gLow": [0, 1], "gHigh": [2, n - 1]}
    physical = {name: [n - 1 - hi, n - 1 - lo] for name, (lo, hi) in logical.items()}

    def rc(groups: dict[str, list[int]], defin_logical: bool) -> RunConfig:
        return RunConfig.model_validate(
            {
                "Generation": "B9LS",
                "optimization": {
                    "WLgroup": groups,
                    "WLgroupDefinLogical": defin_logical,
                    "score_parts": [
                        {
                            "name": "p",
                            "type": "FBC",
                            "order": ["WL", "WLgroup"],
                            "aggregations": {"WL": {"op": "mean"}, "WLgroup": {"op": "max"}},
                        }
                    ],
                    "expression": "p",
                },
            }
        )

    return rc(logical, defin_logical=True), rc(physical, defin_logical=False), n


def test_physical_group_def_matches_logical(data_dir_mini: Path, tmp_path: Path) -> None:
    """Physical 記法のグループ定義が Logical 記法と同じ結果になることを検証する。"""
    rc_logical, rc_physical, n = _logical_physical_configs(data_dir_mini)
    geninfo = tmp_path / "B9LS.json"
    geninfo.write_text(json.dumps({"numWLs": n}), encoding="utf-8")

    expected = compute_score_file(data_dir_mini, rc_logical)["p"]
    actual = compute_score_file(data_dir_mini, rc_physical, generation_info_path=geninfo)["p"]
    assert actual == pytest.approx(expected)


def test_physical_group_def_discovers_geninfo_in_data_dir(data_dir_mini: Path, tmp_path: Path) -> None:
    """データディレクトリ内の {Generation}.json を自動発見することを検証する。"""
    rc_logical, rc_physical, n = _logical_physical_configs(data_dir_mini)
    d = tmp_path / "data"
    import shutil

    shutil.copytree(data_dir_mini, d)
    (d / "B9LS.json").write_text(json.dumps({"numWLs": n}), encoding="utf-8")
    assert compute_score_file(d, rc_physical)["p"] == pytest.approx(compute_score_file(data_dir_mini, rc_logical)["p"])


def test_physical_group_def_without_geninfo_derives_counts_from_data(data_dir_mini: Path) -> None:
    """{Generation}.json が無い場合は測定csvから軸総数を導出する。

    本数は世代で固定・フローは全数を測定するため max+1 が正確
    (docs/spec_change_dataname_measure.md 9節)。json ありと同じ結果になること。
    """
    rc_logical, rc_physical, _ = _logical_physical_configs(data_dir_mini)
    assert compute_score_file(data_dir_mini, rc_physical)["p"] == pytest.approx(
        compute_score_file(data_dir_mini, rc_logical)["p"]
    )


def test_physical_group_def_underivable_axis_errors(tmp_path: Path, data_dir_mini: Path) -> None:
    """Json も無く、データにも該当軸が無い場合は明確なエラー。"""
    import shutil

    d = tmp_path / "data"
    shutil.copytree(data_dir_mini, d)
    rc = RunConfig.model_validate(
        {
            "Generation": "B9LS",
            "optimization": {
                "groupDefs": {"Xgroup": {"axis": "NoSuchAxis", "groups": {"a": [0, 1]}, "definedInLogical": False}},
                "score_parts": [{"name": "p", "type": "FBC", "order": ["WL"], "aggregations": {"WL": {"op": "mean"}}}],
                "expression": "p",
            },
        }
    )
    with pytest.raises(ValueError, match="could not be determined"):
        compute_score_file(d, rc)


def test_resolve_group_defs_identity_for_logical(data_dir_mini: Path) -> None:
    """Logical 記法では resolve_group_defs が定義をそのまま返すことを検証する。"""
    rc_logical, _, _ = _logical_physical_configs(data_dir_mini)
    defs = resolve_group_defs(rc_logical, data_dir_mini)
    assert defs["WLgroup"].groups == rc_logical.group_defs()["WLgroup"].groups


def test_resolved_groups_reverses_ranges() -> None:
    """definedInLogical=False の resolved_groups が範囲を反転することを検証する。"""
    gd = GroupDef(axis="WL", groups={"a": (0, 2), "b": (3, 5)}, definedInLogical=False)
    assert gd.resolved_groups(6) == {"a": (3, 5), "b": (0, 2)}
    with pytest.raises(ValueError, match="axis count"):
        gd.resolved_groups(None)


# ------------------------------------------------ 単項変換op(abs / log)


def test_unary_abs_step(data_dir_mini: Path) -> None:
    """__abs__ = |x| を行単位で適用(0.6.0 で追加)。"""
    base = compute_score_part(
        data_dir_mini, _mean_part(extra_order=["__neg__"], extra_aggs={"__neg__": {"op": "mul", "value": -1}})
    )
    part = _mean_part(
        extra_order=["__neg__", "__abs__"],
        extra_aggs={"__neg__": {"op": "mul", "value": -1}, "__abs__": {"op": "abs"}},
    )
    assert compute_score_part(data_dir_mini, part) == pytest.approx(abs(base))


def test_unary_log_matches_manual(data_dir_mini: Path) -> None:
    """KLD の標準計算の形が polars で手組みした同じ計算と一致することを検証する。

    形は Board/Chip 平均 → log(max(|x|, 1e-6)) → 重み 0.1 * SGWLD 総和。
    """
    import math

    part = ScorePart.model_validate(
        {
            "name": "kld",
            "type": "KLD",
            "order": ["Board", "Chip", "__log__", "SGWLD"],
            "aggregations": {
                "Board": {"op": "mean"},
                "Chip": {"op": "mean"},
                "__log__": {"op": "log", "floor": 1e-6},
                "SGWLD": {"op": "sum", "weight": 0.1},
            },
        }
    )
    actual = compute_score_part(data_dir_mini, part)
    df = pl.read_csv(data_dir_mini / "KLD.csv")
    # mini は Board*Chip*SGWLD の全組み合わせなので逐次 mean = SGWLD ごとの単純 mean
    per_sgwld = df.group_by("SGWLD").agg(pl.col("KLD").mean())["KLD"].to_list()
    expected = sum(0.1 * math.log(max(abs(v), 1e-6)) for v in per_sgwld)
    assert actual == pytest.approx(expected)


def test_unary_op_validation() -> None:
    """単項変換op(log/abs)のモデル検証を確認する。"""
    with pytest.raises(ValidationError, match="floor"):
        AggregationSpec(op="log")  # floor 必須
    with pytest.raises(ValidationError, match="floor"):
        AggregationSpec(op="log", floor=-1.0)  # 正の値のみ
    with pytest.raises(ValidationError, match="value"):
        AggregationSpec(op="abs", value=1)  # 定数は取らない
    with pytest.raises(ValidationError, match="floor"):
        AggregationSpec(op="mul", value=2, floor=1e-6)  # floor は log 専用
