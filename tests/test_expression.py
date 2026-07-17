import pytest

from scorelib.expression import evaluate_expression


def test_basic_arithmetic():
    assert evaluate_expression("1 + 2 * 3", {}) == pytest.approx(7.0)


def test_variables():
    assert evaluate_expression("0.5 * a + b", {"a": 10, "b": 1}) == pytest.approx(6.0)


def test_log_is_log10():
    assert evaluate_expression("log(100)", {}) == pytest.approx(2.0)


def test_min_max():
    assert evaluate_expression("min(a, b)", {"a": 3, "b": 5}) == 3
    assert evaluate_expression("max(a, b)", {"a": 3, "b": 5}) == 5


def test_mean_over_values_list():
    assert evaluate_expression("mean(values)", {"values": [1, 2, 3, 4]}) == pytest.approx(2.5)


def test_score_composition_style():
    values = {"FBC_a": 2.0, "FBC_b": 100.0, "dVt_c": 1.5}
    result = evaluate_expression("0.5 * FBC_a + 0.3 * log(FBC_b) - dVt_c", values)
    assert result == pytest.approx(0.5 * 2.0 + 0.3 * 2.0 - 1.5)


def test_no_arbitrary_code_execution():
    with pytest.raises(Exception):
        evaluate_expression("__import__('os').system('echo pwned')", {})
