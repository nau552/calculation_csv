"""集計時重み（AggregationSpec.weight / weight_ref）のテスト。

意味は「その軸を潰す直前に、軸の値ごとの重みを値列に乗じてから集計」
（正規化された加重平均ではない: mean なら mean(weight × value)）。
変換ステップ（by + mul）を集計直前に置いた場合と結果が一致すること、
weight_ref が weightSets から解決されること、形状検査を確認する。
"""
import math

import polars as pl
import pytest

from scorelib_param.aggregate import apply_axis_op
from scorelib_param.cli import compute_score_part
from scorelib_param.models import AggregationSpec, GroupDef, ScorePart
from ui.state import _part_weight_refs


def _lf():
    return pl.LazyFrame({"grp": ["g0", "g0", "g1"], "v": [1.0, 2.0, 5.0]})


class TestApplyAxisOp:
    def test_weighted_max_scales_before_reduce(self):
        spec = AggregationSpec(op="max", weight={"g0": 10.0, "g1": 1.0})
        out = apply_axis_op(_lf(), "v", "grp", spec, []).collect()
        # 重みなしなら max=5 だが、乗算後 (10, 20, 5) の max=20
        assert out["v"].to_list() == [20.0]

    def test_weighted_mean_is_not_normalized(self):
        spec = AggregationSpec(op="mean", weight={"g0": 10.0, "g1": 1.0})
        out = apply_axis_op(_lf(), "v", "grp", spec, []).collect()
        assert out["v"][0] == pytest.approx((10.0 + 20.0 + 5.0) / 3)

    def test_scalar_weight(self):
        spec = AggregationSpec(op="sum", weight=2.0)
        out = apply_axis_op(_lf(), "v", "grp", spec, []).collect()
        assert out["v"][0] == pytest.approx(16.0)

    def test_uncovered_value_raises(self):
        spec = AggregationSpec(op="mean", weight={"g0": 1.0})
        with pytest.raises(ValueError, match="no entry in the aggregation weights"):
            apply_axis_op(_lf(), "v", "grp", spec, []).collect()

    def test_subset_selection_limits_coverage_check(self):
        """選択集合で絞った後の値だけ重みがあればよい。"""
        spec = AggregationSpec(op="mean", value=["g0"], weight={"g0": 3.0})
        out = apply_axis_op(_lf(), "v", "grp", spec, []).collect()
        assert out["v"][0] == pytest.approx((3.0 + 6.0) / 2)


class TestEquivalenceWithTransformStep:
    def test_inline_weight_equals_transform_step_before_collapse(self, data_dir_mini):
        """集計時重みは、集計直前に置いた変換ステップ（by + mul）と同値。"""
        group_defs = {
            "WLgroup": GroupDef(axis="WL", groups={"lo": (0, 2), "hi": (3, 5)}, definedInLogical=True)
        }
        weights = {"lo": 1.0, "hi": 10.0}
        common = {
            "type": "FBC",
            "relative": {
                "split_axis": "Read_Override", "numerator_when": True,
                "denominator_when": False, "denominator_offset": 1,
            },
        }
        aggs = {
            "Read_Label": {"op": "filter", "value": "read_level_upper1"},
            "State": {"op": "filter", "value": "A2B"},
            "WL": {"op": "mean"}, "STR": {"op": "mean"}, "Board": {"op": "mean"},
            "Chip": {"op": "mean"}, "Block": {"op": "mean"},
        }
        inline = ScorePart.model_validate({
            "name": "inline", **common,
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block", "WLgroup"],
            "aggregations": {**aggs, "WLgroup": {"op": "max", "weight": weights}},
        })
        stepped = ScorePart.model_validate({
            "name": "stepped", **common,
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block",
                      "__wt__", "WLgroup"],
            "aggregations": {**aggs,
                             "__wt__": {"op": "mul", "by": "WLgroup", "value": weights},
                             "WLgroup": {"op": "max"}},
        })
        a = compute_score_part(data_dir_mini, inline, group_defs=group_defs)
        b = compute_score_part(data_dir_mini, stepped, group_defs=group_defs)
        assert math.isclose(a, b, rel_tol=1e-12)
        # 重みが実際に効いていること（重みなしと異なる値になる）
        unweighted = ScorePart.model_validate({
            "name": "plain", **common,
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block", "WLgroup"],
            "aggregations": {**aggs, "WLgroup": {"op": "max"}},
        })
        c = compute_score_part(data_dir_mini, unweighted, group_defs=group_defs)
        assert not math.isclose(a, c, rel_tol=1e-6)


class TestWeightRef:
    def _part(self, wlgroup_spec) -> ScorePart:
        return ScorePart.model_validate({
            "name": "p", "type": "FBC",
            "order": ["WLgroup"],
            "aggregations": {"WLgroup": wlgroup_spec},
        })

    def test_weight_ref_resolves_from_weight_sets(self):
        p = self._part({"op": "max", "weight_ref": "W"})
        resolved = p.resolve_selection_refs({}, {"W": {"lo": 1.0, "hi": 10.0}})
        spec = resolved.aggregations["WLgroup"]
        assert spec.weight == {"lo": 1.0, "hi": 10.0}
        assert spec.weight_ref is None

    def test_scalar_weight_set(self):
        p = self._part({"op": "mean", "weight_ref": "W"})
        assert p.resolve_selection_refs({}, {"W": 2.0}).aggregations["WLgroup"].weight == 2.0

    def test_unknown_weight_ref_raises(self):
        p = self._part({"op": "max", "weight_ref": "nope"})
        with pytest.raises(ValueError, match="unknown weight set 'nope'"):
            p.resolve_selection_refs({}, {"W": {}})


class TestValidation:
    def test_weight_on_filter_rejected(self):
        with pytest.raises(ValueError, match="apply only to aggregation ops"):
            AggregationSpec(op="filter", value="A2B", weight={"a": 1.0})

    def test_weight_and_weight_ref_rejected(self):
        with pytest.raises(ValueError, match="not both"):
            AggregationSpec(op="mean", weight={"a": 1.0}, weight_ref="W")

    def test_non_numeric_weight_rejected(self):
        with pytest.raises(ValueError, match="dict of numbers"):
            AggregationSpec(op="mean", weight={"a": "x"})


def test_part_weight_refs_includes_inline_weight_ref():
    part = {
        "aggregations": {
            "WLgroup": {"op": "max", "weight_ref": "W1"},
            "__wt__": {"op": "mul", "by": "WLgroup", "ref": "W2"},
        },
    }
    assert _part_weight_refs(part) == ["W1", "W2"]
