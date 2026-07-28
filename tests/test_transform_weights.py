"""変換ステップの拡張（add/sub/mul/div・複数回・グループ別重み）と
Physical 記法グループ定義（definedInLogical / WLgroupDefinLogical）のテスト。"""
import json

import polars as pl
import pytest
from pydantic import ValidationError

from scorelib_param.cli import compute_score_file, compute_score_part, resolve_group_defs
from scorelib_param.models import AggregationSpec, GroupDef, RunConfig, ScorePart


def _mean_part(name="p", extra_order=(), extra_aggs=None):
    """FBC を WL 平均だけで畳む最小パーツ（他の軸は暗黙集約）。"""
    return ScorePart(
        name=name,
        type="FBC",
        order=["WL", *extra_order],
        aggregations={"WL": {"op": "mean"}, **(extra_aggs or {})},
    )


def _raw_fbc(data_dir_mini) -> pl.DataFrame:
    return pl.read_csv(data_dir_mini / "FBC.csv")


# ------------------------------------------------ 定数演算（add/sub/mul/div）

def test_constant_transforms_chained(data_dir_mini):
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


def test_sign_flip_with_mul(data_dir_mini):
    base = compute_score_part(data_dir_mini, _mean_part())
    part = _mean_part(extra_order=["__flip__"], extra_aggs={"__flip__": {"op": "mul", "value": -1}})
    assert compute_score_part(data_dir_mini, part) == pytest.approx(-base)


# ------------------------------------------------ グループ別重み（by + 辞書）

def _wl_split(data_dir_mini):
    """mini データの WL 範囲を2グループに割った定義と、素の csv からの
    グループ別 FBC 平均（独立再計算の正解）を返す。"""
    df = _raw_fbc(data_dir_mini)
    wl_max = df["WL"].max()
    groups = {"gLow": (0, 1), "gHigh": (2, wl_max)}
    means = {
        name: df.filter((pl.col("WL") >= lo) & (pl.col("WL") <= hi))["FBC"].mean()
        for name, (lo, hi) in groups.items()
    }
    return groups, means


def test_group_weight_inline_dict(data_dir_mini):
    groups, means = _wl_split(data_dir_mini)
    weights = {"gLow": 1.0, "gHigh": 10.0}
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "__weight__", "WLg"],
        aggregations={
            "WL": {"op": "mean"},
            "__weight__": {"op": "mul", "by": "WLg", "value": weights},
            "WLg": {"op": "max"},
        },
    )
    defs = {"WLg": GroupDef(axis="WL", groups=groups)}
    actual = compute_score_part(data_dir_mini, part, group_defs=defs)
    assert actual == pytest.approx(max(weights[g] * means[g] for g in groups))


def test_group_weight_missing_group_errors(data_dir_mini):
    groups, _ = _wl_split(data_dir_mini)
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "__weight__", "WLg"],
        aggregations={
            "WL": {"op": "mean"},
            "__weight__": {"op": "mul", "by": "WLg", "value": {"gLow": 1.0}},  # gHigh 欠落
            "WLg": {"op": "max"},
        },
    )
    with pytest.raises(ValueError, match="no entry in the transform weights.*gHigh"):
        compute_score_part(data_dir_mini, part, group_defs={"WLg": GroupDef(axis="WL", groups=groups)})


def test_group_weight_after_axis_collapsed_errors(data_dir_mini):
    groups, _ = _wl_split(data_dir_mini)
    part = ScorePart(
        name="w",
        type="FBC",
        order=["WL", "WLg", "__weight__"],  # WLg を潰した後に重み
        aggregations={
            "WL": {"op": "mean"},
            "WLg": {"op": "max"},
            "__weight__": {"op": "mul", "by": "WLg", "value": {"gLow": 1.0, "gHigh": 2.0}},
        },
    )
    with pytest.raises(ValueError, match="not present at this step"):
        compute_score_part(data_dir_mini, part, group_defs={"WLg": GroupDef(axis="WL", groups=groups)})


def _weight_run_config(data_dir_mini, weight, weight_spec):
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


def test_wlgroup_weight_ref_matches_inline(data_dir_mini):
    weights = {"gLow": 1.0, "gHigh": 10.0}
    by_ref = _weight_run_config(
        data_dir_mini, weights, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    )
    inline = _weight_run_config(
        data_dir_mini, None, {"op": "mul", "by": "WLgroup", "value": weights}
    )
    assert compute_score_file(data_dir_mini, by_ref)["w"] == pytest.approx(
        compute_score_file(data_dir_mini, inline)["w"]
    )


def test_wlgroup_weight_scalar_applies_to_all(data_dir_mini):
    _, means = _wl_split(data_dir_mini)
    rc = _weight_run_config(
        data_dir_mini, 2.5, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    )
    assert compute_score_file(data_dir_mini, rc)["w"] == pytest.approx(2.5 * max(means.values()))


def test_unknown_weight_set_errors(data_dir_mini):
    rc = _weight_run_config(
        data_dir_mini, None, {"op": "mul", "by": "WLgroup", "ref": "WLgroupWeight"}
    )
    with pytest.raises(ValueError, match="unknown weight set 'WLgroupWeight'"):
        compute_score_file(data_dir_mini, rc)


# ------------------------------------------------ モデル検証

def test_by_rejected_for_non_transform_op():
    with pytest.raises(ValidationError, match="'by' applies only to transform ops"):
        AggregationSpec.model_validate({"op": "filter", "value": "A2B", "by": "WLgroup"})


def test_transform_ref_requires_by():
    with pytest.raises(ValidationError, match="also needs 'by'"):
        AggregationSpec.model_validate({"op": "mul", "ref": "WLgroupWeight"})


def test_div_by_zero_rejected():
    with pytest.raises(ValidationError, match="divide by zero"):
        AggregationSpec.model_validate({"op": "div", "value": 0})
    with pytest.raises(ValidationError, match="divide by zero"):
        AggregationSpec.model_validate({"op": "div", "by": "g", "value": {"a": 0}})


def test_bad_wlgroup_weight_rejected():
    with pytest.raises(ValidationError, match="weight set 'WLgroupWeight'"):
        RunConfig.model_validate(
            {"Generation": "G", "optimization": {"WLgroupWeight": "heavy"}}
        )


def test_wlgroup_defin_logical_string_parsing():
    for raw, expected in [("True", True), ("False", False), (True, True), (False, False)]:
        rc = RunConfig.model_validate(
            {
                "Generation": "G",
                "optimization": {"WLgroup": {"g": [0, 1]}, "WLgroupDefinLogical": raw},
            }
        )
        assert rc.group_defs()["WLgroup"].definedInLogical is expected
    with pytest.raises(ValidationError, match="WLgroupDefinLogical"):
        RunConfig.model_validate(
            {"Generation": "G", "optimization": {"WLgroupDefinLogical": "yes"}}
        )


# ------------------------------------------------ Physical 記法のグループ定義

def _logical_physical_configs(data_dir_mini):
    """同じ分割を Logical / Physical の両記法で書いた config の組。"""
    df = _raw_fbc(data_dir_mini)
    n = int(df["WL"].max()) + 1
    logical = {"gLow": [0, 1], "gHigh": [2, n - 1]}
    physical = {name: [n - 1 - hi, n - 1 - lo] for name, (lo, hi) in logical.items()}

    def rc(groups, defin_logical):
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

    return rc(logical, True), rc(physical, False), n


def test_physical_group_def_matches_logical(data_dir_mini, tmp_path):
    rc_logical, rc_physical, n = _logical_physical_configs(data_dir_mini)
    geninfo = tmp_path / "B9LS.json"
    geninfo.write_text(json.dumps({"numWLs": n}), encoding="utf-8")

    expected = compute_score_file(data_dir_mini, rc_logical)["p"]
    actual = compute_score_file(
        data_dir_mini, rc_physical, generation_info_path=geninfo
    )["p"]
    assert actual == pytest.approx(expected)


def test_physical_group_def_discovers_geninfo_in_data_dir(data_dir_mini, tmp_path):
    rc_logical, rc_physical, n = _logical_physical_configs(data_dir_mini)
    d = tmp_path / "data"
    import shutil

    shutil.copytree(data_dir_mini, d)
    (d / "B9LS.json").write_text(json.dumps({"numWLs": n}), encoding="utf-8")
    assert compute_score_file(d, rc_physical)["p"] == pytest.approx(
        compute_score_file(data_dir_mini, rc_logical)["p"]
    )


def test_physical_group_def_without_geninfo_derives_counts_from_data(data_dir_mini):
    """{Generation}.json が無い場合は測定csvから軸総数を導出する（本数は世代で
    固定・フローは全数を測定するため max+1 が正確 —
    docs/spec_change_dataname_measure.md 9節）。json ありと同じ結果になること。"""
    rc_logical, rc_physical, _ = _logical_physical_configs(data_dir_mini)
    assert compute_score_file(data_dir_mini, rc_physical)["p"] == pytest.approx(
        compute_score_file(data_dir_mini, rc_logical)["p"]
    )


def test_physical_group_def_underivable_axis_errors(tmp_path, data_dir_mini):
    """json も無く、データにも該当軸が無い場合は明確なエラー。"""
    import shutil

    d = tmp_path / "data"
    shutil.copytree(data_dir_mini, d)
    rc = RunConfig.model_validate({
        "Generation": "B9LS",
        "optimization": {
            "groupDefs": {"Xgroup": {"axis": "NoSuchAxis", "groups": {"a": [0, 1]},
                                     "definedInLogical": False}},
            "score_parts": [{"name": "p", "type": "FBC", "order": ["WL"],
                             "aggregations": {"WL": {"op": "mean"}}}],
            "expression": "p",
        },
    })
    with pytest.raises(ValueError, match="could not be determined"):
        compute_score_file(d, rc)


def test_resolve_group_defs_identity_for_logical(data_dir_mini):
    rc_logical, _, _ = _logical_physical_configs(data_dir_mini)
    defs = resolve_group_defs(rc_logical, data_dir_mini)
    assert defs["WLgroup"].groups == rc_logical.group_defs()["WLgroup"].groups


def test_resolved_groups_reverses_ranges():
    gd = GroupDef(axis="WL", groups={"a": (0, 2), "b": (3, 5)}, definedInLogical=False)
    assert gd.resolved_groups(6) == {"a": (3, 5), "b": (0, 2)}
    with pytest.raises(ValueError, match="axis count"):
        gd.resolved_groups(None)
