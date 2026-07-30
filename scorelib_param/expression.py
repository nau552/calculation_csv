# Copyright (c) 2026
"""自由記述式の評価。以下の2箇所で共用する:

- スコアパーツの `expr` 集計op(docs/score_gui_design.md 4.3節)
- スコア合成式 `expression`(同 5節)

自前DSLではなく simpleeval(サンドボックス化された評価器。任意コードは
実行できない)を採用している。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from simpleeval import DEFAULT_FUNCTIONS, EvalWithCompoundTypes

if TYPE_CHECKING:
    from collections.abc import Mapping


def _mean(*xs: float) -> float:
    """mean(1, 2, 3) と mean([1, 2, 3]) の両方を受ける。"""
    values = xs[0] if len(xs) == 1 and isinstance(xs[0], (list, tuple)) else xs
    return sum(values) / len(values)


def _make_functions() -> dict[str, Any]:
    """式の中で使える関数の登録(log は log10 に割り当てる慣習に注意)。"""
    functions = dict(DEFAULT_FUNCTIONS)
    functions.update(
        {
            "log": math.log10,
            "ln": math.log,
            "log2": math.log2,
            "exp": math.exp,
            "sqrt": math.sqrt,
            "min": min,
            "max": max,
            "mean": _mean,
            "sum": sum,
            "abs": abs,
        }
    )
    return functions


def evaluate_expression(expr: str, variables: Mapping[str, Any]) -> float:
    """式 `expr` を、`variables` を変数名前空間として評価する。"""
    evaluator = EvalWithCompoundTypes(functions=_make_functions(), names=dict(variables))
    return evaluator.eval(expr)
