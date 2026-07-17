"""Free-form expression evaluation shared by:

- ScorePart `expr` aggregation op (score_gui_design.md section 4.3)
- Score composition `expression` (section 5)

Uses simpleeval (sandboxed, no arbitrary code execution) rather than a
hand-rolled DSL.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from simpleeval import DEFAULT_FUNCTIONS, EvalWithCompoundTypes


def _mean(*xs: float) -> float:
    values = xs[0] if len(xs) == 1 and isinstance(xs[0], (list, tuple)) else xs
    return sum(values) / len(values)


def _make_functions() -> dict[str, Any]:
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
    evaluator = EvalWithCompoundTypes(functions=_make_functions(), names=dict(variables))
    return evaluator.eval(expr)
