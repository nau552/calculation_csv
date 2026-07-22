"""filter 前絞り（_hoistable_prefilters）のテスト。

暗黙挿入される __relative__ より前に安全な filter の行絞りを適用する最適化が、
(1) 対象の判定を正しく行うこと、(2) 結果を変えないこと（前絞りなしと同値）、
(3) prefix_cache を混線させないこと、を確認する。
"""
import math

import pytest

from scorelib_param import cli, io_jsonc
from scorelib_param.cli import SharedComputeContext, _hoistable_prefilters, compute_score_part
from scorelib_param.dvtbudget import load_board_temperatures
from scorelib_param.models import DvtBudgetCoefFile, GroupDef, ScorePart


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

    def test_no_relative_still_hoists(self):
        """relative が無くても filter の前出しは有効（split軸等の除外が無いだけ）。"""
        assert _hoistable_prefilters(_part(relative=None)) == [
            ("Read_Label", "read_level_upper1"),
            ("State", "A2B"),
        ]

    def test_explicit_relative_part_also_hoists(self):
        """明示配置の __relative__ より後ろの filter も前に出す（可換なため）。"""
        p = _part(order=["Read_Label", "__relative__", "State", "WL", "STR", "Board", "Chip", "Block"])
        assert _hoistable_prefilters(p) == [
            ("Read_Label", "read_level_upper1"),
            ("State", "A2B"),
        ]

    def test_filters_beyond_non_filter_steps_are_hoisted(self):
        """途中に集計ステップが挟まっても、その先の filter は前に出す。"""
        p = _part(order=["Read_Label", "WL", "State", "STR", "Board", "Chip", "Block"])
        assert _hoistable_prefilters(p) == [
            ("Read_Label", "read_level_upper1"),
            ("State", "A2B"),
        ]

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
        # WLgroup は除外されるが、他の filter は前に出る
        assert _hoistable_prefilters(p, group_defs) == [
            ("Read_Label", "read_level_upper1"),
            ("State", "A2B"),
        ]


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

    def test_same_value_for_explicit_relative_dvt_part(
        self, data_dir_mini, dvtbudget_coef_path, monkeypatch
    ):
        """明示 __relative__ + 末尾 State filter（deltaR_upper_tail 相当の形）でも同値。"""
        p = ScorePart.model_validate({
            "name": "delta_style",
            "type": "dVtBudget",
            "relative": {
                "split_axis": "Read_Override", "numerator_when": True,
                "denominator_when": False, "denominator_offset": 0,
            },
            "order": ["Read_Label", "__offset__", "STR", "WL",
                      "__relative__", "__dvtbudget__", "State", "Chip", "Block", "Board"],
            "aggregations": {
                "__offset__": {"op": "add", "value": 1},
                "Read_Label": {"op": "filter", "value": "read_level_upper1"},
                "State": {"op": "filter", "value": "R2A"},
                "WL": {"op": "mean"}, "STR": {"op": "mean"}, "Board": {"op": "max"},
                "Chip": {"op": "mean"}, "Block": {"op": "mean"},
            },
        })
        assert _hoistable_prefilters(p) == [
            ("Read_Label", "read_level_upper1"), ("State", "R2A"),
        ]
        kwargs = {
            "generation": "B9LS",
            "dvtbudget_coef": io_jsonc.load_dvtbudget_coef(dvtbudget_coef_path),
            "board_temperatures": load_board_temperatures(data_dir_mini / "initial_temperature.csv"),
        }
        a, b = self._both_ways(data_dir_mini, p, monkeypatch, **kwargs)
        assert math.isclose(a, b, rel_tol=1e-12)

    def test_same_value_without_relative(self, data_dir_mini, monkeypatch):
        """relative の無いパーツでも、集計より後ろの filter を前に出して同値。"""
        p = _part(
            relative=None,
            order=["WL", "Read_Label", "State", "STR", "Board", "Chip", "Block"],
        )
        assert _hoistable_prefilters(p) == [
            ("Read_Label", "read_level_upper1"), ("State", "A2B"),
        ]
        a, b = self._both_ways(data_dir_mini, p, monkeypatch)
        assert math.isclose(a, b, rel_tol=1e-12)


class TestDiagnosticsChange:
    def test_partial_coef_suffices_when_state_is_filtered(self, data_dir_mini):
        """filter 前絞りの診断上の変化（意図した仕様）: State を filter で1つに
        絞るパーツは、dVtBudget 係数がその State の分だけあれば計算できる
        （従来は変換が全 State に走るため全 State 分の係数が必要だった）。"""
        entry = {"a": 0.13, "b": -4.667}
        coef = DvtBudgetCoefFile.model_validate(
            {"B9LS": {"-30": {"R2A": entry}, "85": {"R2A": entry}}}
        )
        p = ScorePart.model_validate({
            "name": "r2a_only",
            "type": "dVtBudget",
            "relative": {
                "split_axis": "Read_Override", "numerator_when": True,
                "denominator_when": False, "denominator_offset": 1,
            },
            "order": ["Read_Label", "State", "WL", "STR", "Board", "Chip", "Block"],
            "aggregations": {
                "Read_Label": {"op": "filter", "value": "read_level_upper1"},
                "State": {"op": "filter", "value": "R2A"},
                "WL": {"op": "mean"}, "STR": {"op": "mean"}, "Board": {"op": "mean"},
                "Chip": {"op": "mean"}, "Block": {"op": "mean"},
            },
        })
        temps = load_board_temperatures(data_dir_mini / "initial_temperature.csv")
        value = compute_score_part(
            data_dir_mini, p, generation="B9LS", dvtbudget_coef=coef, board_temperatures=temps
        )
        assert math.isfinite(value)


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
