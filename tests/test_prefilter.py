"""filter 前絞り（_hoistable_prefilters）のテスト。

暗黙挿入される __relative__ より前に安全な filter の行絞りを適用する最適化が、
(1) 対象の判定を正しく行うこと、(2) 結果を変えないこと（前絞りなしと同値）、
(3) prefix_cache を混線させないこと、を確認する。
"""
import math

import pytest

from scorelib_param import cli
from scorelib_param.cli import SharedComputeContext, _hoistable_prefilters, compute_score_part
from scorelib_param.models import GroupDef, ScorePart


def _part(**overrides) -> ScorePart:
    """暗黙 __relative__ + 先頭 filter 2つの典型形（dVtBudget_R2A 相当の FBC 版）。"""
    base = {
        "name": "p",
        "type": "FBC",
        "relative": {
            "split_axis": "Read_Override",
            "numerator_when": True,
            "denominator_when": False,
            "denominator_offset": 1,
        },
        "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
        "aggregations": {
            "Read_Label": {"op": "filter", "value": "read_level_upper1"},
            "State": {"op": "filter", "value": "A2B"},
            "WL": {"op": "mean"},
            "STR": {"op": "mean"},
            "Board": {"op": "mean"},
            "Chip": {"op": "mean"},
            "Block": {"op": "mean"},
        },
    }
    base.update(overrides)
    return ScorePart(**base)


class TestHoistableDetection:
    def test_leading_filters_are_hoisted_in_order(self):
        assert _hoistable_prefilters(_part()) == [
            ("Read_Label", "read_level_upper1"),
            ("State", "A2B"),
        ]

    def test_no_relative_returns_empty(self):
        assert _hoistable_prefilters(_part(relative=None)) == []

    def test_explicit_relative_step_is_respected(self):
        p = _part(order=["Read_Label", "__relative__", "State", "WL", "STR", "Board", "Chip", "Block"])
        assert _hoistable_prefilters(p) == []

    def test_stops_at_first_non_filter_step(self):
        p = _part(order=["Read_Label", "WL", "State", "STR", "Board", "Chip", "Block"])
        assert _hoistable_prefilters(p) == [("Read_Label", "read_level_upper1")]

    def test_denominator_pre_aggregation_axis_blocks(self):
        p = _part(relative={
            "split_axis": "Read_Override", "numerator_when": True,
            "denominator_when": False, "denominator_offset": 1,
            "denominator_pre_aggregation": [{"axis": "State", "op": "mean"}],
        })
        # State は分母で潰される軸なので Read_Label で走査が止まる
        assert _hoistable_prefilters(p) == [("Read_Label", "read_level_upper1")]

    def test_derived_axis_of_preaggregated_source_blocks(self):
        """WL を分母事前集計するとき、WL 由来の派生軸 WLgroup の filter も前に出さない。"""
        group_defs = {"WLgroup": GroupDef(axis="WL", groups={"g": (0, 5)}, definedInLogical=True)}
        p = _part(
            order=["WLgroup", "Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            aggregations={
                "WLgroup": {"op": "filter", "value": "g"},
                "Read_Label": {"op": "filter", "value": "read_level_upper1"},
                "State": {"op": "filter", "value": "A2B"},
                "WL": {"op": "mean"}, "STR": {"op": "mean"}, "Board": {"op": "mean"},
                "Chip": {"op": "mean"}, "Block": {"op": "mean"},
            },
            relative={
                "split_axis": "Read_Override", "numerator_when": True,
                "denominator_when": False, "denominator_offset": 1,
                "denominator_pre_aggregation": [{"axis": "WL", "op": "mean"}],
            },
        )
        assert _hoistable_prefilters(p, group_defs) == []


class TestEquivalence:
    def _both_ways(self, data_dir, part, monkeypatch, **kwargs):
        with_prefilter = compute_score_part(data_dir, part, **kwargs)
        monkeypatch.setattr(cli, "_hoistable_prefilters", lambda *a, **k: [])
        without = compute_score_part(data_dir, part, **kwargs)
        return with_prefilter, without

    def test_same_value_with_and_without_prefilter(self, data_dir_mini, monkeypatch):
        a, b = self._both_ways(data_dir_mini, _part(), monkeypatch)
        assert math.isclose(a, b, rel_tol=1e-12)

    def test_same_value_when_preagg_blocks_one_filter(self, data_dir_mini, monkeypatch):
        """WL/STR の分母事前集計あり: Read_Label だけ前に出て State は出ない形でも同値。"""
        p = _part(relative={
            "split_axis": "Read_Override", "numerator_when": True,
            "denominator_when": False, "denominator_offset": 1,
            "denominator_pre_aggregation": [
                {"axis": "WL", "op": "mean"},
                {"axis": "STR", "op": "mean"},
            ],
        })
        assert _hoistable_prefilters(p) == [
            ("Read_Label", "read_level_upper1"), ("State", "A2B"),
        ]
        a, b = self._both_ways(data_dir_mini, p, monkeypatch)
        assert math.isclose(a, b, rel_tol=1e-12)


class TestCacheSafety:
    def test_parts_differing_only_in_prefilter_do_not_share_cache(self, data_dir_mini):
        """State filter の値だけ違う2パーツ: ステップ署名列は相対化まで同一なので、
        prefilters がキャッシュキーに入っていないと2つ目が1つ目の中間結果を
        誤って再利用する。単独計算との一致で混線がないことを確認する。"""
        p1 = _part(name="a2b")
        p2 = _part(name="r2a")
        p2 = p2.model_copy(update={
            "aggregations": {**p2.aggregations,
                             "State": p2.aggregations["State"].model_copy(update={"value": "R2A"})}
        })
        ctx = SharedComputeContext(data_dir_mini, [p1, p2])
        v1 = compute_score_part(data_dir_mini, p1, shared_ctx=ctx)
        v2 = compute_score_part(data_dir_mini, p2, shared_ctx=ctx)
        assert math.isclose(v1, compute_score_part(data_dir_mini, p1), rel_tol=1e-12)
        assert math.isclose(v2, compute_score_part(data_dir_mini, p2), rel_tol=1e-12)
        assert not math.isclose(v1, v2, rel_tol=1e-6)  # 別物の値になっているはず
