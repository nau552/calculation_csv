"""dVtBudget conversion.

type == "dVtBudget" score parts read FBC.csv (see axis_resolve), are
relative-ized (relative.py) same as any other relative FBC score part, and
then converted row-wise here while Board/State are still present as columns:

    dVtBudget = -log10(relative_value) / b * 1000

`b` is looked up per State from the coefficient table for the chip
Generation (read from the run config) and the temperature nearest to the
Board's actually-measured temperature (initial_temperature.csv), per
score_gui_design.md section 3.5. `a` is not used.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

import polars as pl

from .models import DvtBudgetCoefFile


def load_board_temperatures(initial_temperature_path: str | Path) -> Dict[int, float]:
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
