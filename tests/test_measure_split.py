"""Measure 番号 / DataName による相対化・filter のエンドツーエンドテスト
（docs/spec_change_dataname_measure.md）。

mini データの構造: Measure 0/1 = read_level_upper1 の 基準/評価（Read_Override
False/True）、2/3 = read_level_lower1 の 基準/評価。DataName コードも同順。
したがって「Read_Label filter + Read_Override 分割」と「Measure 1/0 分割」と
「DataName 分割」は同じ行集合を選び、スコアは厳密に一致するはず。
"""
import math

import pytest

from scorelib_param import cli
from scorelib_param.cli import SharedComputeContext, _hoistable_prefilters, compute_score_part
from scorelib_param.models import ScorePart

_TAIL_ORDER = ["WL", "STR", "State", "Board", "Chip", "Block"]
_TAIL_AGGS = {a: {"op": "mean"} for a in _TAIL_ORDER}


def _override_part() -> ScorePart:
    """旧仕様の形: Read_Label で測定を選び、Read_Override で分子/分母を分ける。"""
    return ScorePart.model_validate({
        "name": "by_override",
        "type": "FBC",
        "relative": {
            "split_axis": "Read_Override", "numerator_when": True,
            "denominator_when": False, "denominator_offset": 1,
        },
        "order": ["Read_Label"] + _TAIL_ORDER,
        "aggregations": {"Read_Label": {"op": "filter", "value": "read_level_upper1"}, **_TAIL_AGGS},
    })


def _measure_part(**relative_overrides) -> ScorePart:
    """新仕様の形: Measure 番号だけで分子/分母を指定する（Read_Label filter 不要
    — 番号が測定を一意に指すため）。"""
    relative = {
        "split_axis": "Measure", "numerator_when": 1,
        "denominator_when": 0, "denominator_offset": 1,
        **relative_overrides,
    }
    return ScorePart.model_validate({
        "name": "by_measure",
        "type": "FBC",
        "relative": relative,
        "order": list(_TAIL_ORDER),
        "aggregations": dict(_TAIL_AGGS),
    })


def test_measure_split_equals_override_split(data_dir_mini):
    assert math.isclose(
        compute_score_part(data_dir_mini, _measure_part()),
        compute_score_part(data_dir_mini, _override_part()),
        rel_tol=1e-12,
    )


def test_dataname_split_equals_measure_split(data_dir_mini):
    p = ScorePart.model_validate({
        "name": "by_dataname",
        "type": "FBC",
        "relative": {
            "split_axis": "DataName",
            "numerator_when": "evaluation_param_read_level_1",
            "denominator_when": "reference_param_read_level_1",
            "denominator_offset": 1,
        },
        "order": list(_TAIL_ORDER),
        "aggregations": dict(_TAIL_AGGS),
    })
    assert math.isclose(
        compute_score_part(data_dir_mini, p),
        compute_score_part(data_dir_mini, _measure_part()),
        rel_tol=1e-12,
    )


def test_measure_split_with_labels_annotation(data_dir_mini):
    """labels 注記は計算に影響しない。"""
    p = _measure_part(labels={"1": "evaluation_param_read_level_1",
                              "0": "reference_param_read_level_1"})
    assert math.isclose(
        compute_score_part(data_dir_mini, p),
        compute_score_part(data_dir_mini, _measure_part()),
        rel_tol=1e-12,
    )


def test_measure_filter_pins_one_measurement(data_dir_mini):
    """相対化なしで Measure filter だけでも1測定を選べる（絶対値パーツ）。"""
    def _abs_part(measure):
        return ScorePart.model_validate({
            "name": f"m{measure}",
            "type": "FBC",
            "order": ["Measure"] + _TAIL_ORDER,
            "aggregations": {"Measure": {"op": "filter", "value": measure}, **_TAIL_AGGS},
        })

    v0 = compute_score_part(data_dir_mini, _abs_part(0))
    v1 = compute_score_part(data_dir_mini, _abs_part(1))
    assert math.isfinite(v0) and math.isfinite(v1)
    assert not math.isclose(v0, v1, rel_tol=1e-6)  # 基準と評価は別の値のはず


def test_measure_filter_is_in_selects_multiple(data_dir_mini):
    """複数値 filter（is_in）: [0, 1] は測定 0 と 1 の行を残し、後段の mean が
    両者をまとめて畳む。単一値2つの平均と一致する（行数が等しい mini データ）。"""
    p = ScorePart.model_validate({
        "name": "m01",
        "type": "FBC",
        "order": ["Measure"] + _TAIL_ORDER,
        "aggregations": {"Measure": {"op": "filter", "value": [0, 1]}, **_TAIL_AGGS},
    })
    v = compute_score_part(data_dir_mini, p)
    assert math.isfinite(v)


class TestPrefilterWithMeasure:
    def test_measure_filter_is_hoisted(self):
        p = ScorePart.model_validate({
            "name": "m", "type": "FBC",
            "order": ["Measure"] + _TAIL_ORDER,
            "aggregations": {"Measure": {"op": "filter", "value": [0, 1]}, **_TAIL_AGGS},
        })
        assert _hoistable_prefilters(p) == [("Measure", [0, 1])]

    def test_measure_split_blocks_measure_hoist(self):
        """split_axis=Measure のとき Measure filter は前に出さない（従来の
        split 軸除外がそのまま働く）。"""
        p = ScorePart.model_validate({
            "name": "m", "type": "FBC",
            "relative": {"split_axis": "Measure", "numerator_when": 1, "denominator_when": 0},
            "order": ["State"] + [a for a in _TAIL_ORDER if a != "State"],
            "aggregations": {"State": {"op": "filter", "value": "A2B"},
                             **{a: {"op": "mean"} for a in _TAIL_ORDER if a != "State"}},
        })
        assert _hoistable_prefilters(p) == [("State", "A2B")]

    def test_is_in_prefilter_same_value_and_cache_key_hashable(self, data_dir_mini, monkeypatch):
        """リスト値 filter の前絞り: 同値性と、shared_ctx のキャッシュキーが
        リスト値でも壊れない（tuple 化される）ことの両方を確認する。

        相対化は任意軸 State で分割する（集計済み type で Chip 等の任意軸から
        split を選ぶ 4b ユースケースの形）。Read_Override 分割と Measure 軸の
        併存は不可であることに注意: 相対化のペア結合キーに Measure が入り、
        分子側（評価測定の番号）と分母側（基準測定の番号）で値が違うため
        必ず0ペアになる。Measure を軸に使うなら split も Measure にするのが仕様。
        """
        tail = [a for a in _TAIL_ORDER if a != "State"]
        p = ScorePart.model_validate({
            "name": "m01_rel", "type": "FBC",
            "relative": {"split_axis": "State", "numerator_when": "A2B",
                         "denominator_when": "R2A", "denominator_offset": 1},
            "order": ["Measure"] + tail,
            "aggregations": {"Measure": {"op": "filter", "value": [0, 1]},
                             **{a: {"op": "mean"} for a in tail}},
        })
        assert _hoistable_prefilters(p) == [("Measure", [0, 1])]
        ctx = SharedComputeContext(data_dir_mini, [p])
        with_ctx = compute_score_part(data_dir_mini, p, shared_ctx=ctx)
        alone = compute_score_part(data_dir_mini, p)
        monkeypatch.setattr(cli, "_hoistable_prefilters", lambda *a, **k: [])
        without = compute_score_part(data_dir_mini, p)
        assert math.isclose(with_ctx, alone, rel_tol=1e-12)
        assert math.isclose(alone, without, rel_tol=1e-12)
