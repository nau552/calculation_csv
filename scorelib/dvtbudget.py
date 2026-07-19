"""dVtBudget 変換。

type == "dVtBudget" のスコアパーツは FBC.csv を読み（axis_resolve）、他の
相対値FBCパーツと同じく相対化（relative.py）された後、Board / State が
まだ列として残っている時点でここで行単位に変換される:

    dVtBudget = -log10(相対値) / b * 1000

`b` は、チップ世代（configの Generation）の係数表から、Board の実測温度
（initial_temperature.csv）に**最も近い温度キー**を選び、State ごとに引く
（docs/score_gui_design.md 3.5節）。`a` は使わない。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

import polars as pl

from .models import DvtBudgetCoefFile


def load_board_temperatures(initial_temperature_path: str | Path) -> Dict[int, float]:
    """initial_temperature.csv（ヘッダなし: Board, 温度）→ {Board: 温度}。"""
    df = pl.read_csv(initial_temperature_path, has_header=False, new_columns=["Board", "Temperature"])
    return {int(board): float(temp) for board, temp in zip(df["Board"], df["Temperature"])}


def _nearest_temp_key(available_keys, target_temp: float) -> str:
    return min(available_keys, key=lambda k: abs(float(k) - target_temp))


def apply_dvtbudget(
    lf: pl.LazyFrame,
    value_col: str,
    generation: str,
    coef: DvtBudgetCoefFile,
    board_temperatures: Mapping[int, float],
) -> pl.LazyFrame:
    schema_cols = lf.collect_schema().names()
    missing = {"Board", "State"} - set(schema_cols)
    if missing:
        raise ValueError(
            f"dVtBudget conversion needs columns {sorted(missing)} still present; "
            "aggregate Board/State only after the __dvtbudget__ step"
        )

    gen_coefs = coef.root[generation]
    temp_keys = list(gen_coefs.keys())

    # (Board, State) → b の小さな対応表を作って結合する
    rows = []
    for board, temp in board_temperatures.items():
        nearest = _nearest_temp_key(temp_keys, temp)
        for state, entry in gen_coefs[nearest].items():
            rows.append({"Board": board, "State": state, "__b__": entry.b})

    coef_lf = pl.DataFrame(rows).lazy()
    lf = lf.join(coef_lf, on=["Board", "State"], how="left")
    lf = lf.with_columns(
        (-(pl.col(value_col).log10()) / pl.col("__b__") * 1000).alias(value_col)
    ).drop("__b__")
    return lf
