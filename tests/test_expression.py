# Copyright (c) 2026
# ruff: file-ignore[magic-value-comparison] テストの期待値は生の数値で書く(定数名に隠すと期待値が読めない)
import pytest

from scorelib_param.expression import evaluate_expression


def test_basic_arithmetic() -> None:
    """四則演算と優先順位を検証する。"""
    assert evaluate_expression("1 + 2 * 3", {}) == pytest.approx(7.0)


def test_variables() -> None:
    """変数の参照を検証する。"""
    assert evaluate_expression("0.5 * a + b", {"a": 10, "b": 1}) == pytest.approx(6.0)


def test_log_is_log10() -> None:
    """式の log が常用対数(log10)であることを検証する。"""
    assert evaluate_expression("log(100)", {}) == pytest.approx(2.0)


def test_min_max() -> None:
    """組み込みの min / max を検証する。"""
    assert evaluate_expression("min(a, b)", {"a": 3, "b": 5}) == 3
    assert evaluate_expression("max(a, b)", {"a": 3, "b": 5}) == 5


def test_mean_over_values_list() -> None:
    """リストに対する mean を検証する。"""
    assert evaluate_expression("mean(values)", {"values": [1, 2, 3, 4]}) == pytest.approx(2.5)


def test_score_composition_style() -> None:
    """スコア合成式の形の評価を検証する。"""
    values = {"FBC_a": 2.0, "FBC_b": 100.0, "dVt_c": 1.5}
    result = evaluate_expression("0.5 * FBC_a + 0.3 * log(FBC_b) - dVt_c", values)
    assert result == pytest.approx(0.5 * 2.0 + 0.3 * 2.0 - 1.5)


def test_no_arbitrary_code_execution() -> None:
    """任意コード実行ができないことを検証する。"""
    with pytest.raises(Exception, match="Function '__import__' not defined"):
        evaluate_expression("__import__('os').system('echo pwned')", {})
